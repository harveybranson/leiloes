#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper de leiloeiros e imóveis — JUCEAC (Junta Comercial do Acre)
Conforme guia captura_dados_leiloes_v2.md
"""

import requests
import json
import time
import re
import csv
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from typing import dict, list

# Config
BASE_URL = "https://juceac.ac.gov.br"
LEILOEIROS_URL = "https://juceac.ac.gov.br/leiloeiro/"
OUTPUT_DIR = Path("C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes\\csv")
PROGRESS_FILE = Path("C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes\\scraper_juceac_progress.json")
CSV_OUTPUT = OUTPUT_DIR / f"leiloeiros_juceac_{datetime.now().strftime('%Y-%m-%d')}.csv"

CAPTURE_DATE = datetime.now()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Armazenar progresso
progress = {
    "started": CAPTURE_DATE.isoformat(),
    "leiloeiros": [],
    "imoveis": [],
    "last_update": CAPTURE_DATE.isoformat(),
    "status": "iniciando"
}

def save_progress():
    """Salva progresso em JSON."""
    progress["last_update"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)

def scrape_leiloeiros():
    """Extrai leiloeiros regular do site JUCEAC."""
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Acessando {LEILOEIROS_URL}")
        r = requests.get(LEILOEIROS_URL, headers=HEADERS, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # Procura por tabelas ou listas de leiloeiros
        # Formato pode variar — vamos procurar padrões comuns
        leiloeiros = []

        # Padrão 1: tabela com linhas de leiloeiros
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            # Procura por padrão: nome, registro, situação
            row_text = " ".join(c.get_text(strip=True) for c in cells)

            # Verifica se é cancelado/suspenso
            if any(palavra in row_text.upper() for palavra in ["CANCELADO", "SUSPENSO", "INATIVO"]):
                continue

            # Tenta extrair nome e link
            link_elem = row.find("a")
            if link_elem:
                nome = link_elem.get_text(strip=True)
                url = link_elem.get("href", "")
                url = urljoin(BASE_URL, url)

                if nome and url:
                    leiloeiros.append({
                        "nome": nome,
                        "url": url,
                        "situacao": "Regular",
                        "data_captura": CAPTURE_DATE.isoformat()
                    })

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Encontrados {len(leiloeiros)} leiloeiros")
        return leiloeiros

    except Exception as e:
        print(f"[ERRO] ao buscar leiloeiros: {e}")
        return []

def scrape_imoveis_leiloeiro(nome: str, url: str) -> list:
    """Scrape imóveis de um leiloeiro específico."""
    imoveis = []
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Buscando imóveis de {nome}")
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # Procura por cards/listagens de imóveis
        # Padrão: divs com classe contendo "imovel", "lote", "produto"
        for elem in soup.find_all(["div", "article", "li"], class_=re.compile(r"imovel|lote|produto|item", re.I)):
            titulo = elem.find(["h1", "h2", "h3", "h4", ".titulo"])
            if titulo:
                titulo_text = titulo.get_text(strip=True)

                # Procura por preço
                preco = None
                for preco_elem in elem.find_all(re.compile(r"span|div|p")):
                    text = preco_elem.get_text(strip=True)
                    if "R$" in text or "valores" in text.lower():
                        preco = text
                        break

                # Procura por data
                data = None
                for date_elem in elem.find_all(re.compile(r"span|div|p")):
                    text = date_elem.get_text(strip=True)
                    # Tenta casar datas em formato DD/MM/YYYY
                    if re.search(r"\d{1,2}/\d{1,2}/\d{4}", text):
                        data = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text).group(0)
                        break

                if titulo_text and data:
                    try:
                        data_leilao = datetime.strptime(data, "%d/%m/%Y")
                        if data_leilao > CAPTURE_DATE:
                            imovel = {
                                "leiloeiro": nome,
                                "titulo": titulo_text,
                                "preco": preco or "Não informado",
                                "data_primeiro_leilao": data,
                                "url_imovel": elem.find("a").get("href", "") if elem.find("a") else url,
                                "data_captura": CAPTURE_DATE.isoformat()
                            }
                            imoveis.append(imovel)
                    except ValueError:
                        pass

        print(f"[{datetime.now().strftime('%H:%M:%S')}]   → {len(imoveis)} imóveis com leilão futuro")
        return imoveis

    except Exception as e:
        print(f"[ERRO] ao buscar imóveis de {nome}: {e}")
        return []

def save_csv(leiloeiros: list, imoveis: list):
    """Salva leiloeiros e imóveis em CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # CSV de leiloeiros
    csv_leiloes = OUTPUT_DIR / f"leiloeiros_juceac_{datetime.now().strftime('%Y-%m-%d')}.csv"
    with open(csv_leiloes, "w", newline="", encoding="utf-8") as f:
        if leiloeiros:
            writer = csv.DictWriter(f, fieldnames=["nome", "url", "situacao"])
            writer.writeheader()
            writer.writerows(leiloeiros)
    print(f"[OK] CSV de leiloeiros salvo: {csv_leiloes}")

    # CSV de imóveis
    csv_imoveis = OUTPUT_DIR / f"imoveis_juceac_{datetime.now().strftime('%Y-%m-%d')}.csv"
    with open(csv_imoveis, "w", newline="", encoding="utf-8") as f:
        if imoveis:
            writer = csv.DictWriter(f, fieldnames=imoveis[0].keys())
            writer.writeheader()
            writer.writerows(imoveis)
    print(f"[OK] CSV de imóveis salvo: {csv_imoveis}")

    return csv_leiloes, csv_imoveis

