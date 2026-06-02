#!/usr/bin/env python3
"""
enrich_bomvalor.py
Enriquece o CSV do bomvalor com:
  - valor_avaliacao   (leilao.vl_avaliacaoempresa)
  - desconto_percentual (recalculado)
  - numero_processo   (judicial.nu_processo)
  - comitente         (judicial.nm_comitente ou rede.nm_redealias)
  - vara              (judicial.nm_vara)
  - comarca           (judicial.nm_municipio)
  - data_primeiro_leilao / data_segundo_leilao (pracas[0/1].dt)
  - descricao         (lote.nm_descricao, HTML stripped)

Lê o CSV mais recente em ./csv/bomvalor_*.csv (ou o passado por --input),
processa em paralelo com N workers, salva em ./csv/bomvalor_enriched_*.csv.

Uso:
  python enrich_bomvalor.py                         # lê CSV mais recente
  python enrich_bomvalor.py --input csv/foo.csv     # CSV específico
  python enrich_bomvalor.py --workers 8 --delay 0.4 # ajusta velocidade
"""

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import urllib3
import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────

DEFAULT_WORKERS = 6
DEFAULT_DELAY   = 0.5   # segundos entre requisições por worker

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def strip_html(html_text):
    """Remove tags HTML e retorna texto puro."""
    if not html_text:
        return ""
    soup = BeautifulSoup(str(html_text), "html.parser")
    return clean(soup.get_text(" "))


def price_to_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        s = re.sub(r"[^\d,]", "", str(val))
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return float(s) if s else None
        except ValueError:
            return None


def desconto(vm, va):
    if vm and va and va > 0:
        return round((1 - vm / va) * 100, 2)
    return ""


