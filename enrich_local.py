# -*- coding: utf-8 -*-
"""
enrich_local.py — enriquecimento OFFLINE (sem rede) a partir do texto já no banco.

Referência: captura_dados_leiloes_master.md (Parte VII normalização + Parte X DoD). Ataca
as reprovações do gate de cobertura usando o que já está armazenado:

  - uf: deduz a sigla de titulo/endereco/descricao/cidade via scraper_commons.inferir_uf
        (padrão Cidade/UF + nome de município único no IBGE). Nunca grava UF inválida.
  - lance_inicial: parseia "R$ ..." de titulo/descricao quando o campo está nulo e o valor
        existe no texto (recuperação parcial; o grosso vem do re-scrape de detalhe).

É idempotente: só preenche campos vazios. Mostra cobertura antes/depois.

Uso:
  python enrich_local.py                 # aplica no banco padrão
  python enrich_local.py --dry-run       # só simula e relata, não grava
  python enrich_local.py --db outro.db
"""
import argparse
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper_commons as sc

DB_PADRAO = "imoveis_leiloeiros.db"
_PRICE_RE = re.compile(r"R\$\s*([\d.]{1,3}(?:\.\d{3})*,\d{2})")


def _parse_brl(txt):
    m = _PRICE_RE.search(txt or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _vazio(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def _pct(con, expr):
    tot = con.execute("SELECT COUNT(*) FROM imoveis").fetchone()[0]
    ok = con.execute(f"SELECT COUNT(*) FROM imoveis WHERE {expr}").fetchone()[0]
    return 100.0 * ok / tot if tot else 0.0


def enriquecer(db_path, dry_run=False):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    uf_antes = _pct(con, "uf IS NOT NULL AND TRIM(uf) <> ''")
    lance_antes = _pct(con, "lance_inicial IS NOT NULL")

    upd_uf, upd_lance = [], []
    for r in con.execute("SELECT id, titulo, descricao, endereco, cidade, uf, lance_inicial "
                          "FROM imoveis"):
        if _vazio(r["uf"]):
            uf = sc.inferir_uf(r["titulo"], r["endereco"], r["descricao"], r["cidade"])
            if uf:
                upd_uf.append((uf, r["id"]))
        if r["lance_inicial"] is None:
            val = _parse_brl(r["titulo"]) or _parse_brl(r["descricao"])
            if val and val > 500:
                upd_lance.append((val, r["id"]))

    print(f"  uf:            {uf_antes:5.1f}%  ->  +{len(upd_uf)} preenchidos")
    print(f"  lance_inicial: {lance_antes:5.1f}%  ->  +{len(upd_lance)} preenchidos "
          f"(texto; resto exige re-scrape de detalhe)")

    if dry_run:
        print("  [dry-run] nada gravado.")
        con.close()
        return

    con.executemany("UPDATE imoveis SET uf = ? WHERE id = ?", upd_uf)
    con.executemany("UPDATE imoveis SET lance_inicial = ? WHERE id = ?", upd_lance)
    con.commit()

    uf_dep = _pct(con, "uf IS NOT NULL AND TRIM(uf) <> ''")
    lance_dep = _pct(con, "lance_inicial IS NOT NULL")
    print(f"  → uf:            {uf_antes:5.1f}% → {uf_dep:5.1f}%")
    print(f"  → lance_inicial: {lance_antes:5.1f}% → {lance_dep:5.1f}%")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="Enriquecimento offline de uf/lance_inicial.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("ENRICH LOCAL (texto já no banco)")
    enriquecer(args.db, args.dry_run)


if __name__ == "__main__":
    main()
