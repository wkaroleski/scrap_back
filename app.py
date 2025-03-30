# -*- coding: utf-8 -*-
# Adicionado -*- coding: utf-8 -*- para garantir compatibilidade de caracteres

import os
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env para o ambiente os.environ
load_dotenv()

import datetime
from datetime import timezone
import traceback
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import psycopg2
import json
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport

app = Flask(__name__)
CORS(app)

# --- Configurações ---
USER_DEX_CACHE_TTL_SECONDS = 15 * 60 # 15 minutos
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME")
}

# Validação das Variáveis de Ambiente
for key, value in DB_CONFIG.items():
    if value is None:
        raise ValueError(f"Variável de ambiente {key} não configurada.")

# Configuração PokéAPI GraphQL
POKEAPI_GRAPHQL_URL = "https://beta.pokeapi.co/graphql/v1beta"
headers = {
    "User-Agent": "Mozilla/5.0", # Simplificado User-Agent
    "Content-Type": "application/json",
}
try:
    transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL, headers=headers, ssl=True)
    client = Client(transport=transport, fetch_schema_from_transport=True)
except Exception as gql_setup_error:
    print(f"CRITICAL: Falha ao configurar cliente GraphQL: {gql_setup_error}")
    # Decide how to handle this - maybe exit? For now, just print.
    client = None # Mark client as unusable

# --- Funções de Banco de Dados ---

def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True # Habilita autocommit para simplificar
        return conn
    except psycopg2.OperationalError as e:
        print(f"DB_ERROR: Falha na conexão: {e}")
        raise # Re-raise after logging

def init_db():
    """Cria as tabelas necessárias se não existirem."""
    print("DB_INIT: Verificando/Criando tabelas...")
    commands = [
        '''
        CREATE TABLE IF NOT EXISTS pokemon (
            id INTEGER PRIMARY KEY, name TEXT, stats JSONB,
            total_base_stats INTEGER, types JSONB, image TEXT, shiny_image TEXT
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS user_dex_cache (
            canal TEXT NOT NULL, usuario TEXT NOT NULL, pokemon_list JSONB,
            last_updated TIMESTAMPTZ NOT NULL, PRIMARY KEY (canal, usuario)
        )
        '''
    ]
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for command in commands:
                    cursor.execute(command)
        print("DB_INIT: Tabelas OK.")
    except Exception as e:
        print(f"DB_INIT_ERROR: {e}")
        raise

# --- Funções de Lógica de Pokémon ---

def fetch_pokemon_details(pokemon_id: int):
    """Busca detalhes do Pokémon, DB > API."""
    if client is None: # Verifica se o cliente GraphQL foi inicializado
        print(f"FETCH_ERROR: Cliente GraphQL não disponível para ID {pokemon_id}")
        return None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # 1. Cache Hit Path
            cursor.execute('SELECT stats, types FROM pokemon WHERE id = %s', (pokemon_id,))
            row = cursor.fetchone()
            if row:
                stats_data = row[0] if row[0] is not None else {}
                types_data = row[1] if row[1] is not None else []
                # --- Log de Debug 1 ---
                print(f"DEBUG_FETCH (Cache Hit ID {pokemon_id}) -> Tipo stats: {type(stats_data)}, Tipo types: {type(types_data)}")
                # Adiciona busca dos outros campos se o return precisar deles
                cursor.execute('SELECT id, name, total_base_stats, image, shiny_image FROM pokemon WHERE id = %s', (pokemon_id,))
                main_row = cursor.fetchone()
                if main_row:
                     return {
                        'id': main_row[0], 'name': main_row[1], 'stats': stats_data,
                        'total_base_stats': main_row[2], 'types': types_data,
                        'image': main_row[3], 'shiny_image': main_row[4]
                    }
                else: # Should not happen if first select worked, but safety check
                    print(f"FETCH_WARN: Inconsistência no cache para ID {pokemon_id}")
                    # Fall through to API fetch

            # 2. Cache Miss Path
            print(f"FETCH_API: Buscando ID {pokemon_id} da API...")
            query = gql('''...''') # Query completa omitida - USE A SUA QUERY COMPLETA AQUI!
            # >>>>>> CERTIFIQUE-SE DE COLOCAR SUA QUERY GRAPHQL COMPLETA AQUI <<<<<<
            query = gql('''
                query GetPokemonDetails($id: Int!) {
                    pokemon_v2_pokemon(where: {id: {_eq: $id}}) {
                        id name
                        pokemon_v2_pokemonstats { base_stat pokemon_v2_stat { name } }
                        pokemon_v2_pokemontypes { pokemon_v2_type { name } }
                        pokemon_v2_pokemonsprites { sprites }
                    }
                }
            ''')
            try:
                result = client.execute(query, variable_values={"id": pokemon_id})
            except Exception as api_err:
                print(f"API_ERROR ID {pokemon_id}: {api_err}")
                return None

            if not result or not result.get("pokemon_v2_pokemon"):
                print(f"API_WARN ID {pokemon_id}: Nenhum dado retornado.")
                return None

            # Process API data
            data = result["pokemon_v2_pokemon"][0]
            stats = {s["pokemon_v2_stat"]["name"]: s["base_stat"] for s in data.get("pokemon_v2_pokemonstats", [])}
            types = [t["pokemon_v2_type"]["name"] for t in data.get("pokemon_v2_pokemontypes", [])]
            total_base_stats = sum(stats.values())
            # ... (Sprite processing - use seu código completo) ...
            sprites_data = data.get("pokemon_v2_pokemonsprites", [])
            sprites = {}
            if sprites_data:
                sprite_json_or_dict = sprites_data[0].get("sprites", "{}")
                try:
                    if isinstance(sprite_json_or_dict, str): sprites = json.loads(sprite_json_or_dict)
                    elif isinstance(sprite_json_or_dict, dict): sprites = sprite_json_or_dict
                except json.JSONDecodeError: pass
            image = sprites.get("front_default")
            shiny_image = sprites.get("front_shiny")

            # Insert into DB cache
            stats_json = json.dumps(stats)
            types_json = json.dumps(types)
            try:
                 with conn.cursor() as cursor_insert: # Use um novo cursor ou o mesmo se fora do with anterior
                    cursor_insert.execute(
                        'INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
                        (data['id'], data['name'], stats_json, total_base_stats, types_json, image, shiny_image)
                    )
            except psycopg2.Error as insert_err:
                 print(f"DB_ERROR (Insert ID {pokemon_id}): {insert_err}")

            # --- Log de Debug 2 ---
            print(f"DEBUG_FETCH (Cache Miss ID {pokemon_id}) -> Tipo stats: {type(stats)}, Tipo types: {type(types)}")

            # Return Python objects
            return {
                'id': data['id'], 'name': data['name'], 'stats': stats,
                'total_base_stats': total_base_stats, 'types': types,
                'image': image, 'shiny_image': shiny_image
            }
    except psycopg2.Error as db_err:
        print(f"DB_ERROR (Fetch ID {pokemon_id}): {db_err}")
        return None
    except Exception as e:
        print(f"UNEXPECTED_ERROR (Fetch ID {pokemon_id}): {e}")
        traceback.print_exc()
        return None
    finally:
        if conn:
            conn.close() # Fecha a conexão manualmente se não usar 'with' para ela

