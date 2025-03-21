from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import psycopg2
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

# Valida se as variáveis de ambiente estão configuradas
for key, value in DB_CONFIG.items():
    if value is None:
        raise ValueError(f"Variável de ambiente {key} não configurada.")

# Configuração da PokéAPI GraphQL
POKEAPI_GRAPHQL_URL = "https://beta.pokeapi.co/graphql/v1beta"

# Adiciona headers personalizados
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Content-Type": "application/json",
}

# Cria um cliente GraphQL com headers e SSL habilitado
transport = AIOHTTPTransport(url=POKEAPI_GRAPHQL_URL, headers=headers, ssl=True)
client = Client(transport=transport, fetch_schema_from_transport=True)

def get_db_connection():
    """Retorna uma conexão com o banco de dados."""
    return psycopg2.connect(**DB_CONFIG)

def init_db():
    """Cria a tabela 'pokemon' se ela não existir."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
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
            conn.commit()

init_db()

def fetch_pokemon_details(pokemon_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT * FROM pokemon WHERE id = %s', (pokemon_id,))
            row = cursor.fetchone()

            if row:
                return {
                    'id': row[0],
                    'name': row[1],
                    'stats': json.loads(row[2]),
                    'total_base_stats': row[3],
                    'types': json.loads(row[4]),
                    'image': row[5],
                    'shiny_image': row[6]
                }

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

                if not result["pokemon_v2_pokemon"]:
                    return None

                data = result["pokemon_v2_pokemon"][0]

                stats = {stat["pokemon_v2_stat"]["name"]: stat["base_stat"] for stat in data["pokemon_v2_pokemonstats"]}
                total_base_stats = sum(stat["base_stat"] for stat in data["pokemon_v2_pokemonstats"])

                types = [t["pokemon_v2_type"]["name"] for t in data["pokemon_v2_pokemontypes"]]

                sprites_data = data.get("pokemon_v2_pokemonsprites", [])
                if sprites_data:
                    sprites = json.loads(sprites_data[0]["sprites"]) if isinstance(sprites_data[0]["sprites"], str) else sprites_data[0]["sprites"]
                else:
                    sprites = {}

                image = sprites.get("front_default")
                shiny_image = sprites.get("front_shiny")

                pokemon_data = {
                    'id': data['id'],
                    'name': data['name'],
                    'stats': json.dumps(stats),
                    'total_base_stats': total_base_stats,
                    'types': json.dumps(types),
                    'image': image,
                    'shiny_image': shiny_image
                }

                cursor.execute('''
                    INSERT INTO pokemon (id, name, stats, total_base_stats, types, image, shiny_image)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
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

                return pokemon_data
            except Exception as e:
                print(f"Erro ao buscar Pokémon: {e}")
                return None

def scrape_pokemon(canal, usuario):
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        pokemons = []
        seen_ids = set()

        for element in soup.select('.Pokemon:not(#unobtained)'):
            index_element = element.select_one('.Index')
            if not index_element:
                continue

            pokemon_id = index_element.text.strip().lstrip('#0')

            if pokemon_id in seen_ids:
                continue

            seen_ids.add(pokemon_id)

            shiny = element.get('id') == 'shiny'

            api_data = fetch_pokemon_details(int(pokemon_id))
            if api_data:
                pokemons.append({
                    'id': pokemon_id,
                    'name': api_data['name'],
                    'shiny': shiny,
                    'stats': api_data['stats'],
                    'total_base_stats': api_data['total_base_stats'],
                    'types': api_data['types'],
                    'image': api_data['shiny_image'] if shiny else api_data['image']
                })
        return pokemons
    except Exception as e:
        return {"error": str(e)}

@app.route('/api/pokemons', methods=['GET'])
def get_pokemons():
    canal = request.args.get('canal')
    usuario = request.args.get('usuario')
    if not canal or not usuario:
        return jsonify({"error": "Forneça 'canal' e 'usuario' como parâmetros."}), 400

    return jsonify(scrape_pokemon(canal, usuario))

if __name__ == '__main__':
    app.run(debug=True)
