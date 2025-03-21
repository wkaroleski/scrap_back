from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time

app = Flask(__name__)
CORS(app)

# Cache para armazenar os detalhes dos Pokémon
pokemon_cache = {}

def fetch_pokemon_details(pokemon_id, max_retries=3, delay=5):
    """
    Busca detalhes do Pokémon na API usando o ID.
    Retorna None se o Pokémon não for encontrado após várias tentativas.
    """
    if pokemon_id in pokemon_cache:
        print(f"Retornando dados do Pokémon ID {pokemon_id} do cache.")
        return pokemon_cache[pokemon_id]

    url = f"https://pokeapi.co/api/v2/pokemon/{pokemon_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # Configuração dos proxies
    proxies = {
        "http": "http://63.143.57.117:80",  # Proxy HTTP
        "https": "http://52.26.114.229:1080",  # Proxy HTTPS
    }

    for attempt in range(max_retries):
        try:
            print(f"Tentativa {attempt + 1} de buscar detalhes do Pokémon ID {pokemon_id}...")
            response = requests.get(url, headers=headers, proxies=proxies)
            response.raise_for_status()
            data = response.json()

            stats = {}
            total_base_stats = 0
            for stat_entry in data['stats']:
                stat_name = stat_entry['stat']['name']
                base_stat = stat_entry['base_stat']
                stats[stat_name] = base_stat
                total_base_stats += base_stat

            pokemon_data = {
                'id': pokemon_id,
                'name': data['name'],
                'stats': stats,
                'total_base_stats': total_base_stats,
                'types': [t['type']['name'] for t in data.get('types', [])],
                'image': data['sprites']['front_default'],
                'shiny_image': data['sprites']['front_shiny']
            }

            pokemon_cache[pokemon_id] = pokemon_data
            print(f"Dados do Pokémon ID {pokemon_id} armazenados no cache.")
            return pokemon_data
        except requests.exceptions.RequestException as e:
            print(f"Erro ao buscar detalhes do Pokémon ID {pokemon_id}: {e}")
            if attempt < max_retries - 1:
                print(f"Tentando novamente em {delay} segundos...")
                time.sleep(delay)
            else:
                print(f"Falha ao buscar detalhes do Pokémon ID {pokemon_id} após {max_retries} tentativas.")
                return None

def scrape_pokemon(canal, usuario):
    """
    Faz o scraping da página e coleta os Pokémon.
    Usa o ID para buscar os dados na API.
    """
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"

    # Headers para simular um navegador real
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # Configuração dos proxies
    proxies = {
        "http": "http://63.143.57.117:80",  # Proxy HTTP
        "https": "http://52.26.114.229:1080",  # Proxy HTTPS
    }

    try:
        print(f"Scraping URL: {url}")
        response = requests.get(url, headers=headers, proxies=proxies)
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

            # Busca detalhes na API usando o ID
            api_data = fetch_pokemon_details(pokemon_id)
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