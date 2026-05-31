"""Intercepta TODOS os requests do Portal Zuk para encontrar API interna."""
import asyncio, sys, re, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()

        all_requests = []

        async def on_req(req):
            if "portalzuk" in req.url and req.url not in all_requests:
                all_requests.append(req.url)

        async def on_resp(resp):
            if "portalzuk" in resp.url:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        body = await resp.json()
                        print(f"\nJSON: {resp.url[:100]}")
                        print(json.dumps(body, ensure_ascii=False)[:400])
                    except:
                        pass

        page.on("request", on_req)
        page.on("response", on_resp)

        # Visita agenda e tenta clicar em um leilão
        await page.goto("https://www.portalzuk.com.br/agenda/leiloes", timeout=30000)
        await asyncio.sleep(5)

        # Tenta clicar em qualquer elemento que pareça ser um leilão
        links = await page.query_selector_all("a[href]")
        clicked = 0
        for link in links[:20]:
            href = await link.get_attribute("href") or ""
            if "leilao" in href.lower() or "imovel" in href.lower():
                try:
                    await link.click()
                    await asyncio.sleep(3)
                    clicked += 1
                    if clicked >= 3:
                        break
                except:
                    pass

        # Também testa o ajax-lote endpoint do robots.txt
        ajax_tests = [
            "https://www.portalzuk.com.br/ajax-lote?id=1",
            "https://www.portalzuk.com.br/ajax-lotes?page=1",
        ]
        for url in ajax_tests:
            try:
                r = await page.evaluate(f"""
                    fetch('{url}', {{headers: {{'Accept': 'application/json'}}}})
                    .then(r => r.text()).catch(e => 'ERROR: ' + e)
                """)
                print(f"\nAJAX test {url}:")
                print(r[:300] if r else "empty")
            except Exception as e:
                print(f"  {e}")

        print(f"\nTotal requests interceptados: {len(all_requests)}")
        for r in all_requests:
            print(f"  {r[:120]}")

        await ctx.close()
        await browser.close()


asyncio.run(main())
