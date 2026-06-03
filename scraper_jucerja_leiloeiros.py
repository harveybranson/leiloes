"""
Scraper JUCERJA — Leiloeiros Regulares 2024
============================================
Visita os sites dos leiloeiros REGULAR extraídos dos 3 PDFs da JUCERJA/CGJ-RJ
e captura imóveis em leilão (fotos, títulos, datas, preços, documentos).

Uso:
    python scraper_jucerja_leiloeiros.py [--csv CSV_PATH] [--reset] [--apenas N]

Relatório de progresso: impresso a cada 5 minutos e no final.
Saída:
  - csv/leiloeiros_jucerja_com_sites.csv  ← nome + site do leiloeiro
  - scraper_jucerja_progress.json         ← retomada automática
  - Banco PostgreSQL via run.py (leilao-scraper)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Caminhos ──────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
CSV_INPUT      = BASE_DIR / "csv" / "leiloeiros_jucerja_regulares_2024.csv"
CSV_OUTPUT     = BASE_DIR / "csv" / "leiloeiros_jucerja_com_sites.csv"
CSV_IMOVEIS    = BASE_DIR / "csv" / f"imoveis_jucerja_{datetime.now():%Y%m%d_%H%M}.csv"
PROGRESS_FILE  = BASE_DIR / "scraper_jucerja_progress.json"
LOG_FILE       = BASE_DIR / "scraper_jucerja.log"

SCRAPER_DIR    = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\leilao-scraper")

# ── Constantes ────────────────────────────────────────────────────────────────
REPORT_INTERVAL = 300   # segundos entre relatórios de progresso (5 min)
MAX_PAGINAS     = 8     # páginas máximas por site
DELAY_ENTRE_SITES = 3   # segundos entre sites

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# ── Seletores CSS para cards de imóveis (genérico) ───────────────────────────
CARD_SELECTORS = [
    ".card-lote", ".lote-card", ".card-imovel", ".imovel-card",
    ".card-item", ".item-lote", ".bem-card", ".card-bem",
    ".auction-item", ".lot-item", ".property-card", ".imovel-item",
    "article.lote", "article.imovel", "article.card",
    ".resultado-item", ".resultado", ".leilao-item",
    "[class*='lote-']", "[class*='imovel-']", "[class*='card-']",
]

LISTING_PATTERNS = [
    r'/imovel', r'/imoveis', r'/lote', r'/lotes', r'/leilao', r'/leiloes',
    r'/catalogo', r'/bem', r'/bens', r'/property', r'/auction',
]

RE_PRECO = re.compile(r'R\$\s*[\d.,]+')
RE_AREA  = re.compile(r'\d+[\.,]?\d*\s*m[²2]', re.IGNORECASE)
RE_DATA  = re.compile(r'\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}')
RE_IMG   = re.compile(r'\.(jpg|jpeg|png|webp)(\?[^"\']*)?$', re.IGNORECASE)


# ── Progress ──────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {"done_sites": [], "imoveis": [], "erros": [], "inicio": str(datetime.now())}

def save_progress(p: dict):
    PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Helpers de extração ───────────────────────────────────────────────────────

def clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()

def preco_para_float(t: str) -> float:
    nums = re.sub(r"[^\d,]", "", t or "")
    if "," in nums:
        nums = nums.replace(".", "").replace(",", ".")
    try:
        return float(nums)
    except Exception:
        return 0.0

def extrair_imagens(soup: BeautifulSoup, base_url: str) -> list[str]:
    imgs = []
    for tag in soup.find_all(["img", "source"]):
        for attr in ("src", "data-src", "data-lazy", "srcset", "data-original"):
            v = tag.get(attr, "")
            if RE_IMG.search(v):
                url = urljoin(base_url, v.split()[0])
                if url not in imgs:
                    imgs.append(url)
    return imgs[:10]

def extrair_documentos(soup: BeautifulSoup, base_url: str) -> list[dict]:
    docs = []
    KW = re.compile(r'edital|matr[íi]cula|laudo|avalia|certid|memorial|escritura|penhora|processo', re.I)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        txt = clean(a.get_text())
        if href.lower().endswith(".pdf") or KW.search(href) or KW.search(txt):
            tipo = "edital" if "edital" in (href + txt).lower() else \
                   "matricula" if "matri" in (href + txt).lower() else "documento"
            docs.append({"tipo": tipo, "url": url, "nome": txt[:80]})
    return docs[:10]

def extrair_dados_card(card_html: str, base_url: str) -> dict:
    soup = BeautifulSoup(card_html, "html.parser")
    titulo = clean(soup.find(["h1","h2","h3","h4","h5",".titulo",".title"]) and
                   soup.find(["h1","h2","h3","h4","h5",".titulo",".title"]).get_text() or "")
    if not titulo:
        titulo = clean(soup.get_text())[:120]

    precos = RE_PRECO.findall(soup.get_text())
    preco = preco_para_float(precos[0]) if precos else 0.0

    area_m = RE_AREA.search(soup.get_text())
    area = clean(area_m.group()) if area_m else ""

    datas = RE_DATA.findall(soup.get_text())
    data_leilao = datas[0] if datas else ""

    link = ""
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if any(re.search(p, h, re.I) for p in LISTING_PATTERNS):
            link = urljoin(base_url, h)
            break
    if not link:
        a = soup.find("a", href=True)
        link = urljoin(base_url, a["href"]) if a else ""

    imagens = extrair_imagens(soup, base_url)
    docs = extrair_documentos(soup, base_url)

    return {
        "titulo": titulo,
        "preco": preco,
        "area": area,
        "data_leilao": data_leilao,
        "url": link,
        "imagens": imagens,
        "documentos": docs,
    }


# ── Scraper de um site ─────────────────────────────────────────────────────────

async def scrape_site(nome: str, site_url: str, max_paginas: int = MAX_PAGINAS) -> list[dict]:
    """Tenta múltiplas estratégias para extrair imóveis de um site de leiloeiro."""
    imoveis = []

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True,
            timeout=httpx.Timeout(30.0), verify=False
        ) as client:

            # 1. Abre a homepage e detecta links de listagem
            resp = await client.get(site_url)
            if resp.status_code >= 400:
                log(f"  ⚠ {nome}: HTTP {resp.status_code} na homepage")
                return []

            soup_home = BeautifulSoup(resp.text, "html.parser")
            listing_urls = _encontrar_urls_listagem(soup_home, site_url)

            if not listing_urls:
                listing_urls = [site_url]

            visitadas = set()
            for list_url in listing_urls[:3]:
                if list_url in visitadas:
                    continue
                visitadas.add(list_url)

                for pag in range(1, max_paginas + 1):
                    url_pag = _montar_paginacao(list_url, pag)
                    try:
                        r = await client.get(url_pag)
                        if r.status_code >= 400:
                            break
                        soup = BeautifulSoup(r.text, "html.parser")
                        cards = _extrair_cards(soup)
                        if not cards:
                            if pag == 1:
                                # Tenta extrair diretamente da página
                                dados = extrair_dados_card(r.text, url_pag)
                                if dados["titulo"] and dados["preco"] > 0:
                                    dados.update({"leiloeiro": nome, "site": site_url,
                                                  "pagina": pag, "url_listagem": url_pag})
                                    imoveis.append(dados)
                            break

                        for card_html in cards:
                            dados = extrair_dados_card(str(card_html), url_pag)
                            if dados["titulo"] or dados["preco"] > 0:
                                dados.update({"leiloeiro": nome, "site": site_url,
                                              "pagina": pag, "url_listagem": url_pag})
                                imoveis.append(dados)

                        if len(cards) < 3:
                            break  # última página provavelmente
                        await asyncio.sleep(1.5)

                    except Exception as e:
                        log(f"  ⚠ Paginação erro ({url_pag}): {e}")
                        break

    except httpx.ConnectError:
        log(f"  ✗ {nome}: Conexão recusada / site offline")
    except httpx.TimeoutException:
        log(f"  ✗ {nome}: Timeout")
    except Exception as e:
        log(f"  ✗ {nome}: {type(e).__name__}: {e}")

    return imoveis


def _encontrar_urls_listagem(soup: BeautifulSoup, base: str) -> list[str]:
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(re.search(p, href, re.I) for p in LISTING_PATTERNS):
            full = urljoin(base, href)
            if full not in urls and urlparse(full).netloc == urlparse(base).netloc:
                urls.append(full)
    return urls[:5]


def _montar_paginacao(url: str, pag: int) -> str:
    if pag == 1:
        return url
    # Tenta diferentes padrões de paginação
    if "?" in url:
        return url + f"&pagina={pag}"
    if re.search(r'/\d+/?$', url):
        return re.sub(r'/\d+/?$', f'/{pag}/', url)
    return url + f"?pagina={pag}"


def _extrair_cards(soup: BeautifulSoup) -> list:
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if len(cards) >= 2:
            return cards
    # Fallback: procura blocos com preço R$
    candidatos = []
    for tag in soup.find_all(["div", "article", "li", "section"]):
        txt = tag.get_text()
        if "R$" in txt and len(txt) < 2000:
            candidatos.append(tag)
    if len(candidatos) >= 2:
        return candidatos[:50]
    return []


# ── CSV de saída ──────────────────────────────────────────────────────────────

def salvar_imoveis_csv(imoveis: list[dict]):
    campos = ["leiloeiro", "site", "titulo", "preco", "area", "data_leilao",
              "url", "pagina", "imagens", "documentos", "url_listagem"]
    with open(CSV_IMOVEIS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for im in imoveis:
            row = dict(im)
            row["imagens"] = " | ".join(im.get("imagens", []))
            row["documentos"] = json.dumps(im.get("documentos", []), ensure_ascii=False)
            w.writerow(row)
    log(f"\n📄 CSV de imóveis salvo: {CSV_IMOVEIS.name} ({len(imoveis)} registros)")


def salvar_leiloeiros_csv(leiloeiros: list[dict]):
    campos = ["nome", "site", "uf", "cidade", "matricula", "email", "situacao"]
    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(leiloeiros)
    log(f"📄 CSV de leiloeiros salvo: {CSV_OUTPUT.name} ({len(leiloeiros)} registros)")


# ── Inserção no banco via run.py ──────────────────────────────────────────────

def inserir_no_banco(imoveis: list[dict]) -> tuple[int, int]:
    """
    Insere imóveis no banco PostgreSQL via run.py scrape-csv do leilao-scraper.
    Retorna (inseridos, erros).
    """
    import subprocess, tempfile

    # Cria CSV temporário no formato esperado pelo run.py
    temp = Path(tempfile.mktemp(suffix=".csv"))
    try:
        vistos_sites = set()
        linhas_unicas = []
        for im in imoveis:
            site = im.get("site", "").strip().rstrip("/")
            nome = im.get("leiloeiro", "").strip()
            if site and site not in vistos_sites:
                vistos_sites.add(site)
                linhas_unicas.append({"nome": nome, "site": site, "uf": "RJ", "cidade": ""})

        if not linhas_unicas:
            return 0, 0

        with open(temp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["nome", "site", "uf", "cidade"])
            w.writeheader()
            w.writerows(linhas_unicas)

        log(f"\n🔄 Inserindo no banco: {len(vistos_sites)} sites únicos via run.py scrape-csv...")
        result = subprocess.run(
            ["python", "run.py", "scrape-csv", str(temp), "--max-paginas", "5"],
            cwd=str(SCRAPER_DIR),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=7200  # 2h
        )
        if result.returncode == 0:
            log("✅ Inserção no banco concluída.")
            # Extrai contagem do output
            ins_m = re.search(r'(\d+)\s+inseridos', result.stdout)
            return int(ins_m.group(1)) if ins_m else 0, 0
        else:
            log(f"⚠ run.py retornou código {result.returncode}")
            log(result.stderr[:500])
            return 0, 1
    finally:
        temp.unlink(missing_ok=True)


# ── Relatório de progresso ────────────────────────────────────────────────────

def relatorio(progress: dict, leiloeiros_total: int, inicio: float):
    elapsed = time.time() - inicio
    done = len(progress["done_sites"])
    total_im = len(progress["imoveis"])
    erros = len(progress["erros"])
    pct = done / leiloeiros_total * 100 if leiloeiros_total else 0

    print("\n" + "═" * 60)
    print(f"📊 RELATÓRIO — {datetime.now():%d/%m/%Y %H:%M:%S}")
    print(f"   Sites visitados : {done}/{leiloeiros_total} ({pct:.0f}%)")
    print(f"   Imóveis coletados: {total_im}")
    print(f"   Erros           : {erros}")
    print(f"   Tempo decorrido : {elapsed/60:.1f} min")

    # Por leiloeiro
    por_leil: dict[str, int] = {}
    for im in progress["imoveis"]:
        k = im.get("leiloeiro", "?")
        por_leil[k] = por_leil.get(k, 0) + 1

    if por_leil:
        print("\n   Imóveis por leiloeiro (top 15):")
        top = sorted(por_leil.items(), key=lambda x: -x[1])[:15]
        for nome, cnt in top:
            print(f"     {cnt:3d}  {nome}")
    print("═" * 60 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv",    default=str(CSV_INPUT), help="CSV de entrada")
    parser.add_argument("--reset",  action="store_true",   help="Reinicia do zero")
    parser.add_argument("--apenas", type=int, default=0,   help="Processa apenas N sites")
    args = parser.parse_args()

    log("=" * 60)
    log("SCRAPER JUCERJA — LEILOEIROS REGULARES RJ 2024")
    log("=" * 60)

    # Carrega CSV de leiloeiros
    leiloeiros = []
    csv_path = Path(args.csv)
    if not csv_path.exists():
        log(f"❌ CSV não encontrado: {csv_path}")
        sys.exit(1)

    with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            site = (row.get("site") or "").strip().rstrip("/")
            if not site.startswith("http"):
                continue
            leiloeiros.append({
                "nome": (row.get("nome") or "").strip(),
                "site": site,
                "uf":   (row.get("uf") or "RJ").strip(),
                "cidade": (row.get("cidade") or "").strip(),
                "matricula": (row.get("matricula") or "").strip(),
                "email": (row.get("email") or "").strip(),
                "situacao": (row.get("situacao") or "Regular").strip(),
            })

    # Deduplica por site
    vistos: set[str] = set()
    leiloeiros_unicos = []
    for lei in leiloeiros:
        if lei["site"].lower() not in vistos:
            vistos.add(lei["site"].lower())
            leiloeiros_unicos.append(lei)

    if args.apenas:
        leiloeiros_unicos = leiloeiros_unicos[:args.apenas]

    log(f"Total de leiloeiros REGULAR com site: {len(leiloeiros_unicos)}")
    salvar_leiloeiros_csv(leiloeiros_unicos)

    # Carrega progresso
    progress = {} if args.reset else load_progress()
    if not progress:
        progress = {"done_sites": [], "imoveis": [], "erros": [], "inicio": str(datetime.now())}

    done_urls = set(progress.get("done_sites", []))

    inicio = time.time()
    ultimo_relatorio = inicio

    # Itera sobre leiloeiros
    for idx, lei in enumerate(leiloeiros_unicos):
        if lei["site"].lower() in done_urls:
            continue  # já processado

        log(f"\n[{idx+1}/{len(leiloeiros_unicos)}] {lei['nome']} — {lei['site']}")

        try:
            imoveis = await scrape_site(lei["nome"], lei["site"])
            if imoveis:
                log(f"  ✅ {len(imoveis)} imóveis encontrados")
                for im in imoveis:
                    im["uf"] = lei["uf"]
                    im["cidade"] = lei["cidade"]
                progress["imoveis"].extend(imoveis)
            else:
                log(f"  ℹ 0 imóveis (site pode não ter leilões ativos ou usa JS pesado)")

        except Exception as e:
            log(f"  ✗ Erro inesperado: {e}")
            progress["erros"].append({"site": lei["site"], "erro": str(e)})

        progress["done_sites"].append(lei["site"].lower())
        save_progress(progress)

        # Relatório a cada 5 minutos
        if time.time() - ultimo_relatorio >= REPORT_INTERVAL:
            relatorio(progress, len(leiloeiros_unicos), inicio)
            ultimo_relatorio = time.time()

        await asyncio.sleep(DELAY_ENTRE_SITES)

    # Salva CSV de imóveis
    salvar_imoveis_csv(progress["imoveis"])

    # Relatório final
    relatorio(progress, len(leiloeiros_unicos), inicio)

    # Insere no banco (via run.py do leilao-scraper)
    if SCRAPER_DIR.exists():
        inserir_no_banco(progress["imoveis"])
    else:
        log(f"⚠ Diretório do leilao-scraper não encontrado: {SCRAPER_DIR}")
        log("  Execute manualmente: python run.py scrape-csv [csv_path]")

    log("\n🏁 SCRAPING CONCLUÍDO.")
    log(f"   Total de imóveis coletados: {len(progress['imoveis'])}")
    log(f"   CSV de imóveis: {CSV_IMOVEIS}")
    log(f"   CSV de leiloeiros: {CSV_OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
