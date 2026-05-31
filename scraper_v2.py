"""
Scraper v2 — segue o guia captura_dados_leiloes_v2.md
Prioridade: API interna > HTML scraping
Leiloeiros: Central Sul (API), Portal Zuk, Grupo Lance, Sold, Frazão, Franco,
            Porto, Luis, Rodolfo, Milan (Cloudflare CF-clearance)
"""
import asyncio, csv, json, re, sys, time
import requests, warnings
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import Counter
from pathlib import Path
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
OUTPUT = "ofertas_leiloeiros.csv"
FIELDNAMES = ["leiloeiro", "url", "titulo", "cidade", "estado", "preco", "avaliacao", "desconto_pct", "duplicado"]


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def abs_url(base, href):
    if not href: return ""
    if href.startswith("http"): return href
    if href.startswith("/"): return base.rstrip("/") + href
    return href

def load_existing():
    rows, urls = [], set()
    if Path(OUTPUT).exists():
        with open(OUTPUT, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            urls = {r.get("url","") for r in rows}
    return rows, urls

def save_all(all_rows):
    seen, deduped = set(), []
    for r in all_rows:
        u = r.get("url","")
        if u and u not in seen:
            seen.add(u); deduped.append(r)
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader(); w.writerows(deduped)
    cnt = Counter(r["leiloeiro"] for r in deduped)
    print(f"\n=== TOTAL: {len(deduped)} ofertas únicas ===")
    for name, n in cnt.most_common():
        print(f"  {n:5d}  {name}")
    return deduped

def add_row(rows, seen, leiloeiro, url, titulo="", cidade="", estado="",
            preco=0.0, avaliacao=0.0, desconto_pct=0.0):
    if url and url not in seen:
        seen.add(url)
        rows.append({"leiloeiro": leiloeiro, "url": url, "titulo": titulo,
                     "cidade": cidade, "estado": estado, "preco": preco,
                     "avaliacao": avaliacao, "desconto_pct": desconto_pct,
                     "duplicado": False})

def parse_city_state(text):
    """'São Paulo / SP' -> ('São Paulo', 'SP')"""
    if not text: return "", ""
    parts = re.split(r"[/\-–]", text)
    cidade = parts[0].strip() if parts else ""
    estado = parts[1].strip()[:2].upper() if len(parts) > 1 else ""
    return cidade, estado


# ─────────────────────────────────────────────────────────────────
# 1. CENTRAL SUL DE LEILÕES — API REST pura
# ─────────────────────────────────────────────────────────────────

def scrape_central_sul(rows, seen):
    BASE = "https://www.centralsuldeleiloes.com.br"
    hdrs = {"User-Agent": UA}
    print("\n[Central Sul de Leilões] via API")

    # Pega todos os leilões
    page, per = 1, 100
    all_auctions = []
    while True:
        r = requests.get(f"{BASE}/api/v2/web/next-auctions?page={page}&per_page={per}&cache=true",
                         headers=hdrs, verify=False, timeout=20)
        if r.status_code != 200: break
        body = r.json().get("body", {})
        data = body.get("data", [])
        if not data: break
        all_auctions.extend(data)
        total = int(body.get("total", 0))
        print(f"  Leilões p{page}: +{len(data)} (total acumulado={len(all_auctions)}/{total})")
        if len(all_auctions) >= total: break
        page += 1

    # Para cada leilão, pega os lotes via API
    added = 0
    for auction in all_auctions:
        aid = auction["id"]
        r2 = requests.get(f"{BASE}/api/v2/web/auction/{aid}/lots",
                          headers=hdrs, verify=False, timeout=20)
        if r2.status_code != 200: continue
        lots = r2.json().get("body", [])
        for lot in lots:
            url = lot.get("url", "")
            if not url:
                slug = lot.get("slug","")
                lid  = lot.get("id","")
                url  = f"{BASE}/leilao/{aid}/lote/{lid}/{slug}"
            titulo   = lot.get("title","")
            avaliacao= float(lot.get("value", 0) or 0)
            preco    = float(lot.get("minimum_bid", 0) or 0)
            desconto = float(lot.get("percentage", 0) or 0)
            # cidade/estado a partir do título do leilão
            loc_text = auction.get("title","")
            cidade, estado = parse_city_state(loc_text.split(" - ")[0] if " - " in loc_text else loc_text)
            add_row(rows, seen, "Central Sul de Leilões", url, titulo, cidade, estado,
                    preco, avaliacao, desconto)
            added += 1
        time.sleep(0.3)

    print(f"  Central Sul: {added} lotes adicionados")


# ─────────────────────────────────────────────────────────────────
# 2. PORTAL ZUK — busca por __NEXT_DATA__ + API interna
# ─────────────────────────────────────────────────────────────────

async def scrape_portal_zuk(browser, rows, seen):
    BASE = "https://www.portalzuk.com.br"
    print("\n[Portal Zuk] investigando __NEXT_DATA__ e APIs")

    api_hits = []
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()

    async def on_resp(resp):
        if "json" in resp.headers.get("content-type","") and "portalzuk" in resp.url:
            try:
                body = await resp.text()
                if len(body) > 100:
                    api_hits.append({"url": resp.url, "body": body})
            except: pass

    page.on("response", on_resp)
    await page.goto(f"{BASE}/leilao-de-imoveis", timeout=30000)
    await asyncio.sleep(5)

    # Tenta __NEXT_DATA__
    html = await page.content()
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd:
        try:
            nd_data = json.loads(nd.group(1))
            # navega pelo JSON buscando URLs de imóveis
            flat = json.dumps(nd_data, ensure_ascii=False)
            prop_urls = re.findall(r'"(https://www\.portalzuk\.com\.br/leilao-de-imoveis/[^"]{20,})"', flat)
            for u in set(prop_urls):
                add_row(rows, seen, "Portal Zuk", u)
            print(f"  __NEXT_DATA__: {len(set(prop_urls))} URLs")
        except Exception as e:
            print(f"  __NEXT_DATA__ erro: {e}")

    # APIs interceptadas
    for hit in api_hits:
        try:
            data = json.loads(hit["body"])
            flat = json.dumps(data, ensure_ascii=False)
            prop_urls = re.findall(r'"(https://www\.portalzuk\.com\.br/leilao-de-imoveis/[^"]{20,})"', flat)
            for u in set(prop_urls):
                add_row(rows, seen, "Portal Zuk", u)
            if prop_urls:
                print(f"  API {hit['url'][:60]}: {len(set(prop_urls))} URLs")
        except: pass

    # Fallback: scrape HTML vendor pages
    vendor_urls = list(set(re.findall(
        r'href="(https://www\.portalzuk\.com\.br/leilao-de-imoveis/v/[^"]+)"', html)))
    await ctx.close()

    added_before = len(rows)
    for vurl in vendor_urls[:50]:
        ctx2 = await browser.new_context(user_agent=UA)
        page2 = await ctx2.new_page()
        try:
            await page2.goto(vurl, timeout=20000)
            await asyncio.sleep(2)
            html2 = await page2.content()
            props = re.findall(r'href="(/leilao-de-imoveis/v/[^"]+/\d+[^"]*?)"', html2)
            for pp in set(props):
                add_row(rows, seen, "Portal Zuk", BASE + pp)
        except: pass
        finally:
            await ctx2.close()
        await asyncio.sleep(0.8)

    print(f"  Portal Zuk total: {len(rows) - added_before} novos")


# ─────────────────────────────────────────────────────────────────
# 3. LANCE NO LEILÃO — busca publica de imoveis
# ─────────────────────────────────────────────────────────────────

async def scrape_lance_no_leilao(browser, rows, seen):
    BASE = "https://www.lancenoleilao.com.br"
    print("\n[Lance no Leilão] buscando endpoint público")

    # Intercepta todas as chamadas XHR
    api_hits = []
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()

    async def on_resp(resp):
        if "json" in resp.headers.get("content-type","") and "lancenoleilao" in resp.url:
            try:
                body = await resp.text()
                if len(body) > 50:
                    api_hits.append({"url": resp.url, "body": body[:500]})
            except: pass

    page.on("response", on_resp)
    try:
        await page.goto(BASE, timeout=50000)
        await asyncio.sleep(8)
        title = await page.title()
        print(f"  Home title: {title}, URL: {page.url}")
    except Exception as e:
        print(f"  ERRO: {e}")
    finally:
        await ctx.close()

    if api_hits:
        print(f"  APIs interceptadas: {len(api_hits)}")
        for h in api_hits[:3]:
            print(f"    {h['url'][:80]}: {h['body'][:150]}")
    else:
        print("  Lance no Leilão: requer login ou sem API pública acessível")


# ─────────────────────────────────────────────────────────────────
# 4. MILAN LEILÕES — Cloudflare (tenta cf_clearance via sessão)
# ─────────────────────────────────────────────────────────────────

async def scrape_milan(browser, rows, seen):
    CF_AUTH = "cf_milan_auth.json"
    BASE = "https://www.milanleiloes.com.br"
    print("\n[Milan Leilões] Cloudflare — tentando com nova sessão")

    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    try:
        await page.goto(BASE, timeout=40000)
        await asyncio.sleep(10)  # espera Cloudflare JS challenge resolver
        title = await page.title()
        cf_blocked = "just a moment" in title.lower()
        print(f"  Title: {title[:60]}, CF blocked: {cf_blocked}")

        if not cf_blocked:
            html = await page.content()
            prop_links = re.findall(r'href="([^"]*(?:imovel|lote|bem|leilao)[^"]{10,})"', html, re.I)
            for lk in set(prop_links):
                add_row(rows, seen, "Milan Leilões", abs_url(BASE, lk))
            print(f"  Milan: {len(set(prop_links))} links encontrados")
        else:
            print("  Milan: CF blocked — necessário resolver manualmente (ver seção 13.5 do guia)")
    except Exception as e:
        print(f"  Milan ERRO: {e}")
    finally:
        await ctx.close()


# ─────────────────────────────────────────────────────────────────
# 5. GRUPO LANCE — completa todas as páginas
# ─────────────────────────────────────────────────────────────────

async def scrape_grupo_lance_complete(browser, rows, seen):
    BASE = "https://www.grupolance.com.br"
    print("\n[Grupo Lance] completando todas as páginas")
    added = 0
    for pg in range(1, 30):
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        try:
            await page.goto(f"{BASE}/imoveis?pagina={pg}", timeout=30000)
            await asyncio.sleep(2.5)
            title = await page.title()
            if "just a moment" in title.lower(): break
            els = await page.query_selector_all('a[href*="/imoveis/"]')
            batch = []
            for el in els:
                h = await el.get_attribute("href") or ""
                if h.count("/") >= 5:
                    url = abs_url(BASE, h)
                    # extrai cidade/estado do slug
                    parts = h.strip("/").split("/")
                    estado = parts[-3].upper() if len(parts) >= 3 else ""
                    cidade = parts[-2].replace("-"," ").title() if len(parts) >= 2 else ""
                    batch.append((url, cidade, estado))
            new = [(u,c,e) for u,c,e in batch if u not in seen]
            if not new: break
            for url, cidade, estado in new:
                add_row(rows, seen, "Grupo Lance", url, cidade=cidade, estado=estado)
                added += 1
            print(f"  p{pg}: +{len(new)} (total Grupo Lance={added})")
        except Exception as e:
            print(f"  p{pg} ERRO: {e}"); break
        finally:
            await ctx.close()
        await asyncio.sleep(1.2)
    print(f"  Grupo Lance: {added} novos total")


# ─────────────────────────────────────────────────────────────────
# 6. SOLD LEILÕES — completa paginação
# ─────────────────────────────────────────────────────────────────

async def scrape_sold_complete(browser, rows, seen):
    BASE = "https://www.sold.com.br"
    print("\n[Sold Leilões] completando paginação")
    added = 0
    for pg in range(1, 150):
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        try:
            await page.goto(f"{BASE}/h/imoveis?searchType=opened&pageNumber={pg}&pageSize=30", timeout=30000)
            await asyncio.sleep(2.5)
            els = await page.query_selector_all('a[href*="/oferta/"]')
            batch = set()
            for el in els:
                h = await el.get_attribute("href") or ""
                if h: batch.add(abs_url(BASE, h))
            new = [u for u in batch if u not in seen]
            if not new: break
            for u in new:
                add_row(rows, seen, "Sold Leilões", u)
                added += 1
            print(f"  p{pg}: +{len(new)} (total={added})")
        except Exception as e:
            print(f"  p{pg} ERRO: {e}"); break
        finally:
            await ctx.close()
        await asyncio.sleep(1)
    print(f"  Sold: {added} novos total")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

async def main():
    all_rows, existing_urls = load_existing()
    print(f"Já no sistema: {len(existing_urls)}")
    seen = set(existing_urls)

    # 1. Central Sul (API pura — sem browser)
    scrape_central_sul(all_rows, seen)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # 2. Portal Zuk
        await scrape_portal_zuk(browser, all_rows, seen)

        # 3. Lance no Leilão
        await scrape_lance_no_leilao(browser, all_rows, seen)

        # 4. Milan Leilões
        await scrape_milan(browser, all_rows, seen)

        # 5. Grupo Lance (completo)
        await scrape_grupo_lance_complete(browser, all_rows, seen)

        # 6. Sold (completo)
        await scrape_sold_complete(browser, all_rows, seen)

        await browser.close()

    save_all(all_rows)
    print(f"\nSalvo em {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
