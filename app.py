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
USER_DEX_CACHE_TTL_SECONDS = 24 * 60 * 60 # 24 horas
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
        conn.autocommit = True
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
                stats_val = row[2]
                types_val = row[4]
                stats_data = {}
                types_data = []
                try:
                    if isinstance(stats_val, str): stats_data = json.loads(stats_val) if stats_val else {}
                    elif isinstance(stats_val, dict): stats_data = stats_val
                    else: stats_data = {}
                    if isinstance(types_val, str): types_data = json.loads(types_val) if types_val else []
                    elif isinstance(types_val, list): types_data = types_val
                    else: types_data = []
                except json.JSONDecodeError as e:
                    print(f"JSON_ERROR (Cache Hit ID {pokemon_id}): Falha DB string parse. Forcing API fetch.")
                    print(f"--> Erro: {e}. Stats: '{stats_val}', Types: '{types_val}'")
                    row = None # Invalida cache hit
                except Exception as e_parse:
                     print(f"PARSE_ERROR (Cache Hit ID {pokemon_id}): Unexpected parse error. Forcing API fetch.")
                     print(f"--> Erro: {e_parse}.")
                     row = None # Invalida cache hit

                if row: # Se parse deu certo
                    return {
                        'id': row[0], 'name': row[1], 'stats': stats_data,
                        'total_base_stats': row[3], 'types': types_data,
                        'image': row[5], 'shiny_image': row[6]
                    }
                # Se row foi invalidado, continua para API...

            # 2. Cache miss ou cache hit invalidado -> Busca na API
            print(f"FETCH_API: Buscando ID {pokemon_id} da API...")
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

            stats_json = json.dumps(stats)
            types_json = json.dumps(types)
            try:
                cursor.execute(
                    'INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
                    (data['id'], data['name'], stats_json, total_base_stats, types_json, image, shiny_image)
                )
                print(f"DB_INSERT: Detalhes do ID {pokemon_id} salvos no cache 'pokemon'.")
            except psycopg2.Error as insert_err:
                 print(f"DB_ERROR (Insert ID {pokemon_id}): {insert_err}")

            return { # Retorna objetos Python
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

def get_cached_dex(canal_lower: str, usuario_lower: str):
    """Tenta buscar a lista de Pokémon do cache 'user_dex_cache'. Usa chaves MINÚSCULAS."""
    print(f"CACHE_LIST: Verificando user_dex_cache para {canal_lower}/{usuario_lower}...")
    sql = "SELECT pokemon_list, last_updated FROM user_dex_cache WHERE canal = %s AND usuario = %s"
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, (canal_lower, usuario_lower))
            row = cursor.fetchone()
            if row:
                cached_list_val, last_updated_ts = row[0], row[1]
                if cached_list_val is None or last_updated_ts is None: return None
                now_utc = datetime.datetime.now(timezone.utc)
                cache_age = now_utc - last_updated_ts
                print(f"CACHE_LIST: Encontrado. Idade: {cache_age}. TTL: {USER_DEX_CACHE_TTL_SECONDS}s")
                if cache_age.total_seconds() <= USER_DEX_CACHE_TTL_SECONDS:
                    print(f"CACHE_LIST: HIT válido para {canal_lower}/{usuario_lower}.")
                    # Processa valor lido (pode ser string ou list)
                    if isinstance(cached_list_val, str):
                        try: cached_list = json.loads(cached_list_val)
                        except json.JSONDecodeError: return None
                    elif isinstance(cached_list_val, list): cached_list = cached_list_val
                    else: return None
                    return cached_list
                else:
                    print(f"CACHE_LIST: Expirado para {canal_lower}/{usuario_lower}.")
                    return None
            else:
                print(f"CACHE_LIST: MISS para {canal_lower}/{usuario_lower}.")
                return None
    except psycopg2.Error as db_err: print(f"DB_ERROR ao buscar user_dex_cache: {db_err}"); return None
    except Exception as e: print(f"UNEXPECTED_ERROR ao buscar user_dex_cache: {e}"); return None
    finally:
        if conn: conn.close()

