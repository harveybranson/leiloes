# -*- coding: utf-8 -*-
"""Converte o CSV cru do scrape_base.py para o formato esperado por importar_site.py
(XII.5 do master): lance_inicial(float) -> preco(BR), data_leilao(ISO) -> dd/mm/aaaa."""
import csv, re, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = src.with_name(src.stem + "_import.csv")


def preco_br(v):
    v = (v or "").strip()
    if not v:
        return ""
    try:
        f = float(v)
    except ValueError:
        return ""
    if f <= 0:
        return ""
    return f"{f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def data_br(v):
    v = (v or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/{y}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    return v if m else ""


with open(src, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))

out_cols = ["leiloeiro", "junta", "site", "titulo", "descricao",
            "cidade", "uf", "preco", "data_leilao", "imagem", "url"]
n_preco = n_data = 0
with open(dst, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=out_cols)
    w.writeheader()
    for r in rows:
        p = preco_br(r.get("lance_inicial"))
        d = data_br(r.get("data_leilao"))
        if p:
            n_preco += 1
        if d:
            n_data += 1
        w.writerow({
            "leiloeiro": r.get("leiloeiro", ""), "junta": r.get("junta", ""),
            "site": r.get("site", ""), "titulo": r.get("titulo", ""),
            "descricao": r.get("descricao", ""), "cidade": r.get("cidade", ""),
            "uf": r.get("uf", ""), "preco": p, "data_leilao": d,
            "imagem": r.get("imagem", ""), "url": r.get("url", ""),
        })

print(f"{len(rows)} linhas -> {dst.name} | com preco: {n_preco} | com data: {n_data}")
