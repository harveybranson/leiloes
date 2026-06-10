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
from typing import Dict, List

# Config
BASE_URL = "https://juceac.ac.gov.br"
LEILOEIROS_URL = "https://juceac.ac.gov.br/leiloeiro/"
OUTPUT_DIR = Path(__file__).resolve().parent / "csv"
PROGRESS_FILE = Path(__file__).resolve().parent / "scraper_juceac_progress.json"
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
    """Extrai leiloeiros regular do site JUCEAC usando Playwright."""
    from playwright.sync_api import sync_playwright

    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Renderizando {LEILOEIROS_URL} com Playwright...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=['--disable-blink-features=AutomationControlled'])
            page = browser.new_page()
            page.goto(LEILOEIROS_URL, wait_until="networkidle", timeout=30000)

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Extrai texto completo
            main = soup.find("main") or soup.find("body")
            if not main:
                return []

            texto = main.get_text()
            leiloeiros = []

            # Padrão: nome em MAIÚSCULA, seguido de situação (REGULAR, CANCELADO, SUSPENSO)
            # Separa por blocos de 'Matrícula n.'
            blocos = re.split(r"Matr[íi]cula\s+n[.º]+\s*", texto)

            for bloco in blocos[1:]:  # Skip primeiro (header)
                linhas = bloco.split("\n")
                if not linhas:
                    continue

                primeira_linha = linhas[0].strip()

                # Procura separadores (múltiplos espaços indicam colunas)
                partes = re.split(r"\s{2,}", primeira_linha)
                if len(partes) < 2:
                    continue

                nome = partes[0].strip()
                situacao_text = " ".join(partes[1:]).strip()

                # Filtra: só REGULAR (exclui CANCELADO, SUSPENSO, INATIVO)
                if any(word in situacao_text.upper() for word in
                       ["CANCELADO", "SUSPENSO", "INATIVO", "IMPEDIDO"]):
                    continue

                # Extrai registro (primeiros 3 dígitos)
                match_reg = re.search(r"\d{3}", primeira_linha)
                registro = match_reg.group(0) if match_reg else "000"

                if nome and len(nome) > 3:
                    # Tenta encontrar website do leiloeiro (primeira URL no bloco)
                    url = BASE_URL
                    for linha in linhas:
                        match_url = re.search(r"https?://[^\s]+", linha)
                        if match_url:
                            url = match_url.group(0)
                            break

                    leiloeiros.append({
                        "nome": nome,
                        "registro": registro,
                        "url": url,
                        "situacao": "Regular",
                        "data_captura": CAPTURE_DATE.isoformat()
                    })

            browser.close()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Encontrados {len(leiloeiros)} leiloeiros REGULARES")
            return leiloeiros

    except Exception as e:
        print(f"[ERRO] ao buscar leiloeiros: {e}")
        import traceback
        traceback.print_exc()
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
