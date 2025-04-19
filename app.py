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
import json # Necessário para queries JSONB e jsonify
import psycopg2 # Necessário para interagir com DB diretamente na rota

# --- Importações dos Módulos Refatorados ---
from database import init_db, get_cached_dex, update_cached_dex, get_db_connection
from scraping import scrape_grynsoft_dex
from pokeapi import fetch_pokemon_details

# --- Inicialização do Flask App ---
app = Flask(__name__)
CORS(app)

# --- Constantes e Configurações ---
ALLOWED_SORT_COLUMNS = {
    "id": "id",
    "name": "name",
    "total_base_stats": "total_base_stats",
    "hp": "(stats->>'hp')::int",
    "attack": "(stats->>'attack')::int",
    "defense": "(stats->>'defense')::int",
    "special-attack": "(stats->>'special-attack')::int",
    "special-defense": "(stats->>'special-defense')::int",
    "speed": "(stats->>'speed')::int",
}
DEFAULT_SORT_COLUMN = "id"
DEFAULT_SORT_ORDER = "ASC"

# --- Função Reutilizável para Obter Lista (Cache > Scrape) ---
def get_or_scrape_user_dex_list(canal_lower: str, usuario_lower: str, canal_original: str, usuario_original: str, refresh: bool = False):
    # (Código desta função mantido como antes)
    cached_list = None
    if not refresh:
        cached_list = get_cached_dex(canal_lower, usuario_lower)
    if cached_list is not None:
        return cached_list
    else:
        print(f"HELPER_LIST: {'Refresh solicitado' if refresh else 'Cache miss/expirado'} para {canal_lower}/{usuario_lower}. Iniciando scraping...")
        scrape_result = scrape_grynsoft_dex(canal_original, usuario_original)
        if isinstance(scrape_result, dict) and 'error' in scrape_result:
            print(f"HELPER_LIST: Erro no scraping para {canal_lower}/{usuario_lower}: {scrape_result['error']}")
            return scrape_result
        scraped_list = scrape_result
        if scraped_list is not None:
             print(f"HELPER_LIST: Scraping para {canal_lower}/{usuario_lower} retornou {len(scraped_list)} itens. Atualizando cache...")
             update_cached_dex(canal_lower, usuario_lower, scraped_list)
             return scraped_list
        else:
             print(f"HELPER_LIST_WARN: Scraping para {canal_lower}/{usuario_lower} retornou None sem erro explícito.")
             update_cached_dex(canal_lower, usuario_lower, [])
             return []

# --- Rotas da API ---

