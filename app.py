# -*- coding: utf-8 -*-
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
USER_DEX_CACHE_TTL_SECONDS = 24 * 60 * 60 #24 horas
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
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
}
try:
    transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL, headers=headers, ssl=True)
    client = Client(transport=transport, fetch_schema_from_transport=True)
    print("INFO: Cliente GraphQL inicializado com sucesso.")
except Exception as gql_setup_error:
    print(f"CRITICAL: Falha ao configurar cliente GraphQL: {gql_setup_error}")
    client = None

# --- Funções de Banco de Dados ---

def get_db_connection():
    """Retorna uma conexão com o banco de dados com autocommit."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True # Habilita autocommit
        return conn
    except psycopg2.OperationalError as e:
        print(f"DB_ERROR: Falha na conexão: {e}")
        raise

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
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            for i, command in enumerate(commands):
                table_name = 'pokemon' if i == 0 else 'user_dex_cache'
                print(f"DB_INIT: Verificando/Criando tabela '{table_name}'...")
                cursor.execute(command)
                print(f"DB_INIT: Tabela '{table_name}' OK.")
        print("DB_INIT: Inicialização do DB concluída.")
    except Exception as e:
        print(f"DB_INIT_ERROR: {e}")
        raise
    finally:
         if conn:
            conn.close()

# --- Funções de Lógica de Pokémon ---

def fetch_pokemon_details(pokemon_id: int):
    """Busca detalhes do Pokémon, DB > API. Retorna dict ou None."""
    if client is None:
        print(f"FETCH_ERROR ID {pokemon_id}: Cliente GraphQL não disponível.")
        return None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # 1. Tenta buscar no banco de dados (cache 'pokemon')
            cursor.execute('SELECT id, name, stats, total_base_stats, types, image, shiny_image FROM pokemon WHERE id = %s', (pokemon_id,))
            row = cursor.fetchone()

            if row:
                # Cache hit! Verifica o tipo e processa stats/types
                stats_val = row[2] # Valor da coluna 'stats' (pode ser str, dict ou None)
                types_val = row[4] # Valor da coluna 'types' (pode ser str, list ou None)
                stats_data = {}    # Valor padrão
                types_data = []    # Valor padrão

                try:
                    # Processa Stats: Se for string, faz parse. Se for dict, usa direto.
                    if isinstance(stats_val, str):
                        stats_data = json.loads(stats_val) if stats_val else {}
                    elif isinstance(stats_val, dict):
                        stats_data = stats_val
                    else: # Trata None ou outros tipos inesperados
                        stats_data = {}

                    # Processa Types: Se for string, faz parse. Se for list, usa direto.
                    if isinstance(types_val, str):
                        types_data = json.loads(types_val) if types_val else []
                    elif isinstance(types_val, list):
                        types_data = types_val
                    else: # Trata None ou outros tipos inesperados
                        types_data = []

                except json.JSONDecodeError as e:
                    print(f"JSON_ERROR (Cache Hit ID {pokemon_id}): Falha ao decodificar string do DB! Forçando busca API.")
                    print(f"--> Erro: {e}. Stats: '{stats_val}', Types: '{types_val}'")
                    row = None # Invalida o cache hit para forçar busca na API
                except Exception as e_parse:
                     print(f"PARSE_ERROR (Cache Hit ID {pokemon_id}): Erro inesperado no parse. Forçando busca API.")
                     print(f"--> Erro: {e_parse}.")
                     row = None # Invalida o cache hit

                if row: # Se não deu erro no try/except acima que invalidou 'row'
                    # Retorna os dados do cache com tipos corretos (dict/list)
                    return {
                        'id': row[0], 'name': row[1], 'stats': stats_data,
                        'total_base_stats': row[3], 'types': types_data,
                        'image': row[5], 'shiny_image': row[6]
                    }
                # Se 'row' foi invalidado, o código continua para a busca na API abaixo...
            # --- Fim do Bloco Corrigido ---

            # 2. Se não achou no DB ou cache hit foi invalidado, busca na API
            print(f"FETCH_API: Buscando ID {pokemon_id} da API...")
            # Query GraphQL COMPLETA
            query = gql('''
                query GetPokemonDetails($id: Int!) {
                    pokemon_v2_pokemon(where: {id: {_eq: $id}}) {
                        id
                        name
                        pokemon_v2_pokemonstats {
                            base_stat
                            pokemon_v2_stat {
                                name
                            }
                        }
                        pokemon_v2_pokemontypes {
                            pokemon_v2_type {
                                name
                            }
                        }
                        pokemon_v2_pokemonsprites {
                            sprites
                        }
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

            # Processa dados da API
            data = result["pokemon_v2_pokemon"][0]
            stats = {s["pokemon_v2_stat"]["name"]: s["base_stat"] for s in data.get("pokemon_v2_pokemonstats", [])}
            types = [t["pokemon_v2_type"]["name"] for t in data.get("pokemon_v2_pokemontypes", [])]
            total_base_stats = sum(stats.values())
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

            # 3. Insere os dados buscados da API no cache 'pokemon'
            stats_json = json.dumps(stats)
            types_json = json.dumps(types)
            try:
                # Reutiliza o cursor da conexão existente
                cursor.execute(
                    'INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
                    (data['id'], data['name'], stats_json, total_base_stats, types_json, image, shiny_image)
                )
                print(f"DB_INSERT: Detalhes do ID {pokemon_id} salvos no cache 'pokemon'.")
            except psycopg2.Error as insert_err:
                 print(f"DB_ERROR (Insert ID {pokemon_id}): {insert_err}")

            # 4. Retorna os dados processados da API (OBJETOS PYTHON)
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
            conn.close()

# --- Funções para Cache da Lista de Usuário ---

def get_cached_dex(canal: str, usuario: str):
    """Tenta buscar a lista de Pokémon do cache 'user_dex_cache'. Retorna lista ou None."""
    print(f"CACHE_LIST: Verificando user_dex_cache para {canal}/{usuario}...")
    sql = "SELECT pokemon_list, last_updated FROM user_dex_cache WHERE canal = %s AND usuario = %s"
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, (canal, usuario))
            row = cursor.fetchone()
            if row:
                cached_list_val, last_updated_ts = row[0], row[1]
                if cached_list_val is None or last_updated_ts is None:
                     print(f"CACHE_LIST: Dados inválidos (null) para {canal}/{usuario}.")
                     return None
                now_utc = datetime.datetime.now(timezone.utc)
                cache_age = now_utc - last_updated_ts
                print(f"CACHE_LIST: Encontrado. Idade: {cache_age}. TTL: {USER_DEX_CACHE_TTL_SECONDS}s")
                if cache_age.total_seconds() <= USER_DEX_CACHE_TTL_SECONDS:
                    print(f"CACHE_LIST: HIT válido para {canal}/{usuario}.")
                    # Verifica e faz parse se necessário (segurança extra)
                    if isinstance(cached_list_val, str):
                        print("CACHE_LIST_WARN: Dado veio como string, tentando parse...")
                        try:
                            cached_list = json.loads(cached_list_val)
                        except json.JSONDecodeError:
                            print("CACHE_LIST_ERROR: Falha ao decodificar lista do cache.")
                            return None
                    elif isinstance(cached_list_val, list):
                         cached_list = cached_list_val
                    else:
                         print(f"CACHE_LIST_ERROR: Tipo inesperado para lista ({type(cached_list_val)}).")
                         return None
                    return cached_list
                else:
                    print(f"CACHE_LIST: Expirado para {canal}/{usuario}.")
                    return None
            else:
                print(f"CACHE_LIST: MISS para {canal}/{usuario}.")
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
    print(f"CACHE_LIST: Atualizando user_dex_cache para {canal}/{usuario}...")
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
        print(f"CACHE_LIST: user_dex_cache atualizado com sucesso.")
    except TypeError as json_err:
         print(f"JSON_ERROR ao serializar lista: {json_err}")
    except psycopg2.Error as db_err:
        print(f"DB_ERROR ao atualizar user_dex_cache: {db_err}")
    except Exception as e:
        print(f"UNEXPECTED_ERROR ao atualizar user_dex_cache: {e}")
    finally:
        if conn:
            conn.close()

# --- Função de Scraping (Só raspa a lista) ---

def scrape_grynsoft_dex(canal: str, usuario: str):
    """APENAS faz o scraping e retorna a lista bruta [{'id':str, 'shiny':bool}] ou dict de erro."""
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"
    print(f"SCRAPING: Iniciando busca da LISTA em: {url}")
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
                print(f"SCRAPING_WARN: ID inválido '{pokemon_id_str}', pulando.")
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
        print(f"API_LOGIC: {'Refresh' if refresh_flag else 'Cache miss/expirado'}. Scraping...")
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
         print("API_WARN: scraped_list é None mesmo após tentativas. Retornando lista vazia.")
         scraped_list = []

    print(f"API_LOGIC: Processando {len(scraped_list)} itens da lista...")
    pokemons_result = []
    for item in scraped_list:
        try:
            pokemon_id_int = int(item['id'])
            shiny_status = item['shiny']
            details = fetch_pokemon_details(pokemon_id_int)
            if details:
                # Garantir que stats e types são dict/list antes de adicionar
                final_stats = details.get('stats', {})
                final_types = details.get('types', [])
                if not isinstance(final_stats, dict):
                    print(f"API_WARN: stats para ID {details.get('id')} não é dict: {type(final_stats)}. Usando {{}}.")
                    final_stats = {}
                if not isinstance(final_types, list):
                    print(f"API_WARN: types para ID {details.get('id')} não é list: {type(final_types)}. Usando [].")
                    final_types = []

                pokemons_result.append({
                    'id': details.get('id'),
                    'name': details.get('name'),
                    'shiny': shiny_status,
                    'stats': final_stats,
                    'total_base_stats': details.get('total_base_stats'),
                    'types': final_types,
                    'image': details.get('shiny_image') if shiny_status else details.get('image')
                })
            # else: # Detalhes não encontrados já é logado em fetch_pokemon_details
            #    print(f"API_WARN: Detalhes não encontrados para ID {pokemon_id_int}.")
        except (ValueError, KeyError, TypeError) as e:
             print(f"API_ERROR: Erro processando item '{item}' da lista final: {e}")

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
    host = os.environ.get('HOST', '0.0.0.0')
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    print(f"==> Iniciando Flask app em http://{host}:{port} (Debug: {debug_mode}) <==")
    # use_reloader=False evita init_db duplo em debug local
    app.run(debug=debug_mode, host=host, port=port, use_reloader=False)