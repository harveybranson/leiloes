"""Coleta os sites que ficaram de fora: Grupo Lance, Sold, Frazão, Franco."""
import asyncio, csv, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def abs_url(base, href):
    if not href: return ""
    if href.startswith("http"): return href
    if href.startswith("/"): return base.rstrip("/") + href
    return href


async def scrape_page(browser, url, selector, base="", min_slashes=0):
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    hrefs = []
    try:
        await page.goto(url, timeout=30000)
        await asyncio.sleep(2.5)
        title = await page.title()
        if "just a moment" in title.lower():
            return []
        els = await page.query_selector_all(selector)
        for el in els:
            h = await el.get_attribute("href") or ""
            if h and h.count("/") >= min_slashes:
                hrefs.append(abs_url(base or url, h))
    except Exception as e:
        print(f"  ERRO {url[:60]}: {e}")
    finally:
        await ctx.close()
    return hrefs


async def paginate(browser, name, url_fn, selector, base, max_pages=30, min_slashes=0):
    rows = []
    seen = set()
    for pg in range(1, max_pages + 1):
        batch = await scrape_page(browser, url_fn(pg), selector, base, min_slashes)
        new = [u for u in batch if u and u not in seen]
        if not new:
            break
        for u in new:
            seen.add(u)
        rows.extend(new)
        print(f"  {name} p{pg}: +{len(new)} (total={len(rows)})")
        await asyncio.sleep(1.2)
    return rows


async def main():
    # Lê o que já existe
    all_rows = []
    existing = set()
    try:
        with open("ofertas_leiloeiros.csv", encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
            existing = {r.get("url", "") for r in all_rows}
        print(f"Já coletadas: {len(existing)}")
    except Exception:
        pass

    new_rows = []
    seen = set(existing)

    def add(leiloeiro, url):
        if url and url not in seen:
            seen.add(url)
            new_rows.append({"leiloeiro": leiloeiro, "url": url,
                             "titulo": "", "cidade": "", "estado": "",
                             "preco": 0.0, "duplicado": False})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── Grupo Lance ───────────────────────────────────────────
        print("\n[Grupo Lance]")
        BASE = "https://www.grupolance.com.br"
        urls = await paginate(browser, "Grupo Lance",
                              lambda pg: f"{BASE}/imoveis?pagina={pg}",
                              'a[href*="/imoveis/"]', BASE,
                              max_pages=15, min_slashes=5)
        for u in urls: add("Grupo Lance", u)

        # ── Sold Leilões ──────────────────────────────────────────
        print("\n[Sold Leilões]")
        BASE = "https://www.sold.com.br"
        urls = await paginate(
            browser, "Sold Leilões",
            lambda pg: f"{BASE}/h/imoveis?searchType=opened&pageNumber={pg}&pageSize=30",
            'a[href*="/oferta/"]', BASE, max_pages=80)
        for u in urls: add("Sold Leilões", u)

        # ── Frazão Leilões ────────────────────────────────────────
        print("\n[Frazão Leilões]")
        BASE = "https://www.frazaoleiloes.com.br"
        urls = await scrape_page(browser, f"{BASE}/leiloes",
                                 'a[href*="/lote/"]', BASE)
        for u in urls: add("Frazão Leilões", u)
        print(f"  Frazão: {len(urls)} lotes")

        # ── Franco Leilões ────────────────────────────────────────
        print("\n[Franco Leilões]")
        BASE = "https://www.francoleiloes.com.br"
        urls = await paginate(
            browser, "Franco Leilões",
            lambda pg: f"{BASE}/proximos_leiloes/{pg}/1/",
            'a[href*="/lote/"]', BASE, max_pages=30)
        for u in urls: add("Franco Leilões", u)

        await browser.close()

    print(f"\nNovas ofertas: {len(new_rows)}")

    # Mescla e salva
    all_rows.extend(new_rows)

    # Remove duplicatas por URL mantendo a ordem
    seen_final = set()
    deduped = []
    for r in all_rows:
        u = r.get("url", "")
        if u and u not in seen_final:
            seen_final.add(u)
            deduped.append(r)

    fieldnames = ["leiloeiro", "url", "titulo", "cidade", "estado", "preco", "duplicado"]
    with open("ofertas_leiloeiros.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)

    print(f"\nCSV final: {len(deduped)} ofertas únicas")
    from collections import Counter
    for name, n in Counter(r["leiloeiro"] for r in deduped).most_common():
        print(f"  {n:4d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
