"""
Scraper multi-site dos leiloeiros parceiros.
Coleta todas as ofertas de imóveis diretamente nos sites dos leiloeiros,
verifica duplicidade com links_leiloeiros.csv e salva em ofertas_leiloeiros.csv.

Sites suportados:
  megaleiloes.com.br | grupolance.com.br | portalzuk.com.br
  frazaoleiloes.com.br | francoleiloes.com.br | sold.com.br
  portoleiloes.com.br | luisleiloeiro.com.br | leiloeiropublico.com.br
"""

import asyncio
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

EXISTING_FILE = "links_leiloeiros.csv"
OUTPUT_FILE   = "ofertas_leiloeiros.csv"
PROGRESS_FILE = "scraper_leiloeiros_progress.json"


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def price_to_float(text: str) -> float:
    """'R$ 1.234.567,89' → 1234567.89"""
    t = re.sub(r"[^\d,]", "", text or "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0

def load_existing_urls() -> set:
    """Carrega URLs já existentes em links_leiloeiros.csv."""
    urls = set()
    if Path(EXISTING_FILE).exists():
        with open(EXISTING_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                urls.add(row.get("property_url", ""))
    return urls

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_sites": [], "collected": []}

def save_progress(p: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def save_csv(rows: list[dict]):
    fieldnames = ["leiloeiro", "titulo", "cidade", "estado",
                  "preco", "url", "fonte", "duplicado"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    total = len(rows)
    dup   = sum(1 for r in rows if r.get("duplicado"))
    novos = total - dup
    print(f"  -> {OUTPUT_FILE}: {total} ofertas ({novos} novas, {dup} duplicadas)")


# ──────────────────────────────────────────────────────────────
# Browser helpers
# ──────────────────────────────────────────────────────────────

async def new_page(browser, timeout=20000):
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    page.set_default_timeout(timeout)
    return ctx, page

async def safe_goto(page, url: str, wait: float = 3.0) -> bool:
    try:
        await page.goto(url, timeout=25000)
        await asyncio.sleep(wait)
        title = await page.title()
        return "just a moment" not in title.lower()
    except Exception:
        return False

async def get_hrefs(page, selector: str) -> list[str]:
    els = await page.query_selector_all(selector)
    hrefs = []
    for el in els:
        h = await el.get_attribute("href")
        if h:
            hrefs.append(h)
    return hrefs

def abs_url(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    return href


# ──────────────────────────────────────────────────────────────
# Site-specific scrapers (retornam lista de dicts)
# ──────────────────────────────────────────────────────────────

async def scrape_megaleiloes(browser) -> list[dict]:
    """megaleiloes.com.br/imoveis?pagina=N  — ~20/pág"""
    BASE = "https://www.megaleiloes.com.br"
    rows = []
    for pg in range(1, 100):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/imoveis?pagina={pg}")
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/imoveis/"]')
        # filtrar só links de imóvel real (≥5 segmentos de path)
        prop = [h for h in hrefs if h.count("/") >= 5 and not h.endswith("/imoveis/")]
        if not prop:
            await ctx.close(); break

        cards = await page.query_selector_all(".card-property, .property-card, article")
        if not cards:
            # fallback: usar os hrefs como fonte
            for href in set(prop):
                url = abs_url(BASE, href)
                rows.append({"leiloeiro": "Mega Leilões", "url": url,
                             "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})
        else:
            for card in cards:
                try:
                    a = await card.query_selector("a[href]")
                    href = (await a.get_attribute("href")) if a else ""
                    url = abs_url(BASE, href)

                    title_el = await card.query_selector("h2, h3, .title, .card-title")
                    titulo = clean((await title_el.inner_text()) if title_el else "")

                    price_el = await card.query_selector(".price, .valor, .lance, [class*='price']")
                    preco = price_to_float((await price_el.inner_text()) if price_el else "")

                    loc_el = await card.query_selector(".location, .cidade, .endereco, [class*='location']")
                    loc = clean((await loc_el.inner_text()) if loc_el else "")
                    parts = loc.split("/")
                    cidade = parts[0].strip() if parts else ""
                    estado = parts[1].strip()[:2] if len(parts) > 1 else ""

                    rows.append({"leiloeiro": "Mega Leilões", "url": url, "titulo": titulo,
                                 "cidade": cidade, "estado": estado, "preco": preco, "fonte": BASE})
                except Exception:
                    pass

        print(f"  Mega Leiloes p.{pg}: {len(prop)} props | total={len(rows)}")
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_grupolance(browser) -> list[dict]:
    """grupolance.com.br/imoveis?pagina=N"""
    BASE = "https://www.grupolance.com.br"
    rows = []
    for pg in range(1, 100):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/imoveis?pagina={pg}")
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/imoveis/"]')
        prop = [h for h in hrefs if h.count("/") >= 4 and not h.rstrip("/").endswith("imoveis")]
        if not prop:
            await ctx.close(); break

        for href in set(prop):
            url = abs_url(BASE, href)
            # extrai cidade/estado/tipo do slug
            parts = href.strip("/").split("/")
            estado = parts[-3].upper() if len(parts) >= 3 else ""
            cidade = parts[-2].replace("-", " ").title() if len(parts) >= 2 else ""
            rows.append({"leiloeiro": "Grupo Lance", "url": url,
                         "titulo": "", "cidade": cidade, "estado": estado, "preco": 0, "fonte": BASE})

        print(f"  Grupo Lance p.{pg}: {len(prop)} props | total={len(rows)}")
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_portalzuk(browser) -> list[dict]:
    """portalzuk.com.br/leilao-de-imoveis — paginacao por skip=N"""
    BASE = "https://www.portalzuk.com.br"
    rows = []
    skip = 0
    while True:
        ctx, page = await new_page(browser)
        url = f"{BASE}/leilao-de-imoveis?skip={skip}" if skip else f"{BASE}/leilao-de-imoveis"
        ok = await safe_goto(page, url)
        if not ok:
            await ctx.close(); break

        hrefs = await get_hrefs(page, 'a[href*="/leilao-de-imoveis/v/"]')
        prop = list(set(hrefs))
        if not prop:
            await ctx.close(); break

        prev_count = len(rows)
        for href in prop:
            u = abs_url(BASE, href)
            # slug ex: /leilao-de-imoveis/v/banco-santander-sp-imoveis/12345
            parts = href.strip("/").split("/")
            rows.append({"leiloeiro": "Portal Zuk", "url": u,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})

        added = len(rows) - prev_count
        print(f"  Portal Zuk skip={skip}: {added} props | total={len(rows)}")

        if added == 0:
            await ctx.close(); break
        skip += 20
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_frazao(browser) -> list[dict]:
    """frazaoleiloes.com.br/leiloes — sem paginação óbvia, carrega todos"""
    BASE = "https://www.frazaoleiloes.com.br"
    rows = []
    ctx, page = await new_page(browser)
    ok = await safe_goto(page, f"{BASE}/leiloes", wait=4)
    if ok:
        hrefs = await get_hrefs(page, 'a[href*="/lote/"]')
        for href in set(hrefs):
            url = abs_url(BASE, href)
            # /lote/36651-nome-do-lote → id=36651
            id_m = re.search(r"/lote/(\d+)", href)
            prop_id = id_m.group(1) if id_m else ""
            rows.append({"leiloeiro": "Frazão Leilões", "url": url,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})
        print(f"  Frazao Leiloes: {len(rows)} props")
    await ctx.close()
    return rows


async def scrape_franco(browser) -> list[dict]:
    """francoleiloes.com.br/proximos_leiloes/{pg}/1/"""
    BASE = "https://www.francoleiloes.com.br"
    rows = []
    for pg in range(1, 50):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/proximos_leiloes/{pg}/1/")
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/lote/"]')
        prop = list(set(hrefs))
        if not prop:
            await ctx.close(); break

        prev = len(rows)
        for href in prop:
            url = abs_url(BASE, href)
            rows.append({"leiloeiro": "Franco Leilões", "url": url,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})

        added = len(rows) - prev
        print(f"  Franco Leiloes p.{pg}: {added} props | total={len(rows)}")
        if added == 0:
            await ctx.close(); break
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_sold(browser) -> list[dict]:
    """sold.com.br/h/imoveis?pageNumber=N"""
    BASE = "https://www.sold.com.br"
    rows = []
    for pg in range(1, 200):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/h/imoveis?searchType=opened&pageNumber={pg}&pageSize=30")
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/oferta/"]')
        prop = list(set(hrefs))
        if not prop:
            await ctx.close(); break

        prev = len(rows)
        for href in prop:
            url = abs_url(BASE, href)
            rows.append({"leiloeiro": "Sold Leilões", "url": url,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})

        added = len(rows) - prev
        print(f"  Sold p.{pg}: {added} props | total={len(rows)}")
        if added == 0:
            await ctx.close(); break
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_portoleiloes(browser) -> list[dict]:
    """portoleiloes.com.br/eventos?tipo=leilao"""
    BASE = "https://www.portoleiloes.com.br"
    rows = []
    for pg in range(1, 50):
        ctx, page = await new_page(browser, timeout=30000)
        ok = await safe_goto(page, f"{BASE}/eventos?tipo=leilao&pagina={pg}", wait=4)
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/eventos/leilao/"]')
        prop = list(set(hrefs))
        if not prop:
            # also try lote links
            hrefs2 = await get_hrefs(page, 'a[href*="/lote/"]')
            prop = list(set(hrefs2))
        if not prop:
            await ctx.close(); break

        prev = len(rows)
        for href in prop:
            url = abs_url(BASE, href)
            rows.append({"leiloeiro": "Porto Leilões", "url": url,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})
        added = len(rows) - prev
        print(f"  Porto Leiloes p.{pg}: {added} props | total={len(rows)}")
        if added == 0:
            await ctx.close(); break
        await ctx.close()
        await asyncio.sleep(2)
    return rows


async def scrape_luisleiloeiro(browser) -> list[dict]:
    """luisleiloeiro.com.br/leiloes"""
    BASE = "https://www.luisleiloeiro.com.br"
    rows = []
    for pg in range(1, 50):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/leiloes?pagina={pg}")
        if not ok:
            await ctx.close(); break
        hrefs = await get_hrefs(page, 'a[href*="/leilao/"]')
        prop = list(set(hrefs))
        if not prop:
            await ctx.close(); break

        prev = len(rows)
        for href in prop:
            url = abs_url(BASE, href)
            rows.append({"leiloeiro": "Luis Leiloeiro", "url": url,
                         "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})
        added = len(rows) - prev
        print(f"  Luis Leiloeiro p.{pg}: {added} props | total={len(rows)}")
        if added == 0:
            await ctx.close(); break
        await ctx.close()
        await asyncio.sleep(1.5)
    return rows


async def scrape_rodolfo(browser) -> list[dict]:
    """leiloeiropublico.com.br — lista leilões → lista lotes"""
    BASE = "https://www.leiloeiropublico.com.br"
    rows = []
    ctx, page = await new_page(browser)
    ok = await safe_goto(page, f"{BASE}/ListagemLeilao.aspx", wait=4)
    if ok:
        hrefs = await get_hrefs(page, 'a[href*="ListagemLote"]')
        leilao_hrefs = list(set(hrefs))
        await ctx.close()

        for leilao_href in leilao_hrefs[:30]:
            leilao_url = abs_url(BASE, leilao_href)
            ctx2, page2 = await new_page(browser)
            ok2 = await safe_goto(page2, leilao_url)
            if ok2:
                lote_hrefs = await get_hrefs(page2, 'a[href*="DetalhesBem"]')
                for href in set(lote_hrefs):
                    url = abs_url(BASE, href)
                    rows.append({"leiloeiro": "Rodolfo Schontag", "url": url,
                                 "titulo": "", "cidade": "", "estado": "", "preco": 0, "fonte": BASE})
                print(f"  Rodolfo leilao {leilao_href}: {len(lote_hrefs)} lotes | total={len(rows)}")
            await ctx2.close()
            await asyncio.sleep(1)
    else:
        await ctx.close()
    return rows


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

SCRAPERS = {
    "Mega Leilões":      scrape_megaleiloes,
    "Grupo Lance":       scrape_grupolance,
    "Portal Zuk":        scrape_portalzuk,
    "Frazão Leilões":    scrape_frazao,
    "Franco Leilões":    scrape_franco,
    "Sold Leilões":      scrape_sold,
    "Porto Leilões":     scrape_portoleiloes,
    "Luis Leiloeiro":    scrape_luisleiloeiro,
    "Rodolfo Schontag":  scrape_rodolfo,
}


async def main(reset: bool = False):
    existing_urls = load_existing_urls()
    print(f"URLs já existentes no sistema: {len(existing_urls)}")

    progress = {} if reset else load_progress()
    if reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()
    progress = load_progress()

    all_rows: list[dict] = list(progress.get("collected", []))
    seen_urls = {r["url"] for r in all_rows}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for name, scraper_fn in SCRAPERS.items():
            if name in progress.get("done_sites", []):
                print(f"[skip] {name} (já processado)")
                continue

            print(f"\n[{name}] Iniciando...")
            try:
                site_rows = await scraper_fn(browser)
            except Exception as e:
                print(f"  ERRO em {name}: {e}")
                site_rows = []

            # deduplicar: url nova?
            added = 0
            for row in site_rows:
                url = row.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    row["duplicado"] = url in existing_urls
                    all_rows.append(row)
                    added += 1

            print(f"  {name}: {added} novas ofertas adicionadas")

            progress["done_sites"] = progress.get("done_sites", []) + [name]
            progress["collected"] = all_rows
            save_progress(progress)
            save_csv(all_rows)

        await browser.close()

    print(f"\nConcluido! Total: {len(all_rows)} ofertas em {OUTPUT_FILE}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="Reiniciar do zero")
    args = ap.parse_args()
    asyncio.run(main(reset=args.reset))
