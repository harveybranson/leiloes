"""
scraper_trt2.py — Scraper genérico para os 60 leiloeiros judiciais TRT-2/SP
Referência: captura_dados_leiloes_v2.md (seções 2, 3, 5, 7)

Fonte: PDF "Relação de Endereços dos Leiloeiros Oficiais CREDENCIADOS"
       Tribunal Regional do Trabalho da 2ª Região — São Paulo

Estratégia (seção 11 do guia):
  1. Para cada site, testa caminhos comuns de listagem de imóveis
  2. Extrai links de propriedades encontrados nas páginas de listagem
  3. Extrai info básica dos cards (título, preço, cidade, estado, URL)
  4. Salva progresso a cada site para retomada
  5. Sites já cobertos pelo scraper_completo.py são pulados

Uso:
  python scraper_trt2.py          # retoma de onde parou
  python scraper_trt2.py --reset  # reinicia do zero
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INPUT_CSV    = "leiloeiros_trt2.csv"
OUTPUT_CSV   = "ofertas_trt2.csv"
PROGRESS_FILE= "scraper_trt2_progress.json"
CONCURRENCY  = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Sites já cobertos pelo scraper_completo.py — pular ou marcar como conhecido
KNOWN_SITES = {
    "megaleiloes.com.br":   "Mega Leilões",
    "grupolance.com.br":    "Grupo Lance",
    "sold.com.br":          "Sold Leilões",
    "frazaoleiloes.com.br": "Frazão Leilões",
    "francoleiloes.com.br": "Franco Leilões",
    "portalzuk.com.br":     "Portal Zuk",
    "milanleiloes.com.br":  "Milan Leilões",
    "centralsuldeleiloes.com.br": "Central Sul de Leilões",
}

# Caminhos a tentar para encontrar listagem de imóveis
LISTING_PATHS = [
    "/imoveis",
    "/leilao-imoveis",
    "/leiloes/imoveis",
    "/proximos-leiloes",
    "/proximos_leiloes",
    "/lotes",
    "/leiloes",
    "/catalogo",
    "/bens",
    "/hastas",
    "/leilao",
    "",
]

# Seletores de links de propriedades (do mais específico ao genérico)
PROP_SELECTORS = [
    "a[href*='/imovel']",
    "a[href*='/lote/']",
    "a[href*='/lotes/']",
    "a[href*='/leilao/imovel']",
    "a[href*='/produto']",
    "a[href*='/bem/']",
    "a[href*='/hasta']",
    ".property a", ".lote a", ".item-leilao a",
    ".card-lote a", ".lote-card a", ".product-card a",
    "article a[href]", ".listing-item a",
]

FIELDNAMES = [
    "fonte", "leiloeiro_nome", "leiloeiro_site",
    "url", "titulo", "cidade", "estado",
    "preco", "avaliacao", "desconto_pct",
    "tipo_imovel", "imagem_url",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_price(text: str) -> float:
    t = re.sub(r"[^\d,]", "", text or "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def classify_tipo(text: str) -> str:
    t = (text or "").lower()
    for kws, tipo in [
        (["apartamento","apto"],   "apartamento"),
        (["casa","sobrado"],       "casa"),
        (["terreno","lote "],      "terreno"),
        (["galpão","galpao"],      "galpao"),
        (["sala","loja","comercial"], "comercial"),
        (["rural","fazenda"],      "rural"),
    ]:
        if any(k in t for k in kws):
            return tipo
    return "outro"


def domain(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h.replace("www.", "")


def abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(base, href)


# ── Progress / CSV ────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_sites": [], "rows": []}


def save_progress(prog: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False)


def save_csv(rows: list):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_leiloeiros() -> list[dict]:
    with open(INPUT_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Extrator genérico de cards de imóveis ────────────────────────────────────

async def extract_cards(page, base_url: str, leiloeiro: dict) -> list[dict]:
    """Extrai cards de propriedades da página atual."""
    rows = []
    seen = set()
    html = await page.content()

    # Tenta cada seletor de propriedade
    hrefs = []
    for sel in PROP_SELECTORS:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                href = await el.get_attribute("href") or ""
                if href:
                    hrefs.append(href)
        except Exception:
            pass
        if len(hrefs) >= 5:
            break

    # Fallback: regex no HTML para links com padrões típicos
    if not hrefs:
        hrefs = re.findall(
            r'href=["\']([^"\']*(?:imovel|lote|hasta|bem|produto)[^"\']*)["\']',
            html, re.I
        )

    for href in hrefs:
        url = abs_url(base_url, href)
        if not url or url in seen:
            continue
        # Filtra links de navegação (sem ID numérico = provavelmente menu)
        if not re.search(r'\d', url):
            continue
        seen.add(url)

        # Tenta extrair info do card via contexto HTML próximo ao link
        # (busca o fragmento HTML em torno do href)
        frag_m = re.search(
            rf'href=["\']?{re.escape(href)}["\']?.{{0,2000}}',
            html, re.S
        )
        frag = frag_m.group(0) if frag_m else ""

        # Título
        titulo = ""
        for pat in [r'<h[123][^>]*>([^<]{5,})</h', r'alt=["\']([^"\']{5,})["\']',
                    r'title=["\']([^"\']{5,})["\']', r'<p[^>]*>([A-ZÁÉÍÓÚ][^<]{10,})</p']:
            tm = re.search(pat, frag, re.I | re.S)
            if tm:
                titulo = re.sub(r'\s+', ' ', tm.group(1)).strip()[:200]
                break

        # Preço
        preco = 0.0
        pm = re.search(r'R[\$]\s*([\d.,]{4,})', frag)
        if pm:
            preco = parse_price(pm.group(1))

        # Localização
        cidade, estado = "", ""
        loc_m = re.search(r'([A-ZÁ-Ú][a-záéíóúâêîôûãõ\s]+)[/\-–]\s*([A-Z]{2})\b', frag)
        if loc_m:
            cidade = loc_m.group(1).strip()[:100]
            estado = loc_m.group(2).upper()

        # Imagem
        img = ""
        img_m = re.search(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']', frag, re.I)
        if img_m and img_m.group(1).startswith("http"):
            img = img_m.group(1)

        rows.append({
            "fonte":           domain(base_url),
            "leiloeiro_nome":  leiloeiro.get("nome", ""),
            "leiloeiro_site":  leiloeiro.get("site", ""),
            "url":             url,
            "titulo":          titulo,
            "cidade":          cidade,
            "estado":          estado,
            "preco":           preco,
            "avaliacao":       0.0,
            "desconto_pct":    0.0,
            "tipo_imovel":     classify_tipo(titulo),
            "imagem_url":      img,
        })

    return rows


async def paginate_site(page, base_url: str, listing_url: str,
                        leiloeiro: dict) -> list[dict]:
    """Visita a página de listagem e pagina até N páginas ou até ficar vazio."""
    all_rows = []
    seen_urls = set()
    MAX_PAGES = 20

    for pg in range(1, MAX_PAGES + 1):
        # Monta URL paginada (tenta vários padrões)
        if pg == 1:
            url = listing_url
        else:
            sep = "&" if "?" in listing_url else "?"
            # Tenta parâmetros comuns de paginação
            for pg_param in [f"pagina={pg}", f"page={pg}", f"pag={pg}", f"p={pg}"]:
                url = f"{listing_url}{sep}{pg_param}"
                break

        try:
            resp = await page.goto(url, timeout=25000, wait_until="domcontentloaded")
            if resp and resp.status >= 400:
                break
            await asyncio.sleep(1.5)

            cards = await extract_cards(page, base_url, leiloeiro)
            new = [c for c in cards if c["url"] not in seen_urls]
            if not new:
                break

            for c in new:
                seen_urls.add(c["url"])
            all_rows.extend(new)
            print(f"    p{pg}: +{len(new)} (total={len(all_rows)})")

        except Exception as e:
            print(f"    ERRO p{pg}: {e}")
            break

    return all_rows


async def scrape_site(browser, leiloeiro: dict) -> list[dict]:
    """Tenta encontrar e raspar as listagens de imóveis de um site."""
    site = leiloeiro.get("site", "").rstrip("/")
    nome = leiloeiro.get("nome", "")
    d = domain(site)

    # Pula sites já cobertos pelo scraper_completo.py
    if d in KNOWN_SITES:
        print(f"  SKIP (já no scraper_completo): {nome} → {d}")
        return []

    print(f"\n  Scraping: {nome} | {site}")
    ctx = await browser.new_context(
        user_agent=UA, locale="pt-BR",
        extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
    )
    page = await ctx.new_page()
    page.set_default_timeout(20000)
    all_rows = []

    try:
        # Testa cada caminho de listagem
        for path in LISTING_PATHS:
            listing_url = site + path
            try:
                resp = await page.goto(listing_url, timeout=20000,
                                       wait_until="domcontentloaded")
                if not resp or resp.status >= 400:
                    continue
                await asyncio.sleep(1.5)

                # Verifica se a página tem conteúdo relevante de imóveis
                html = await page.content()
                keywords = ["imóvel","imovel","lote","leilão","leilao","hasta",
                            "arrematação","arremate","lance"]
                if not any(k in html.lower() for k in keywords):
                    continue

                # Encontrou página relevante — extrai e pagina
                print(f"    Encontrado: {listing_url}")
                rows = await paginate_site(page, site, listing_url, leiloeiro)
                if rows:
                    all_rows.extend(rows)
                    break  # usa a primeira listagem que funcionou

            except Exception as e:
                print(f"    {path}: {type(e).__name__}")
                continue

        if not all_rows:
            print(f"    Sem imóveis encontrados em {nome}")

    finally:
        await ctx.close()

    return all_rows


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(reset: bool):
    leiloeiros = load_leiloeiros()
    total_sites = len(leiloeiros)

    if reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()

    prog      = load_progress()
    done      = set(prog["done_sites"])
    rows_out  = prog["rows"]

    pending = [l for l in leiloeiros if l["site"] not in done]
    print(f"Total: {total_sites} sites | Já feitos: {len(done)} | Pendentes: {len(pending)}")
    print(f"Imóveis coletados até agora: {len(rows_out)}")

    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i, leiloeiro in enumerate(pending):
            site = leiloeiro["site"]
            print(f"\n[{len(done)+1}/{total_sites}] {leiloeiro['nome']}")

            async with sem:
                rows = await scrape_site(browser, leiloeiro)

            rows_out.extend(rows)
            done.add(site)
            prog["done_sites"] = list(done)
            prog["rows"] = rows_out
            save_progress(prog)
            save_csv(rows_out)

            pct = round(len(done) / total_sites * 100, 1)
            print(f"  → {len(rows)} imóveis | Total: {len(rows_out)} | {pct}%")

        await browser.close()

    # Resumo
    from collections import Counter
    print(f"\n{'='*60}")
    print(f"CONCLUÍDO — {OUTPUT_CSV}: {len(rows_out)} imóveis de {len(done)} sites")
    cnt = Counter(r["fonte"] for r in rows_out)
    for k, v in cnt.most_common(20):
        print(f"  {v:4d}  {k}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(reset=args.reset))
