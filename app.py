from flask import Flask, jsonify, request
from flask_cors import CORS  # Importe o CORS
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)  # Habilite o CORS para todas as rotas

def normalize_pokemon_name(name):
    """
    Normaliza o nome do Pokémon para ser compatível com a API.
    Exemplo: "Farfetch'd" -> "farfetchd"
    """
    return name.lower().replace("'", "").replace(" ", "-")

def fetch_pokemon_details(pokemon_id):
    """
    Busca detalhes do Pokémon na API usando o ID.
    Retorna None se o Pokémon não for encontrado.
    """
    url = f"https://pokeapi.co/api/v2/pokemon/{pokemon_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Lança um erro se a requisição falhar
        data = response.json()

        # Extrai os stats
        stats = {}
        total_base_stats = 0  # Variável para armazenar o total dos stats base
        for stat_entry in data['stats']:
            stat_name = stat_entry['stat']['name']
            base_stat = stat_entry['base_stat']
            stats[stat_name] = base_stat
            total_base_stats += base_stat  # Soma o valor do stat ao total

        return {
            'id': pokemon_id,
            'name': data['name'],
            'stats': stats,
            'total_base_stats': total_base_stats,  # Adiciona o total dos stats base
            'types': [t['type']['name'] for t in data.get('types', [])],
            'image': data['sprites']['front_default'],
            'shiny_image': data['sprites']['front_shiny']
        }
    except requests.exceptions.RequestException as e:
        print(f"Erro ao buscar detalhes do Pokémon ID {pokemon_id}: {e}")
        return None

def scrape_pokemon(canal, usuario):
    """
    Faz o scraping da página e coleta os Pokémon.
    Usa o ID para buscar os dados na API.
    """
    url = f"https://grynsoft.com/spos-app/?c={canal}&u={usuario}"
    
    # Configuração do Proxy
    proxies = {
        "http": "http://63.143.57.117:80",  # Proxy HTTP
        "http": "https://52.26.114.229:1080",  # Proxy HTTPS
    }

    # Headers para simular um navegador real
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        print(f"Scraping URL: {url}")
        print(f"Usando Proxy: {proxies}")  # Imprime o proxy sendo usado

        # Faz a requisição com o proxy e headers
        response = requests.get(url, proxies=proxies, headers=headers)
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
        pokemons = scrape_pokemon(canal, usuario)
        return jsonify(pokemons)  # Retorna os dados em JSON
    else:
        return jsonify({"error": "Por favor, forneça 'canal' e 'usuario' como parâmetros na URL."}), 400

if __name__ == '__main__':
    app.run(debug=True)