@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    """
    Endpoint principal. Retorna lista PAGINADA de Pokémon com detalhes,
    com suporte a FILTRAGEM por tipo e ORDENAÇÃO por stats/id/nome no backend.
    """
    # --- Obtenção e Validação de Parâmetros ---
    canal_original = request.args.get('canal')
    usuario_original = request.args.get('usuario')
    refresh_flag = request.args.get('refresh', 'false').lower() == 'true'

    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        if page < 1: page = 1
        if per_page < 1: per_page = 20
    except ValueError:
        return jsonify({"error": "Parâmetros 'page' e 'per_page' devem ser números inteiros."}), 400

    filter_type = request.args.get('type')
    sort_by_param = request.args.get('sort_by')
    order_param = request.args.get('order', DEFAULT_SORT_ORDER).upper()

    canal_lower = canal_original.lower() if canal_original else None
    usuario_lower = usuario_original.lower() if usuario_original else None
    if not canal_lower or not usuario_lower:
        return jsonify({"error": "Forneça 'canal' e 'usuario'."}), 400

    print(f"API_REQ /pokemons: Canal={canal_lower}, Usuario={usuario_lower}, Refresh={refresh_flag}, "
          f"Page={page}, PerPage={per_page}, Type={filter_type}, SortBy={sort_by_param}, Order={order_param}")

    # --- Obter a Lista Bruta Base (IDs/Shiny) ---
    list_result = get_or_scrape_user_dex_list(canal_lower, usuario_lower, canal_original, usuario_original, refresh=refresh_flag)
    if isinstance(list_result, dict) and 'error' in list_result:
        return jsonify(list_result), 500
    if list_result is None:
         return jsonify({"error": "Erro interno ao obter a lista de Pokémon base."}), 500
    if not list_result:
         print(f"API_LOGIC /pokemons: Usuário {canal_lower}/{usuario_lower} não possui Pokémon.")
         return jsonify({"items": [], "metadata": {"page": 1, "per_page": per_page, "total_items": 0, "total_pages": 0}})

    # Extrair IDs e criar mapa de shiny_status
    user_pokemon_ids_str = set()
    shiny_map = {}
    for item in list_result:
         if isinstance(item, dict) and 'id' in item and item['id'].isdigit():
              id_str = item['id']
              user_pokemon_ids_str.add(id_str)
              shiny_map[id_str] = item.get('shiny', False) == True
         else:
              print(f"API_WARN /pokemons: Item inválido na lista base para {canal_lower}/{usuario_lower}: {item}")

    if not user_pokemon_ids_str:
        print(f"API_LOGIC /pokemons: Nenhum ID válido encontrado para {canal_lower}/{usuario_lower}.")
        return jsonify({"items": [], "metadata": {"page": 1, "per_page": per_page, "total_items": 0, "total_pages": 0}})

    user_pokemon_ids_int = [int(id_str) for id_str in user_pokemon_ids_str]

    # --- Filtragem e Ordenação no Banco de Dados ---
    filtered_sorted_ids = []
    total_items_after_filter = 0
    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            raise Exception("Falha ao conectar ao banco de dados para filtrar/ordenar.")

        with conn.cursor() as cursor:
            # --- Construção da Query SQL ---
            # CORREÇÃO: Inicializa lista de parâmetros
            params_list = []
            sql_select = "SELECT id"
            sql_from_table = "pokemon"

            # CORREÇÃO: Monta WHERE e adiciona parâmetros à lista
            sql_where = "WHERE id = ANY(%s)"
            params_list.append(user_pokemon_ids_int) # Adiciona a lista de IDs diretamente

            if filter_type:
                sql_where += " AND types @> %s::jsonb"
                params_list.append(json.dumps([filter_type])) # Adiciona o parâmetro de tipo
                print(f"API_LOGIC /pokemons: Aplicando filtro type='{filter_type}'")

            # Monta ORDER BY (sem mudanças aqui)
            sql_order_by = ""
            sort_column_sql = ALLOWED_SORT_COLUMNS.get(sort_by_param, ALLOWED_SORT_COLUMNS[DEFAULT_SORT_COLUMN])
            sort_order_sql = "DESC" if order_param == "DESC" else "ASC"
            sql_order_by = f"ORDER BY {sort_column_sql} {sort_order_sql}, id ASC"
            print(f"API_LOGIC /pokemons: Aplicando ordenação por {sort_column_sql} {sort_order_sql}")

            # --- Query para Contagem Total (Após Filtros) ---
            sql_count = f"SELECT COUNT(*) FROM {sql_from_table} {sql_where}"
            print(f"DEBUG /pokemons: Executando COUNT query: {cursor.mogrify(sql_count, params_list).decode('utf-8')}")
            # CORREÇÃO: Passa a lista de parâmetros diretamente
            cursor.execute(sql_count, params_list)
            total_items_after_filter = cursor.fetchone()[0]
            print(f"API_LOGIC /pokemons: Total de itens após filtro: {total_items_after_filter}")

            # --- Query para Obter IDs da Página ---
            total_pages = 0 # Inicializa total_pages
            if total_items_after_filter > 0:
                 total_pages = (total_items_after_filter + per_page - 1) // per_page
                 if page > total_pages:
                      print(f"API_WARN /pokemons: Página {page} solicitada excede o total de {total_pages} páginas após filtro.")
                 else:
                    offset = (page - 1) * per_page
                    sql_fetch_ids = f"{sql_select} FROM {sql_from_table} {sql_where} {sql_order_by} LIMIT %s OFFSET %s"
                    # CORREÇÃO: Cria lista final de parâmetros para esta query
                    fetch_params_list = params_list + [per_page, offset]
                    print(f"DEBUG /pokemons: Executando FETCH IDs query: {cursor.mogrify(sql_fetch_ids, fetch_params_list).decode('utf-8')}")
                    # CORREÇÃO: Passa a lista de parâmetros correta
                    cursor.execute(sql_fetch_ids, fetch_params_list)
                    filtered_sorted_ids = [row[0] for row in cursor.fetchall()]
                    print(f"API_LOGIC /pokemons: IDs para a página {page} (após filtro/sort): {filtered_sorted_ids}")
            # else: # Não precisa do else, total_pages já é 0

    except psycopg2.Error as db_err:
        print(f"DB_ERROR /pokemons: Erro ao filtrar/ordenar IDs: {db_err}")
        traceback.print_exc()
        return jsonify({"error": "Erro no banco de dados ao processar filtros/ordenação."}), 500
    except Exception as e:
        print(f"APP_ERROR /pokemons: Erro inesperado ao filtrar/ordenar: {e}")
        traceback.print_exc()
        return jsonify({"error": "Erro interno inesperado ao processar filtros/ordenação."}), 500
    finally:
        if conn:
            conn.close()

    # --- Paginação (Cálculo final dos metadados) ---
    # A variável total_pages já foi calculada dentro do bloco try
    if 'total_pages' not in locals(): total_pages = 0 # Garante que total_pages exista
    metadata = {"page": page, "per_page": per_page, "total_items": total_items_after_filter, "total_pages": total_pages}

    if total_items_after_filter == 0 or not filtered_sorted_ids:
         print(f"API_LOGIC /pokemons: Nenhum item encontrado para a página {page} após filtros/ordenação.")
         return jsonify({"items": [], "metadata": metadata}) # Retorna metadados mesmo se vazio

    # --- Buscar Detalhes Apenas para os IDs da Página ---
    pokemons_data_page = []
    print(f"API_LOGIC /pokemons: Buscando detalhes para {len(filtered_sorted_ids)} IDs da página {page}...")
    for pokemon_id_int in filtered_sorted_ids:
        details = fetch_pokemon_details(pokemon_id_int)
        id_str = str(pokemon_id_int)

        if details:
            shiny_status = shiny_map.get(id_str, False)
            final_stats = details.get('stats', {})
            final_types = details.get('types', [])
            if not isinstance(final_stats, dict): final_stats = {}
            if not isinstance(final_types, list): final_types = []

            pokemons_data_page.append({
                'id': details.get('id'), 'name': details.get('name'),
                'shiny': shiny_status, 'stats': final_stats,
                'total_base_stats': details.get('total_base_stats'),
                'types': final_types,
                'image': details.get('shiny_image') if shiny_status else details.get('image')
            })
        else:
            print(f"API_WARN /pokemons: Falha ao obter detalhes para ID {pokemon_id_int} (pós-filtro/sort) na página {page}.")

    # --- Montar a Resposta Final Paginada ---
    response_data = {
        "items": pokemons_data_page,
        "metadata": metadata # Usa os metadados calculados anteriormente
    }
    print(f"API_RESP /pokemons: Retornando {len(pokemons_data_page)} Pokémon para a página {page}/{total_pages} (Total filtrado: {total_items_after_filter}).")
    return jsonify(response_data)


