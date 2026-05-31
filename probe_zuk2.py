"""Analisa HTML do Portal Zuk para encontrar dados embutidos de imóveis."""
import asyncio, sys, re, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()

        await page.goto(
            "https://www.portalzuk.com.br/leilao-de-imoveis/v/porto-seguro-companhia-de-seguros-gerais",
            timeout=30000)
        await asyncio.sleep(5)
        html = await page.content()

        # Scripts com dados de imóveis
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
        for i, s in enumerate(scripts):
            low = s.lower()
            if any(k in low for k in ["imovel","lote","leilao","property","listing","item","produto"]) and len(s) > 100:
                print(f"Script {i} len={len(s)}:")
                print(s[:600])
                print("---")

        # data- attributes
        data_attrs = re.findall(r'data-(?:url|href|id|lote|imovel|slug)=["\']([^"\']+)["\']', html, re.I)
        print(f"\ndata-* attributes: {len(data_attrs)}")
        for d in data_attrs[:10]:
            print(f"  {d[:100]}")

        # Links individuais com padrão numérico (propriedades)
        zuk_props = [l for l in set(re.findall(r'href="(https?://[^"]+)"', html))
                     if "portalzuk" in l and "/v/" in l and re.search(r"/\d+", l)]
        print(f"\nProp links (com ID numérico): {len(zuk_props)}")
        for l in zuk_props[:10]:
            print(f"  {l}")

        # Tenta avaliar JS para obter dados de propriedades
        try:
            listing_data = await page.evaluate("""() => {
                const data = {};
                // Tenta window.__data, window.store, window.initialData etc
                for (const k of ['__data','__state','__initialProps','__INITIAL_STATE__','store','listings','imoveis']) {
                    if (window[k]) data[k] = JSON.stringify(window[k]).substring(0, 500);
                }
                return data;
            }""")
            if listing_data:
                print("\nWindow data:", json.dumps(listing_data, ensure_ascii=False)[:500])
        except Exception as e:
            print(f"JS eval: {e}")

        await ctx.close()
        await browser.close()


asyncio.run(main())
