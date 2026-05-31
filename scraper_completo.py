"""
Scraper completo de leilões de imóveis — v3
Referência: captura_dados_leiloes_v2.md

Fontes (em ordem de prioridade: API > Playwright > FlareSolverr):

  1. Central Sul de Leilões  — API REST pura            (~339 lotes)
  2. Mega Leilões            — Playwright, paginação    (~669 lotes)
  3. Grupo Lance             — Playwright, paginação    (~306 lotes)
  4. Sold Leilões            — Playwright, paginação    (~108 lotes)
  5. Portal Zuk              — Playwright, skip         ( ~51 lotes)
  6. Franco Leilões          — Playwright, paginação    ( ~22 lotes)
  7. Frazão Leilões          — Playwright, pág. única   ( ~20 lotes)
  8. Milan Leilões           — FlareSolverr CF-bypass   ( ~20 lotes)

Dependências:
  pip install playwright requests
  playwright install chromium
  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest

Uso:
  python scraper_completo.py                        # roda todos
  python scraper_completo.py --reset                # reinicia do zero
  python scraper_completo.py --only central_sul milan
  python scraper_completo.py --skip milan
"""

import argparse
import asyncio
import csv
import json
import re
import subprocess
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

import requests
from playwright.async_api import async_playwright

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────

OUTPUT_FILE   = "ofertas_completo.csv"
PROGRESS_FILE = "scraper_completo_progress.json"
FLARESOLVERR  = "http://localhost:8191/v1"

