"""
Scraper multi-site — leiloeiros JUCESP Atuante Regular.
Usa requests (verify=False) + BeautifulSoup com estratégias múltiplas.
Progresso reportado a cada 5 minutos.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import csv, json, re, time, logging, os, hashlib, warnings
from datetime import datetime, date
from urllib.parse import urlparse, urljoin, quote
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
warnings.filterwarnings("ignore")

# ── Configuração ─────────────────────────────────────────────────────────────
BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)

LEILOEIROS_CSV = BASE_DIR / "leiloeiros_regulares.csv"
OUTPUT_CSV     = CSV_DIR  / f"imoveis_jucesp_{date.today()}.csv"
OUTPUT_HTML    = BASE_DIR / "viewer_imoveis_jucesp.html"
PROGRESS_FILE  = BASE_DIR / "scraper_jucesp_progress.json"
LOG_FILE       = BASE_DIR / "scraper_jucesp.log"

# Logger — só file handler (evita problema de encoding no console Windows)
log = logging.getLogger("scraper_jucesp")
log.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)
# Console handler com encoding seguro
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(ch)

EMAIL_SERVICES = {
    "gmail","hotmail","yahoo","terra","bol","uol","outlook","ig","live",
    "icloud","msn","globo","zipmail","superig","ymail","proton","gmx",
    "tutanota","mail","hotmal","itelefonica","oabsp","compe","fsa",
    "socorronet","r7","zap","wp","me","net","com","org","gov","edu",
}
LEILAO_KW = ["leil","lance","arrema","prego","hasta","lote","praca","bid","auction","imovel","judicial"]

TODAY = date.today()
FIELDNAMES = ["id","leiloeiro","site","titulo","descricao","endereco","cidade","uf",
              "lance_inicial","avaliacao","data_leilao","url","tipo"]

# ── Session HTTP ──────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return s

SESSION = make_session()


# ── Helpers ───────────────────────────────────────────────────────────────────
def extract_email_domain(email: str) -> str | None:
    if not email: return None
    m = re.search(r"@([\w.\-]+)", email.lower())
    if not m: return None
    domain = m.group(1)
    parts = domain.split(".")
    base  = parts[-2] if len(parts) >= 2 else parts[0]
    if base in EMAIL_SERVICES: return None
    if not any(kw in domain for kw in LEILAO_KW): return None
    return domain

def normalize_url(url: str) -> str:
    if not url: return ""
    url = url.strip().rstrip("/")
    if not url.startswith("http"): url = "https://" + url
    return url

def money_to_float(txt: str) -> float | None:
    if not txt: return None
    txt = re.sub(r"[^\d,.]", "", str(txt))
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif "," in txt:
        txt = txt.replace(",", ".")
    try: return float(txt)
    except: return None

def parse_date_br(txt: str) -> str | None:
    if not txt: return None
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", str(txt))
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2: y = "20" + y
        try:
            dt = date(int(y), int(mo), int(d))
            return dt.isoformat()
        except: pass
    # ISO
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(txt))
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None

def make_id(lote: dict) -> str:
    key = (str(lote.get("titulo","")) + str(lote.get("url","")) + str(lote.get("lance_inicial",""))).encode()
    return hashlib.md5(key).hexdigest()[:12]

def deve_inserir(imovel: dict) -> bool:
    ds = imovel.get("data_leilao")
    if not ds: return True
    try:
        return date.fromisoformat(ds) >= TODAY
    except: return True

def safe_get(url: str, timeout: int = 15, accept_json: bool = False) -> requests.Response | None:
    headers = {"Accept": "application/json"} if accept_json else {}
    try:
        r = SESSION.get(url, timeout=timeout, verify=False, headers=headers, allow_redirects=True)
        if r.status_code < 400:
            return r
    except Exception as e:
        log.debug(f"GET {url}: {e}")
    return None


# ── Carregamento do CSV ───────────────────────────────────────────────────────
def load_leiloeiros() -> list[dict]:
    with open(LEILOEIROS_CSV, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    result = {}
    for r in rows:
        if r.get("junta_comercial","").upper() != "JUCESP": continue

        site = normalize_url(r.get("site",""))
        if not site:
            dom = extract_email_domain(r.get("email",""))
            if dom: site = f"https://{dom}"
        if not site: continue

        parsed = urlparse(site)
        key = parsed.netloc.lower().replace("www.","")
        if key and key not in result:
            result[key] = {
                "nome": r["nome"],
                "site": site,
                "email": r.get("email",""),
                "cidade": r.get("cidade",""),
                "domain": key,
            }

    items = list(result.values())
    log.info(f"Leiloeiros JUCESP Atuante Regular com site (unicos): {len(items)}")
    return items


# ── Estratégias de scraping ────────────────────────────────────────────────────

LISTING_PATHS = [
    "",
    "/leiloes", "/leilao", "/leiloes-de-imoveis",
    "/imoveis", "/imóveis",
    "/lotes", "/lotes/imovel", "/lotes/imoveis",
    "/catalogo", "/catálogo",
    "/agenda", "/agenda-de-leiloes",
    "/emandamento", "/em-andamento",
    "/ativos", "/bens",
    "/busca?tipo=imovel", "/busca?categoria=imovel",
]

def detect_listing_pages(base: str, soup: BeautifulSoup) -> list[str]:
    """Detecta páginas de listagem a partir da home."""
    candidates = set()
    kws = ["leil","imov","lote","judic","catal","agenda","bens","ativo","praça"]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() + href).lower()
        if any(k in text for k in kws):
            full = urljoin(base, href)
            if urlparse(full).netloc == urlparse(base).netloc and len(full) < 200:
                candidates.add(full)
    # Adiciona paths comuns
    for path in LISTING_PATHS:
        candidates.add(base.rstrip("/") + path)
    # Ordena por comprimento (URLs mais curtas primeiro)
    return sorted(candidates, key=len)[:12]


def extract_imoveis_from_page(url: str, leiloeiro: dict) -> list[dict]:
    resp = safe_get(url)
    if not resp: return []

    # Verifica encoding
    try:
        resp.encoding = resp.apparent_encoding or "utf-8"
    except: pass

    text = resp.text
    low  = text.lower()

    # Descarta se não tem conteúdo relevante
    if not any(k in low for k in ["imov","lote","leil","judic","lance","praca","hasta"]):
        return []

    soup = BeautifulSoup(text, "html.parser")
    imoveis = []

    # Estratégia 1: JSON-LD
    imoveis += _from_jsonld(soup, url, leiloeiro)

    # Estratégia 2: __NEXT_DATA__
    if not imoveis:
        imoveis += _from_nextdata(soup, url, leiloeiro)

    # Estratégia 3: JSON embutido (window.__INITIAL_STATE__, window.DATA, etc.)
    if not imoveis:
        imoveis += _from_embedded_json(text, url, leiloeiro)

    # Estratégia 4: API interna (XHR hints nos scripts)
    if not imoveis:
        imoveis += _from_api_hints(soup, url, leiloeiro)

    # Estratégia 5: HTML cards
    if not imoveis:
        imoveis += _from_html_cards(soup, url, leiloeiro)

    return imoveis


def _from_jsonld(soup, url, leiloeiro):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type","")
                if t in ("Product","RealEstateListing","Offer","AuctionEvent","ItemList"):
                    if t == "ItemList":
                        for sub in item.get("itemListElement",[]):
                            out.append(_jsonld_to_imovel(sub.get("item",sub), url, leiloeiro))
                    else:
                        out.append(_jsonld_to_imovel(item, url, leiloeiro))
        except: pass
    return [x for x in out if x.get("titulo")]

def _jsonld_to_imovel(item, url, leiloeiro):
    # Preço
    price = None
    offers = item.get("offers",{})
    if isinstance(offers, dict): price = offers.get("price")
    elif isinstance(offers, list) and offers: price = offers[0].get("price")
    if not price: price = item.get("price")

    # Endereço
    addr = item.get("address","")
    if isinstance(addr, dict):
        parts = [addr.get("streetAddress",""), addr.get("addressLocality",""),
                 addr.get("addressRegion","")]
        addr = ", ".join(p for p in parts if p)

    return {
        "id": make_id({"titulo": item.get("name",""), "url": url}),
        "leiloeiro": leiloeiro["nome"],
        "site": leiloeiro["site"],
        "titulo": str(item.get("name",""))[:150],
        "descricao": str(item.get("description",""))[:300],
        "endereco": str(addr)[:200],
        "cidade": str(item.get("addressLocality","") or leiloeiro.get("cidade",""))[:100],
        "uf": str(item.get("addressRegion","SP"))[:2],
        "lance_inicial": money_to_float(str(price or "")),
        "avaliacao": None,
        "data_leilao": parse_date_br(str(item.get("startDate",""))),
        "url": url,
        "tipo": "imovel",
    }


def _from_nextdata(soup, url, leiloeiro):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag: return []
    try:
        data = json.loads(tag.string or "")
        return _walk_json(data, url, leiloeiro)
    except: return []


def _from_embedded_json(text, url, leiloeiro):
    patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.{20,}}?);',
        r'window\.__STATE__\s*=\s*({.{20,}}?);',
        r'window\.DATA\s*=\s*({.{20,}}?);',
        r'var\s+lotes\s*=\s*(\[.{20,}?\]);',
        r'var\s+imoveis\s*=\s*(\[.{20,}?\]);',
    ]
    out = []
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                out += _walk_json(data, url, leiloeiro)
                if out: break
            except: pass
    return out


def _walk_json(obj, url, leiloeiro, depth=0) -> list[dict]:
    if depth > 6: return []
    out = []
    if isinstance(obj, list):
        for item in obj[:200]:
            out += _walk_json(item, url, leiloeiro, depth+1)
    elif isinstance(obj, dict):
        keys_lower = {k.lower() for k in obj}
        # Heurística: tem título + preço/lance
        has_title = any(k in keys_lower for k in ["titulo","title","name","nome","descricao","description"])
        has_price = any(k in keys_lower for k in ["lance","preco","valor","price","amount","vlr"])
        if has_title and has_price:
            im = _generic_dict_to_imovel(obj, url, leiloeiro)
            if im.get("titulo"):
                out.append(im)
        else:
            for v in obj.values():
                out += _walk_json(v, url, leiloeiro, depth+1)
    return out


def _get_val(obj: dict, *keys) -> str:
    for k in keys:
        for dk in obj:
            if dk.lower() == k.lower():
                v = obj[dk]
                if isinstance(v, (str, int, float)) and v != "":
                    return str(v)
    return ""

def _generic_dict_to_imovel(obj, url, leiloeiro):
    titulo = _get_val(obj,"titulo","title","name","nome","descricao","description")[:150]
    lance  = money_to_float(_get_val(obj,"lance","lance_inicial","lanceinicial","preco","valor",
                                      "price","amount","vlr","valorlance"))
    data   = parse_date_br(_get_val(obj,"data","dataleilao","data_leilao","dataLeilao",
                                     "date","startDate","datainicio"))
    end    = _get_val(obj,"endereco","address","logradouro","localizacao","local")[:200]
    cid    = _get_val(obj,"cidade","city","municipio") or leiloeiro.get("cidade","")
    link   = _get_val(obj,"url","link","href","urlLote","urlImovel") or url

    return {
        "id": make_id({"titulo": titulo, "url": link, "lance_inicial": lance}),
        "leiloeiro": leiloeiro["nome"],
        "site": leiloeiro["site"],
        "titulo": titulo,
        "descricao": "",
        "endereco": end,
        "cidade": cid[:100],
        "uf": _get_val(obj,"uf","estado","state") or "SP",
        "lance_inicial": lance,
        "avaliacao": money_to_float(_get_val(obj,"avaliacao","valoravaliacao","preco_avaliacao")),
        "data_leilao": data,
        "url": link,
        "tipo": "imovel",
    }


def _from_api_hints(soup, url, leiloeiro):
    """Detecta chamadas de API nos scripts e tenta chamá-las."""
    out = []
    for script in soup.find_all("script"):
        src = script.string or ""
        # Procura patterns de URLs de API
        api_paths = set()
        for pat in [r'["\'](/api/lotes[^\'"]*)["\']',
                    r'["\'](/api/imoveis[^\'"]*)["\']',
                    r'["\'](/api/v\d+/lotes[^\'"]*)["\']',
                    r'fetch\(["\']([^"\']+lote[^"\']*)["\']',
                    r'axios\.get\(["\']([^"\']+lote[^"\']*)["\']']:
            for m in re.finditer(pat, src, re.IGNORECASE):
                api_paths.add(m.group(1))
        for path in list(api_paths)[:3]:
            full = urljoin(url, path)
            r = safe_get(full, accept_json=True)
            if r:
                try:
                    data = r.json()
                    out += _walk_json(data, url, leiloeiro)
                except: pass
    return out


def _from_html_cards(soup, url, leiloeiro):
    """Extrai cards de imóveis do HTML."""
    SELECTORS = [
        ".lote-card", ".card-lote", "[class*=lote-item]", "[class*=item-lote]",
        "[class*=property-card]", "[class*=imovel-card]",
        "article.lote", "article.item", "article.property",
        ".listing-item", ".property-item", ".auction-item",
        "[data-lote]", "[data-idlote]",
        # Fallback genérico
        ".card", "article",
    ]

    cards = []
    sel_used = ""
    for sel in SELECTORS:
        found = soup.select(sel)
        if len(found) > 1:
            # Só aceita se os cards têm texto substancial
            texts = [c.get_text(" ",strip=True) for c in found]
            if any(len(t) > 40 for t in texts):
                cards = found
                sel_used = sel
                break

    out = []
    for card in cards[:100]:
        im = _parse_card(card, url, leiloeiro)
        if im and im.get("titulo"):
            out.append(im)
    if out:
        log.debug(f"    HTML cards [{sel_used}]: {len(out)}")
    return out


def _parse_card(card, url, leiloeiro):
    text = card.get_text(" ", strip=True)
    if len(text) < 30: return None

    # Filtra por conteúdo relevante
    low = text.lower()
    if not any(k in low for k in ["m2","m²","terreno","apt","casa","imov","lote","area","quarto","andar"]):
        return None

    # Título
    titulo = ""
    for tag in ["h1","h2","h3","h4","h5",".titulo",".title","strong"]:
        el = card.select_one(tag)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 5:
                titulo = t[:150]
                break
    if not titulo:
        titulo = text[:100]

    # Preço
    lance = None
    for pat in [r"R\$\s*([\d.,]+)", r"Lance.*?R\$\s*([\d.,]+)", r"Valor.*?R\$\s*([\d.,]+)"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            lance = money_to_float(m.group(1))
            break

    # Data
    data_leilao = None
    for pat in [r"(\d{1,2}/\d{1,2}/\d{4})", r"(\d{4}-\d{2}-\d{2})"]:
        m = re.search(pat, text)
        if m:
            data_leilao = parse_date_br(m.group(1))
            break

    # Endereço
    end = ""
    for sel in [".endereco",".address","[class*=address]","[class*=endereco]","[class*=local]"]:
        el = card.select_one(sel)
        if el:
            end = el.get_text(strip=True)[:200]
            break

    # Link
    link = url
    a = card.select_one("a[href]")
    if a:
        href = a["href"]
        if href and not href.startswith("#"):
            link = urljoin(url, href)

    return {
        "id": make_id({"titulo": titulo, "url": link, "lance_inicial": lance}),
        "leiloeiro": leiloeiro["nome"],
        "site": leiloeiro["site"],
        "titulo": titulo,
        "descricao": text[:300],
        "endereco": end,
        "cidade": leiloeiro.get("cidade",""),
        "uf": "SP",
        "lance_inicial": lance,
        "avaliacao": None,
        "data_leilao": data_leilao,
        "url": link,
        "tipo": "imovel",
    }


# ── Scraping por leiloeiro ─────────────────────────────────────────────────────
def scrape_leiloeiro(leiloeiro: dict) -> list[dict]:
    base = leiloeiro["site"].rstrip("/")
    log.info(f"  -> {leiloeiro['nome']} | {base}")

    all_items = []
    visited   = set()

    # Carrega home e detecta páginas de listagem
    resp_home = safe_get(base)
    if resp_home:
        try:
            resp_home.encoding = resp_home.apparent_encoding or "utf-8"
        except: pass
        soup_home = BeautifulSoup(resp_home.text, "html.parser")
        pages = detect_listing_pages(base, soup_home)
    else:
        pages = [base + path for path in LISTING_PATHS[:6]]

    for page_url in pages[:10]:
        if page_url in visited: continue
        visited.add(page_url)
        try:
            items = extract_imoveis_from_page(page_url, leiloeiro)
            all_items.extend(items)
            if len(all_items) >= 200:
                break
        except Exception as e:
            log.debug(f"    Erro {page_url}: {e}")
        time.sleep(0.3)

    # Deduplicar
    seen = set()
    deduped = []
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)

    # Filtro de data
    validos = [i for i in deduped if deve_inserir(i)]
    log.info(f"    {len(validos)} imoveis validos (de {len(deduped)} extraidos)")
    return validos


# ── CSV ────────────────────────────────────────────────────────────────────────
def save_csv(imoveis: list[dict]):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    log.info(f"CSV salvo: {OUTPUT_CSV} ({len(imoveis)} registros)")


# ── Viewer HTML ────────────────────────────────────────────────────────────────
def render_html(imoveis: list[dict], stats: dict):
    rows_html = ""
    for im in imoveis:
        lance = ""
        if im.get("lance_inicial"):
            try:
                lance = f"R$ {float(im['lance_inicial']):,.2f}".replace(",","X").replace(".",",").replace("X",".")
            except: lance = str(im["lance_inicial"])
        else:
            lance = "—"

        data  = im.get("data_leilao","") or "—"
        end   = (im.get("endereco","") or im.get("cidade","") or "")[:60]
        title = (im.get("titulo","") or "")[:80]
        url_  = im.get("url","") or im.get("site","")
        site_ = (im.get("site","") or "").replace("https://","").replace("http://","")[:35]

        rows_html += f"""
        <tr id="row-{im['id']}" class="imovel-row">
          <td><button class="btn-x" onclick="excluir('{im['id']}')">X</button></td>
          <td><a href="{url_}" target="_blank" class="titulo-link">{title}</a></td>
          <td>{im.get('leiloeiro','')[:35]}</td>
          <td>{end}</td>
          <td class="lance">{lance}</td>
          <td>{data}</td>
          <td><a href="{im.get('site','')}" target="_blank" class="site-link">{site_}</a></td>
        </tr>"""

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    total   = len(imoveis)
    pct     = int(stats.get("done",0) / max(stats.get("total",1),1) * 100)
    csv_name = OUTPUT_CSV.name

    stats_html = (
        f"<div class='stat'>Imoveis: <strong>{total}</strong></div>"
        f"<div class='stat'>Sites OK: <strong>{stats.get('sites_ok',0)}</strong></div>"
        f"<div class='stat'>Erros: <strong>{stats.get('sites_err',0)}</strong></div>"
        f"<div class='stat'>Progresso: <strong>{stats.get('done',0)}/{stats.get('total',0)} ({pct}%)</strong></div>"
        f"<div class='stat'>Atualizado: <strong>{now_str}</strong></div>"
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Imoveis Leiloeiros JUCESP - {now_str}</title>
<style>
:root{{--bg:#0f172a;--surface:#1e293b;--border:#334155;--accent:#3b82f6;--danger:#ef4444;--success:#22c55e;--text:#e2e8f0;--muted:#94a3b8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;min-height:100vh}}
header{{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
header h1{{font-size:1.1rem;color:#fff}}
.stats{{display:flex;gap:10px;flex-wrap:wrap}}
.stat{{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 12px;font-size:.8rem;color:var(--muted)}}
.stat strong{{color:var(--text)}}
.toolbar{{padding:10px 20px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--border);background:var(--surface)}}
.toolbar input{{flex:1;min-width:180px;background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:6px 10px;color:var(--text);font-size:.85rem}}
.toolbar input:focus{{outline:none;border-color:var(--accent)}}
.btn{{padding:6px 14px;border-radius:5px;border:none;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}}
.btn-primary{{background:var(--accent);color:#fff}}.btn-primary:hover{{background:#2563eb}}
.btn-danger{{background:var(--danger);color:#fff}}.btn-danger:hover{{background:#dc2626}}
.btn-success{{background:var(--success);color:#fff}}.btn-success:hover{{background:#16a34a}}
.counter{{margin-left:auto;color:var(--muted);font-size:.8rem}}
.table-wrap{{overflow-x:auto;padding:0 20px 20px}}
table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:.82rem}}
thead th{{background:var(--surface);color:var(--muted);text-align:left;padding:9px 7px;border-bottom:2px solid var(--border);white-space:nowrap}}
tbody tr{{border-bottom:1px solid var(--border);transition:.1s}}
tbody tr:hover{{background:rgba(59,130,246,.06)}}
tbody tr.hidden{{display:none}}
tbody tr.excluded{{opacity:.25;background:rgba(239,68,68,.05)}}
td{{padding:8px 7px;vertical-align:top}}
.btn-x{{background:var(--danger);color:#fff;border:none;border-radius:4px;width:24px;height:24px;cursor:pointer;font-size:.8rem;line-height:1}}
.btn-x:hover{{background:#dc2626}}
.titulo-link{{color:var(--accent);text-decoration:none;font-weight:500}}.titulo-link:hover{{text-decoration:underline}}
.site-link{{color:var(--muted);text-decoration:none;font-size:.78rem}}.site-link:hover{{color:var(--text)}}
.lance{{font-weight:600;color:var(--success);white-space:nowrap}}
.toast{{position:fixed;bottom:20px;right:20px;background:var(--success);color:#fff;padding:10px 18px;border-radius:7px;font-size:.85rem;display:none;z-index:999}}
#modal-ov{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}}
#modal-ov.open{{display:flex}}
#modal{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px;max-width:460px;width:90%}}
#modal h2{{margin-bottom:10px}}
#modal p{{color:var(--muted);margin-bottom:18px;font-size:.88rem}}
#modal .btns{{display:flex;gap:8px;justify-content:flex-end}}
.progress-bar{{height:4px;background:var(--border);border-radius:2px;margin:0}}
.progress-bar div{{height:100%;background:var(--accent);border-radius:2px;width:{pct}%;transition:.5s}}
</style>
</head>
<body>
<header>
  <h1>Imoveis — Leiloeiros JUCESP Atuante Regular</h1>
  <div class="stats">{stats_html}</div>
</header>
<div class="progress-bar"><div></div></div>
<div class="toolbar">
  <input type="text" id="search" placeholder="Filtrar por titulo, leiloeiro, endereco..." oninput="filtrar()">
  <select id="sel-uf" onchange="filtrar()" style="background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:6px 8px;color:var(--text);font-size:.85rem">
    <option value="">Todos UF</option><option>SP</option><option>RJ</option><option>MG</option>
  </select>
  <button class="btn btn-danger" onclick="excluirTodos()">X Excluir visíveis</button>
  <button class="btn btn-success" onclick="abrirModal()">Enviar para CSV / banco</button>
  <span class="counter" id="counter">{total} registros</span>
</div>
<div class="table-wrap">
  <table id="tabela">
    <thead>
      <tr>
        <th style="width:32px"></th>
        <th>Título</th>
        <th>Leiloeiro</th>
        <th>Endereço / Cidade</th>
        <th>Lance Inicial</th>
        <th>Data Leilão</th>
        <th>Site</th>
      </tr>
    </thead>
    <tbody id="tbody">{rows_html}</tbody>
  </table>
</div>
<div class="toast" id="toast"></div>
<div id="modal-ov">
  <div id="modal">
    <h2>Exportar para CSV</h2>
    <p>Serao exportados <strong id="mc">0</strong> imoveis (nao excluidos).</p>
    <div class="btns">
      <button class="btn" style="background:var(--border)" onclick="fecharModal()">Cancelar</button>
      <button class="btn btn-success" onclick="confirmarEnvio()">Confirmar</button>
    </div>
  </div>
</div>
<script>
const DATA = {json.dumps(imoveis, ensure_ascii=False, default=str)};
const CSV_FN = "{csv_name}";
const excludedIds = new Set();

function excluir(id) {{
  excludedIds.add(id);
  const row = document.getElementById('row-' + id);
  if (row) row.classList.add('excluded');
  atualizarCounter();
}}
function excluirTodos() {{
  document.querySelectorAll('#tbody tr:not(.hidden):not(.excluded)').forEach(row => {{
    excludedIds.add(row.id.replace('row-',''));
    row.classList.add('excluded');
  }});
  atualizarCounter();
}}
function filtrar() {{
  const q = document.getElementById('search').value.toLowerCase();
  const uf = document.getElementById('sel-uf').value.toLowerCase();
  let vis = 0;
  document.querySelectorAll('#tbody tr').forEach(row => {{
    const txt = row.textContent.toLowerCase();
    const show = (!q || txt.includes(q)) && (!uf || txt.includes(uf));
    row.classList.toggle('hidden', !show);
    if (show && !row.classList.contains('excluded')) vis++;
  }});
  document.getElementById('counter').textContent = vis + ' registros';
}}
function atualizarCounter() {{
  const t = document.querySelectorAll('#tbody tr:not(.hidden):not(.excluded)').length;
  document.getElementById('counter').textContent = t + ' registros';
}}
function abrirModal() {{
  document.getElementById('mc').textContent = DATA.filter(d=>!excludedIds.has(d.id)).length;
  document.getElementById('modal-ov').classList.add('open');
}}
function fecharModal() {{ document.getElementById('modal-ov').classList.remove('open'); }}
function confirmarEnvio() {{
  fecharModal();
  const sel = DATA.filter(d => !excludedIds.has(d.id));
  const fields = {json.dumps(FIELDNAMES)};
  const lines = [fields.join(',')];
  sel.forEach(row => {{
    const vals = fields.map(f => '"' + (row[f]??'').toString().replace(/"/g,'""') + '"');
    lines.push(vals.join(','));
  }});
  const blob = new Blob(['\\uFEFF' + lines.join('\\n')], {{type:'text/csv;charset=utf-8;'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = CSV_FN;
  a.click();
  toast('Exportados ' + sel.length + ' imoveis -> ' + CSV_FN);
}}
function toast(msg,dur=4000) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(()=>el.style.display='none', dur);
}}
</script>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Viewer HTML salvo: {OUTPUT_HTML}")


# ── Progresso ──────────────────────────────────────────────────────────────────
def save_progress(stats):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({**stats, "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)

def print_progress(stats, total_imoveis):
    done  = stats.get("done",0)
    total = stats.get("total",1)
    pct   = int(done/max(total,1)*100)
    bar   = "#" * (pct//5) + "." * (20-pct//5)
    print(f"\n{'='*60}")
    print(f"  PROGRESSO: [{bar}] {pct}%")
    print(f"  Sites: {done}/{total}  OK:{stats.get('sites_ok',0)}  Erro:{stats.get('sites_err',0)}")
    print(f"  Imoveis capturados: {total_imoveis}")
    print(f"  Hora: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}\n")
    sys.stdout.flush()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Scraper JUCESP Atuante Regular iniciando ===")
    leiloeiros = load_leiloeiros()

    stats = {"total": len(leiloeiros), "done": 0, "sites_ok": 0, "sites_err": 0}
    all_imoveis: list[dict] = []
    last_report  = time.time()
    REPORT_EVERY = 5 * 60  # 5 minutos

    for idx, leiloeiro in enumerate(leiloeiros, 1):
        stats["done"] = idx
        try:
            items = scrape_leiloeiro(leiloeiro)
            all_imoveis.extend(items)
            if items:
                stats["sites_ok"] += 1
            else:
                stats["sites_err"] += 1
        except Exception as e:
            log.warning(f"  ERRO {leiloeiro['site']}: {e}")
            stats["sites_err"] += 1

        # Relatório a cada 5 min
        if time.time() - last_report >= REPORT_EVERY:
            print_progress(stats, len(all_imoveis))
            save_csv(all_imoveis)
            render_html(all_imoveis, stats)
            save_progress(stats)
            last_report = time.time()

        time.sleep(0.6)

    print_progress(stats, len(all_imoveis))
    save_csv(all_imoveis)
    render_html(all_imoveis, stats)
    save_progress(stats)
    log.info(f"=== Concluido! {len(all_imoveis)} imoveis de {stats['sites_ok']} sites ===")


if __name__ == "__main__":
    main()
