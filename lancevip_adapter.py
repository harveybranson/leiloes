# -*- coding: utf-8 -*-
"""Adapter LanceVip (plataforma suporteleiloes, SSR).
Eventos 'sicoob ... imovel' = 1 imovel cada; lote em /eventos/leilao/<id>/<slug>/lote.
Sem API JSON: pagina renderizada por Playwright (render() do scraper_rr_ro)."""
import re, requests, urllib3
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
urllib3.disable_warnings()
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "pt-BR"}
SLUG_IMOVEL = re.compile(r"imove|imóve|rural|terreno|casa|apartament|chacara|sitio|gleba|fazenda|lote-urbano", re.I)
PRICE = re.compile(r"R\$\s*([\d.]+,\d{2})")
DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _host(site):
    return re.sub(r"^https?://", "", site).strip("/").split("/")[0]


def is_lancevip(site):
    return "lancevip" in _host(site)


def _to_real(s):
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return None


def fetch_imoveis(site, today, render):
    """render: funcao render(url)->(html,final) do scraper. Retorna lista de imoveis."""
    if not is_lancevip(site):
        return None
    base = f"https://{_host(site)}"
    try:
        home = requests.get(base + "/", headers=H, timeout=25, verify=False).text
    except Exception:
        return []
    events = []
    for m in re.findall(r"/eventos/leilao/\d+/[a-z0-9\-]+", home):
        if SLUG_IMOVEL.search(m) and m not in events:
            events.append(m)
    out = []
    for ev in events:
        url = urljoin(base, ev) + "/lote"
        html = render(url, wait_ms=4500)[0] or ""
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find(["h1", "h2"])
        title = h1.get_text(" ", strip=True) if h1 else ev.split("/")[-1].replace("-", " ")
        title = re.sub(r"\s+", " ", title).strip()
        txt = soup.get_text(" ", strip=True)
        # cidade/uf do titulo
        cm = re.search(r"\bEM\s+(.+?)/([A-Z]{2})\b", title, re.I) or re.search(r"([A-ZÀ-Ý][\w' ]+?)\s*/\s*([A-Z]{2})\b", title)
        cidade = cm.group(1).strip().title() if cm else ""
        uf = cm.group(2).upper() if cm else "RO"
        # datas futuras
        fut = sorted({datetime(int(y), int(mo), int(d)) for d, mo, y in DATE.findall(txt)
                      if _valid(d, mo, y) and datetime(int(y), int(mo), int(d)) >= today})
        if not fut:
            continue
        d1 = fut[0]
        # preco: valor apos 'lance inicial'/'1º lance'/'lance', senao o 2o maior
        precos = [(_to_real(p), p) for p in PRICE.findall(txt)]
        precos = [(v, p) for v, p in precos if v and v >= 1000]
        lance = ""
        ml = re.search(r"(?:lance inicial|1[ºoª]\s*lance|lance m[íi]nimo)[^R]{0,30}R\$\s*([\d.]+,\d{2})", txt, re.I)
        if ml:
            lance = ml.group(1)
        elif precos:
            vals = sorted(set(v for v, _ in precos), reverse=True)
            # avaliacao = maior; lance ~ 2o maior
            pick = vals[1] if len(vals) > 1 else vals[0]
            lance = next(p for v, p in precos if v == pick)
        # imagem
        img = ""
        for im in soup.find_all("img"):
            src = im.get("src") or im.get("data-src") or ""
            if src.startswith("http") and not any(x in src.lower() for x in ["logo", "icon", "whats", "banner", "avatar"]):
                img = src
                break
        # anexos (edital)
        anx = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)
               if a["href"].lower().endswith(".pdf") or "edital" in a.get_text().lower() or "matr" in a.get_text().lower()]
        out.append({
            "titulo": title[:200], "url": url, "imagem": img,
            "preco": lance, "cidade": cidade, "uf": uf,
            "data_leilao": d1.strftime("%d/%m/%Y"),
            "anexos": "; ".join(anx[:5]),
            "ctx": title,
        })
    return out


def _valid(d, mo, y):
    try:
        datetime(int(y), int(mo), int(d)); return True
    except ValueError:
        return False


if __name__ == "__main__":
    import sys, scraper_rr_ro as s
    from playwright.sync_api import sync_playwright
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s._PW = sync_playwright().start(); s._BROWSER = s._PW.chromium.launch(headless=True, args=["--no-sandbox"])
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ims = fetch_imoveis("https://www.lancevip.com.br", today, s.render)
    print("LanceVip imoveis:", len(ims))
    for im in ims:
        print("  -", im["data_leilao"], "|", im["titulo"][:55], "|", im["cidade"], im["uf"], "| R$", im["preco"], "| img", bool(im["imagem"]), "| anx", bool(im["anexos"]))
    s._BROWSER.close(); s._PW.stop()