def extract_lote_json(html_text):
    """Extrai o objeto JSON do lote embutido na página via sharedData."""
    m = re.search(r"lote:\s*(\{.*?\}),\s*\n", html_text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ──────────────────────────────────────────────────────────────
# Fetch + parse de uma URL de detalhe
# ──────────────────────────────────────────────────────────────

def fetch_detail(session, url, delay):
    """Busca a página de detalhe e retorna dict com campos enriquecidos."""
    time.sleep(delay)
    try:
        r = session.get(url, headers=HEADERS, timeout=25, verify=False)
        if r.status_code != 200:
            return {"_status": r.status_code}
    except Exception as e:
        return {"_status": f"ERR:{e}"}

    lote = extract_lote_json(r.text)
    if not lote:
        return {"_status": "no_json"}

    result = {"_status": "ok"}

    # ── Valor avaliação ──────────────────────────────────────────
    leilao = lote.get("leilao") or {}
    va_raw = leilao.get("vl_avaliacaoempresa")
    result["valor_avaliacao"] = price_to_float(va_raw) or ""

    # ── Número do processo ──────────────────────────────────────
    jud = lote.get("judicial") or {}
    result["numero_processo"] = (jud.get("nu_processo") or "").strip()

    # ── Comarca ────────────────────────────────────────────────
    result["comarca"] = (jud.get("nm_municipio") or "").strip()

    # ── Vara ──────────────────────────────────────────────────
    result["vara"] = (jud.get("nm_vara") or "").strip()

    # ── Comitente ─────────────────────────────────────────────
    # Prioridade: judicial.nm_comitente > rede.nm_redealias > leilao.nm_rede
    comitente = (jud.get("nm_comitente") or "").strip()
    if not comitente:
        rede = lote.get("rede") or {}
        comitente = (rede.get("nm_redealias") or "").strip()
    if not comitente:
        comitente = (leilao.get("nm_rede") or "").strip()
    result["comitente"] = comitente

    # ── Datas separadas por praça ───────────────────────────────
    pracas = lote.get("pracas") or []
    # Ordena por nu_praca
    pracas_sorted = sorted(pracas, key=lambda p: p.get("nu_praca", 99))
    if pracas_sorted:
        result["data_primeiro_leilao"] = (pracas_sorted[0].get("dt") or "").strip()
    if len(pracas_sorted) >= 2:
        result["data_segundo_leilao"] = (pracas_sorted[1].get("dt") or "").strip()

    # ── Valor mínimo da 1ª praça (pode ser mais preciso que o da listagem) ──
    if pracas_sorted:
        vl = pracas_sorted[0].get("vl_lanceinicial")
        vm = price_to_float(vl)
        if vm:
            result["valor_minimo"] = vm

    # ── Descrição (HTML stripped) ───────────────────────────────
    desc_html = lote.get("nm_descricao") or ""
    result["descricao"] = strip_html(desc_html)[:800]

    # ── Recalcula desconto ──────────────────────────────────────
    vm = result.get("valor_minimo") or price_to_float(lote.get("vl_lanceinicial"))
    va = result.get("valor_avaliacao")
    if vm and va:
        result["desconto_percentual"] = desconto(float(vm), float(va))

    return result


# ──────────────────────────────────────────────────────────────
# Pipeline principal
# ──────────────────────────────────────────────────────────────

def enrich(input_csv, n_workers, delay):
    # Ler CSV de entrada
    with open(input_csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    print(f"Lendo {len(rows)} linhas de {input_csv}")

    # Garantir que as colunas de enriquecimento existem
    extra_cols = [
        "valor_avaliacao", "desconto_percentual", "numero_processo",
        "comitente", "vara", "comarca",
        "data_primeiro_leilao", "data_segundo_leilao", "descricao",
    ]
    for col in extra_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    # Output CSV
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = Path("csv") / f"bomvalor_enriched_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Índice por URL para lookup rápido
    url_to_row = {r["url_original"]: r for r in rows if r.get("url_original")}
    urls = list(url_to_row.keys())
    total = len(urls)
    print(f"{total} URLs únicas para enriquecer com {n_workers} workers, delay={delay}s/worker")
    print(f"Estimativa: ~{total * delay / n_workers / 60:.0f} minutos\n")

    done = 0
    errors = 0

    # Thread-local sessions
    import threading
    _session_local = threading.local()

    def get_session():
        if not hasattr(_session_local, "session"):
            _session_local.session = requests.Session()
            _session_local.session.headers.update(HEADERS)
        return _session_local.session

    def process_url(url):
        session = get_session()
        return url, fetch_detail(session, url, delay)

    # Processar em paralelo
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(process_url, url): url for url in urls}

        for future in as_completed(futures):
            url, enriched = future.result()
            row = url_to_row[url]

            status = enriched.pop("_status", "?")
            if status == "ok":
                # Só sobrescreve campos vazios (não desfaz o que já tem)
                for k, v in enriched.items():
                    if v != "" and v is not None:
                        row[k] = v
            else:
                errors += 1

            done += 1
            if done % 100 == 0 or done == total:
                pct = done * 100 // total
                print(f"  {done}/{total} ({pct}%) | erros={errors}", flush=True)

    # Salvar CSV enriquecido
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✓ Salvo: {out_path}")
    print(f"  Total: {len(rows)} linhas | Erros de fetch: {errors}")

    # Estatísticas de preenchimento
    for col in ["valor_avaliacao", "numero_processo", "comitente", "data_primeiro_leilao"]:
        n = sum(1 for r in rows if r.get(col) and str(r[col]).strip())
        print(f"  {col}: {n}/{len(rows)} preenchidos ({n*100//len(rows)}%)")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",   type=str, help="CSV de entrada (default: mais recente em ./csv/)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--delay",   type=float, default=DEFAULT_DELAY)
    args = parser.parse_args()

    if args.input:
        input_csv = Path(args.input)
    else:
        csvs = sorted(Path("csv").glob("bomvalor_2*.csv"), reverse=True)
        # Pula arquivos enriched
        csvs = [p for p in csvs if "enriched" not in p.name]
        if not csvs:
            print("Nenhum CSV bomvalor encontrado em ./csv/")
            sys.exit(1)
        input_csv = csvs[0]
        print(f"CSV mais recente: {input_csv}")

    enrich(input_csv, args.workers, args.delay)


if __name__ == "__main__":
    main()