# (As funções get_cached_dex, update_cached_dex, scrape_grynsoft_dex permanecem as mesmas)
def get_cached_dex(canal: str, usuario: str):
    """Tenta buscar a lista de Pokémon do cache 'user_dex_cache'."""
    print(f"CACHE: Verificando user_dex_cache para {canal}/{usuario}...")
    sql = "SELECT pokemon_list, last_updated FROM user_dex_cache WHERE canal = %s AND usuario = %s"
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, (canal, usuario))
            row = cursor.fetchone()
            if row:
                cached_list, last_updated_ts = row[0], row[1]
                if cached_list is None or last_updated_ts is None:
                     print(f"CACHE: Encontrado para {canal}/{usuario} mas dados inválidos (null).")
                     return None
                now_utc = datetime.datetime.now(timezone.utc)
                cache_age = now_utc - last_updated_ts
                print(f"CACHE: Encontrado. Idade: {cache_age}. TTL: {USER_DEX_CACHE_TTL_SECONDS}s")
                if cache_age.total_seconds() <= USER_DEX_CACHE_TTL_SECONDS:
                    print(f"CACHE: HIT válido para {canal}/{usuario}.")
                    return cached_list
                else:
                    print(f"CACHE: Expirado para {canal}/{usuario}.")
                    return None
            else:
                print(f"CACHE: MISS para {canal}/{usuario}.")
                return None
    except psycopg2.Error as db_err:
        print(f"DB_ERROR ao buscar user_dex_cache: {db_err}")
        return None
    except Exception as e:
        print(f"UNEXPECTED_ERROR ao buscar user_dex_cache: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_cached_dex(canal: str, usuario: str, pokemon_list: list):
    """Insere ou atualiza a lista de Pokémon no cache 'user_dex_cache'."""
    print(f"CACHE: Atualizando user_dex_cache para {canal}/{usuario}...")
    sql = """
        INSERT INTO user_dex_cache (canal, usuario, pokemon_list, last_updated)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (canal, usuario) DO UPDATE SET
            pokemon_list = EXCLUDED.pokemon_list,
            last_updated = EXCLUDED.last_updated;
    """
    now_utc = datetime.datetime.now(timezone.utc)
    conn = None
    try:
        pokemon_list_json = json.dumps(pokemon_list)
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, (canal, usuario, pokemon_list_json, now_utc))
        print(f"CACHE: user_dex_cache atualizado com sucesso.")
    except TypeError as json_err:
         print(f"JSON_ERROR ao serializar lista: {json_err}")
    except psycopg2.Error as db_err:
        print(f"DB_ERROR ao atualizar user_dex_cache: {db_err}")
    except Exception as e:
        print(f"UNEXPECTED_ERROR ao atualizar user_dex_cache: {e}")
    finally:
        if conn:
            conn.close()

