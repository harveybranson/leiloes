# -*- coding: utf-8 -*-
"""Adapter Superbid (white-label): offer-query API filtrada por imoveis."""
import requests, urllib3, re
from datetime import datetime
urllib3.disable_warnings()
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
     "Accept": "application/json, application/hal+json"}
CFG = "https://siteconfigprod.superbid.net/{host}/style.config.json"
API = ("https://offer-query.superbid.net/offers/?filter=auction.modalityId:[1,4];"
       "product.productType.description:imoveis;stores.id:{store}&locale=pt_BR&"
       "orderBy=endDate:asc&pageNumber={pg}&pageSize=30&portalId={portal}&"
       "requestOrigin=store&searchType=opened&timeZoneId=America/Sao_Paulo")


def _host(site):
    return re.sub(r"^https?://", "", site).strip("/").split("/")[0]


def get_store(site):
    """Retorna (storeId, portalId) se for Superbid, senao None."""
    host = _host(site)
    for h in (host, host.replace("www.", "")):
        try:
            r = requests.get(CFG.format(host=h), headers=H, timeout=20, verify=False)
            if r.status_code == 200 and "storeId" in r.text:
                j = r.json()
                portal = j.get("portalId") or [2, 15]
                return j["storeId"], "[" + ",".join(str(x) for x in portal) + "]", h
        except Exception:
            continue
    return None


def _date(s):
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_imoveis(site, today):
    info = get_store(site)
    if not info:
        return None  # nao e superbid
    store, portal, host = info
    out = []
    pg = 1
    total = None
    while True:
        url = API.format(store=store, portal=portal, pg=pg)
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            j = r.json()
        except Exception:
            break
        if total is None:
            total = j.get("total", 0)
        offers = j.get("offers") or []
        if not offers:
            break
        for of in offers:
            prod = of.get("product") or {}
            loc = prod.get("location") or {}
            auc = of.get("auction") or {}
            # 1a praca: primeiro stage do eventPipeline, senao beginDate
            stages = ((auc.get("eventPipeline") or {}).get("stages")) or []
            d1 = None
            if stages:
                d1 = _date(stages[0].get("endDate") or stages[0].get("beginDate") or "")
            d1 = d1 or _date(auc.get("beginDate") or "") or _date(of.get("endDate") or "")
            if not d1 or d1 < today:
                continue
            city = (loc.get("city") or "")
            uf = ""
            m = re.search(r"-\s*([A-Z]{2})\b", city)
            if m:
                uf = m.group(1)
                city = city.split("-")[0].strip()
            title = (prod.get("shortDesc") or auc.get("desc") or "").strip()
            if not title:
                continue
            oid = of.get("id")
            out.append({
                "titulo": title[:200],
                "url": f"https://{host}/oferta/{oid}",
                "imagem": prod.get("thumbnailUrl") or "",
                "preco": (of.get("priceFormatted") or "").replace("R$", "").strip(),
                "cidade": city, "uf": uf,
                "data_leilao": d1.strftime("%d/%m/%Y"),
                "anexos": "",
                "ctx": f"{title} | {auc.get('desc','')} | {loc.get('state','')}",
            })
        pg += 1
        if total and len(out) >= total:
            break
        if pg > 30:
            break
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for site in ["https://www.hoppeleiloes.com.br", "https://www.asamileiloes.com.br"]:
        ims = fetch_imoveis(site, today)
        print(f"\n{site}: {'NAO-superbid' if ims is None else str(len(ims))+' imoveis'}")
        for im in (ims or [])[:6]:
            print("  -", im["data_leilao"], "|", im["titulo"][:55], "| R$", im["preco"], "|", im["cidade"], im["uf"], "| img", bool(im["imagem"]))
