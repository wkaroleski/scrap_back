import os
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env para o ambiente os.environ
# Faça isso ANTES de qualquer código que use os.getenv para configurações
load_dotenv()

import datetime
from datetime import timezone # Import específico para timezone UTC
import traceback # Garante que está importado
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
# Tempo de vida do cache da lista de dex do usuário (em segundos)
# Ex: 15 minutos = 15 * 60 segundos
USER_DEX_CACHE_TTL_SECONDS = 15 * 60

# Configurações do banco de dados (lidas das variáveis de ambiente)
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME")
}

# Valida se as variáveis de ambiente do DB estão configuradas
for key, value in DB_CONFIG.items():
    if value is None:
        raise ValueError(f"Variável de ambiente {key} não configurada. Verifique seu arquivo .env ou as variáveis de ambiente.")

# Configuração da PokéAPI GraphQL
POKEAPI_GRAPHQL_URL = "https://beta.pokeapi.co/graphql/v1beta"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Content-Type": "application/json",
}
transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL, headers=headers, ssl=True)
client = Client(transport=transport, fetch_schema_from_transport=True)

# --- Funções de Banco de Dados ---

def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as e:
        print(f"!!! ERRO DE CONEXÃO COM O BANCO DE DADOS !!!")
        print(f"Detalhes: {e}")
        print(f"Verifique se o banco de dados está acessível e as credenciais estão corretas.")
        print(f"Configurações usadas: Host={DB_CONFIG['host']}, Port={DB_CONFIG['port']}, DB={DB_CONFIG['database']}, User={DB_CONFIG['user']}")
        raise

def init_db():
    """Cria as tabelas necessárias ('pokemon', 'user_dex_cache') se não existirem."""
    print("Inicializando DB (verificando/criando tabelas)...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Cria tabela pokemon (cache de detalhes)
                print("Verificando/Criando tabela 'pokemon'...")
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pokemon (
                        id INTEGER PRIMARY KEY,
                        name TEXT,
                        stats JSONB,
                        total_base_stats INTEGER,
                        types JSONB,
                        image TEXT,
                        shiny_image TEXT
                    )
                ''')
                print("Tabela 'pokemon' OK.")

                # Cria tabela user_dex_cache (cache da lista raspada)
                print("Verificando/Criando tabela 'user_dex_cache'...")
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS user_dex_cache (
                        canal TEXT NOT NULL,
                        usuario TEXT NOT NULL,
                        pokemon_list JSONB,
                        last_updated TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (canal, usuario)
                    )
                ''')
                print("Tabela 'user_dex_cache' OK.")
        print("Inicialização do DB concluída.")
    except Exception as e:
        print(f"Erro durante init_db: {e}")
        raise

# --- Funções de Lógica de Pokémon ---

