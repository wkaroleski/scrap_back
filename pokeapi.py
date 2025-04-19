# -*- coding: utf-8 -*-
import os
import json
import traceback
import psycopg2 # Para interagir com o cache no DB
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport
from dotenv import load_dotenv

# Importa a função de conexão do módulo database
from database import get_db_connection

# Carrega variáveis de ambiente (necessário para POKEAPI_GRAPHQL_URL se estiver no .env)
load_dotenv()

# --- Configuração PokéAPI GraphQL ---
POKEAPI_GRAPHQL_URL = os.getenv("POKEAPI_GRAPHQL_URL", "https://beta.pokeapi.co/graphql/v1beta") # Default URL
HEADERS = {
    "User-Agent": "Mozilla/5.0", # User agent genérico
    "Content-Type": "application/json",
}

# --- Inicialização do Cliente GraphQL ---
gql_client = None
try:
    # Idealmente, usar um SSLContext mais robusto em produção se necessário
    # ssl_context = ssl.create_default_context()
    transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL, headers=HEADERS, ssl=True) # ssl=True é o padrão, pode omitir
    # fetch_schema_from_transport pode ser lento na inicialização, considere alternativas
    # ou lidar com possíveis falhas aqui de forma mais robusta
    gql_client = Client(transport=transport, fetch_schema_from_transport=True)
    print("INFO: Cliente GraphQL inicializado com sucesso.")
except Exception as gql_setup_error:
    print(f"CRITICAL: Falha ao configurar cliente GraphQL: {gql_setup_error}")
    # A aplicação pode continuar, mas fetch_pokemon_details falhará se o cliente for None
    gql_client = None


# --- Função de Lógica de Pokémon (DB Cache + API Fetch) ---

