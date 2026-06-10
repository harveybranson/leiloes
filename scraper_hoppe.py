# -*- coding: utf-8 -*-
"""
Adapter dedicado: HOPPE LEILOES (Alex Willian Hoppe - JUCAP 13/2021) — plataforma SUPERBID.
O scraper generico dava 0 porque os lotes Superbid vem de API tipada (offer-query.superbid.net),
nao do DOM. Aqui usamos a API diretamente (mais limpa e confiavel que raspar HTML).

Descoberta da loja: GET siteconfigprod.superbid.net/<host>/style.config.json -> storeId.
Listagem: GET offer-query.superbid.net/offers/ com filtro product.productType.id:13 (Imoveis).
1a praca:
  - judicialPraca == 2  -> 1a praca = stages[0].endDate (ja passou) -> normalmente excluida
  - praca 1 / Praca Unica -> 1a praca = offer.endDate
Mantem so 1a praca > hoje. Insere em imoveis_leiloeiros.db (dedup por URL) + CSV em /csv.
Reaproveitavel para qualquer site Superbid: troque HOST/STORE_ID.
"""
import csv, sys, sqlite3, hashlib, urllib3
from datetime import datetime
from pathlib import Path
import requests

urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
DB = BASE / "imoveis_leiloeiros.db"
HOST = "www.hoppeleiloes.com.br"
STORE_ID = "16194"
LEILOEIRO = "Alex Willian Hoppe"
JUNTA = "JUCAP/AP"
SITE = f"https://{HOST}"
OUT = BASE / "csv" / f"imoveis_hoppe_amapa_{datetime.now():%Y-%m-%d}.csv"
TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

API = "https://offer-query.superbid.net/offers/"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, application/hal+json",
     "Origin": SITE, "Referer": SITE + "/"}


def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def primeira_praca(o):
    auc = o.get("auction", {}) or {}
    ep = o.get("eventPipeline") or {}
    stages = ep.get("stages") or []
    if auc.get("judicialPraca") == 2 and stages:
        return parse_dt(stages[0].get("endDate"))      # 1a praca ja ocorreu
    return parse_dt(o.get("endDate")) or (parse_dt(stages[0].get("endDate")) if stages else None)


def fetch_imoveis():
    out = []
    page = 1
    while True:
        params = {"filter": f"product.productType.id:13;stores.id:{STORE_ID}",
                  "locale": "pt_BR", "orderBy": "endDate:asc",
                  "pageNumber": str(page), "pageSize": "50", "portalId": "[2,15]",
                  "preOrderBy": "orderByFirstOpenedOffers", "requestOrigin": "store",
                  "searchType": "opened", "timeZoneId": "America/Sao_Paulo"}
        r = requests.get(API, headers=H, params=params, timeout=40, verify=False)
        if r.status_code != 200:
            print(f"  API status {r.status_code} pg{page}: {r.text[:120]}", flush=True)
            break
        d = r.json()
        offs = d.get("offers") or []
        out.extend(offs)
        total = d.get("total") or 0
        if len(out) >= total or not offs:
            break
        page += 1
    return out, total


def main():
    print("=" * 70)
    print(f"ADAPTER HOPPE (Superbid store {STORE_ID}) | captura {datetime.now():%d/%m/%Y %H:%M}")
    print("=" * 70, flush=True)
    offers, total = fetch_imoveis()
    print(f"ofertas de imovel retornadas: {len(offers)} (total API={total})", flush=True)
    rows = []
    for o in offers:
        prod = o.get("product", {}) or {}
        primeira = primeira_praca(o)
        if not primeira:
            continue
        fut = primeira.date() > TODAY.date()   # 1a praca POSTERIOR ao dia da captura
        city = (prod.get("location", {}) or {}).get("city", "") or ""
        cidade, uf = (city.rsplit(" - ", 1) + [""])[:2] if " - " in city else (city, "")
        praca = (o.get("auction", {}) or {}).get("judicialPracaDescription", "")
        tag = "OK-FUTURO" if fut else "1a praca passou"
        print(f"  {primeira:%d/%m/%Y} [{tag}] {praca} | {cidade}/{uf} | {(prod.get('shortDesc') or '')[:50]}", flush=True)
        if not fut:
            continue
        rows.append({
            "leiloeiro": LEILOEIRO, "junta": JUNTA, "site": SITE,
            "titulo": (prod.get("shortDesc") or "")[:250],
            "cidade": cidade, "uf": uf,
            "preco": o.get("priceFormatted", ""), "lance": o.get("price"),
            "data_leilao": primeira.strftime("%d/%m/%Y"),
            "imagem": prod.get("thumbnailUrl", "") or "",
            "anexos": "",
            "url": f"{SITE}/oferta/{o.get('id')}",
        })

    print(f"\nIMOVEIS com 1a praca futura: {len(rows)}", flush=True)
    if not rows:
        return
    cols = ["leiloeiro", "junta", "site", "titulo", "cidade", "uf", "preco",
            "data_leilao", "imagem", "anexos", "url"]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {OUT}")

    conn = sqlite3.connect(DB); cur = conn.cursor()
    novos = existe = 0
    for r in rows:
        cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (r["url"],))
        if cur.fetchone():
            existe += 1
            continue
        rid = hashlib.md5(r["url"].encode("utf-8")).hexdigest()[:16]
        cur.execute("""INSERT INTO imoveis
            (id,leiloeiro,junta,site,titulo,descricao,endereco,cidade,uf,
             lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, r["leiloeiro"], r["junta"], r["site"], r["titulo"], "", "", r["cidade"],
             r["uf"], r["lance"], None, r["data_leilao"], r["url"], "imovel",
             r["imagem"], datetime.now().isoformat()))
        novos += 1
    conn.commit()
    grav = sum(1 for r in rows if conn.execute("SELECT 1 FROM imoveis WHERE url=?", (r["url"],)).fetchone())
    conn.close()
    print(f"[BANCO] novos={novos} ja_existiam={existe}")
    print(f"[VERIFICACAO] coletados={len(rows)} gravados={grav} -> {'OK' if grav==len(rows) else 'FALTAM'}")


if __name__ == "__main__":
    main()
