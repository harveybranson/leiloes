"""
Portal Zuk scraper — extrai dados de .leilao-vitrine (inclui data-url-externa)
e usa as rotas Ziggy descobertas para paginação via API interna.
"""
import asyncio, csv, re, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://www.portalzuk.com.br"


async def extract_lots(page) -> list[dict]:
    """Extrai dados de cada .leilao-vitrine da página atual."""
    lots = []
    try:
        # Usa JS para extrair data-attributes de todos os elementos .leilao-vitrine
        result = await page.evaluate("""() => {
            const els = document.querySelectorAll('[data-idlote], [data-id-lote], .leilao-vitrine, .card-lote, [data-lote]');
            return Array.from(els).map(el => ({
                idLote:      el.dataset.idlote || el.dataset.idLote || el.dataset.id || '',
                idLeilao:    el.dataset.idleilao || el.dataset.idLeilao || '',
                urlExterna:  el.dataset.urlExterna || el.dataset.url_externa || el.dataset.href || '',
                comitente:   el.dataset.comitente || '',
                titulo:      (el.querySelector('h2,h3,.titulo,.title,p')?.innerText || '').trim().substring(0, 150),
                href:        el.href || el.querySelector('a')?.href || ''
            })).filter(d => d.idLote || d.href);
        }""")
        lots.extend(result)
    except Exception as e:
        print(f"  JS eval error: {e}")

    # Fallback: extrai IDs do dataLayer e product_ids
    html = await page.content()

    # Product IDs do dataLayer
    m = re.search(r"'productId'\s*:\s*'([0-9,]+)'", html)
    if m:
        ids = m.group(1).split(",")
        # Pega o vendor slug da URL
        vendor = re.search(r"/v/([^/?]+)", page.url)
        vslug = vendor.group(1) if vendor else ""
        for pid in ids:
            pid = pid.strip()
            url = f"{BASE}/leilao-de-imoveis/v/{vslug}/{pid}"
            lots.append({"idLote": pid, "idLeilao": "", "urlExterna": "",
                         "comitente": vslug, "titulo": "", "href": url})

    return lots


async def get_all_vendor_pages(browser) -> list[str]:
    """Coleta todas as páginas de vendedor do Portal Zuk."""
    vendor_pages = []
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    try:
        # Usa o endpoint de busca livre para listar todos os comitentes
        await page.goto(f"{BASE}/leilao-de-imoveis", timeout=30000)
        await asyncio.sleep(4)
        html = await page.content()

        # Extrai vendor slugs de hrefs
        vendors = set(re.findall(
            r'href="((?:https://www\.portalzuk\.com\.br)?/leilao-de-imoveis/v/[^/"?]+)"', html))
        vendor_pages.extend(vendors)

        # Também pega da agenda de leilões
        await page.goto(f"{BASE}/agenda/leiloes", timeout=30000)
        await asyncio.sleep(4)
        html2 = await page.content()
        vendors2 = set(re.findall(
            r'href="((?:https://www\.portalzuk\.com\.br)?/leilao-de-imoveis/v/[^/"?]+)"', html2))
        vendor_pages.extend(vendors2)

        # Normaliza
        result = []
        seen = set()
        for v in vendor_pages:
            if not v.startswith("http"):
                v = BASE + v
            if v not in seen and "/v/" in v:
                seen.add(v)
                result.append(v)
        print(f"  Vendor pages encontradas: {len(result)}")
        return result
    finally:
        await ctx.close()


async def scrape_vendor(browser, vurl: str, rows: list, seen: set):
    """Scrapa um vendor page e todas as sub-páginas."""
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    added = 0
    try:
        await page.goto(vurl, timeout=30000)
        await asyncio.sleep(3)
        title = await page.title()
        if "just a moment" in title.lower():
            return 0
        html = await page.content()

        # Descobre total de lotes
        count_m = re.search(r"countLotes\s*=\s*['\"](\d+)['\"]", html)
        total_lotes = int(count_m.group(1)) if count_m else 0

        # Extrai lotes da página atual
        lots = await extract_lots(page)

        # Paginação: se há mais lotes que os da primeira página
        # O Portal Zuk usa offset/skip=N
        if total_lotes > len(lots):
            skip = len(lots)
            while skip < total_lotes:
                ctx2 = await browser.new_context(user_agent=UA)
                page2 = await ctx2.new_page()
                try:
                    url_pag = f"{vurl}?skip={skip}" if "?" not in vurl else f"{vurl}&skip={skip}"
                    await page2.goto(url_pag, timeout=25000)
                    await asyncio.sleep(2.5)
                    lots2 = await extract_lots(page2)
                    if not lots2: break
                    lots.extend(lots2)
                    skip += len(lots2)
                except Exception:
                    break
                finally:
                    await ctx2.close()
                await asyncio.sleep(1)

        # Adiciona ao resultado
        for lot in lots:
            url = lot.get("href") or ""
            if not url:
                lid = lot.get("idLote","")
                vslug = re.search(r"/v/([^/?]+)", vurl)
                vslug = vslug.group(1) if vslug else ""
                url = f"{BASE}/leilao-de-imoveis/v/{vslug}/{lid}"

            if url and url not in seen:
                seen.add(url)
                rows.append({
                    "leiloeiro":    "Portal Zuk",
                    "url":          url,
                    "titulo":       lot.get("titulo",""),
                    "cidade":       "",
                    "estado":       "",
                    "preco":        0.0,
                    "avaliacao":    0.0,
                    "desconto_pct": 0.0,
                    "duplicado":    False,
                    "url_externa":  lot.get("urlExterna",""),
                })
                added += 1
    except Exception as e:
        print(f"  ERRO {vurl}: {e}")
    finally:
        await ctx.close()
    return added


async def main():
    # Lê dados existentes
    existing_rows = []
    existing_urls = set()
    try:
        with open("ofertas_leiloeiros.csv", encoding="utf-8") as f:
            existing_rows = list(csv.DictReader(f))
            existing_urls = {r.get("url","") for r in existing_rows}
        print(f"Já existentes: {len(existing_urls)}")
    except Exception:
        pass

    new_rows = []
    seen = set(existing_urls)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        print("[Portal Zuk] coletando vendor pages...")
        vendor_pages = await get_all_vendor_pages(browser)

        total_added = 0
        for vurl in vendor_pages:
            added = await scrape_vendor(browser, vurl, new_rows, seen)
            if added > 0:
                print(f"  {vurl[-60:]}: +{added}")
            total_added += added
            await asyncio.sleep(1)

        print(f"\nPortal Zuk total: {total_added} novos lotes")
        await browser.close()

    # Salva CSV
    all_rows = existing_rows + new_rows
    seen_final = set()
    deduped = []
    for r in all_rows:
        u = r.get("url","")
        if u and u not in seen_final:
            seen_final.add(u); deduped.append(r)

    fieldnames = ["leiloeiro","url","titulo","cidade","estado","preco",
                  "avaliacao","desconto_pct","duplicado","url_externa"]
    with open("ofertas_leiloeiros.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader(); w.writerows(deduped)

    from collections import Counter
    cnt = Counter(r["leiloeiro"] for r in deduped)
    print(f"\nCSV: {len(deduped)} total")
    for name, n in cnt.most_common():
        print(f"  {n:5d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
