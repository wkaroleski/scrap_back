# -*- coding: utf-8 -*-
import os
import psycopg2
import json
import datetime
from datetime import timezone
import traceback
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env para o ambiente os.environ
# É bom garantir que sejam carregadas antes de usar os.getenv
load_dotenv()

# --- Configurações ---
USER_DEX_CACHE_TTL_SECONDS = 24 * 60 * 60 # 24 horas

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME")
}

# Validação das Variáveis de Ambiente do DB
for key, value in DB_CONFIG.items():
    if value is None:
        # Se rodar como módulo, print pode não ser ideal, mas para agora ok
        print(f"CRITICAL_DB_CONFIG: Variável de ambiente DB_{key.upper()} não configurada.")
        # Poderia levantar um erro aqui para impedir a execução
        # raise ValueError(f"Variável de ambiente DB_{key.upper()} não configurada.")

# --- Funções de Banco de Dados ---

def get_db_connection():
    """Retorna uma conexão com o banco de dados com autocommit."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        # print("DB_DEBUG: Conexão estabelecida.") # Descomente para debug
        return conn
    except psycopg2.OperationalError as e:
        print(f"DB_ERROR: Falha na conexão: {e}")
        # Em vez de raise, pode ser melhor retornar None e tratar na chamada
        return None # Modificado para não quebrar a app inteira logo de cara
    except Exception as e:
        print(f"DB_ERROR: Erro inesperado ao conectar: {e}")
        return None


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
        if conn is None:
            print("DB_INIT_ERROR: Não foi possível obter conexão com o banco.")
            return # Sai da função se não conectar

        with conn.cursor() as cursor:
            for i, command in enumerate(commands):
                table_name = 'pokemon' if i == 0 else 'user_dex_cache'
                print(f"DB_INIT: Verificando/Criando tabela '{table_name}'...")
                cursor.execute(command)
                print(f"DB_INIT: Tabela '{table_name}' OK.")
        print("DB_INIT: Inicialização do DB concluída.")
    except Exception as e:
        print(f"DB_INIT_ERROR: {e}")
        # Não relança o erro para não parar a app principal necessariamente
    finally:
         if conn:
            # print("DB_DEBUG: Fechando conexão init_db.") # Descomente para debug
            conn.close()


# --- Funções para Cache da Lista de Usuário ---

def get_cached_dex(canal_lower: str, usuario_lower: str):
    """Tenta buscar a lista de Pokémon do cache 'user_dex_cache'. Usa chaves MINÚSCULAS."""
    print(f"CACHE_LIST: Verificando user_dex_cache para {canal_lower}/{usuario_lower}...")
    sql = "SELECT pokemon_list, last_updated FROM user_dex_cache WHERE canal = %s AND usuario = %s"
    conn = None
    result = None
    try:
        conn = get_db_connection()
        if conn is None: return None

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
                    processed_list = None
                    if isinstance(cached_list_val, str):
                        try: processed_list = json.loads(cached_list_val)
                        except json.JSONDecodeError: print(f"CACHE_LIST_ERROR: JSON inválido no cache para {canal_lower}/{usuario_lower}"); return None
                    elif isinstance(cached_list_val, list): processed_list = cached_list_val
                    else: print(f"CACHE_LIST_ERROR: Tipo inesperado no cache para {canal_lower}/{usuario_lower}"); return None

                    # Validação básica se é uma lista de dicts com 'id'
                    if isinstance(processed_list, list) and all(isinstance(item, dict) and 'id' in item for item in processed_list):
                        result = processed_list
                    else:
                         print(f"CACHE_LIST_ERROR: Formato inválido da lista no cache para {canal_lower}/{usuario_lower}")
                         return None

                else:
                    print(f"CACHE_LIST: Expirado para {canal_lower}/{usuario_lower}.")
                    # Retorna None para indicar que expirou
            else:
                print(f"CACHE_LIST: MISS para {canal_lower}/{usuario_lower}.")
                # Retorna None para indicar miss
    except psycopg2.Error as db_err: print(f"DB_ERROR ao buscar user_dex_cache: {db_err}")
    except Exception as e: print(f"UNEXPECTED_ERROR ao buscar user_dex_cache: {e}")
    finally:
        if conn: conn.close()
    return result


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
    updated = False
    try:
        # Validação antes de tentar serializar
        if not isinstance(pokemon_list, list):
             raise TypeError("pokemon_list deve ser uma lista")
        pokemon_list_json = json.dumps(pokemon_list)

        conn = get_db_connection()
        if conn is None: return False

        with conn.cursor() as cursor:
            cursor.execute(sql, (canal_lower, usuario_lower, pokemon_list_json, now_utc))
        print(f"CACHE_LIST: user_dex_cache atualizado com sucesso.")
        updated = True
    except TypeError as json_err: print(f"JSON_ERROR ao serializar lista: {json_err}")
    except psycopg2.Error as db_err: print(f"DB_ERROR ao atualizar user_dex_cache: {db_err}")
    except Exception as e: print(f"UNEXPECTED_ERROR ao atualizar user_dex_cache: {e}")
    finally:
        if conn: conn.close()
    return updated # Retorna True se sucesso, False se falha