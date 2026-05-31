"""
Scraper for leilaoimovel.com.br
Coleta o link do site oficial do leiloeiro para cada imóvel em leilão.

ESTRATÉGIA (contorno do Cloudflare):
  - Coleta URLs de imóveis a partir das páginas de PRIMEIRO ACESSO acessíveis:
      /leilao/extrajudicial, /encontre-seu-imovel, e /leiloeiro/{slug}
  - Para cada URL de imóvel usa um contexto de browser fresco (sem histórico)
  - Para cada leiloeiro (nome) obtém o site oficial via perfil em /leiloeiro/{slug}
  - Salva resultado em links_leiloeiros.csv (incremental)

COBERTURA ESPERADA: ~150-200 imóveis únicos de leilão.
Para ampliar: adicione URLs de entrada em SEED_PAGES ou LEILOEIRO_SLUGS.
"""

import asyncio
import csv
import json
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://www.leilaoimovel.com.br"
OUTPUT_FILE = "links_leiloeiros.csv"
PROGRESS_FILE = "scraper_progress.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Pages that are accessible without Cloudflare blocking (first page only)
SEED_PAGES = [
    "/leilao/extrajudicial",
    "/encontre-seu-imovel",
]

# Leiloeiro partner slugs (from /leiloeiros-parceiros)
LEILOEIRO_SLUGS = [
    "grupo-lance",
    "lance-no-leilao",
    "luis-leiloeiro",
    "mega-leiloes",
    "porto-leiloes",
    "rodolfo-schontag",
    "sold-leiloes",
]

IGNORE_DOMAINS = {
    "leilaoimovel.com.br",
    "auket.com.br",
    "instagram.com",
    "facebook.com",
    "linkedin.com",
    "twitter.com",
    "youtube.com",
    "whatsapp",
    "google",
    "t.co",
}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def is_external(href: str) -> bool:
    if not href or not href.startswith("http"):
        return False
    return not any(d in href for d in IGNORE_DOMAINS)


def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "collected_urls": [],
        "processed_properties": {},
        "leiloeiro_sites": {},
    }


def save_progress(p: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def save_csv(p: dict):
    rows = []
    for url, prop in p["processed_properties"].items():
        slug = prop.get("leiloeiro_slug") or ""
        site = p["leiloeiro_sites"].get(slug, "")
        rows.append({
            "property_id":    prop.get("id", ""),
            "property_url":   BASE_URL + url,
            "leiloeiro_nome": prop.get("leiloeiro_name", ""),
            "leiloeiro_slug": slug,
            "leiloeiro_site": site,
        })

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "property_id", "property_url",
            "leiloeiro_nome", "leiloeiro_slug", "leiloeiro_site",
        ])
        w.writeheader()
        w.writerows(rows)

    leilao_rows = [r for r in rows if r["leiloeiro_nome"]]
    print(f"  -> CSV salvo: {len(rows)} imóveis ({len(leilao_rows)} com leiloeiro) em {OUTPUT_FILE}")


# ──────────────────────────────────────────────
# Page-level helpers
# ──────────────────────────────────────────────

async def collect_property_urls(browser, url: str) -> list[str]:
    """
    Opens a fresh context, loads `url`, collects all /imovel/ links.
    Accessible pages work on first load without Cloudflare challenge.
    """
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    urls = []
    try:
        await page.goto(url, timeout=30000)
        await asyncio.sleep(random.uniform(3, 5))

        title = await page.title()
        if "just a moment" in title.lower():
            print(f"  [CF-blocked] {url}")
        else:
            links = await page.query_selector_all('a[href*="/imovel/"]')
            seen = set()
            for link in links:
                href = await link.get_attribute("href")
                if href and href not in seen and len(href) > 20:
                    seen.add(href)
                    urls.append(href)
            print(f"  {url.replace(BASE_URL, '')}: {len(urls)} URLs")
    except Exception as e:
        print(f"  [error] {url}: {e}")
    finally:
        await ctx.close()
    return urls


async def scrape_property_page(browser, path: str) -> dict:
    """
    Opens a fresh context and navigates directly to the property page.
    Extracts: id, leiloeiro_name, leiloeiro_slug.
    """
    full_url = BASE_URL + path
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    result = {"url": path, "id": None, "leiloeiro_name": None, "leiloeiro_slug": None}
    try:
        await page.goto(full_url, timeout=25000)
        await asyncio.sleep(random.uniform(2, 4))

        title = await page.title()
        if "just a moment" in title.lower():
            result["error"] = "cf_blocked"
            return result

        html = await page.content()

        # Property ID - from URL
        id_match = re.search(r"-(\d{6,})", path)
        result["id"] = id_match.group(1) if id_match else None

        # Leiloeiro name
        leil_match = re.search(
            r"<b>Leiloeiro:</b>\s*([^<\n]{2,80})", html
        )
        if leil_match:
            name = leil_match.group(1).strip()
            # Remove trailing whitespace/newlines
            name = re.sub(r"\s+", " ", name).strip()
            result["leiloeiro_name"] = name
            result["leiloeiro_slug"] = slugify(name)

    except Exception as e:
        result["error"] = str(e)
    finally:
        await ctx.close()
    return result


