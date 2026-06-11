# -*- coding: utf-8 -*-
"""
aplicar_revisao.py — aplica decisões de triagem de UF feitas no uf_revisao.csv.

Referência: captura_dados_leiloes_master.md (Parte XI). Fecha o ciclo da auditoria:
o `enrich_local.py --auditar --csv` exporta divergências com uma coluna `decisao` em
branco; você preenche e este script aplica no banco. Valores aceitos em `decisao`:

  (vazio) / manter / ok   → mantém a UF atual (não muda)
  aplicar / sim / x       → grava a `uf_inferida` da linha
  <UF> (ex.: RJ, SP)      → grava essa UF explícita (validada contra o IBGE)

Uso:
  python aplicar_revisao.py --csv csv/uf_revisao.csv --dry-run
  python aplicar_revisao.py --csv csv/uf_revisao.csv
"""
import argparse
import csv
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper_commons as sc

DB_PADRAO = "imoveis_leiloeiros.db"
MANTER = {"", "manter", "ok", "nao", "não", "-"}
APLICAR = {"aplicar", "sim", "x", "s", "yes", "y"}


def aplicar(db_path, csv_path, dry_run=False):
    _byname, ufs, _canon = sc.carregar_municipios()
    with open(csv_path, encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    updates, invalidas, mantidas = [], [], 0
    for r in linhas:
        dec = (r.get("decisao") or "").strip().lower()
        rid = r.get("id")
        if not rid or dec in MANTER:
            mantidas += 1
            continue
        if dec in APLICAR:
            nova = (r.get("uf_inferida") or "").strip().upper()
        else:
            nova = dec.upper()
        if nova in ufs:
            updates.append((nova, rid))
        else:
            invalidas.append((rid, r.get("decisao")))

    print(f"  linhas: {len(linhas)} | a aplicar: {len(updates)} | "
          f"mantidas: {mantidas} | decisões inválidas: {len(invalidas)}")
    for rid, dec in invalidas[:10]:
        print(f"    ✗ decisão inválida {dec!r} (id={rid})")

    if dry_run:
        print("  [dry-run] nada gravado.")
        return updates
    if updates:
        con = sqlite3.connect(db_path)
        con.executemany("UPDATE imoveis SET uf = ? WHERE id = ?", updates)
        con.commit()
        con.close()
        print(f"  ✔ {len(updates)} UFs atualizadas conforme a triagem.")
    else:
        print("  nada a aplicar (preencha a coluna 'decisao' no CSV).")
    return updates


def main():
    ap = argparse.ArgumentParser(description="Aplica decisões de triagem de UF.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--csv", default="csv/uf_revisao.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("APLICAR REVISÃO DE UF")
    aplicar(args.db, args.csv, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
