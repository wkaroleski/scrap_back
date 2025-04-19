# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import traceback

# Headers podem ser definidos aqui ou importados de um config central
HEADERS = {
    "User-Agent": "Mozilla/5.0", # User agent genérico
    # Adicione outros headers se necessário
}

def scrape_grynsoft_dex(canal_original: str, usuario_original: str):
    """Faz o scraping e retorna a lista bruta [{'id':str, 'shiny':bool}] ou dict de erro."""
    url = f"https://grynsoft.com/spos-app/?c={canal_original}&u={usuario_original}"
    print(f"SCRAPING: Iniciando busca da LISTA em: {url}")
    scraped_list = []
    seen_ids = set()
    try:
        response = requests.get(url, headers=HEADERS, timeout=20) # Timeout de 20s
        response.raise_for_status() # Levanta erro para status HTTP 4xx ou 5xx
        soup = BeautifulSoup(response.content, 'html.parser')

        # Seleciona todos os elementos com classe 'Pokemon' que NÃO têm id 'unobtained'
        pokemon_elements = soup.select('.Pokemon:not(#unobtained)')

        for element in pokemon_elements:
            index_element = element.select_one('.Index')
            # Pula se não encontrar o elemento do índice
            if not index_element: continue

            # Extrai o ID, remove '#', '0' à esquerda e verifica se é dígito
            pokemon_id_str = index_element.text.strip().lstrip('#0')
            if not pokemon_id_str.isdigit(): continue

            # Evita duplicatas na lista raspada (caso a página tenha algum erro)
            if pokemon_id_str in seen_ids: continue
            seen_ids.add(pokemon_id_str)

            # Verifica se é shiny pelo atributo 'id' do elemento 'Pokemon'
            shiny = element.get('id') == 'shiny'

            scraped_list.append({'id': pokemon_id_str, 'shiny': shiny})

        print(f"SCRAPING: Lista concluída para {canal_original}/{usuario_original}. Encontrados {len(scraped_list)} Pokémon únicos.")
        return scraped_list

    except requests.exceptions.Timeout:
        print(f"SCRAPING_ERROR: Timeout ao raspar {url}")
        return {"error": f"Timeout (20s) ao buscar dados de {usuario_original}."}
    except requests.exceptions.HTTPError as http_err:
         print(f"SCRAPING_ERROR: Erro HTTP {http_err.response.status_code} ao raspar {url}")
         # Pode retornar erro específico ou genérico
         if http_err.response.status_code == 404:
              return {"error": f"Usuário ou canal não encontrado em Grynsoft ({usuario_original})."}
         else:
              return {"error": f"Erro HTTP ({http_err.response.status_code}) ao buscar dados de {usuario_original}."}
    except requests.exceptions.RequestException as req_err:
        print(f"SCRAPING_ERROR: Erro de rede ao raspar {url}: {req_err}")
        return {"error": f"Erro de rede ao buscar dados de {usuario_original}."}
    except Exception as e:
        print(f"SCRAPING_ERROR: Erro inesperado ao raspar {url}")
        traceback.print_exc() # Imprime o stack trace completo para debug no servidor
        return {"error": "Erro inesperado no scraping. Verifique os logs do servidor."}