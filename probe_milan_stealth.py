"""
Opção B — Playwright + playwright-stealth v2.
apply_stealth_async é chamado na page individual.
"""
import asyncio, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

BASE = "https://www.milanleiloes.com.br"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

stealth = Stealth(navigator_user_agent_override=UA)

def is_blocked(title, html):
    t = (title + html).lower()
    return "just a moment" in t or "enable javascript" in t or "checking your browser" in t

def report_success(html):
    links = re.findall(r'href=["\']([^"\']*(?:imovel|lote|bem|leilao)[^"\']{5,})["\']', html, re.I)
    print(f"  Links relevantes: {len(links)}")
    for l in links[:8]:
        print(f"    {l}")


async def attempt(browser, label, wait=8.0, scroll=False):
    ctx = await browser.new_context(
        user_agent=UA, locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
    )
    page = await ctx.new_page()
    await stealth.apply_stealth_async(page)
    try:
        await page.goto(BASE, timeout=50000, wait_until="domcontentloaded")
        if scroll:
            await asyncio.sleep(4)
            await page.mouse.move(600, 400)
            await page.mouse.wheel(0, 600)
        await asyncio.sleep(wait)
        title = await page.title()
        html  = await page.content()
        blocked = is_blocked(title, html)
        print(f"  [{label}] title={title!r}  blocked={blocked}")
        return not blocked, html
    except Exception as e:
        print(f"  [{label}] ERRO: {e}")
        return False, ""
    finally:
        await ctx.close()


async def main():
    print("=== Opção B: Playwright + Stealth v2 ===\n")

    # ── 1: headless padrão ────────────────────────────────────────
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ok, html = await attempt(browser, "headless", wait=8)
        await browser.close()
    if ok:
        print("\n✓ SUCESSO — headless + stealth!")
        report_success(html); return

    # ── 2: headless + scroll ──────────────────────────────────────
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ok, html = await attempt(browser, "headless+scroll", wait=12, scroll=True)
        await browser.close()
    if ok:
        print("\n✓ SUCESSO — headless + scroll!")
        report_success(html); return

    # ── 3: headed (janela visível, aguarda CF resolver) ───────────
    print("\n  [headed] Abrindo janela visível, aguardando 25s para CF resolver...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ok, html = await attempt(browser, "headed", wait=25, scroll=True)
        await browser.close()
    if ok:
        print("\n✓ SUCESSO — headed!")
        report_success(html); return

    print("\n✗ Opção B falhou. Prosseguir para Opção A (FlareSolverr).")


asyncio.run(main())
