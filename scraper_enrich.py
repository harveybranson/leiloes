"""
scraper_enrich.py — enriquece preço, cidade, estado em ofertas_leiloeiros.csv

Fases:
  0. Corrige URLs com HTML entities (&amp; → &)
  1. Extrai cidade/estado de padrões de URL (sem browser)
  2. Expande Portal Zuk: vendor pages → lotes individuais
  3. Visita páginas de detalhe para preços (Playwright + requests)

Progresso salvo em enrich_progress.json para retomada segura.
"""

import asyncio
import csv
import json
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

INPUT_FILE    = "ofertas_leiloeiros.csv"
OUTPUT_FILE   = "ofertas_leiloeiros.csv"
PROGRESS_FILE = "enrich_progress.json"

FIELDNAMES = [
    "leiloeiro", "url", "titulo", "cidade", "estado",
    "preco", "avaliacao", "desconto_pct", "duplicado", "url_externa",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────────────
# I/O helpers
# ─────────────────────────────────────────────────────────────────

def load_rows() -> list[dict]:
    with open(INPUT_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(rows: list[dict]):
    seen, deduped = set(), []
    for r in rows:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            deduped.append(r)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)
    print(f"  -> Salvo: {len(deduped)} linhas em {OUTPUT_FILE}")


def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_urls": [], "zuk_expanded": False}


def save_progress(prog: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────
# Fase 0 — corrige HTML entities nas URLs
# ─────────────────────────────────────────────────────────────────

def fix_urls(rows: list[dict]) -> int:
    fixed = 0
    for r in rows:
        clean = unescape(r.get("url", ""))
        if clean != r["url"]:
            r["url"] = clean
            fixed += 1
    return fixed


# ─────────────────────────────────────────────────────────────────
# Fase 1 — extrai cidade/estado de padrões de URL
# ─────────────────────────────────────────────────────────────────

def extract_city_state_from_url(url: str) -> tuple[str, str]:
    """Returns (cidade, estado) from URL slug; empty strings if not found."""

    # Mega Leilões: /imoveis/{tipo}/{estado:2}/{cidade}/{slug}
    m = re.search(r"megaleiloes\.com\.br/imoveis/[^/]+/([a-z]{2})/([^/?#]+)", url)
    if m:
        return m.group(2).replace("-", " ").title(), m.group(1).upper()

    # Grupo Lance: /imoveis/{tipo}/{estado:2}/{cidade}/{slug}
    m = re.search(r"grupolance\.com\.br/imoveis/[^/]+/([a-z]{2})/([^/?#]+)", url)
    if m:
        return m.group(2).replace("-", " ").title(), m.group(1).upper()

    # Sold Leilões: /oferta/{slug}-{estado:2}-{id5+}
    m = re.search(r"sold\.com\.br/oferta/.+-([a-z]{2})-(\d{5,})$", url)
    if m:
        estado = m.group(1).upper()
        slug_no_suffix = re.sub(r"-[a-z]{2}-\d{5,}$", "", url.split("/oferta/")[-1])
        parts = slug_no_suffix.split("-")
        cidade = " ".join(parts[-2:]).title() if len(parts) >= 2 else parts[-1].title()
        return cidade, estado

    # Frazão: .../lote/{id}-descrição-cidade-uf or similar
    m = re.search(r"frazaoleiloes\.com\.br/lote/\d+-(.+)", url)
    if m:
        slug = m.group(1)
        parts = slug.split("-")
        if len(parts) >= 2 and len(parts[-1]) == 2:
            return parts[-2].title(), parts[-1].upper()

    # Porto Leilões: slug might contain state
    m = re.search(r"portoleiloes\.com\.br/.+/([a-z]{2})/", url)
    if m:
        return "", m.group(1).upper()

    return "", ""


def enrich_city_state(rows: list[dict]) -> int:
    enriched = 0
    for r in rows:
        if r.get("cidade") and r.get("estado"):
            continue
        cidade, estado = extract_city_state_from_url(r["url"])
        if cidade or estado:
            if not r.get("cidade"):
                r["cidade"] = cidade
            if not r.get("estado"):
                r["estado"] = estado
            enriched += 1
    return enriched


# ─────────────────────────────────────────────────────────────────
# Fase 2 — Portal Zuk: expande vendor pages → lotes individuais
# ─────────────────────────────────────────────────────────────────

ZUK_BASE = "https://www.portalzuk.com.br"

async def expand_zuk_vendor(browser, vendor_url: str) -> list[str]:
    """Visita uma vendor page e retorna URLs de lotes individuais."""
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    lote_urls = []
    try:
        await page.goto(vendor_url, timeout=30000)
        await asyncio.sleep(3)
        title = await page.title()
        if "just a moment" in title.lower():
            print(f"    [CF] {vendor_url}")
            return []

        html = await page.content()
        # Individual lote URLs follow /leilao-de-imoveis/v/{vendor}/{id} or /{tipo}/{slug}
        found = re.findall(
            r'href="(/leilao-de-imoveis/[^"]*?/\d+[^"]*?)"',
            html
        )
        # Also catch full URLs
        found += re.findall(
            r'href="(https://www\.portalzuk\.com\.br/leilao-de-imoveis/[^"]+)"',
            html
        )
        for href in set(found):
            full = href if href.startswith("http") else ZUK_BASE + href
            # exclude vendor listing pages themselves
            if re.search(r"/\d+", full):
                lote_urls.append(full)
    except Exception as e:
        print(f"    [ERR] {vendor_url}: {e}")
    finally:
        await ctx.close()
    return list(set(lote_urls))


async def expand_all_zuk(rows: list[dict]) -> list[dict]:
    """Substitui vendor pages por lotes individuais no dataset."""
    vendor_rows = [r for r in rows if re.search(r"portalzuk\.com\.br/leilao-de-imoveis/v/", r["url"])]
    other_rows  = [r for r in rows if r not in vendor_rows]
    already_zuk_urls = {r["url"] for r in rows if "portalzuk" in r["url"] and "/v/" not in r["url"]}

    print(f"\n[Fase 2] Portal Zuk: {len(vendor_rows)} vendor pages → expandindo lotes")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        new_lote_rows = []
        for i, vr in enumerate(vendor_rows, 1):
            lotes = await expand_zuk_vendor(browser, vr["url"])
            added = 0
            for lu in lotes:
                if lu not in already_zuk_urls:
                    already_zuk_urls.add(lu)
                    new_lote_rows.append({
                        "leiloeiro": "Portal Zuk",
                        "url": lu,
                        "titulo": "", "cidade": "", "estado": "",
                        "preco": 0.0, "avaliacao": 0.0, "desconto_pct": 0.0,
                        "duplicado": False, "url_externa": "",
                    })
                    added += 1
            print(f"  [{i}/{len(vendor_rows)}] {vr['url'].split('/')[-1]}: {added} novos lotes")
            await asyncio.sleep(1.5)
        await browser.close()

    print(f"  Portal Zuk: {len(new_lote_rows)} lotes individuais obtidos (vendor pages removidas)")
    return other_rows + new_lote_rows


# ─────────────────────────────────────────────────────────────────
# Fase 3 — Extração de preços nas páginas de detalhe
# ─────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float:
    """Converte 'R$ 1.234.567,89' → 1234567.89"""
    if not text:
        return 0.0
    t = re.sub(r"[^\d,]", "", text.split("R$")[-1] if "R$" in text else text)
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def _prices_from_text(text: str) -> list[float]:
    """Encontra todos os valores R$ num bloco de texto."""
    matches = re.findall(r"R\$\s*[\d.,]+", text)
    vals = []
    for m in matches:
        v = _parse_price(m)
        if v >= 100:  # ignora valores muito pequenos
            vals.append(v)
    return sorted(vals)


def _prices_from_json(flat_json: str) -> list[float]:
    """Extrai valores numéricos de campos de preço em JSON."""
    patterns = [
        r'"(?:price|preco|valorInicial|valorMinimo|lance_inicial|minimum_bid|'
        r'valor|valor_avaliacao|valorAvaliacao|initialValue|startingBid)"\s*:\s*'
        r'(\d+(?:[.,]\d+)?)',
    ]
    vals = []
    for pat in patterns:
        for m in re.findall(pat, flat_json, re.I):
            try:
                v = float(m.replace(",", "."))
                if v >= 100:
                    vals.append(v)
            except ValueError:
                pass
    return sorted(vals)


def scrape_rodolfo_requests(url: str) -> dict:
    """Usa requests (sem browser) para leiloeiropublico.com.br — site SSR."""
    result = {"preco": 0.0, "avaliacao": 0.0, "titulo": "", "cidade": "", "estado": ""}
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20, verify=False)
        if r.status_code != 200:
            return result
        html = r.text

        # Título
        m = re.search(r"<title>([^<]+)</title>", html, re.I)
        result["titulo"] = m.group(1).strip() if m else ""

        # Preço: procura padrões "Lance Mínimo: R$ X" ou "Avaliação: R$ X"
        lance = re.search(r"Lance\s*M[ií]nimo[^R]*R\$\s*([\d.,]+)", html, re.I)
        if lance:
            result["preco"] = _parse_price(lance.group(1))

        aval = re.search(r"Avalia[çc][aã]o[^R]*R\$\s*([\d.,]+)", html, re.I)
        if aval:
            result["avaliacao"] = _parse_price(aval.group(1))

        # Cidade/estado
        loc = re.search(r"Município[^:]*:\s*([^<\n]+)", html, re.I)
        if loc:
            result["cidade"] = loc.group(1).strip()
        uf = re.search(r"\bUF[^:]*:\s*([A-Z]{2})\b", html, re.I)
        if uf:
            result["estado"] = uf.group(1).upper()

        # Fallback: generic price extraction
        if not result["preco"]:
            vals = _prices_from_text(html)
            if vals:
                result["preco"] = vals[0]
                if len(vals) >= 2:
                    result["avaliacao"] = vals[-1]
    except Exception as e:
        result["error"] = str(e)
    return result


async def scrape_detail_browser(browser, url: str) -> dict:
    """Extrai preço/título/localização de uma página de detalhe via Playwright."""
    result = {"preco": 0.0, "avaliacao": 0.0, "titulo": "", "cidade": "", "estado": ""}
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=30000)
        await asyncio.sleep(2.5)

        title = await page.title()
        if "just a moment" in title.lower():
            result["error"] = "cf_blocked"
            return result

        result["titulo"] = title
        html = await page.content()

        # 1. __NEXT_DATA__
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd:
            try:
                flat = nd.group(1)
                vals = _prices_from_json(flat)
                if vals:
                    result["preco"] = vals[0]
                    if len(vals) >= 2:
                        result["avaliacao"] = vals[-1]
            except Exception:
                pass

        # 2. JSON-LD
        if not result["preco"]:
            for tag in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
                try:
                    data = json.loads(tag)
                    flat = json.dumps(data)
                    vals = _prices_from_json(flat)
                    if vals:
                        result["preco"] = vals[0]
                        if len(vals) >= 2:
                            result["avaliacao"] = vals[-1]
                        break
                except Exception:
                    pass

        # 3. Texto visível da página
        if not result["preco"]:
            text = await page.evaluate("document.body.innerText")
            vals = _prices_from_text(text)
            if vals:
                result["preco"] = vals[0]
                if len(vals) >= 2:
                    result["avaliacao"] = vals[-1]

            # Tenta extrair cidade/estado do texto
            if not result["cidade"]:
                loc = re.search(
                    r"(?:Município|Cidade|Localização)[^:]*:\s*([^\n|/,<]{3,40})",
                    text, re.I
                )
                if loc:
                    result["cidade"] = loc.group(1).strip()

            if not result["estado"]:
                uf_m = re.search(r"\b([A-Z]{2})\s*/\s*([A-Z]{2})\b", text)
                if uf_m:
                    result["estado"] = uf_m.group(2)

    except Exception as e:
        result["error"] = str(e)
    finally:
        await ctx.close()
    return result


