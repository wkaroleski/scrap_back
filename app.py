from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time
import psycopg2
from psycopg2 import sql
import os
import json
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport

app = Flask(__name__)
CORS(app)

# Configurações do banco de dados (usando variáveis de ambiente)
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME")
}

# Configuração da PokéAPI GraphQL
POKEAPI_GRAPHQL_URL = "https://beta.pokeapi.co/graphql/v1beta"

# Cria um cliente GraphQL
transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL)
client = Client(transport=transport, fetch_schema_from_transport=True)

def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    return psycopg2.connect(**DB_CONFIG)

def init_db():
    """Cria a tabela 'pokemon' se ela não existir."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pokemon (
            id INTEGER PRIMARY KEY,
            name TEXT,
            stats JSONB,  -- Armazena os stats como JSON
            total_base_stats INTEGER,
            types JSONB,  -- Armazena os tipos como JSON
            image TEXT,
            shiny_image TEXT
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

# Inicializa o banco de dados
init_db()

def fetch_pokemon_details(pokemon_id):
    """
    Busca detalhes do Pokémon na PokéAPI GraphQL usando o ID.
    Retorna None se o Pokémon não for encontrado.
    """
    # Verifica se os dados já estão no banco de dados
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM pokemon WHERE id = %s', (pokemon_id,))
    row = cursor.fetchone()

    if row:
        print(f"Retornando dados do Pokémon ID {pokemon_id} do banco de dados.")
        return {
            'id': row[0],
            'name': row[1],
            'stats': json.loads(row[2]),  # Converte o JSON de volta para um dicionário
            'total_base_stats': row[3],
            'types': json.loads(row[4]),  # Converte o JSON de volta para uma lista
            'image': row[5],
            'shiny_image': row[6]
        }

    # Query GraphQL para buscar os detalhes do Pokémon
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
        print(f"Buscando detalhes do Pokémon ID {pokemon_id} na PokéAPI GraphQL...")
        result = client.execute(query, variable_values={"id": pokemon_id})

        if not result["pokemon_v2_pokemon"]:
            print(f"Pokémon ID {pokemon_id} não encontrado.")
            return None

        data = result["pokemon_v2_pokemon"][0]

        # Extrai os stats
        stats = {stat["pokemon_v2_stat"]["name"]: stat["base_stat"] for stat in data["pokemon_v2_pokemonstats"]}
        total_base_stats = sum(stat["base_stat"] for stat in data["pokemon_v2_pokemonstats"])

        # Extrai os tipos
        types = [t["pokemon_v2_type"]["name"] for t in data["pokemon_v2_pokemontypes"]]

        # Extrai as imagens
        sprites = json.loads(data["pokemon_v2_pokemonsprites"][0]["sprites"])
        image = sprites.get("front_default")
        shiny_image = sprites.get("front_shiny")

        pokemon_data = {
            'id': data['id'],
            'name': data['name'],
            'stats': json.dumps(stats),  # Converte o dicionário para JSON
            'total_base_stats': total_base_stats,
            'types': json.dumps(types),  # Converte a lista para JSON
            'image': image,
            'shiny_image': shiny_image
        }

        # Armazena os dados no banco de dados
        cursor.execute('''
        INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING;  -- Evita duplicatas
        ''', (
            pokemon_data['id'],
            pokemon_data['name'],
            pokemon_data['stats'],
            pokemon_data['total_base_stats'],
            pokemon_data['types'],
            pokemon_data['image'],
            pokemon_data['shiny_image']
        ))
        conn.commit()

        print(f"Dados do Pokémon ID {pokemon_data['id']} armazenados no banco de dados.")
        return pokemon_data
    except Exception as e:
        print(f"Erro ao buscar detalhes do Pokémon ID {pokemon_id}: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def scrape_pokemon(canal, usuario):
    """
    Faz o scraping da página e coleta os Pokémon.
    Usa o ID para buscar os dados na PokéAPI GraphQL.
    """
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"

    # Headers para simular um navegador real
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        print(f"Scraping URL: {url}")
        response = requests.get(url, headers=headers)
        print(f"Status Code: {response.status_code}")
        print(f"Response Content: {response.text}")  # Verifica o conteúdo da resposta
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        pokemons = []
        seen_ids = set()  # Armazena os IDs dos Pokémon já processados

        pokemon_elements = soup.select('.Pokemon:not(#unobtained)')
        print(f"Elementos encontrados: {len(pokemon_elements)}")

        for element in pokemon_elements:
            # Extrai o ID do Pokémon
            index_element = element.select_one('.Index')
            if not index_element:
                continue  # Ignora se não houver ID

            pokemon_id = index_element.text.strip().replace('#', '')  # Remove o '#' do ID
            pokemon_id = str(int(pokemon_id))  # Remove zeros à esquerda (ex: "001" -> "1")

            if pokemon_id in seen_ids:
                continue  # Ignora Pokémon repetidos

            seen_ids.add(pokemon_id)  # Adiciona o ID ao conjunto de Pokémon processados

            # Extrai o nome do Pokémon
            name_element = element.select_one('b')
            name = name_element.text.strip() if name_element else None

            # Verifica se o Pokémon é shiny
            shiny = element.get('id') == 'shiny'

            # Busca detalhes na PokéAPI GraphQL usando o ID
            api_data = fetch_pokemon_details(int(pokemon_id))
            if api_data:
                pokemons.append({
                    'id': pokemon_id,
                    'name': name,
                    'shiny': shiny,
                    'stats': api_data.get('stats'),
                    'total_base_stats': api_data.get('total_base_stats'),
                    'types': api_data.get('types'),
                    'image': api_data['shiny_image'] if shiny else api_data['image']
                })
            else:
                # Se a API falhar, adiciona apenas os dados básicos
                pokemons.append({
                    'id': pokemon_id,
                    'name': name,
                    'shiny': shiny,
                    'stats': None,
                    'total_base_stats': None,
                    'types': None,
                    'image': None
                })

        print(f"Pokémon coletados (sem repetição): {len(pokemons)}")
        return pokemons
    except requests.exceptions.RequestException as e:
        return f"Erro ao acessar a página: {e}"
    except Exception as e:
        return f"Ocorreu um erro: {e}"

# Rota para fornecer os dados dos Pokémon em JSON
@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    canal = request.args.get('canal')
    usuario = request.args.get('usuario')
    if canal and usuario:
        print(f"Recebida requisição para canal={canal}, usuario={usuario}")
        pokemons = scrape_pokemon(canal, usuario)
        return jsonify(pokemons)  # Retorna os dados em JSON
    else:
        return jsonify({"error": "Por favor, forneça 'canal' e 'usuario' como parâmetros na URL."}), 400

if __name__ == '__main__':
    app.run(debug=True)