def update_cached_dex(canal_lower: str, usuario_lower: str, pokemon_list: list):
    """Insere/atualiza lista no cache 'user_dex_cache'. Usa chaves MINÚSCULAS."""
    print(f"CACHE_LIST: Atualizando user_dex_cache para {canal_lower}/{usuario_lower}...")
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
            cursor.execute(sql, (canal_lower, usuario_lower, pokemon_list_json, now_utc))
        print(f"CACHE_LIST: user_dex_cache atualizado com sucesso.")
    except TypeError as json_err: print(f"JSON_ERROR ao serializar lista: {json_err}")
    except psycopg2.Error as db_err: print(f"DB_ERROR ao atualizar user_dex_cache: {db_err}")
    except Exception as e: print(f"UNEXPECTED_ERROR ao atualizar user_dex_cache: {e}")
    finally:
        if conn: conn.close()

# --- Função de Scraping ---

def scrape_grynsoft_dex(canal_original: str, usuario_original: str):
    """Faz o scraping e retorna a lista bruta [{'id':str, 'shiny':bool}] ou dict de erro."""
    url = f"https://grynsoft.com/spos-app/?c={canal_original}&u={usuario_original}"
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
            if not pokemon_id_str.isdigit(): continue
            if pokemon_id_str in seen_ids: continue
            seen_ids.add(pokemon_id_str)
            shiny = element.get('id') == 'shiny'
            scraped_list.append({'id': pokemon_id_str, 'shiny': shiny})
        print(f"SCRAPING: Lista concluída. Encontrados {len(scraped_list)} Pokémon únicos.")
        return scraped_list
    except requests.exceptions.Timeout: return {"error": f"Timeout ao raspar {url}"}
    except requests.exceptions.RequestException as e: return {"error": f"Erro de rede/HTTP ao raspar {url}"}
    except Exception as e: print(f"SCRAPING_ERROR: {e}"); traceback.print_exc(); return {"error": "Erro inesperado no scraping"}

# --- Função Reutilizável para Obter Lista (Cache > Scrape) ---

def get_or_scrape_user_dex_list(canal_lower: str, usuario_lower: str, canal_original: str, usuario_original: str, refresh: bool = False):
    """Busca a lista bruta de Pokémon [{'id':str, 'shiny':bool}] para um usuário."""
    scraped_list = None
    if not refresh:
        scraped_list = get_cached_dex(canal_lower, usuario_lower)
    if scraped_list is None:
        print(f"HELPER_LIST: {'Refresh' if refresh else 'Cache miss/expirado'} para {canal_lower}/{usuario_lower}. Scraping...")
        scrape_result = scrape_grynsoft_dex(canal_original, usuario_original)
        if isinstance(scrape_result, dict) and 'error' in scrape_result:
            return scrape_result
        scraped_list = scrape_result
        if scraped_list:
            update_cached_dex(canal_lower, usuario_lower, scraped_list)
    if scraped_list is None: scraped_list = []
    return scraped_list

# --- Rota Principal da API (/api/pokemons) ---

@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    """Endpoint principal. Usa get_or_scrape_user_dex_list e depois busca detalhes."""
    canal_original = request.args.get('canal')
    usuario_original = request.args.get('usuario')
    refresh_flag = request.args.get('refresh', 'false').lower() == 'true'
    canal_lower = canal_original.lower() if canal_original else None
    usuario_lower = usuario_original.lower() if usuario_original else None
    if not canal_lower or not usuario_lower: return jsonify({"error": "Forneça 'canal' e 'usuario'."}), 400

    print(f"API_REQ /pokemons: Canal={canal_lower}, Usuario={usuario_lower}, Refresh={refresh_flag}")
    list_result = get_or_scrape_user_dex_list(canal_lower, usuario_lower, canal_original, usuario_original, refresh=refresh_flag)
    if isinstance(list_result, dict) and 'error' in list_result: return jsonify(list_result), 500

    print(f"API_LOGIC /pokemons: Processando {len(list_result)} itens da lista...")
    pokemons_result = []
    for item in list_result:
        try:
            pokemon_id_int = int(item['id'])
            shiny_status = item['shiny']
            details = fetch_pokemon_details(pokemon_id_int)
            if details:
                final_stats = details.get('stats', {})
                final_types = details.get('types', [])
                if not isinstance(final_stats, dict): final_stats = {}
                if not isinstance(final_types, list): final_types = []
                pokemons_result.append({
                    'id': details.get('id'), 'name': details.get('name'),
                    'shiny': shiny_status, 'stats': final_stats,
                    'total_base_stats': details.get('total_base_stats'),
                    'types': final_types,
                    'image': details.get('shiny_image') if shiny_status else details.get('image')
                })
        except (ValueError, KeyError, TypeError) as e:
             print(f"API_ERROR /pokemons: Erro processando item '{item}': {e}")

    print(f"API_RESP /pokemons: Retornando {len(pokemons_result)} Pokémon.")
    return jsonify(pokemons_result)

