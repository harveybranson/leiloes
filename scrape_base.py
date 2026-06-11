# -*- coding: utf-8 -*-
"""
scrape_base.py — scraping dirigido pela BASE de leiloeiros do banco (Postgres).
================================================================================

Em vez de derivar o site do dominio de e-mail (como o scraper_leiloeiros_direto
faz por padrao), este driver consome a base exportada do /admin
(base_leiloeiros_completa.csv) e usa a coluna `site` REAL como fonte primaria
(Parte I do master.md: "fonte mais limpa primeiro"), caindo para o dominio de
e-mail so quando nao ha site cadastrado.

Reaproveita TODA a escada de extracao (Tier 1-4: JSON-LD, __NEXT_DATA__, JSON
embutido, API hints, HTML cards) e o viewer HTML do scraper_leiloeiros_direto.py.

NAO escreve no banco. Saidas temporarias:
  - csv/imoveis_base_<data>.csv
  - viewer_base_leiloeiros.html
  - scrape_base_progress.json

Uso:
  python scrape_base.py [caminho_base.csv]
"""
import csv
import sys
from pathlib import Path

import scraper_leiloeiros_direto as S

BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
DEFAULT_BASE = Path(
    r"c:\Users\arthur\Downloads\Cursor\leiloes\leiloes_ferramentas\csv\base_leiloeiros_completa.csv"
)

# Situacoes que NAO devem ser raspadas (registro inativo no orgao).
SITUACAO_EXCLUIR = {"cancelado", "suspenso"}


def carregar_base(caminho: Path) -> list[dict]:
    """Le a base do /admin e monta a lista de trabalho, priorizando a coluna
    `site` real e deduplicando por dominio. Fallback: dominio do e-mail."""
    rows = []
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(caminho, encoding=enc) as f:
                rows = list(csv.DictReader(f))
            if rows:
                break
        except Exception:
            continue
    if not rows:
        print(f"!! base vazia/invalida: {caminho}")
        sys.exit(1)

    resultado: dict[str, dict] = {}
    total = com_site = via_email = excluidos = 0

    for r in rows:
        total += 1
        sit = (r.get("situacao") or "").strip().lower()
        if sit in SITUACAO_EXCLUIR:
            excluidos += 1
            continue
        if (r.get("ativo") or "").strip().lower() in ("false", "0", "f"):
            excluidos += 1
            continue

        nome = (r.get("nome") or "").strip()
        junta = (r.get("junta_comercial") or r.get("junta") or "").strip()
        site = (r.get("site") or "").strip()
        email = (r.get("email") or "").strip()

        if site:
            url = S.normalize_url(site)
            dominio = (
                url.replace("https://", "").replace("http://", "").replace("www.", "")
            ).split("/")[0]
            origem = "site"
            com_site += 1
        else:
            dom = S.extract_email_domain(email)
            if not dom:
                continue
            url = S.normalize_url(dom)
            dominio = dom.replace("www.", "")
            origem = "email"
            via_email += 1

        chave = dominio.lower()
        if chave not in resultado:
            resultado[chave] = {
                "nome": nome,
                "junta": junta,
                "site": url,
                "domain": chave,
                "origem": origem,
            }

    itens = list(resultado.values())
    print(
        f"Base: {total} leiloeiros | excluidos(cancel/susp/inativo): {excluidos} | "
        f"com site real: {com_site} | via e-mail: {via_email} | "
        f"dominios unicos a raspar: {len(itens)}"
    )
    return itens


def main():
    caminho = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BASE

    # Redireciona as saidas do modulo reaproveitado para arquivos "_base".
    S.OUTPUT_CSV = S.CSV_DIR / f"imoveis_base_{S.date.today()}.csv"
    S.OUTPUT_HTML = BASE_DIR / "viewer_base_leiloeiros.html"
    S.PROGRESS_FILE = BASE_DIR / "scrape_base_progress.json"

    # Troca o carregador: passa a usar a base do /admin (coluna `site` real).
    S.load_leiloeiros = lambda: carregar_base(caminho)

    print("=" * 60)
    print("  SCRAPING DIRIGIDO PELA BASE DO /admin (coluna `site` real)")
    print(f"  Base: {caminho}")
    print(f"  Saida CSV:  {S.OUTPUT_CSV}")
    print(f"  Saida HTML: {S.OUTPUT_HTML}")
    print("=" * 60 + "\n")

    S.main()


if __name__ == "__main__":
    main()