def fetch_pokemon_details(pokemon_id: int):
    """
    Busca detalhes do Pokémon na PokéAPI v1beta (GraphQL), usando cache no DB.
    Retorna um dicionário com detalhes ou None em caso de erro.
    """
    if gql_client is None:
        print(f"FETCH_ERROR ID {pokemon_id}: Cliente GraphQL não disponível.")
        return None

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
             print(f"FETCH_ERROR ID {pokemon_id}: Não foi possível conectar ao DB para verificar cache.")
             # Decide se quer tentar a API mesmo sem DB ou falhar. Vamos tentar a API.
             pass # Continua para a API

        # 1. Tenta buscar no banco de dados (cache 'pokemon') se conectado
        if conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT id, name, stats, total_base_stats, types, image, shiny_image FROM pokemon WHERE id = %s', (pokemon_id,))
                row = cursor.fetchone()

                if row:
                    # Cache hit! Processa JSONB (que pode ser dict ou string)
                    stats_val = row[2]
                    types_val = row[4]
                    stats_data = {}
                    types_data = []
                    parse_error = False
                    try:
                        # Tenta processar como dict primeiro, depois como string JSON
                        if isinstance(stats_val, dict): stats_data = stats_val
                        elif isinstance(stats_val, str): stats_data = json.loads(stats_val) if stats_val else {}
                        else: stats_data = {}

                        if isinstance(types_val, list): types_data = types_val
                        elif isinstance(types_val, str): types_data = json.loads(types_val) if types_val else []
                        else: types_data = []

                        # Validação básica dos tipos processados
                        if not isinstance(stats_data, dict): raise TypeError("Stats não é dict")
                        if not isinstance(types_data, list): raise TypeError("Types não é list")

                    except (json.JSONDecodeError, TypeError) as e:
                        print(f"JSON_ERROR (Cache Hit ID {pokemon_id}): Falha DB parse. Forcing API fetch.")
                        print(f"--> Erro: {e}. Stats: '{stats_val}', Types: '{types_val}'")
                        row = None # Invalida cache hit
                        parse_error = True
                    except Exception as e_parse:
                         print(f"PARSE_ERROR (Cache Hit ID {pokemon_id}): Unexpected parse error. Forcing API fetch.")
                         print(f"--> Erro: {e_parse}.")
                         row = None # Invalida cache hit
                         parse_error = True

                    if row and not parse_error: # Se cache hit válido
                        print(f"CACHE_HIT: Detalhes do ID {pokemon_id} encontrados no cache 'pokemon'.")
                        # Fecha a conexão antes de retornar
                        conn.close()
                        return {
                            'id': row[0], 'name': row[1], 'stats': stats_data,
                            'total_base_stats': row[3], 'types': types_data,
                            'image': row[5], 'shiny_image': row[6]
                        }
                    # Se row foi invalidado ou erro no parse, continua para API...
            # Fecha a conexão se não for mais usá-la aqui antes da chamada da API
            if conn: conn.close(); conn = None


        # 2. Cache miss ou cache hit invalidado -> Busca na API
        print(f"FETCH_API: Buscando ID {pokemon_id} da API (GraphQL)...")
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
            # Idealmente, usar a versão assíncrona se o Flask for async,
            # mas para Flask padrão, execute() síncrono está ok.
            result = gql_client.execute(query, variable_values={"id": pokemon_id})

        except Exception as api_err:
            print(f"API_ERROR ID {pokemon_id}: Falha na chamada GraphQL: {api_err}")
            # Não tentar salvar no cache se a API falhou
            return None # Retorna None em caso de erro na API

        # Verifica se a resposta da API é válida
        if not result or not result.get("pokemon_v2_pokemon"):
            print(f"API_WARN ID {pokemon_id}: Nenhum dado retornado pela API.")
            # Considerar salvar um "placeholder" ou registro de falha no cache?
            # Por ora, apenas retornamos None.
            return None

        # Processa os dados da API
        data = result["pokemon_v2_pokemon"][0]
        stats = {s["pokemon_v2_stat"]["name"]: s["base_stat"] for s in data.get("pokemon_v2_pokemonstats", [])}
        types = [t["pokemon_v2_type"]["name"] for t in data.get("pokemon_v2_pokemontypes", [])]
        total_base_stats = sum(stats.values())

        # Processamento de Sprites (com cuidado extra para JSON malformado ou ausente)
        sprites = {}
        sprites_data = data.get("pokemon_v2_pokemonsprites", [])
        if sprites_data:
            # A API retorna uma lista, pegamos o primeiro elemento
            sprite_json_or_dict = sprites_data[0].get("sprites", "{}")
            try:
                if isinstance(sprite_json_or_dict, str):
                    # Evita erro se string for vazia ou inválida
                    sprites = json.loads(sprite_json_or_dict) if sprite_json_or_dict else {}
                elif isinstance(sprite_json_or_dict, dict):
                    sprites = sprite_json_or_dict
                else: # Caso inesperado
                     sprites = {}
            except json.JSONDecodeError:
                 print(f"API_WARN ID {pokemon_id}: JSON de sprites inválido recebido: '{sprite_json_or_dict}'")
                 sprites = {} # Define como vazio se o parse falhar

        image = sprites.get("front_default")
        shiny_image = sprites.get("front_shiny")

        # Prepara dados para salvar no cache
        stats_json = json.dumps(stats)
        types_json = json.dumps(types)

        # 3. Salva no cache do banco de dados se a conexão for possível
        conn_save = None
        try:
            conn_save = get_db_connection()
            if conn_save:
                with conn_save.cursor() as cursor_save:
                     # Usar ON CONFLICT para inserir ou ignorar se já existe (evita race condition se outra req já inseriu)
                     # Se quiser atualizar sempre, use ON CONFLICT DO UPDATE
                    cursor_save.execute(
                        '''
                        INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        ''',
                        (data['id'], data['name'], stats_json, total_base_stats, types_json, image, shiny_image)
                    )
                    print(f"DB_INSERT: Detalhes do ID {pokemon_id} salvos/verificados no cache 'pokemon'.")
            else:
                 print(f"DB_WARN ID {pokemon_id}: Não foi possível conectar ao DB para salvar no cache.")
        except psycopg2.Error as insert_err:
             print(f"DB_ERROR (Insert ID {pokemon_id}): {insert_err}")
             # Continua para retornar os dados mesmo se salvar falhar
        except Exception as e_save:
             print(f"DB_ERROR (Unexpected Insert ID {pokemon_id}): {e_save}")
        finally:
            if conn_save: conn_save.close()


        # Retorna os dados processados da API (em formato dict/list Python)
        return {
            'id': data['id'], 'name': data['name'], 'stats': stats,
            'total_base_stats': total_base_stats, 'types': types,
            'image': image, 'shiny_image': shiny_image
        }

    except psycopg2.Error as db_err:
        # Erro durante a tentativa inicial de leitura do cache
        print(f"DB_ERROR (Fetch Cache ID {pokemon_id}): {db_err}")
        # Pode decidir tentar a API mesmo assim ou retornar None.
        # A lógica atual já continuaria para a API se a conexão falhar.
        # Se o erro for aqui, provavelmente já tentou a API.
        return None
    except Exception as e:
        print(f"UNEXPECTED_ERROR (Fetch ID {pokemon_id}): {e}")
        traceback.print_exc()
        return None
    finally:
        # Garante que a conexão inicial seja fechada se ainda estiver aberta
        if conn:
            conn.close()