def scrape_grynsoft_dex(canal: str, usuario: str):
    """APENAS faz o scraping do Grynsoft e retorna a lista bruta [{'id':str, 'shiny':bool}]."""
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"
    print(f"SCRAPING: Iniciando busca da LISTA em: {url}")
    # (Código do scraping permanece o mesmo)
    scraped_list = []
    seen_ids = set()
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        pokemon_elements = soup.select('.Pokemon:not(#unobtained)')
        for element in pokemon_elements:
            index_element = element.select_one('.Index')
            if not index_element: continue
            pokemon_id_str = index_element.text.strip().lstrip('#0')
            if not pokemon_id_str.isdigit():
                print(f"SCRAPING: ID inválido '{pokemon_id_str}', pulando.")
                continue
            if pokemon_id_str in seen_ids: continue
            seen_ids.add(pokemon_id_str)
            shiny = element.get('id') == 'shiny'
            scraped_list.append({'id': pokemon_id_str, 'shiny': shiny})
        print(f"SCRAPING: Lista concluída. Encontrados {len(scraped_list)} Pokémon únicos.")
        return scraped_list
    except requests.exceptions.Timeout:
        msg = f"Timeout ao raspar {url}"
        print(f"SCRAPING_ERROR: {msg}")
        return {"error": msg}
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if e.response is not None else 'N/A'
        msg = f"Erro de rede/HTTP {status_code} ao raspar {url}"
        print(f"SCRAPING_ERROR: {msg}")
        return {"error": msg}
    except Exception as e:
        msg = f"Erro inesperado durante scraping {url}: {e}"
        print(f"SCRAPING_ERROR: {msg}")
        traceback.print_exc()
        return {"error": msg}

# --- Rota da API ---

@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    """Endpoint principal."""
    canal = request.args.get('canal')
    usuario = request.args.get('usuario')
    refresh_flag = request.args.get('refresh', 'false').lower() == 'true'

    if not canal or not usuario:
        return jsonify({"error": "Forneça 'canal' e 'usuario'."}), 400

    print(f"API_REQ: Canal={canal}, Usuario={usuario}, Refresh={refresh_flag}")
    scraped_list = None

    if not refresh_flag:
        scraped_list = get_cached_dex(canal, usuario)

    if scraped_list is None:
        print(f"API_LOGIC: {'Refresh solicitado' if refresh_flag else 'Cache miss/expirado'}. Scraping...")
        scrape_result = scrape_grynsoft_dex(canal, usuario)
        if isinstance(scrape_result, dict) and 'error' in scrape_result:
            print(f"API_ERROR: Falha no scraping para {canal}/{usuario}.")
            return jsonify(scrape_result), 500
        scraped_list = scrape_result
        if scraped_list:
            print(f"API_LOGIC: Scraping OK ({len(scraped_list)} itens). Atualizando cache...")
            update_cached_dex(canal, usuario, scraped_list)
        else:
             print(f"API_LOGIC: Scraping OK, sem itens para {canal}/{usuario}.")

    if scraped_list is None:
         print("API_ERROR: scraped_list é None mesmo após tentativas.")
         scraped_list = [] # Evita erro abaixo, retorna lista vazia

    print(f"API_LOGIC: Processando {len(scraped_list)} itens da lista...")
    pokemons_result = []
    for item in scraped_list:
        try:
            pokemon_id_int = int(item['id'])
            shiny_status = item['shiny']
            details = fetch_pokemon_details(pokemon_id_int)
            if details:
                pokemons_result.append({
                    'id': details['id'], 'name': details['name'], 'shiny': shiny_status,
                    'stats': details['stats'], 'total_base_stats': details['total_base_stats'],
                    'types': details['types'],
                    'image': details['shiny_image'] if shiny_status else details['image']
                })
            else:
                print(f"API_WARN: Detalhes não encontrados para ID {pokemon_id_int}.")
        except (ValueError, KeyError, TypeError) as e:
             print(f"API_ERROR: Erro processando item '{item}': {e}")

    # --- Log de Debug 3 ---
    if pokemons_result:
         print(f"DEBUG_JSONIFY - Tipo stats[0]: {type(pokemons_result[0].get('stats'))}, Tipo types[0]: {type(pokemons_result[0].get('types'))}")
    else:
         print("DEBUG_JSONIFY - pokemons_result está vazio.")

    print(f"API_RESP: Retornando {len(pokemons_result)} Pokémon.")
    return jsonify(pokemons_result)

# --- Inicialização do Servidor ---

if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        print(f"CRITICAL_ERROR: Falha inicialização DB: {e}")
        exit(1)

    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    print(f"==> Iniciando Flask app em http://{host}:{port} <==")
    # Desativa o reloader do Flask no debug mode para evitar rodar init_db duas vezes
    # Use debug=False para produção
    app.run(debug=True, host=host, port=port, use_reloader=False)