# -*- coding: utf-8 -*-
"""
exportar_para_jsonl.py — exporta os imoveis do SQLite consolidado
(imoveis_leiloeiros.db) para um JSONL no formato que o importador do site
espera (pipeline/importar_scraping.py -> run.py importar-scraping).

Mapeia as colunas do SQLite para as chaves que o site le:
  titulo, url, tipo_imovel, fotos[], estado, descricao_completa,
  preco, cidade, endereco_completo, nome_anunciante

Gera:  scraping_export/imoveis_consolidado.jsonl

Uso:
  python exportar_para_jsonl.py
  python exportar_para_jsonl.py --novos-desde 2026-06-10   # so importados nessa data+
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
DB_SQLITE = BASE / "imoveis_leiloeiros.db"
OUT_DIR = BASE / "scraping_export"
OUT_FILE = OUT_DIR / "imoveis_consolidado.jsonl"

# tipo do SQLite -> string que o _TIPO_MAP do importador entende
_TIPO_SQLITE = {
    "IMOVEL": "",          # generico -> OUTRO no site
    "APARTAMENTO": "apartamento",
    "CASA": "casa",
    "TERRENO": "terreno",
    "COMERCIAL": "comercial",
    "RURAL": "rural",
    "GALPAO": "galpao",
    "SALA": "sala",
    "VAGA": "vaga",
}


def _registro(row):
    """Converte uma linha do SQLite no dict JSONL do site."""
    d = dict(row)
    url = (d.get("url") or "").strip()
    if not url.startswith("http"):
        return None
    titulo = (d.get("titulo") or "").strip()
    if not titulo:
        return None

    fotos = [d["imagem"]] if d.get("imagem") and str(d["imagem"]).startswith("http") else []
    tipo = _TIPO_SQLITE.get((d.get("tipo") or "").upper().strip(), "")

    return {
        "titulo": titulo,
        "url": url,
        "source_url": d.get("site") or url,
        "tipo_imovel": tipo,
        "fotos": fotos,
        "estado": (d.get("uf") or "").strip().upper() or None,
        "cidade": (d.get("cidade") or "").strip() or None,
        "endereco_completo": (d.get("endereco") or "").strip() or None,
        "descricao_completa": (d.get("descricao") or "").strip() or None,
        "preco": d.get("lance_inicial"),
        "avaliacao": d.get("avaliacao"),
        "nome_anunciante": (d.get("leiloeiro") or "").strip() or None,
    }


def exportar(novos_desde: str | None = None):
    """Gera o JSONL. Retorna (caminho, n_registros)."""
    OUT_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(str(DB_SQLITE))
    con.row_factory = sqlite3.Row
    sql = "SELECT * FROM imoveis"
    params = ()
    if novos_desde:
        sql += " WHERE importado_em >= ?"
        params = (novos_desde,)

    n = 0
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for row in con.execute(sql, params):
            rec = _registro(row)
            if rec is None:
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    con.close()
    return OUT_FILE, n


def main():
    ap = argparse.ArgumentParser(description="Exporta SQLite -> JSONL para o site importar")
    ap.add_argument("--novos-desde", help="filtra importado_em >= YYYY-MM-DD")
    args = ap.parse_args()
    caminho, n = exportar(args.novos_desde)
    print(f"OK: {n} registros -> {caminho}")


if __name__ == "__main__":
    main()