async def enrich_prices(rows: list[dict], prog: dict):
    """Fase 3: visita páginas de detalhe para preencher preços."""
    done_set = set(prog.get("done_urls", []))

    to_enrich = [
        r for r in rows
        if not float(r.get("preco") or 0)
        and r.get("url")
        and r["url"] not in done_set
    ]

    print(f"\n[Fase 3] {len(to_enrich)} ofertas sem preço para enriquecer")

    # Separa Rodolfo Schontag (usa requests, sem browser)
    rodolfo = [r for r in to_enrich if "leiloeiropublico" in r["url"]]
    browser_list = [r for r in to_enrich if "leiloeiropublico" not in r["url"]]

    # ── Rodolfo Schontag via requests (mais rápido) ──────────────
    if rodolfo:
        import warnings
        warnings.filterwarnings("ignore")
        print(f"\n  [Rodolfo Schontag] {len(rodolfo)} páginas via requests")
        for i, r in enumerate(rodolfo, 1):
            det = scrape_rodolfo_requests(r["url"])
            if det.get("preco"):
                r["preco"]     = det["preco"]
                r["avaliacao"] = det["avaliacao"]
            if det.get("titulo") and not r.get("titulo"):
                r["titulo"] = det["titulo"]
            if det.get("cidade") and not r.get("cidade"):
                r["cidade"] = det["cidade"]
            if det.get("estado") and not r.get("estado"):
                r["estado"] = det["estado"]

            done_set.add(r["url"])
            prog["done_urls"] = list(done_set)

            err = f" [{det.get('error')}]" if det.get("error") else ""
            print(
                f"  [{i}/{len(rodolfo)}] R${det.get('preco',0):,.0f}"
                f" | {r.get('cidade','')} {r.get('estado','')}"
                f"{err}"
            )

            if i % 20 == 0:
                save_rows(rows)
                save_progress(prog)
            time.sleep(0.4)

        save_rows(rows)
        save_progress(prog)

    # ── Demais sites via Playwright ───────────────────────────────
    if browser_list:
        print(f"\n  [Outros sites] {len(browser_list)} páginas via Playwright")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            for i, r in enumerate(browser_list, 1):
                det = await scrape_detail_browser(browser, r["url"])

                if det.get("preco"):
                    r["preco"]     = det["preco"]
                    r["avaliacao"] = det["avaliacao"]
                if det.get("titulo") and not r.get("titulo"):
                    r["titulo"] = det["titulo"]
                if det.get("cidade") and not r.get("cidade"):
                    r["cidade"] = det["cidade"]
                if det.get("estado") and not r.get("estado"):
                    r["estado"] = det["estado"]

                done_set.add(r["url"])
                prog["done_urls"] = list(done_set)

                err = f" [{det.get('error')}]" if det.get("error") else ""
                leil = r.get("leiloeiro", "")[:18]
                print(
                    f"  [{i}/{len(browser_list)}] {leil} | "
                    f"R${det.get('preco',0):,.0f} | "
                    f"{r.get('cidade','')} {r.get('estado','')}"
                    f"{err}"
                )

                if i % 10 == 0:
                    save_rows(rows)
                    save_progress(prog)
                    await asyncio.sleep(0.5)

                await asyncio.sleep(1.5)

            await browser.close()

        save_rows(rows)
        save_progress(prog)


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

