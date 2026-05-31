"""Coleta Porto Leiloes, Luis Leiloeiro, Portal Zuk e Rodolfo Schontag."""
import asyncio, csv, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

async def get_html(browser, url, wait=3.0):
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    html = ""
    try:
        await page.goto(url, timeout=35000)
        await asyncio.sleep(wait)
        title = await page.title()
        if "just a moment" not in title.lower():
            html = await page.content()
    except Exception as e:
        print(f"  ERRO {url[:60]}: {e}")
    finally:
        await ctx.close()
    return html


async def main():
    # URLs já coletadas
    existing = set()
    all_rows = []
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

        # ── Porto Leilões ─────────────────────────────────────────
        print("\n[Porto Leilões]")
        BASE_P = "https://www.portoleiloes.com.br"

        # Descobrir slugs de eventos na home e na listagem
        html_home = await get_html(browser, BASE_P)
        html_list = await get_html(browser, f"{BASE_P}/eventos?tipo=leilao")
        event_raw = re.findall(r"/eventos/leilao/(\d+)/([^\"\'<>\s&]+)",
                               html_home + html_list)
        event_urls = list({f"/eventos/leilao/{eid}/{eslug}"
                           for eid, eslug in event_raw})
        print(f"  Eventos: {len(event_urls)}")

        for ev_path in event_urls[:15]:
            html_ev = await get_html(browser, BASE_P + ev_path)
            lotes = re.findall(
                r"portoleiloes\.com\.br(/eventos/leilao/[^&\"\'<>\s]+/lote/\d+/lote)",
                html_ev)
            for lu in set(lotes):
                add("Porto Leilões", BASE_P + lu)
            print(f"  {ev_path}: {len(set(lotes))} lotes")
            await asyncio.sleep(1.2)

        # ── Luis Leiloeiro ────────────────────────────────────────
        print("\n[Luis Leiloeiro]")
        BASE_L = "https://www.luisleiloeiro.com.br"
        html_luis = await get_html(browser, BASE_L)
        luis_links = re.findall(r"href=\"(/leilao/[^\"]+(?:/online|/lote_id/\d+)[^\"]*?)\"",
                                html_luis)
        for lk in set(luis_links):
            add("Luis Leiloeiro", BASE_L + lk)
        print(f"  Luis eventos/lotes: {sum(1 for r in new_rows if r['leiloeiro']=='Luis Leiloeiro')}")

        # ── Portal Zuk ─────────────────────────────────────────────
        print("\n[Portal Zuk]")
        BASE_Z = "https://www.portalzuk.com.br"
        html_zuk = await get_html(browser, f"{BASE_Z}/leilao-de-imoveis")
        vendor_urls = list(set(re.findall(
            r"href=\"(https://www\.portalzuk\.com\.br/leilao-de-imoveis/v/[^\"]+)\"",
            html_zuk)))
        print(f"  Vendors: {len(vendor_urls)}")

        for vurl in vendor_urls[:40]:
            html_v = await get_html(browser, vurl, wait=2.5)
            # Propriedades individuais têm ID numérico no final do path
            props = re.findall(
                r"href=\"(/leilao-de-imoveis/v/[^\"]+/\d+[^\"]*?)\"", html_v)
            for pp in set(props):
                add("Portal Zuk", BASE_Z + pp)
            await asyncio.sleep(0.8)

        zuk_total = sum(1 for r in new_rows if r["leiloeiro"] == "Portal Zuk")
        print(f"  Portal Zuk propriedades: {zuk_total}")

        # ── Rodolfo Schontag ──────────────────────────────────────
        print("\n[Rodolfo Schontag]")
        BASE_R = "https://www.leiloeiropublico.com.br"
        html_rod = await get_html(browser, BASE_R)
        leilao_hrefs = re.findall(r"href=\"(ListagemLote\.aspx\?Leilao=[^\"]+)\"", html_rod)
        print(f"  Leilões: {len(leilao_hrefs)}")

        for lh in leilao_hrefs[:20]:
            lh_url = f"{BASE_R}/{lh}"
            html_l = await get_html(browser, lh_url)
            # busca qualquer link de detalhe/lote
            dets = re.findall(r"href=\"([^\"]*(?:Detalhes|detalhe|bem|Bem|lote)[^\"]{4,})\"",
                              html_l, re.I)
            for d in set(dets):
                full = (BASE_R + "/" + d.lstrip("/")
                        if not d.startswith("http") else d)
                add("Rodolfo Schontag", full)
            await asyncio.sleep(1)

        rod_total = sum(1 for r in new_rows if r["leiloeiro"] == "Rodolfo Schontag")
        print(f"  Rodolfo lotes: {rod_total}")

        await browser.close()

    print(f"\nNovas ofertas: {len(new_rows)}")

    # Mescla e salva
    all_rows.extend(new_rows)
    fieldnames = ["leiloeiro", "url", "titulo", "cidade", "estado", "preco", "duplicado"]
    with open("ofertas_leiloeiros.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)

    print(f"CSV final: {len(all_rows)} ofertas")
    from collections import Counter
    for name, n in Counter(r["leiloeiro"] for r in all_rows).most_common():
        print(f"  {n:4d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
