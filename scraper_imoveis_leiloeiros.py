#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper de imóveis — coleta de 12 leiloeiros
Valida datas e gera reports a cada 5 minutos
"""

import csv
import json
import time
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import urllib3
from playwright.sync_api import sync_playwright

urllib3.disable_warnings()

# Config
CSV_LEILOEIROS = Path("C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes\\csv\\leiloeiros_juceac_2026-06-08.csv")
OUTPUT_DIR = Path("C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes\\csv")
PROGRESS_FILE = Path("C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes\\scraper_imoveis_progress.json")

CAPTURE_DATE = datetime.now()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

progress = {
    "iniciado": CAPTURE_DATE.isoformat(),
    "leiloeiros_processados": 0,
    "imoveis_total": 0,
    "imoveis_por_leiloeiro": {},
    "erros": [],
    "last_report": CAPTURE_DATE.isoformat()
}

def save_progress():
    """Salva progresso."""
    progress["last_report"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)

def extrair_data(texto):
    """Extrai primeira data encontrada em formato DD/MM/YYYY."""
    if not texto:
        return None
    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", texto)
    if match:
        dia, mes, ano = match.groups()
        try:
            return datetime(int(ano), int(mes), int(dia))
        except ValueError:
            return None
    return None

def scrape_site_simples(url):
    """Scrape básico com requests."""
    try:
        r = requests.get(url, headers=HEADERS, verify=False, timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')

        imoveis = []

        # Procura padrões comuns
        for elem in soup.find_all(['div', 'tr', 'article', 'li']):
            texto = elem.get_text(separator=' ')

            # Procura por títulos de imóveis (contém endereço ou tipo)
            if any(palavra in texto.lower() for palavra in
                   ['apto', 'casa', 'terreno', 'sala', 'galpão', 'rua ', 'av. ', 'avenida']):

                # Procura data
                data = extrair_data(texto)
                if data and data > CAPTURE_DATE:
                    # Procura preço
                    preco = ''
                    match_preco = re.search(r'R\$\s*([\d.,]+)', texto)
                    if match_preco:
                        preco = match_preco.group(1)

                    # Extrai título (primeiros 100 chars)
                    titulo = texto[:100].strip()

                    imoveis.append({
                        'titulo': titulo,
                        'preco': preco or 'Não informado',
                        'data_leilao': data.strftime('%d/%m/%Y'),
                        'url': url
                    })

        return imoveis
    except Exception as e:
        return []

def scrape_site_playwright(url):
    """Scrape com Playwright para sites com JS."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            try:
                page.goto(url, wait_until='networkidle', timeout=20000)
                time.sleep(2)

                html = page.content()
                soup = BeautifulSoup(html, 'html.parser')

                imoveis = []

                # Procura títulos de imóveis
                for titulo_elem in soup.find_all(['h1', 'h2', 'h3', 'h4', '.titulo', '[class*="titulo"]']):
                    titulo = titulo_elem.get_text(strip=True)

                    if any(palavra in titulo.lower() for palavra in
                           ['apto', 'casa', 'terreno', 'sala', 'galpão', 'imovel', 'lote']):

                        # Procura data e preço no contexto
                        container = titulo_elem.parent
                        if container:
                            contexto = container.get_text(separator=' ')

                            data = extrair_data(contexto)
                            if data and data > CAPTURE_DATE:
                                preco = ''
                                match_preco = re.search(r'R\$\s*([\d.,]+)', contexto)
                                if match_preco:
                                    preco = match_preco.group(1)

                                imoveis.append({
                                    'titulo': titulo,
                                    'preco': preco or 'Não informado',
                                    'data_leilao': data.strftime('%d/%m/%Y'),
                                    'url': url
                                })

                browser.close()
                return imoveis
            except:
                browser.close()
                return []
    except:
        return []

def scrape_leiloeiro(nome, url):
    """Coleta imóveis de um leiloeiro."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Buscando imóveis em: {url}")

    imoveis = []

    # Tenta simples primeiro (mais rápido)
    imoveis = scrape_site_simples(url)

    # Se não achou, tenta Playwright
    if not imoveis:
        print(f"  → Tentando com Playwright...")
        imoveis = scrape_site_playwright(url)

    if imoveis:
        print(f"  [OK] Encontrados {len(imoveis)} imoveis com leilao futuro")
    else:
        print(f"  [ZERO] Nenhum imovel com leilao futuro")

    return imoveis

def main():
    print("=" * 70)
    print(f"SCRAPER DE IMÓVEIS — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 70)

    # Carrega leiloeiros
    leiloeiros = []
    with open(CSV_LEILOEIROS, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        leiloeiros = list(reader)

    print(f"\nProcessando {len(leiloeiros)} leiloeiros...\n")

    all_imoveis = []
    last_report = time.time()

    for i, lei in enumerate(leiloeiros, 1):
        nome = lei.get('nome', '')
        url = lei.get('site', '')

        if not url:
            progress['erros'].append(f"{nome}: sem URL")
            continue

        # Normaliza URL
        if not url.startswith('http'):
            url = 'https://' + url

        try:
            imoveis = scrape_leiloeiro(nome, url)
            for im in imoveis:
                im['leiloeiro'] = nome

            all_imoveis.extend(imoveis)
            progress['imoveis_por_leiloeiro'][nome] = len(imoveis)
            progress['imoveis_total'] = len(all_imoveis)
            progress['leiloeiros_processados'] = i

        except Exception as e:
            progress['erros'].append(f"{nome}: {str(e)[:50]}")

        time.sleep(1)  # Rate limit

        # Report a cada 5 min
        if time.time() - last_report > 300:
            print(f"\n[REPORT {(time.time() - last_report)/60:.0f} min]")
            print(f"  Leiloeiros: {i}/{len(leiloeiros)}")
            print(f"  Imóveis: {len(all_imoveis)}")
            save_progress()
            last_report = time.time()

    # Salva CSV de imóveis
    if all_imoveis:
        csv_file = OUTPUT_DIR / f"imoveis_leiloeiros_{CAPTURE_DATE.strftime('%Y-%m-%d')}.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['leiloeiro', 'titulo', 'preco', 'data_leilao', 'url'])
            writer.writeheader()
            writer.writerows(all_imoveis)
        print(f"\n[OK] CSV de imóveis salvo: {csv_file}")

    # Relatório final
    print("\n" + "=" * 70)
    print("RELATÓRIO FINAL")
    print("=" * 70)
    print(f"Leiloeiros processados: {progress['leiloeiros_processados']}/{len(leiloeiros)}")
    print(f"Imóveis com leilão futuro: {len(all_imoveis)}")

    if progress['imoveis_por_leiloeiro']:
        print(f"\nImóveis por leiloeiro:")
        for lei, qtd in sorted(progress['imoveis_por_leiloeiro'].items()):
            if qtd > 0:
                print(f"  • {lei}: {qtd}")

    if progress['erros']:
        print(f"\nErros ({len(progress['erros'])}):")
        for erro in progress['erros'][:5]:
            print(f"  ! {erro}")

    progress['status'] = 'concluido'
    progress['tempo_total_min'] = (time.time() - last_report) / 60
    save_progress()

    print(f"\nArquivos:")
    print(f"  Progresso: {PROGRESS_FILE}")

if __name__ == "__main__":
    main()