def main():
    """Executa o scraper com reports a cada 5 minutos."""
    global progress

    print("=" * 60)
    print(f"SCRAPER JUCEAC — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 60)

    # 1. Extrair leiloeiros
    print("\n[FASE 1] Extraindo leiloeiros regular...")
    progress["status"] = "extraindo_leiloeiros"
    leiloeiros = scrape_leiloeiros()
    progress["leiloeiros"] = leiloeiros
    save_progress()

    # 2. Para cada leiloeiro, extrair imóveis
    print("\n[FASE 2] Extraindo imóveis...")
    progress["status"] = "extraindo_imoveis"

    all_imoveis = []
    last_report = time.time()

    for i, lei in enumerate(leiloeiros, 1):
        imoveis = scrape_imoveis_leiloeiro(lei["nome"], lei["url"])
        all_imoveis.extend(imoveis)
        progress["imoveis"] = all_imoveis

        # Report a cada 5 min
        if time.time() - last_report > 300:
            print(f"\n[REPORT {(time.time() - last_report)/60:.1f} min]")
            print(f"   Leiloeiros processados: {i}/{len(leiloeiros)}")
            print(f"   Total de imóveis: {len(all_imoveis)}")
            save_progress()
            last_report = time.time()

        time.sleep(1)  # Rate limit

    # 3. Salvar CSV
    print("\n[FASE 3] Salvando CSVs...")
    csv_lei, csv_imo = save_csv(leiloeiros, all_imoveis)

    # 4. Relatório final
    print("\n" + "=" * 60)
    print("RELATÓRIO FINAL")
    print("=" * 60)
    print(f"Leiloeiros extraídos: {len(leiloeiros)}")
    print(f"Imóveis com leilão futuro: {len(all_imoveis)}")
    print(f"Tempo total: {(time.time() - last_report)/60:.1f} min")
    print(f"\nArquivos:")
    print(f"  Leiloeiros: {csv_lei}")
    print(f"  Imóveis: {csv_imo}")
    print(f"  Progresso: {PROGRESS_FILE}")

    progress["status"] = "concluido"
    progress["total_tempo_min"] = (time.time() - last_report) / 60
    save_progress()

if __name__ == "__main__":
    main()