# --- ROTA DE COMPARAÇÃO DE DEX (/api/compare_dex) --- AJUSTADA ---

@app.route('/api/compare_dex', methods=['GET'])
def compare_dex():
    """
    Compara as listas de Pokémon entre dois usuários no mesmo canal.
    Retorna os detalhes COMPLETOS dos Pokémon que usuario2 tem e usuario1 NÃO tem.
    """
    # 1. Pega e valida parâmetros
    canal_original = request.args.get('canal')
    usuario1_original = request.args.get('usuario1') # "Eu" (usuário base)
    usuario2_original = request.args.get('usuario2') # "O outro" (usuário comparado)

    if not all([canal_original, usuario1_original, usuario2_original]):
        return jsonify({"error": "Forneça canal, usuario1 (você) e usuario2 (o outro)."}), 400
    if usuario1_original.lower() == usuario2_original.lower():
         return jsonify({"error": "usuario1 e usuario2 não podem ser iguais."}), 400

    # 2. Padroniza para minúsculas para cache
    canal_lower = canal_original.lower()
    usuario1_lower = usuario1_original.lower()
    usuario2_lower = usuario2_original.lower()

    print(f"API_REQ /compare_dex: Canal={canal_lower}, Base(User1)={usuario1_lower}, Comparado(User2)={usuario2_lower}")

    # 3. Obtém as listas brutas (contêm 'id' e 'shiny')
    list1_result = get_or_scrape_user_dex_list(canal_lower, usuario1_lower, canal_original, usuario1_original, refresh=False)
    if isinstance(list1_result, dict) and 'error' in list1_result:
        return jsonify({"error_user1": f"Falha ao obter dados para {usuario1_original}: {list1_result['error']}"}), 500

    list2_result = get_or_scrape_user_dex_list(canal_lower, usuario2_lower, canal_original, usuario2_original, refresh=False)
    if isinstance(list2_result, dict) and 'error' in list2_result:
        return jsonify({"error_user2": f"Falha ao obter dados para {usuario2_original}: {list2_result['error']}"}), 500

    # 4. Cria lookups e extrai IDs
    user2_dict = {item['id']: item for item in list2_result}
    set1_ids = {item['id'] for item in list1_result} # IDs que EU tenho
    set2_ids = set(user2_dict.keys())                # IDs que o OUTRO tem

    # 5. Calcula a diferença: IDs que o OUTRO (user2) tem e EU (user1) NÃO tenho
    usuario2_tem_que_usuario1_nao_tem_ids = sorted(list(set2_ids - set1_ids), key=int)

    print(f"API_LOGIC /compare_dex: User2 ({usuario2_lower}) tem {len(usuario2_tem_que_usuario1_nao_tem_ids)} Pokémon exclusivos.")

    # 6. Busca detalhes COMPLETOS para a lista de diferença
    pokemon_faltantes_para_user1 = []
    for id_str in usuario2_tem_que_usuario1_nao_tem_ids:
        try:
            pokemon_id_int = int(id_str)
            details = fetch_pokemon_details(pokemon_id_int) # Usa cache 'pokemon'
            if details:
                original_item = user2_dict.get(id_str)
                shiny_status = original_item['shiny'] if original_item else False
                final_stats = details.get('stats', {})
                final_types = details.get('types', [])
                if not isinstance(final_stats, dict): final_stats = {}
                if not isinstance(final_types, list): final_types = []
                pokemon_faltantes_para_user1.append({
                    'id': details.get('id'), 'name': details.get('name'),
                    'shiny': shiny_status, 'stats': final_stats,
                    'total_base_stats': details.get('total_base_stats'),
                    'types': final_types,
                    'image': details.get('shiny_image') if shiny_status else details.get('image')
                })
        except (ValueError, KeyError, TypeError) as e:
             print(f"COMPARE_ERROR: Erro processando ID {id_str} para detalhes completos: {e}")

    # 7. Monta e retorna a resposta final simplificada
    response_data = {
        "canal": canal_original,
        "usuario_base": usuario1_original,
        "usuario_comparado": usuario2_original,
        "pokemon_que_faltam": pokemon_faltantes_para_user1 # Lista com detalhes completos
    }

    return jsonify(response_data)

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
    app.run(debug=debug_mode, host=host, port=port, use_reloader=False)