FIELDNAMES = [
    "leiloeiro", "url", "titulo", "cidade", "estado",
    "preco", "avaliacao", "desconto_pct", "duplicado",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────
# HELPERS — CSV / progresso / dedup
# ──────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "rows": []}


def save_progress(prog: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


def save_csv(rows: list[dict]):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def add_row(rows: list, seen: set, leiloeiro: str, url: str,
            titulo="", cidade="", estado="",
            preco=0.0, avaliacao=0.0, desconto_pct=0.0):
    if url and url not in seen:
        seen.add(url)
        rows.append({
            "leiloeiro": leiloeiro, "url": url,
            "titulo": titulo, "cidade": cidade, "estado": estado,
            "preco": preco, "avaliacao": avaliacao,
            "desconto_pct": desconto_pct, "duplicado": False,
        })
        return True
    return False


def abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    return href


def parse_price(text: str) -> float:
    """'R$ 1.234.567,89'  →  1234567.89"""
    t = re.sub(r"[^\d,]", "", text or "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def parse_city_state(text: str):
    """'São Paulo / SP'  →  ('São Paulo', 'SP')"""
    if not text:
        return "", ""
    parts = re.split(r"[/\-–]", text)
    cidade = parts[0].strip() if parts else ""
    estado = parts[1].strip()[:2].upper() if len(parts) > 1 else ""
    return cidade, estado


# ──────────────────────────────────────────────────────────────────
# HELPERS — Playwright
# ──────────────────────────────────────────────────────────────────

async def new_page(browser, timeout=25000):
    ctx = await browser.new_context(
        user_agent=UA,
        locale="pt-BR",
        extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
    )
    page = await ctx.new_page()
    page.set_default_timeout(timeout)
    return ctx, page


async def safe_goto(page, url: str, wait: float = 3.0) -> bool:
    try:
        await page.goto(url, timeout=30000)
        await asyncio.sleep(wait)
        title = await page.title()
        return "just a moment" not in title.lower()
    except Exception:
        return False


async def get_hrefs(page, selector: str) -> list[str]:
    els = await page.query_selector_all(selector)
    return [await el.get_attribute("href") or "" for el in els]


# ──────────────────────────────────────────────────────────────────
# HELPERS — FlareSolverr  (seção 13 do guia v2)
# ──────────────────────────────────────────────────────────────────

def _fs_post(cmd: str, **kw) -> dict:
    r = requests.post(FLARESOLVERR, json={"cmd": cmd, **kw}, timeout=120)
    r.raise_for_status()
    return r.json()


def fs_get(url: str, session_id: str, max_timeout: int = 60000) -> dict:
    return _fs_post(
        "request.get", url=url, session=session_id, maxTimeout=max_timeout
    ).get("solution", {})


def ensure_flaresolverr() -> bool:
    """Verifica se FlareSolverr está rodando; tenta iniciar via Docker se não."""
    for _ in range(5):
        try:
            r = requests.get(FLARESOLVERR.replace("/v1", "/"), timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)

    print("  FlareSolverr não encontrado — tentando iniciar container Docker...")
    try:
        result = subprocess.run(
            ["docker", "start", "flaresolverr"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            time.sleep(8)
            return ensure_flaresolverr.__wrapped__()
        # container não existe: criar
        subprocess.run([
            "docker", "run", "-d", "--name", "flaresolverr",
            "-p", "8191:8191",
            "ghcr.io/flaresolverr/flaresolverr:latest",
        ], check=True, timeout=60)
        time.sleep(15)
        return True
    except Exception as e:
        print(f"  Não foi possível iniciar FlareSolverr: {e}")
        print("  Execute manualmente:")
        print("  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return False

# Versão sem recursão infinita
_ensure_fs_inner = ensure_flaresolverr
ensure_flaresolverr.__wrapped__ = lambda: any(
    requests.get(FLARESOLVERR.replace("/v1", "/"), timeout=3).status_code == 200
    for _ in range(1)
)


def is_cf_blocked(html: str) -> bool:
    l = html.lower()
    return "just a moment" in l or "cf_chl_f_tk" in l


# ──────────────────────────────────────────────────────────────────
# 1. CENTRAL SUL DE LEILÕES — API REST (seção 3.2 do guia)
# ──────────────────────────────────────────────────────────────────

def scrape_central_sul(rows: list, seen: set):
    BASE = "https://www.centralsuldeleiloes.com.br"
    hdrs = {"User-Agent": UA}
    print("\n[1/8] Central Sul de Leilões — API REST")

    # Coleta todos os leilões paginando
    auctions, pg = [], 1
    while True:
        try:
            r = requests.get(
                f"{BASE}/api/v2/web/next-auctions",
                params={"page": pg, "per_page": 100, "cache": "true"},
                headers=hdrs, verify=False, timeout=20,
            )
            if r.status_code != 200:
                break
            body = r.json().get("body", {})
            data = body.get("data", [])
            if not data:
                break
            auctions.extend(data)
            total = int(body.get("total", 0))
            print(f"  leilões p{pg}: +{len(data)} (total={len(auctions)}/{total})")
            if len(auctions) >= total:
                break
            pg += 1
        except Exception as e:
            print(f"  ERRO p{pg}: {e}")
            break

    # Para cada leilão, coleta os lotes via API
    added = 0
    for auction in auctions:
        aid = auction.get("id")
        try:
            r2 = requests.get(
                f"{BASE}/api/v2/web/auction/{aid}/lots",
                headers=hdrs, verify=False, timeout=20,
            )
            if r2.status_code != 200:
                continue
            lots = r2.json().get("body", [])
        except Exception:
            continue

        for lot in lots:
            url = lot.get("url", "")
            if not url:
                slug = lot.get("slug", "")
                lid  = lot.get("id", "")
                url  = f"{BASE}/leilao/{aid}/lote/{lid}/{slug}"
            titulo    = lot.get("title", "")
            avaliacao = float(lot.get("value", 0) or 0)
            preco     = float(lot.get("minimum_bid", 0) or 0)
            desconto  = float(lot.get("percentage", 0) or 0)
            loc_text  = auction.get("title", "")
            cidade, estado = parse_city_state(
                loc_text.split(" - ")[0] if " - " in loc_text else loc_text
            )
            if add_row(rows, seen, "Central Sul de Leilões", url,
                       titulo, cidade, estado, preco, avaliacao, desconto):
                added += 1
        time.sleep(0.3)

    print(f"  Central Sul: {added} lotes adicionados")


# ──────────────────────────────────────────────────────────────────
# 2. MEGA LEILÕES — Playwright, paginação
# ──────────────────────────────────────────────────────────────────

async def scrape_megaleiloes(browser, rows: list, seen: set):
    BASE = "https://www.megaleiloes.com.br"
    print("\n[2/8] Mega Leilões — Playwright")
    added = 0

    for pg in range(1, 200):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/imoveis?pagina={pg}")
        if not ok:
            await ctx.close()
            break

        hrefs = await get_hrefs(page, 'a[href*="/imoveis/"]')
        prop  = [h for h in hrefs if h.count("/") >= 5 and not h.rstrip("/").endswith("imoveis")]
        if not prop:
            await ctx.close()
            break

        new_in_page = 0
        for href in set(prop):
            if add_row(rows, seen, "Mega Leilões", abs_url(BASE, href)):
                added += 1
                new_in_page += 1

        print(f"  p{pg}: +{new_in_page} (total={added})")
        await ctx.close()
        await asyncio.sleep(1.2)

    print(f"  Mega Leilões: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 3. GRUPO LANCE — Playwright, paginação
# ──────────────────────────────────────────────────────────────────

async def scrape_grupolance(browser, rows: list, seen: set):
    BASE = "https://www.grupolance.com.br"
    print("\n[3/8] Grupo Lance — Playwright")
    added = 0

    for pg in range(1, 100):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/imoveis?pagina={pg}")
        if not ok:
            await ctx.close()
            break

        hrefs = await get_hrefs(page, 'a[href*="/imoveis/"]')
        prop  = [h for h in hrefs if h.count("/") >= 4 and not h.rstrip("/").endswith("imoveis")]
        if not prop:
            await ctx.close()
            break

        new_in_page = 0
        for href in set(prop):
            url    = abs_url(BASE, href)
            parts  = href.strip("/").split("/")
            estado = parts[-3].upper() if len(parts) >= 3 else ""
            cidade = parts[-2].replace("-", " ").title() if len(parts) >= 2 else ""
            if add_row(rows, seen, "Grupo Lance", url, cidade=cidade, estado=estado):
                added += 1
                new_in_page += 1

        print(f"  p{pg}: +{new_in_page} (total={added})")
        await ctx.close()
        await asyncio.sleep(1.2)

    print(f"  Grupo Lance: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 4. SOLD LEILÕES — Playwright, paginação
# ──────────────────────────────────────────────────────────────────

async def scrape_sold(browser, rows: list, seen: set):
    BASE = "https://www.sold.com.br"
    print("\n[4/8] Sold Leilões — Playwright")
    added = 0

    for pg in range(1, 200):
        ctx, page = await new_page(browser)
        ok = await safe_goto(
            page,
            f"{BASE}/h/imoveis?searchType=opened&pageNumber={pg}&pageSize=30",
        )
        if not ok:
            await ctx.close()
            break

        hrefs = await get_hrefs(page, 'a[href*="/oferta/"]')
        prop  = list(set(hrefs))
        if not prop:
            await ctx.close()
            break

        new_in_page = 0
        for href in prop:
            if add_row(rows, seen, "Sold Leilões", abs_url(BASE, href)):
                added += 1
                new_in_page += 1

        print(f"  p{pg}: +{new_in_page} (total={added})")
        await ctx.close()
        await asyncio.sleep(1.2)

    print(f"  Sold Leilões: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 5. PORTAL ZUK — Playwright, skip
# ──────────────────────────────────────────────────────────────────

async def scrape_portalzuk(browser, rows: list, seen: set):
    BASE = "https://www.portalzuk.com.br"
    print("\n[5/8] Portal Zuk — Playwright")
    added, skip = 0, 0

    while True:
        ctx, page = await new_page(browser)
        url = (f"{BASE}/leilao-de-imoveis?skip={skip}"
               if skip else f"{BASE}/leilao-de-imoveis")
        ok = await safe_goto(page, url)
        if not ok:
            await ctx.close()
            break

        hrefs = await get_hrefs(page, 'a[href*="/leilao-de-imoveis/v/"]')
        prop  = list(set(hrefs))
        if not prop:
            await ctx.close()
            break

        new_in_page = 0
        for href in prop:
            if add_row(rows, seen, "Portal Zuk", abs_url(BASE, href)):
                added += 1
                new_in_page += 1

        print(f"  skip={skip}: +{new_in_page} (total={added})")
        if new_in_page == 0:
            await ctx.close()
            break
        skip += 20
        await ctx.close()
        await asyncio.sleep(1.5)

    print(f"  Portal Zuk: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 6. FRANCO LEILÕES — Playwright, paginação
# ──────────────────────────────────────────────────────────────────

async def scrape_franco(browser, rows: list, seen: set):
    BASE = "https://www.francoleiloes.com.br"
    print("\n[6/8] Franco Leilões — Playwright")
    added = 0

    for pg in range(1, 50):
        ctx, page = await new_page(browser)
        ok = await safe_goto(page, f"{BASE}/proximos_leiloes/{pg}/1/")
        if not ok:
            await ctx.close()
            break

        hrefs = await get_hrefs(page, 'a[href*="/lote/"]')
        prop  = list(set(hrefs))
        if not prop:
            await ctx.close()
            break

        new_in_page = 0
        for href in prop:
            if add_row(rows, seen, "Franco Leilões", abs_url(BASE, href)):
                added += 1
                new_in_page += 1

        print(f"  p{pg}: +{new_in_page} (total={added})")
        if new_in_page == 0:
            await ctx.close()
            break
        await ctx.close()
        await asyncio.sleep(1.2)

    print(f"  Franco Leilões: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 7. FRAZÃO LEILÕES — Playwright, página única
# ──────────────────────────────────────────────────────────────────

async def scrape_frazao(browser, rows: list, seen: set):
    BASE = "https://www.frazaoleiloes.com.br"
    print("\n[7/8] Frazão Leilões — Playwright")
    ctx, page = await new_page(browser)
    added = 0

    if await safe_goto(page, f"{BASE}/leiloes", wait=4):
        hrefs = await get_hrefs(page, 'a[href*="/lote/"]')
        for href in set(hrefs):
            if add_row(rows, seen, "Frazão Leilões", abs_url(BASE, href)):
                added += 1

    await ctx.close()
    print(f"  Frazão Leilões: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# 8. MILAN LEILÕES — FlareSolverr  (seção 13 do guia v2)
# ──────────────────────────────────────────────────────────────────

def _milan_extract_lotes(html: str, lid: str) -> list[str]:
    nums = list(dict.fromkeys(re.findall(rf'/leilao/{lid}/lote/(\w+)', html)))
    return [f"https://www.milanleiloes.com.br/leilao/{lid}/lote/{n}" for n in nums]


def _milan_card_info(card_html: str, url: str) -> dict:
    info = {
        "leiloeiro": "Milan Leilões", "url": url,
        "titulo": "", "cidade": "", "estado": "",
        "preco": 0.0, "avaliacao": 0.0, "desconto_pct": 0.0, "duplicado": False,
    }
    # Título
    t_m = re.search(r'card_lote_titulo[^>]*>([^<]+)', card_html, re.S)
    if t_m:
        info["titulo"] = t_m.group(1).strip()
        cs = re.search(r'^(.+?)\s*[–\-]\s*([A-Z]{2})[.\s–\-]', info["titulo"])
        if cs:
            info["cidade"] = cs.group(1).strip().title()
            info["estado"] = cs.group(2).upper()
    # Preço — "card_lote_lanceMinimo__xxx><span>LANCE MÍNIMO/INICIAL:</span> R$ X"
    # Nota: usar R[\$] em vez de R\$ ($ em Python regex age como âncora de fim)
    p_m = re.search(r'lanceMinimo[^>]*>.*?R[\$]\s*([\d.,]+)', card_html, re.I | re.S)
    if p_m:
        info["preco"] = parse_price(p_m.group(1))
    return info


def scrape_milan(rows: list, seen: set):
    BASE = "https://www.milanleiloes.com.br"
    print("\n[8/8] Milan Leilões — FlareSolverr (CF bypass)")

    if not ensure_flaresolverr():
        print("  AVISO: FlareSolverr indisponível — Milan pulado")
        return

    sid = _fs_post("sessions.create")["session"]
    print(f"  Sessão FS: {sid}")
    added = 0

    try:
        # Home para resolver o CF e coletar IDs de leilões
        sol_home = fs_get(BASE, sid)
        h_home   = sol_home.get("response", "")
        if is_cf_blocked(h_home):
            print("  ERRO: CF ainda bloqueado na home")
            return

        leilao_ids = list(dict.fromkeys(
            re.findall(r'/leilao/imoveis/(\d+)', h_home)
        ))

        # Tenta /imoveis para pegar IDs adicionais
        sol_list = fs_get(f"{BASE}/imoveis", sid, max_timeout=90000)
        h_list   = sol_list.get("response", "")
        for eid in dict.fromkeys(re.findall(r'/leilao/imoveis/(\d+)', h_list)):
            if eid not in leilao_ids:
                leilao_ids.append(eid)

        print(f"  Leilões de imóveis: {leilao_ids}")

        for lid in leilao_ids:
            lurl = f"{BASE}/leilao/imoveis/{lid}"
            sol  = fs_get(lurl, sid)
            h    = sol.get("response", "")
            if is_cf_blocked(h):
                print(f"  leilão {lid}: CF-blocked, pulando")
                continue

            lote_urls = _milan_extract_lotes(h, lid)

            # Retry se SPA renderizou sem lotes
            if not lote_urls:
                time.sleep(3)
                sol2  = fs_get(lurl, sid, max_timeout=90000)
                h     = sol2.get("response", "")
                lote_urls = _milan_extract_lotes(h, lid)

            lote_added = 0
            for lote_url in lote_urls:
                lote_num    = lote_url.split("/lote/")[-1]
                card_pat    = (
                    rf'href=["\']?/leilao/{lid}/lote/{re.escape(lote_num)}["\']?'
                    r'.{0,4000}'
                )
                cm          = re.search(card_pat, h, re.S)
                card_html   = cm.group(0) if cm else h
                info        = _milan_card_info(card_html, lote_url)

                if add_row(rows, seen, "Milan Leilões", lote_url,
                           info["titulo"], info["cidade"], info["estado"],
                           info["preco"]):
                    lote_added += 1
                    added += 1

            print(f"  leilão {lid}: {lote_added} lotes")

    finally:
        _fs_post("sessions.destroy", session=sid)
        print(f"  Sessão FS {sid} destruída")

    print(f"  Milan Leilões: {added} lotes")


# ──────────────────────────────────────────────────────────────────
# ORQUESTRADOR PRINCIPAL
# ──────────────────────────────────────────────────────────────────

ALL_SCRAPERS = [
    "central_sul",
    "megaleiloes",
    "grupolance",
    "sold",
    "portalzuk",
    "franco",
    "frazao",
    "milan",
]

SCRAPER_LABELS = {
    "central_sul": "Central Sul de Leilões",
    "megaleiloes": "Mega Leilões",
    "grupolance":  "Grupo Lance",
    "sold":        "Sold Leilões",
    "portalzuk":   "Portal Zuk",
    "franco":      "Franco Leilões",
    "frazao":      "Frazão Leilões",
    "milan":       "Milan Leilões",
}


async def main(only: list[str] | None = None,
               skip: list[str] | None = None,
               reset: bool = False):

    if reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()

    prog = load_progress()
    rows: list[dict] = prog.get("rows", [])
    seen: set = {r["url"] for r in rows}

    targets = [s for s in ALL_SCRAPERS
               if (not only or s in only)
               and (not skip or s not in skip)
               and s not in prog.get("done", [])]

    if not targets:
        print("Nenhum scraper pendente.")
    else:
        print(f"Scrapers a executar: {targets}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for name in targets:
            print(f"\n{'='*60}")
            before = len(rows)

            try:
                if name == "central_sul":
                    scrape_central_sul(rows, seen)

                elif name == "megaleiloes":
                    await scrape_megaleiloes(browser, rows, seen)

                elif name == "grupolance":
                    await scrape_grupolance(browser, rows, seen)

                elif name == "sold":
                    await scrape_sold(browser, rows, seen)

                elif name == "portalzuk":
                    await scrape_portalzuk(browser, rows, seen)

                elif name == "franco":
                    await scrape_franco(browser, rows, seen)

                elif name == "frazao":
                    await scrape_frazao(browser, rows, seen)

                elif name == "milan":
                    scrape_milan(rows, seen)

            except Exception as e:
                print(f"  ERRO em {name}: {e}")

            gained = len(rows) - before
            print(f"  +{gained} lotes nesta etapa | acumulado={len(rows)}")

            prog["done"] = prog.get("done", []) + [name]
            prog["rows"] = rows
            save_progress(prog)
            save_csv(rows)

        await browser.close()

    # ── Resumo final ───────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"CONCLUÍDO — {OUTPUT_FILE}: {len(rows)} lotes únicos")
    cnt = Counter(r["leiloeiro"] for r in rows)
    for label, n in cnt.most_common():
        print(f"  {n:5d}  {label}")


# ──────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scraper completo de leilões de imóveis v3"
    )
    ap.add_argument("--reset", action="store_true",
                    help="Reinicia do zero (apaga progresso salvo)")
    ap.add_argument("--only", nargs="+", metavar="SITE",
                    choices=ALL_SCRAPERS,
                    help=f"Roda apenas estes sites: {ALL_SCRAPERS}")
    ap.add_argument("--skip", nargs="+", metavar="SITE",
                    choices=ALL_SCRAPERS,
                    help="Pula estes sites")
    ap.add_argument("--list", action="store_true",
                    help="Lista os sites disponíveis e sai")
    args = ap.parse_args()

    if args.list:
        print("Sites disponíveis:")
        for k, v in SCRAPER_LABELS.items():
            print(f"  {k:<15} {v}")
        sys.exit(0)

    asyncio.run(main(
        only=args.only,
        skip=args.skip,
        reset=args.reset,
    ))
