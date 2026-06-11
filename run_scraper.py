"""Scraper direto nos sites dos leiloeiros — coleta todas as ofertas de imóveis."""
import asyncio, csv, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def abs_url(base, href):
    if not href: return ""
    if href.startswith("http"): return href
    if href.startswith("/"): return base.rstrip("/") + href
    return href


async def get_hrefs(page, selector):
    els = await page.query_selector_all(selector)
    return [await el.get_attribute("href") or "" for el in els]


async def scrape_page(browser, name, url, selector, base, min_slashes=0):
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    rows = []
    try:
        await page.goto(url, timeout=25000)
        await asyncio.sleep(2.5)
        title = await page.title()
        if "just a moment" in title.lower():
            return rows
        for h in set(await get_hrefs(page, selector)):
            if h and h.count("/") >= min_slashes:
                rows.append({"leiloeiro": name, "url": abs_url(base, h),
                             "titulo": "", "cidade": "", "estado": "", "preco": 0.0})
    except Exception as e:
        print(f"  ERRO {name}: {e}")
    finally:
        await ctx.close()
    return rows


async def paginate(browser, name, url_fn, selector, base, max_pages=50, min_slashes=0):
    rows = []
    seen = set()
    for pg in range(1, max_pages + 1):
        batch = await scrape_page(browser, name, url_fn(pg), selector, base, min_slashes)
        new = [r for r in batch if r["url"] and r["url"] not in seen]
        if not new:
            break
        for r in new: seen.add(r["url"])
        rows.extend(new)
        print(f"  {name} p{pg}: +{len(new)} (total={len(rows)})")
        await asyncio.sleep(1.2)
    return rows


async def paginate_skip(browser, name, url_fn, selector, base, step=20, max_skip=2000):
    rows = []
    seen = set()
    skip = 0
    while skip <= max_skip:
        batch = await scrape_page(browser, name, url_fn(skip), selector, base)
        new = [r for r in batch if r["url"] and r["url"] not in seen]
        if not new:
            break
        for r in new: seen.add(r["url"])
        rows.extend(new)
        print(f"  {name} skip={skip}: +{len(new)} (total={len(rows)})")
        skip += step
        await asyncio.sleep(1.2)
    return rows