# --- Rota de Comparação (Mantida da versão anterior) ---
@app.route('/api/compare_dex', methods=['GET'])
def compare_dex():
    # (Código desta função mantido exatamente como na versão anterior)
    # 1. Pega e valida parâmetros
    canal_original = request.args.get('canal')
    usuario1_original = request.args.get('usuario1')
    usuario2_original = request.args.get('usuario2')

    if not all([canal_original, usuario1_original, usuario2_original]):
        return jsonify({"error": "Forneça canal, usuario1 (você) e usuario2 (o outro)."}), 400
    if usuario1_original.lower() == usuario2_original.lower():
         return jsonify({"error": "usuario1 e usuario2 não podem ser iguais."}), 400

    # 2. Padroniza
    canal_lower = canal_original.lower()
    usuario1_lower = usuario1_original.lower()
    usuario2_lower = usuario2_original.lower()
    print(f"API_REQ /compare_dex: Canal={canal_lower}, Base(User1)={usuario1_lower}, Comparado(User2)={usuario2_lower}")

    # 3. Obtém as listas brutas
    list1_result = get_or_scrape_user_dex_list(canal_lower, usuario1_lower, canal_original, usuario1_original, refresh=False)
    if isinstance(list1_result, dict) and 'error' in list1_result: return jsonify({"error_user1": f"Falha ao obter dados para {usuario1_original}: {list1_result['error']}"}), 500
    if list1_result is None: return jsonify({"error_user1": f"Erro interno ao obter dados para {usuario1_original}."}), 500

    list2_result = get_or_scrape_user_dex_list(canal_lower, usuario2_lower, canal_original, usuario2_original, refresh=False)
    if isinstance(list2_result, dict) and 'error' in list2_result: return jsonify({"error_user2": f"Falha ao obter dados para {usuario2_original}: {list2_result['error']}"}), 500
    if list2_result is None: return jsonify({"error_user2": f"Erro interno ao obter dados para {usuario2_original}."}), 500

    # 4. Cria lookups e extrai IDs
    user2_dict = {item['id']: item for item in list2_result if isinstance(item, dict) and 'id' in item}
    set1_ids = {item['id'] for item in list1_result if isinstance(item, dict) and 'id' in item}
    set2_ids = set(user2_dict.keys())

    # 5. Calcula a diferença
    try:
        usuario2_tem_que_usuario1_nao_tem_ids = sorted(list(set2_ids - set1_ids), key=int)
    except ValueError:
         print(f"COMPARE_WARN: IDs não numéricos encontrados na diferença: {list(set2_ids - set1_ids)}")
         usuario2_tem_que_usuario1_nao_tem_ids = sorted(list(set2_ids - set1_ids))
    print(f"API_LOGIC /compare_dex: User2 ({usuario2_lower}) tem {len(usuario2_tem_que_usuario1_nao_tem_ids)} Pokémon exclusivos.")

    # 6. Busca detalhes COMPLETOS
    pokemon_faltantes_para_user1 = []
    for id_str in usuario2_tem_que_usuario1_nao_tem_ids:
        try:
            pokemon_id_int = int(id_str)
            details = fetch_pokemon_details(pokemon_id_int)
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
            else:
                 print(f"COMPARE_WARN: Falha ao obter detalhes para ID {id_str} na comparação.")
        except (ValueError, KeyError, TypeError) as e:
             print(f"COMPARE_ERROR: Erro processando ID {id_str} para detalhes completos: {e}")

    # 7. Monta e retorna a resposta final
    response_data = {
        "canal": canal_original,
        "usuario_base": usuario1_original,
        "usuario_comparado": usuario2_original,
        "pokemon_que_faltam": pokemon_faltantes_para_user1
    }
    print(f"API_RESP /compare_dex: Retornando {len(pokemon_faltantes_para_user1)} Pokémon faltantes para {usuario1_original}.")
    return jsonify(response_data)


# --- Inicialização do Servidor ---
if __name__ == '__main__':
    try:
        print("APP_INIT: Preparando para chamar init_db()...")
        init_db()
        print("APP_INIT: Chamada a init_db() concluída.")
    except Exception as e:
        print(f"CRITICAL_ERROR: Falha na chamada de init_db: {e}")

    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true' or os.environ.get('FLASK_ENV') == 'development'

    print(f"==> Iniciando Flask app em http://{host}:{port} (Debug: {debug_mode}) <==")
    app.run(debug=debug_mode, host=host, port=port, use_reloader=False)