async def get_leiloeiro_site(browser, slug: str) -> str:
    """
    Opens a fresh context and visits /leiloeiro/{slug}.
    Returns the first external link found (the auctioneer's website).
    """
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    site = ""
    try:
        await page.goto(f"{BASE_URL}/leiloeiro/{slug}", timeout=20000)
        await asyncio.sleep(random.uniform(2, 3))

        title = await page.title()
        if "just a moment" in title.lower() or "404" in title or "não encontrada" in title.lower():
            return ""

        links = await page.query_selector_all("a[href]")
        for link in links:
            href = await link.get_attribute("href")
            if is_external(href):
                site = href
                break
    except Exception:
        pass
    finally:
        await ctx.close()
    return site


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

async def main(delay_between: float = 2.0, batch_size: int = 5):
    """
    delay_between : seconds to wait between property page visits
    batch_size    : save progress every N properties
    """
    progress = load_progress()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── Fase 1: Coletar URLs de imóveis ────────────────────────
        print("[Fase 1] Coletando URLs de imóveis das páginas de entrada...")

        existing = set(progress["collected_urls"])

        # Seed pages
        for seed in SEED_PAGES:
            urls = await collect_property_urls(browser, BASE_URL + seed)
            for u in urls:
                if u not in existing:
                    existing.add(u)
                    progress["collected_urls"].append(u)
            await asyncio.sleep(random.uniform(1, 2))

        # Leiloeiro profile pages
        for slug in LEILOEIRO_SLUGS:
            urls = await collect_property_urls(browser, f"{BASE_URL}/leiloeiro/{slug}")
            for u in urls:
                if u not in existing:
                    existing.add(u)
                    progress["collected_urls"].append(u)
            await asyncio.sleep(random.uniform(1, 2))

        save_progress(progress)
        print(f"\n  Total de URLs coletadas: {len(progress['collected_urls'])}")

        # ── Fase 2: Processar páginas de imóveis ────────────────────
        pending = [
            u for u in progress["collected_urls"]
            if u not in progress["processed_properties"]
        ]
        print(f"\n[Fase 2] Processando {len(pending)} imóveis (fresh context por imóvel)...")

        for i, url in enumerate(pending, 1):
            result = await scrape_property_page(browser, url)
            progress["processed_properties"][url] = result

            leil = result.get("leiloeiro_name") or "sem leiloeiro"
            err = f" [ERRO: {result.get('error')}]" if result.get("error") else ""
            print(
                f"  [{i}/{len(pending)}] {result.get('id', '?')} | "
                f"{leil}{err} | {url[-50:]}"
            )

            if i % batch_size == 0:
                save_progress(progress)
                save_csv(progress)

            await asyncio.sleep(random.uniform(delay_between, delay_between + 1))

        save_progress(progress)

        # ── Fase 3: Buscar sites de leiloeiros ──────────────────────
        slugs_needed = set()
        for prop in progress["processed_properties"].values():
            slug = prop.get("leiloeiro_slug")
            if slug and slug not in progress["leiloeiro_sites"]:
                slugs_needed.add(slug)

        print(f"\n[Fase 3] Buscando sites de {len(slugs_needed)} leiloeiros...")
        for i, slug in enumerate(sorted(slugs_needed), 1):
            site = await get_leiloeiro_site(browser, slug)
            progress["leiloeiro_sites"][slug] = site
            print(f"  [{i}/{len(slugs_needed)}] {slug} -> {site or 'NAO ENCONTRADO'}")
            save_progress(progress)
            await asyncio.sleep(random.uniform(1, 2))

        # ── Salvar CSV final ─────────────────────────────────────────
        print("\n[Final] Salvando CSV...")
        save_csv(progress)

        await browser.close()

    print(f"\nConcluido! Resultados em {OUTPUT_FILE}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper leilaoimovel.com.br")
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Tempo base de espera entre requisições em segundos (default: 2.0)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=5,
        help="Salvar progresso a cada N imóveis (default: 5)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Apagar progresso anterior e começar do zero",
    )
    args = parser.parse_args()

    if args.reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()
        print("Progresso anterior apagado.")

    asyncio.run(main(delay_between=args.delay, batch_size=args.batch))