async def main():
    from playwright.async_api import async_playwright  # import lazy (só na coleta)
    # URLs já existentes para marcar duplicados
    existing = set()
    try:
        with open("links_leiloeiros.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.add(r.get("property_url", ""))
    except Exception:
        pass
    print(f"URLs já no sistema: {len(existing)}")

    all_rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── Mega Leilões ──────────────────────────────────────────
        print("\n[Mega Leilões]")
        BASE = "https://www.megaleiloes.com.br"
        rows = await paginate(
            browser, "Mega Leilões",
            lambda pg: f"{BASE}/imoveis?pagina={pg}",
            'a[href*="/imoveis/"]', BASE, max_pages=20, min_slashes=6
        )
        all_rows.extend(rows)

        # ── Grupo Lance ───────────────────────────────────────────
        print("\n[Grupo Lance]")
        BASE = "https://www.grupolance.com.br"
        rows = await paginate(
            browser, "Grupo Lance",
            lambda pg: f"{BASE}/imoveis?pagina={pg}",
            'a[href*="/imoveis/"]', BASE, max_pages=15, min_slashes=5
        )
        all_rows.extend(rows)

        # ── Portal Zuk ────────────────────────────────────────────
        print("\n[Portal Zuk]")
        BASE = "https://www.portalzuk.com.br"
        rows = await paginate_skip(
            browser, "Portal Zuk",
            lambda sk: f"{BASE}/leilao-de-imoveis?skip={sk}" if sk else f"{BASE}/leilao-de-imoveis",
            'a[href*="/leilao-de-imoveis/v/"]', BASE, step=20, max_skip=2000
        )
        all_rows.extend(rows)

        # ── Frazão Leilões ────────────────────────────────────────
        print("\n[Frazão Leilões]")
        BASE = "https://www.frazaoleiloes.com.br"
        rows = await scrape_page(browser, "Frazão Leilões",
                                 f"{BASE}/leiloes", 'a[href*="/lote/"]', BASE)
        all_rows.extend(rows)
        print(f"  Frazão: {len(rows)} lotes")

        # ── Franco Leilões ────────────────────────────────────────
        print("\n[Franco Leilões]")
        BASE = "https://www.francoleiloes.com.br"
        rows = await paginate(
            browser, "Franco Leilões",
            lambda pg: f"{BASE}/proximos_leiloes/{pg}/1/",
            'a[href*="/lote/"]', BASE, max_pages=30
        )
        all_rows.extend(rows)

        # ── Sold Leilões ──────────────────────────────────────────
        print("\n[Sold Leilões]")
        BASE = "https://www.sold.com.br"
        rows = await paginate(
            browser, "Sold Leilões",
            lambda pg: f"{BASE}/h/imoveis?searchType=opened&pageNumber={pg}&pageSize=30",
            'a[href*="/oferta/"]', BASE, max_pages=100
        )
        all_rows.extend(rows)

        # ── Porto Leilões ─────────────────────────────────────────
        print("\n[Porto Leilões]")
        BASE = "https://www.portoleiloes.com.br"
        rows = await paginate(
            browser, "Porto Leilões",
            lambda pg: f"{BASE}/eventos?tipo=leilao&pagina={pg}",
            'a[href*="/eventos/leilao/"]', BASE, max_pages=30
        )
        all_rows.extend(rows)

        # ── Luis Leiloeiro ────────────────────────────────────────
        print("\n[Luis Leiloeiro]")
        BASE = "https://www.luisleiloeiro.com.br"
        rows = await paginate(
            browser, "Luis Leiloeiro",
            lambda pg: f"{BASE}/leiloes?pagina={pg}",
            'a[href*="/leilao/"]', BASE, max_pages=30
        )
        all_rows.extend(rows)

        # ── Rodolfo Schontag ──────────────────────────────────────
        print("\n[Rodolfo Schontag]")
        BASE = "https://www.leiloeiropublico.com.br"
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        rodolfo_rows = []
        try:
            await page.goto(f"{BASE}/ListagemLeilao.aspx", timeout=25000)
            await asyncio.sleep(3)
            leilao_hrefs = set(await get_hrefs(page, 'a[href*="ListagemLote"]'))
            await ctx.close()

            for lh in list(leilao_hrefs)[:20]:
                lote_rows = await scrape_page(
                    browser, "Rodolfo Schontag",
                    abs_url(BASE, lh), 'a[href*="DetalhesBem"]', BASE
                )
                rodolfo_rows.extend(lote_rows)
                print(f"  Rodolfo {lh[:50]}: {len(lote_rows)} lotes")
                await asyncio.sleep(1)
        except Exception as e:
            print(f"  Rodolfo ERRO: {e}")
            await ctx.close()
        all_rows.extend(rodolfo_rows)

        await browser.close()

    # ── Deduplicação e salvamento ─────────────────────────────────
    seen_urls = set()
    unique = []
    for r in all_rows:
        u = r.get("url", "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            r["duplicado"] = u in existing
            unique.append(r)

    fieldnames = ["leiloeiro", "url", "titulo", "cidade", "estado", "preco", "duplicado"]
    with open("ofertas_leiloeiros.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(unique)

    total = len(unique)
    dup   = sum(1 for r in unique if r["duplicado"])
    print(f"\nSalvo: {total} ofertas ({total - dup} novas, {dup} duplicadas) em ofertas_leiloeiros.csv")

    from collections import Counter
    cnt = Counter(r["leiloeiro"] for r in unique)
    print("\nPor leiloeiro:")
    for name, n in cnt.most_common():
        print(f"  {n:4d}  {name}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Coleta ofertas dos leiloeiros.")
    ap.add_argument("--finalize", action="store_true",
                    help="após a coleta, roda o gate de qualidade (finalizar_coleta.py)")
    ap.add_argument("--only-finalize", action="store_true",
                    help="pula a coleta e roda apenas o gate de qualidade")
    args, _ = ap.parse_known_args()

    rc = 0
    if not args.only_finalize:
        asyncio.run(main())
    if args.finalize or args.only_finalize:
        import finalizar_coleta
        from datetime import date
        rc = finalizar_coleta.finalizar(desde=date.today().isoformat())
    sys.exit(rc)