async def main(skip_zuk: bool = False, skip_prices: bool = False):
    prog = load_progress()
    rows = load_rows()
    print(f"Carregadas {len(rows)} linhas de {INPUT_FILE}")

    # Fase 0 — corrige HTML entities
    fixed = fix_urls(rows)
    print(f"\n[Fase 0] URLs corrigidas: {fixed}")

    # Fase 1 — cidade/estado por URL
    enriched = enrich_city_state(rows)
    print(f"\n[Fase 1] Cidade/estado extraídos de URL: {enriched} linhas")
    save_rows(rows)

    # Fase 2 — expande Portal Zuk (apenas uma vez)
    if not skip_zuk and not prog.get("zuk_expanded"):
        rows = await expand_all_zuk(rows)
        prog["zuk_expanded"] = True
        save_rows(rows)
        save_progress(prog)
    else:
        print("\n[Fase 2] Portal Zuk já expandido — pulando")

    # Fase 3 — preços via detalhe
    if not skip_prices:
        await enrich_prices(rows, prog)
    else:
        print("\n[Fase 3] Pulando (--skip-prices)")

    # Estatísticas finais
    rows = load_rows()
    with_price = [r for r in rows if float(r.get("preco") or 0) > 0]
    with_city  = [r for r in rows if r.get("cidade")]
    print(f"\n=== Concluído ===")
    print(f"  Total: {len(rows)}")
    print(f"  Com preço: {len(with_price)}")
    print(f"  Com cidade: {len(with_city)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Enriquece preços em ofertas_leiloeiros.csv")
    parser.add_argument("--skip-zuk",    action="store_true", help="Pula expansão Portal Zuk")
    parser.add_argument("--skip-prices", action="store_true", help="Pula scraping de preços")
    parser.add_argument("--reset",       action="store_true", help="Reinicia progresso")
    args = parser.parse_args()

    if args.reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()
        print("Progresso reiniciado.")

    asyncio.run(main(skip_zuk=args.skip_zuk, skip_prices=args.skip_prices))