def fetch_pokemon_details(pokemon_id: int):
    """Busca detalhes do Pokémon, primeiro no DB (cache 'pokemon'), depois na API."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # 1. Tenta buscar no banco de dados (cache de detalhes)
                cursor.execute('SELECT id, name, stats, total_base_stats, types, image, shiny_image FROM pokemon WHERE id = %s', (pokemon_id,))
                row = cursor.fetchone()

                if row:
                    # Cache hit! Processa dados do DB
                    # psycopg2 já decodifica JSONB para dict/list
                    stats_data = row[2] if row[2] is not None else {}
                    types_data = row[4] if row[4] is not None else []
                    # --- INÍCIO DA CORREÇÃO NO CACHE HIT ---
                    return {
                        'id': row[0],
                        'name': row[1],
                        'stats': stats_data,         # <-- CORRIGIDO: Retorna o dict/list diretamente
                        'total_base_stats': row[3],
                        'types': types_data,         # <-- CORRIGIDO: Retorna o dict/list diretamente
                        'image': row[5],
                        'shiny_image': row[6]
                    }
                    # --- FIM DA CORREÇÃO NO CACHE HIT ---

                # 2. Se não achou no DB, busca na API
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
                    print(f"API_ERROR: Erro durante chamada GraphQL para ID {pokemon_id}: {api_err}")
                    return None

                if not result or not result.get("pokemon_v2_pokemon"):
                    print(f"API_WARN: Nenhum dado retornado pela API para ID {pokemon_id}")
                    return None

                data = result["pokemon_v2_pokemon"][0]
                stats = {s["pokemon_v2_stat"]["name"]: s["base_stat"] for s in data.get("pokemon_v2_pokemonstats", [])}
                total_base_stats = sum(stats.values())
                types = [t["pokemon_v2_type"]["name"] for t in data.get("pokemon_v2_pokemontypes", [])]

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
                stats_json = json.dumps(stats) # Converte para JSON SÓ para inserir
                types_json = json.dumps(types) # Converte para JSON SÓ para inserir
                try:
                    cursor.execute('''
                        INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING;
                    ''', (data['id'], data['name'], stats_json, total_base_stats, types_json, image, shiny_image))
                except psycopg2.Error as insert_err:
                     print(f"DB_ERROR: Falha ao inserir detalhes do ID {pokemon_id} no cache 'pokemon': {insert_err}")

                # 4. Retorna os dados processados da API (OBJETOS PYTHON)
                # (Esta parte já estava correta no código que você enviou antes)
                return {
                    'id': data['id'], 'name': data['name'], 'stats': stats, # Retorna dict
                    'total_base_stats': total_base_stats, 'types': types, # Retorna list
                    'image': image, 'shiny_image': shiny_image
                }
    except psycopg2.Error as db_err:
        print(f"DB_ERROR: Erro em fetch_pokemon_details para ID {pokemon_id}: {db_err}")
        traceback.print_exc()
        return None
    except Exception as e:
        print(f"UNEXPECTED_ERROR: Erro em fetch_pokemon_details para ID {pokemon_id}: {e}")
        traceback.print_exc()
        return None

# --- Funções para Cache da Lista de Usuário ---

def get_cached_dex(canal: str, usuario: str):
    """Tenta buscar a lista de Pokémon do cache 'user_dex_cache'."""
    print(f"CACHE: Verificando user_dex_cache para {canal}/{usuario}...")
    sql = "SELECT pokemon_list, last_updated FROM user_dex_cache WHERE canal = %s AND usuario = %s"
    try:
        with get_db_connection() as conn:
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
                        return cached_list # Retorna a lista (objeto Python)
                    else:
                        print(f"CACHE: Expirado para {canal}/{usuario}.")
                        return None
                else:
                    print(f"CACHE: MISS para {canal}/{usuario}.")
                    return None
    except psycopg2.Error as db_err:
        print(f"DB_ERROR ao buscar user_dex_cache para {canal}/{usuario}: {db_err}")
        traceback.print_exc()
        return None
    except Exception as e:
        print(f"UNEXPECTED_ERROR ao buscar user_dex_cache para {canal}/{usuario}: {e}")
        traceback.print_exc()
        return None

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
    try:
        pokemon_list_json = json.dumps(pokemon_list) # Converte lista para string JSON
    except TypeError as json_err:
         print(f"JSON_ERROR: Não foi possível serializar a lista de pokémon para {canal}/{usuario}: {json_err}")
         return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (canal, usuario, pokemon_list_json, now_utc))
        print(f"CACHE: user_dex_cache atualizado com sucesso para {canal}/{usuario}.")
    except psycopg2.Error as db_err:
        print(f"DB_ERROR ao atualizar user_dex_cache para {canal}/{usuario}: {db_err}")
        traceback.print_exc()
    except Exception as e:
        print(f"UNEXPECTED_ERROR ao atualizar user_dex_cache para {canal}/{usuario}: {e}")
        traceback.print_exc()

# --- Função de Scraping (Só raspa a lista) ---

def scrape_grynsoft_dex(canal: str, usuario: str):
    """APENAS faz o scraping do Grynsoft e retorna a lista bruta [{'id':str, 'shiny':bool}]."""
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
                print(f"SCRAPING: ID inválido '{pokemon_id_str}', pulando.")
                continue
            if pokemon_id_str in seen_ids: continue
            seen_ids.add(pokemon_id_str)
            shiny = element.get('id') == 'shiny'
            scraped_list.append({'id': pokemon_id_str, 'shiny': shiny})
        print(f"SCRAPING: Lista concluída. Encontrados {len(scraped_list)} Pokémon únicos.")
        return scraped_list # Retorna lista [{id:str, shiny:bool}] ou []
    except requests.exceptions.Timeout:
        msg = f"Timeout ao raspar a lista de {canal}/{usuario} em {url}"
        print(f"SCRAPING_ERROR: {msg}")
        return {"error": msg} # Retorna dicionário de erro
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if e.response is not None else 'N/A'
        msg = f"Erro de rede/HTTP {status_code} ao raspar a lista de {canal}/{usuario} em {url}"
        print(f"SCRAPING_ERROR: {msg} - Detalhes: {e}")
        return {"error": msg}
    except Exception as e:
        msg = f"Erro inesperado durante scraping da lista em {url}: {e}"
        print(f"SCRAPING_ERROR: {msg}")
        traceback.print_exc()
        return {"error": msg}

# --- Rota da API ---

@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    """Endpoint principal. Usa cache da lista (user_dex_cache) e atualização manual."""
    canal = request.args.get('canal')
    usuario = request.args.get('usuario')
    refresh_flag = request.args.get('refresh', 'false').lower() == 'true'

    if not canal or not usuario:
        return jsonify({"error": "Forneça 'canal' e 'usuario' como parâmetros."}), 400

    print(f"API_REQ: /api/pokemons - Canal: {canal}, Usuario: {usuario}, Refresh: {refresh_flag}")
    scraped_list = None

    # 1. Tenta obter do cache se 'refresh' não for True
    if not refresh_flag:
        scraped_list = get_cached_dex(canal, usuario)

    # 2. Se não veio do cache, faz scraping da lista
    if scraped_list is None:
        if refresh_flag: print("API_LOGIC: Refresh solicitado, forçando scraping da lista.")
        else: print("API_LOGIC: Cache miss ou expirado, iniciando scraping da lista.")
        scrape_result = scrape_grynsoft_dex(canal, usuario)
        if isinstance(scrape_result, dict) and 'error' in scrape_result:
            print(f"API_ERROR: Falha no scraping da lista para {canal}/{usuario}.")
            return jsonify(scrape_result), 500
        scraped_list = scrape_result
        if scraped_list: # Atualiza cache apenas se scraping retornou algo
            print(f"API_LOGIC: Scraping da lista OK ({len(scraped_list)} itens). Atualizando cache...")
            update_cached_dex(canal, usuario, scraped_list)
        else:
             print(f"API_LOGIC: Scraping da lista OK, mas não retornou itens para {canal}/{usuario}.")

    # 3. Verifica se temos uma lista (mesmo que vazia)
    if scraped_list is None:
         print("API_ERROR: Estado inesperado - scraped_list é None após cache e scraping.")
         return jsonify({"error": "Não foi possível obter a lista de Pokémon."}), 500

    # 4. Processa a lista para buscar detalhes
    print(f"API_LOGIC: Processando {len(scraped_list)} itens da lista para buscar detalhes...")
    pokemons_result = []
    for item in scraped_list:
        try:
            pokemon_id_str = item['id']
            shiny_status = item['shiny']
            pokemon_id_int = int(pokemon_id_str)
            details = fetch_pokemon_details(pokemon_id_int) # Usa cache 'pokemon'
            if details:
                pokemons_result.append({
                    'id': details['id'], 'name': details['name'],
                    'shiny': shiny_status, 'stats': details['stats'], # details['stats'] já é dict
                    'total_base_stats': details['total_base_stats'],
                    'types': details['types'], # details['types'] já é list
                    'image': details['shiny_image'] if shiny_status else details['image']
                })
            else:
                print(f"API_WARN: Detalhes não encontrados para ID {pokemon_id_int} (listado para {canal}/{usuario}). Pulando.")
        except (ValueError, KeyError, TypeError) as e:
             print(f"API_ERROR: Erro ao processar item '{item}' da lista: {e}")

    # --- Resposta Final ---
    print(f"API_RESP: Retornando {len(pokemons_result)} Pokémon com detalhes para {canal}/{usuario}.")
    return jsonify(pokemons_result)

# --- Inicialização do Servidor ---

if __name__ == '__main__':
    # Garante que a inicialização do DB ocorra antes de rodar o app
    try:
        init_db()
    except Exception as e:
        print(f"CRITICAL: Não foi possível inicializar o banco de dados. Encerrando. Erro: {e}")
        exit(1) # Sai se não conseguir inicializar o DB

    # Roda o servidor Flask
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    print(f"==> Iniciando Flask app em http://{host}:{port} <==")
    app.run(debug=True, host=host, port=port) # debug=True para desenvolvimento