"""
Scraper de imóveis — leiloeiros REGULAR de TODAS as juntas comerciais.
Lê CSV de leiloeiros, filtra REGULAR, extrai domínios de e-mail únicos,
scrapa cada site e gera viewer HTML interativo com X para excluir.
Progresso reportado a cada 5 minutos.

Uso:
    python scraper_leiloeiros_direto.py
    python scraper_leiloeiros_direto.py csv/meu_arquivo.csv
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import csv, json, re, time, logging, hashlib, warnings
from datetime import datetime, date
from urllib.parse import urlparse, urljoin
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ── Configuração ──────────────────────────────────────────────────────────────
BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)

# Arquivo de entrada: aceita argumento CLI ou usa default
INPUT_CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else CSV_DIR / "leiloeiros_todos.csv"
if not INPUT_CSV.is_absolute():
    INPUT_CSV = BASE_DIR / INPUT_CSV

OUTPUT_CSV    = CSV_DIR  / f"imoveis_leiloeiros_{date.today()}.csv"
OUTPUT_HTML   = BASE_DIR / "viewer_leiloeiros_direto.html"
PROGRESS_FILE = BASE_DIR / "scraper_leiloeiros_progress.json"
LOG_FILE      = BASE_DIR / "scraper_leiloeiros.log"

TODAY = date.today()

FIELDNAMES = ["id","leiloeiro","junta","site","titulo","descricao","endereco",
              "cidade","uf","lance_inicial","avaliacao","data_leilao","url","tipo","imagem"]

# Serviços de e-mail — apenas o NOME do domínio (sem TLD) é comparado
EMAIL_SERVICES = {
    "gmail","hotmail","yahoo","terra","bol","uol","outlook","ig","live",
    "icloud","msn","globo","zipmail","superig","ymail","proton","gmx",
    "tutanota","hotmal","ibest","pop","aol","vivo",
    "oabsp","oab","creci","adv",
}

# ── Logger ────────────────────────────────────────────────────────────────────
log = logging.getLogger("scraper_lei")
log.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(ch)

# ── Session HTTP ──────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adp = HTTPAdapter(max_retries=retry)
    s.mount("https://", adp)
    s.mount("http://",  adp)
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
    em = email.strip().lower()
    if em in ("não informado", "nao informado", "n/a", ""): return None
    m = re.search(r"@([\w.\-]+)", em)
    if not m: return None
    domain = m.group(1).strip()
    if not domain or len(domain) < 5: return None
    parts = domain.split(".")

    # Para domínios .br (ex: hoppeleiloes.com.br → base="hoppeleiloes")
    # Para domínios .com (ex: alfaleiloes.com → base="alfaleiloes")
    if parts[-1] in ("br","ar","mx","co","uk","pt") and len(parts) >= 3:
        base = parts[-3]
    else:
        base = parts[-2] if len(parts) >= 2 else parts[0]

    if base in EMAIL_SERVICES: return None
    if len(base) < 3: return None
    return domain

def normalize_url(s: str) -> str:
    s = s.strip().rstrip("/")
    if not s: return ""
    if not s.startswith("http"): s = "https://" + s
    return s

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
        try: return date(int(y), int(mo), int(d)).isoformat()
        except: pass
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(txt))
    if m2: return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None

def make_id(data) -> str:
    return hashlib.md5(str(data).encode()).hexdigest()[:12]

def deve_inserir(imovel: dict) -> bool:
    ds = imovel.get("data_leilao")
    if not ds: return True
    try: return date.fromisoformat(str(ds)[:10]) >= TODAY
    except: return True

def safe_get(url: str, timeout: int = 18, accept_json: bool = False):
    headers = {"Accept": "application/json, text/javascript, */*"} if accept_json else {}
    try:
        r = SESSION.get(url, timeout=timeout, verify=False,
                        headers=headers, allow_redirects=True)
        if r.status_code < 400: return r
    except Exception as e:
        log.debug(f"GET {url}: {e}")
    return None


# ── Carregamento e filtragem do CSV ───────────────────────────────────────────
# Possíveis nomes de colunas (case-insensitive)
_COL_NOME  = ["Nome completo","nome","NOME COMPLETO","Nome"]
_COL_EMAIL = ["E-mail Principal","email","EMAIL","E-mail","e-mail","E-Mail Principal"]
_COL_SIT   = ["Situação","Situacao","situacao","SITUAÇÃO","situação","Situação","Situação"]
_COL_JUNTA = ["Junta Comercial","junta_comercial","JUNTA COMERCIAL","Junta"]

def _find_col(sample_row: dict, options: list[str]) -> str | None:
    keys = list(sample_row.keys())
    for opt in options:
        if opt in keys: return opt
        for k in keys:
            if k.lower().strip() == opt.lower(): return k
    return None

def load_leiloeiros() -> list[dict]:
    """Lê CSV, filtra REGULAR, deduplica por domínio."""
    if not INPUT_CSV.exists():
        log.error(f"CSV não encontrado: {INPUT_CSV}")
        print(f"\n{'!'*60}")
        print(f"  Arquivo não encontrado: {INPUT_CSV}")
        print(f"  Salve o CSV de leiloeiros nesse caminho e execute novamente.")
        print(f"  Ou passe o caminho como argumento: python {__file__} caminho/arquivo.csv")
        print(f"{'!'*60}\n")
        sys.exit(1)

    # Tenta encodings comuns
    rows = []
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(INPUT_CSV, encoding=enc) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            if rows: break
        except Exception:
            continue

    if not rows:
        log.error("CSV vazio ou inválido.")
        sys.exit(1)

    sample = rows[0]
    col_nome  = _find_col(sample, _COL_NOME)
    col_email = _find_col(sample, _COL_EMAIL)
    col_sit   = _find_col(sample, _COL_SIT)
    col_junta = _find_col(sample, _COL_JUNTA)

    log.info(f"Colunas → nome:{col_nome}  email:{col_email}  situação:{col_sit}  junta:{col_junta}")

    result: dict[str, dict] = {}
    total = regular = 0

    for r in rows:
        total += 1
        sit = r.get(col_sit, "").strip() if col_sit else ""
        if "regular" not in sit.lower(): continue
        regular += 1

        nome  = r.get(col_nome,  "").strip() if col_nome  else ""
        email = r.get(col_email, "").strip() if col_email else ""
        junta = r.get(col_junta, "").strip() if col_junta else ""

        domain = extract_email_domain(email)
        if not domain: continue

        key = domain.lower().replace("www.", "")
        if key not in result:
            result[key] = {
                "nome":   nome,
                "email":  email,
                "junta":  junta,
                "site":   normalize_url(domain),
                "domain": key,
            }

    items = list(result.values())
    log.info(f"Total CSV: {total} | REGULAR: {regular} | Domínios únicos: {len(items)}")
    return items


# ── Estratégias de scraping ───────────────────────────────────────────────────
LISTING_PATHS = [
    "", "/leiloes", "/leilao", "/imoveis", "/imóveis",
    "/lotes", "/lotes/imovel", "/lotes/imoveis",
    "/judicial", "/judicial/leiloes",
    "/extrajudicial", "/extrajudicial/leiloes",
    "/catalogo", "/catálogo", "/agenda",
    "/em-andamento", "/ativos", "/bens",
    "/busca?tipo=imovel", "/leiloes?tipo=imovel",
]

def detect_listing_pages(base: str, soup: BeautifulSoup) -> list[str]:
    candidates = set()
    kws = ["leil","imov","lote","judic","catal","agenda","bens","ativo","praça","hasta","arrema"]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt  = (a.get_text() + href).lower()
        if any(k in txt for k in kws):
            full = urljoin(base, href)
            if urlparse(full).netloc == urlparse(base).netloc and len(full) < 250:
                candidates.add(full.split("?")[0])   # sem paginação na descoberta
    for path in LISTING_PATHS:
        candidates.add(base.rstrip("/") + path)
    return sorted(candidates, key=len)[:15]


def extract_imoveis_from_page(url: str, lei: dict) -> list[dict]:
    resp = safe_get(url)
    if not resp: return []
    try: resp.encoding = resp.apparent_encoding or "utf-8"
    except: pass
    text = resp.text
    low  = text.lower()
    if not any(k in low for k in ["imov","lote","leil","judic","lance","praca","hasta","arrema"]):
        return []
    soup = BeautifulSoup(text, "html.parser")
    out  = []
    out += _from_jsonld(soup, url, lei)
    if not out: out += _from_nextdata(soup, url, lei)
    if not out: out += _from_embedded_json(text, url, lei)
    if not out: out += _from_api_hints(soup, url, lei)
    if not out: out += _from_html_cards(soup, url, lei)
    return out


# ── Estratégia 1: JSON-LD ─────────────────────────────────────────────────────
def _from_jsonld(soup, url, lei):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data  = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type","")
                if t in ("Product","RealEstateListing","Offer","AuctionEvent","ItemList"):
                    if t == "ItemList":
                        for sub in item.get("itemListElement",[]):
                            out.append(_jsonld_item(sub.get("item",sub), url, lei))
                    else:
                        out.append(_jsonld_item(item, url, lei))
        except: pass
    return [x for x in out if x.get("titulo")]

def _jsonld_item(item, url, lei):
    price  = None
    offers = item.get("offers",{})
    if isinstance(offers, dict): price = offers.get("price")
    elif isinstance(offers, list) and offers: price = offers[0].get("price")
    if not price: price = item.get("price")
    addr = item.get("address","")
    if isinstance(addr, dict):
        parts = [addr.get("streetAddress",""), addr.get("addressLocality",""),
                 addr.get("addressRegion","")]
        addr = ", ".join(p for p in parts if p)
    img = ""
    imgs = item.get("image","")
    if isinstance(imgs, list) and imgs: img = imgs[0]
    elif isinstance(imgs, str): img = imgs
    return {
        "id": make_id(str(item.get("name",""))+url),
        "leiloeiro": lei["nome"], "junta": lei.get("junta",""), "site": lei["site"],
        "titulo": str(item.get("name",""))[:150],
        "descricao": str(item.get("description",""))[:300],
        "endereco": str(addr)[:200],
        "cidade": str(item.get("addressLocality",""))[:100],
        "uf": str(item.get("addressRegion",""))[:2],
        "lance_inicial": money_to_float(str(price or "")),
        "avaliacao": None,
        "data_leilao": parse_date_br(str(item.get("startDate",""))),
        "url": url, "tipo": "imovel", "imagem": img,
    }


# ── Estratégia 2: __NEXT_DATA__ ───────────────────────────────────────────────
def _from_nextdata(soup, url, lei):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag: return []
    try: return _walk_json(json.loads(tag.string or ""), url, lei)
    except: return []


# ── Estratégia 3: JSON embutido ───────────────────────────────────────────────
def _from_embedded_json(text, url, lei):
    pats = [
        r'window\.__INITIAL_STATE__\s*=\s*({.{20,}}?);',
        r'window\.__STATE__\s*=\s*({.{20,}}?);',
        r'window\.DATA\s*=\s*({.{20,}}?);',
        r'var\s+lotes\s*=\s*(\[.{20,}?\]);',
        r'var\s+imoveis\s*=\s*(\[.{20,}?\]);',
        r'var\s+produtos\s*=\s*(\[.{20,}?\]);',
        r'"lotes"\s*:\s*(\[.{20,}?\])',
    ]
    for pat in pats:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                items = _walk_json(data, url, lei)
                if items: return items
            except: pass
    return []


# ── Estratégia 4: API hints ───────────────────────────────────────────────────
def _from_api_hints(soup, url, lei):
    out = []
    for script in soup.find_all("script"):
        src = script.string or ""
        api_paths = set()
        for pat in [r'["\'](/api/lotes[^\'"<]{0,120})["\']',
                    r'["\'](/api/imoveis[^\'"<]{0,120})["\']',
                    r'["\'](/api/v\d+/[^\'"<]{0,120}lote[^\'"<]{0,80})["\']',
                    r'fetch\(["\']([^"\'<]{5,120}(?:lote|imovel)[^"\'<]{0,80})["\']',
                    r'axios\.get\(["\']([^"\'<]{5,120}(?:lote|imovel)[^"\'<]{0,80})["\']']:
            for m in re.finditer(pat, src, re.IGNORECASE):
                api_paths.add(m.group(1))
        for path in list(api_paths)[:4]:
            full = urljoin(url, path)
            r = safe_get(full, accept_json=True)
            if r:
                try:
                    out += _walk_json(r.json(), url, lei)
                except: pass
    return out


# ── Walk JSON recursivo ───────────────────────────────────────────────────────
def _walk_json(obj, url, lei, depth=0) -> list[dict]:
    if depth > 6: return []
    out = []
    if isinstance(obj, list):
        for item in obj[:200]:
            out += _walk_json(item, url, lei, depth+1)
    elif isinstance(obj, dict):
        kl = {k.lower() for k in obj}
        has_title = any(k in kl for k in ["titulo","title","name","nome","descricao","description"])
        has_price = any(k in kl for k in ["lance","preco","valor","price","amount","vlr"])
        if has_title and has_price:
            im = _dict_to_imovel(obj, url, lei)
            if im.get("titulo"): out.append(im)
        else:
            for v in obj.values():
                out += _walk_json(v, url, lei, depth+1)
    return out

def _gv(obj: dict, *keys) -> str:
    for k in keys:
        for dk in obj:
            if dk.lower() == k.lower():
                v = obj[dk]
                if isinstance(v, (str, int, float)) and str(v).strip():
                    return str(v)
    return ""

def _dict_to_imovel(obj, url, lei):
    titulo = _gv(obj,"titulo","title","name","nome","descricao","description")[:150]
    lance  = money_to_float(_gv(obj,"lance","lance_inicial","lanceinicial","preco","valor",
                                  "price","amount","vlr","valorlance","valor_lance"))
    data   = parse_date_br(_gv(obj,"data","dataleilao","data_leilao","dataLeilao",
                                 "date","startDate","datainicio","data_encerramento"))
    end    = _gv(obj,"endereco","address","logradouro","localizacao","local")[:200]
    cid    = _gv(obj,"cidade","city","municipio")[:100]
    link   = _gv(obj,"url","link","href","urlLote","urlImovel") or url
    img    = _gv(obj,"imagem","image","foto","thumbnail","img","imagemUrl","imagem_url")
    uf     = _gv(obj,"uf","estado","state","uf_leilao")[:2]
    aval   = money_to_float(_gv(obj,"avaliacao","valoravaliacao","preco_avaliacao","valor_avaliacao"))
    return {
        "id": make_id(titulo+link+str(lance)),
        "leiloeiro": lei["nome"], "junta": lei.get("junta",""), "site": lei["site"],
        "titulo": titulo, "descricao": "",
        "endereco": end, "cidade": cid, "uf": uf,
        "lance_inicial": lance, "avaliacao": aval, "data_leilao": data,
        "url": link, "tipo": "imovel", "imagem": img,
    }


# ── Estratégia 5: HTML cards ──────────────────────────────────────────────────
def _from_html_cards(soup, url, lei):
    SELS = [
        ".lote-card", ".card-lote", "[class*=lote-item]", "[class*=item-lote]",
        "[class*=property-card]", "[class*=imovel-card]", "[class*=card-imovel]",
        "article.lote", "article.item", "article.property",
        ".listing-item", ".property-item", ".auction-item",
        "[data-lote]", "[data-idlote]", "[data-id]",
        ".produto-card", "[class*=produto]",
        ".card", "article",
    ]
    cards, sel_used = [], ""
    for sel in SELS:
        found = soup.select(sel)
        if len(found) > 1 and any(len(c.get_text(" ",strip=True)) > 40 for c in found):
            cards = found; sel_used = sel; break

    out = []
    for card in cards[:100]:
        im = _parse_card(card, url, lei)
        if im and im.get("titulo"): out.append(im)
    if out: log.debug(f"    HTML cards [{sel_used}]: {len(out)}")
    return out

def _parse_card(card, url, lei):
    text = card.get_text(" ", strip=True)
    if len(text) < 30: return None
    low = text.lower()
    if not any(k in low for k in ["m2","m²","terreno","apt","casa","imov","lote","area",
                                    "quarto","andar","sala","kit","studio","apart",
                                    "lance","arrema","judicial","extrajudicial","leilao"]):
        return None
    # Título
    titulo = ""
    for ts in ["h1","h2","h3","h4","h5",".titulo",".title",".nome","strong"]:
        el = card.select_one(ts)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 5: titulo = t[:150]; break
    if not titulo: titulo = text[:100]
    # Preço
    lance = None
    for pat in [r"R\$\s*([\d.,]+)", r"Lance.*?R\$\s*([\d.,]+)", r"Valor.*?R\$\s*([\d.,]+)"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: lance = money_to_float(m.group(1)); break
    # Data
    data_leilao = None
    for pat in [r"(\d{1,2}/\d{1,2}/\d{4})", r"(\d{4}-\d{2}-\d{2})"]:
        m = re.search(pat, text)
        if m: data_leilao = parse_date_br(m.group(1)); break
    # Endereço
    end = ""
    for sel in [".endereco",".address","[class*=address]","[class*=endereco]","[class*=local]"]:
        el = card.select_one(sel)
        if el: end = el.get_text(strip=True)[:200]; break
    # Link
    link = url
    a = card.select_one("a[href]")
    if a:
        href = a.get("href","")
        if href and not href.startswith("#"): link = urljoin(url, href)
    # Imagem
    img = ""
    im_el = card.select_one("img[src]")
    if im_el: img = im_el.get("src","") or im_el.get("data-src","") or ""
    return {
        "id": make_id(titulo+link+str(lance)),
        "leiloeiro": lei["nome"], "junta": lei.get("junta",""), "site": lei["site"],
        "titulo": titulo, "descricao": text[:300],
        "endereco": end, "cidade": "", "uf": "",
        "lance_inicial": lance, "avaliacao": None, "data_leilao": data_leilao,
        "url": link, "tipo": "imovel", "imagem": img,
    }


# ── Scraping por leiloeiro ────────────────────────────────────────────────────
def scrape_leiloeiro(lei: dict) -> list[dict]:
    base = lei["site"].rstrip("/")
    log.info(f"  -> {lei['nome'][:40]} [{lei.get('junta','')}] | {base}")

    all_items = []
    visited   = set()

    resp_home = safe_get(base, timeout=22)
    if resp_home:
        try: resp_home.encoding = resp_home.apparent_encoding or "utf-8"
        except: pass
        soup_home = BeautifulSoup(resp_home.text, "html.parser")
        pages = detect_listing_pages(base, soup_home)
    else:
        pages = [base + p for p in LISTING_PATHS[:8]]

    for page_url in pages[:14]:
        if page_url in visited: continue
        visited.add(page_url)
        try:
            items = extract_imoveis_from_page(page_url, lei)
            all_items.extend(items)
            if len(all_items) >= 300: break
        except Exception as e:
            log.debug(f"    Erro {page_url}: {e}")
        time.sleep(0.4)

    # Deduplicar
    seen, deduped = set(), []
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)

    validos = [i for i in deduped if deve_inserir(i)]
    if validos: log.info(f"    {len(validos)} válidos / {len(deduped)} extraídos")
    return validos


# ── CSV ───────────────────────────────────────────────────────────────────────
def save_csv(imoveis: list[dict]):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    log.info(f"CSV: {OUTPUT_CSV} ({len(imoveis)} registros)")


# ── HTML Viewer ───────────────────────────────────────────────────────────────
def render_html(imoveis: list[dict], stats: dict):
    now_str  = datetime.now().strftime("%d/%m/%Y %H:%M")
    total    = len(imoveis)
    done     = stats.get("done", 0)
    tot      = max(stats.get("total", 1), 1)
    pct      = min(100, int(done / tot * 100))
    csv_name = OUTPUT_CSV.name

    rows_html = ""
    for im in imoveis:
        lance = "—"
        if im.get("lance_inicial"):
            try:
                lance = (f"R$ {float(im['lance_inicial']):,.2f}"
                         .replace(",","X").replace(".",",").replace("X","."))
            except: lance = str(im["lance_inicial"])
        data_  = im.get("data_leilao","") or "—"
        titulo = (im.get("titulo","") or "")[:90]
        end    = (im.get("endereco","") or im.get("cidade","") or "")[:55]
        url_   = im.get("url","") or im.get("site","")
        site_  = (im.get("site","") or "").replace("https://","").replace("http://","")[:35]
        uf_    = (im.get("uf","") or "")[:2]
        tipo_  = (im.get("tipo","") or "imovel").upper()[:12]
        img_   = im.get("imagem","") or ""
        junta_ = (im.get("junta","") or "")[:10]
        lei_   = (im.get("leiloeiro","") or "")[:40]
        iid    = im["id"]
        img_html = (f"<img class='thumb' src='{img_}' loading='lazy' "
                    f"onerror=\"this.style.display='none'\">") if img_ else ""
        rows_html += (
            f"\n<tr id='row-{iid}' class='ir' data-uf='{uf_}' data-tipo='{tipo_}' data-junta='{junta_}'>"
            f"<td><button class='bx' onclick=\"ex('{iid}')\">&#x2715;</button></td>"
            f"<td>{img_html}<a href='{url_}' target='_blank' class='tl'>{titulo}</a>"
            f"<span class='badge'>{tipo_}</span></td>"
            f"<td class='lei'>{lei_}<br><span class='jt'>{junta_}</span></td>"
            f"<td class='end'>{end} <span class='uf'>{uf_}</span></td>"
            f"<td class='lc'>{lance}</td>"
            f"<td class='dt'>{data_}</td>"
            f"<td><a href='{im.get('site','')}' target='_blank' class='sl'>{site_}</a></td>"
            f"</tr>"
        )

    juntas  = sorted({(im.get("junta","") or "") for im in imoveis if im.get("junta")})
    uf_list = sorted({(im.get("uf","") or "") for im in imoveis if im.get("uf")})
    jopts   = "".join(f"<option>{j}</option>" for j in juntas)
    uopts   = "".join(f"<option>{u}</option>" for u in uf_list)

    sts = (
        f"<span class='st'>Imóveis: <b>{total}</b></span>"
        f"<span class='st'>Sites OK: <b>{stats.get('sites_ok',0)}</b></span>"
        f"<span class='st'>Erros: <b>{stats.get('sites_err',0)}</b></span>"
        f"<span class='st'>Progresso: <b>{done}/{tot} ({pct}%)</b></span>"
        f"<span class='st'>Atualizado: <b>{now_str}</b></span>"
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Imóveis — Leiloeiros Regular — {now_str}</title>
<style>
:root{{--bg:#0f172a;--s:#1e293b;--b:#334155;--a:#3b82f6;--d:#ef4444;--ok:#22c55e;--t:#e2e8f0;--m:#94a3b8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--t);font-family:'Segoe UI',system-ui,sans-serif;font-size:.84rem}}
header{{background:var(--s);border-bottom:1px solid var(--b);padding:12px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
header h1{{font-size:1rem;color:#fff}}
.stats{{display:flex;gap:7px;flex-wrap:wrap}}
.st{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:3px 10px;color:var(--m);font-size:.76rem}}
.st b{{color:var(--t)}}
.pbar{{height:3px;background:var(--b)}}.pbar div{{height:100%;background:var(--a);width:{pct}%}}
.tb{{padding:8px 18px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--b);background:var(--s)}}
.tb input,.tb select{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:5px 8px;color:var(--t);font-size:.8rem}}
.tb input{{flex:1;min-width:160px}}.tb input:focus,.tb select:focus{{outline:none;border-color:var(--a)}}
.btn{{padding:5px 13px;border-radius:5px;border:none;cursor:pointer;font-size:.79rem;font-weight:600;white-space:nowrap}}
.btn-d{{background:var(--d);color:#fff}}.btn-d:hover{{opacity:.85}}
.btn-ok{{background:var(--ok);color:#fff}}.btn-ok:hover{{opacity:.85}}
.cnt{{margin-left:auto;color:var(--m);font-size:.78rem}}
.tw{{overflow-x:auto;padding:0 18px 20px}}
table{{width:100%;border-collapse:collapse;margin-top:12px;min-width:820px}}
thead th{{background:var(--s);color:var(--m);text-align:left;padding:8px 6px;border-bottom:2px solid var(--b);white-space:nowrap;cursor:pointer;user-select:none}}
thead th:hover{{color:var(--t)}}
tbody tr{{border-bottom:1px solid var(--b);transition:background .08s}}
tbody tr:hover{{background:rgba(59,130,246,.06)}}
tr.hidden{{display:none!important}}
tr.excluded{{opacity:.12;pointer-events:none;background:rgba(239,68,68,.04)!important}}
td{{padding:7px 6px;vertical-align:middle}}
.bx{{background:var(--d);color:#fff;border:none;border-radius:4px;width:24px;height:24px;cursor:pointer;font-size:.85rem;display:inline-flex;align-items:center;justify-content:center}}
.bx:hover{{background:#dc2626}}
.tl{{color:var(--a);text-decoration:none;font-weight:500}}.tl:hover{{text-decoration:underline}}
.sl{{color:var(--m);font-size:.75rem;text-decoration:none}}.sl:hover{{color:var(--t)}}
.lc{{font-weight:700;color:var(--ok);white-space:nowrap}}
.lei{{font-size:.78rem;color:var(--m)}}
.jt{{font-size:.67rem;background:rgba(148,163,184,.12);border-radius:3px;padding:1px 4px;margin-top:2px;display:inline-block;color:var(--m)}}
.uf{{background:var(--b);border-radius:3px;padding:1px 5px;font-size:.7rem;color:var(--m);margin-left:3px}}
.badge{{background:rgba(59,130,246,.12);color:var(--a);border-radius:3px;padding:1px 5px;font-size:.67rem;margin-left:4px;vertical-align:middle}}
.thumb{{width:52px;height:40px;object-fit:cover;border-radius:4px;margin-right:6px;vertical-align:middle}}
.dt{{white-space:nowrap;font-size:.79rem}}
.end{{font-size:.79rem;color:var(--m)}}
.toast{{position:fixed;bottom:18px;right:18px;background:var(--ok);color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;display:none;z-index:200;box-shadow:0 4px 16px rgba(0,0,0,.3)}}
#mo{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:150;align-items:center;justify-content:center}}
#mo.open{{display:flex}}
#mid{{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:24px;max-width:460px;width:90%}}
#mid h2{{margin-bottom:10px;font-size:1.05rem}}
#mid p{{color:var(--m);margin-bottom:18px;font-size:.87rem;line-height:1.5}}
#mid .btns{{display:flex;gap:8px;justify-content:flex-end}}
</style>
</head>
<body>
<header>
  <h1>🏠 Imóveis — Leiloeiros REGULAR (Todas as Juntas)</h1>
  <div class="stats">{sts}</div>
</header>
<div class="pbar"><div></div></div>
<div class="tb">
  <input type="text" id="srch" placeholder="Filtrar por título, leiloeiro, endereço..." oninput="fil()">
  <select id="su" onchange="fil()"><option value="">UF</option>{uopts}</select>
  <select id="sj" onchange="fil()"><option value="">Junta Comercial</option>{jopts}</select>
  <select id="st" onchange="fil()">
    <option value="">Tipo</option>
    <option>IMOVEL</option><option>CASA</option><option>APARTAMENTO</option>
    <option>TERRENO</option><option>COMERCIAL</option><option>RURAL</option>
  </select>
  <select id="ss" onchange="sortTab()">
    <option value="data">Ordenar: Data</option>
    <option value="lance">Ordenar: Lance ↓</option>
    <option value="leiloeiro">Ordenar: Leiloeiro</option>
  </select>
  <button class="btn btn-d" onclick="exTodos()">&#x2715; Excluir visíveis</button>
  <button class="btn btn-ok" onclick="abrirModal()">⬇ Exportar CSV</button>
  <span class="cnt" id="cnt">{total} registros</span>
</div>
<div class="tw">
  <table id="tab">
    <thead>
      <tr>
        <th style="width:28px"></th>
        <th onclick="setSC('titulo')">Título ↕</th>
        <th onclick="setSC('leiloeiro')">Leiloeiro / Junta ↕</th>
        <th onclick="setSC('cidade')">Endereço / UF ↕</th>
        <th onclick="setSC('lance')">Lance Inicial ↕</th>
        <th onclick="setSC('data')">Data Leilão ↕</th>
        <th>Site</th>
      </tr>
    </thead>
    <tbody id="tbody">{rows_html}</tbody>
  </table>
</div>
<div class="toast" id="toast"></div>
<div id="mo">
  <div id="mid">
    <h2>Exportar para CSV</h2>
    <p>Serão exportados <b id="mc">0</b> imóveis selecionados.<br>Arquivo: <code>{csv_name}</code></p>
    <div class="btns">
      <button class="btn" style="background:var(--b);color:var(--t)" onclick="closeMo()">Cancelar</button>
      <button class="btn btn-ok" onclick="doExp()">✓ Confirmar Export</button>
    </div>
  </div>
</div>
<script>
const DATA = {json.dumps(imoveis, ensure_ascii=False, default=str)};
const FN   = "{csv_name}";
const FNS  = {json.dumps(FIELDNAMES)};
const excl = new Set();
let sc='data', sasc=true;

function ex(id){{
  excl.add(id);
  const r=document.getElementById('row-'+id);
  if(r) r.classList.add('excluded');
  upCnt();
}}
function exTodos(){{
  document.querySelectorAll('#tbody tr.ir:not(.hidden):not(.excluded)').forEach(r=>{{
    excl.add(r.id.replace('row-',''));
    r.classList.add('excluded');
  }});
  upCnt();
}}
function fil(){{
  const q=document.getElementById('srch').value.toLowerCase();
  const uf=document.getElementById('su').value;
  const jt=document.getElementById('sj').value;
  const tp=document.getElementById('st').value;
  let v=0;
  document.querySelectorAll('#tbody tr.ir').forEach(r=>{{
    const txt=r.textContent.toLowerCase();
    const ok=(!q||txt.includes(q))&&(!uf||r.dataset.uf===uf)
             &&(!jt||r.dataset.junta===jt)&&(!tp||r.dataset.tipo.includes(tp));
    r.classList.toggle('hidden',!ok);
    if(ok&&!r.classList.contains('excluded')) v++;
  }});
  document.getElementById('cnt').textContent=v+' registros';
}}
function upCnt(){{
  const t=document.querySelectorAll('#tbody tr.ir:not(.hidden):not(.excluded)').length;
  document.getElementById('cnt').textContent=t+' registros';
}}
function setSC(c){{sc=c;sasc=!sasc;sortTab();}}
function sortTab(){{
  const col=document.getElementById('ss').value||sc;
  const tbody=document.getElementById('tbody');
  const rows=Array.from(tbody.querySelectorAll('tr.ir'));
  rows.sort((a,b)=>{{
    let av='',bv='';
    if(col==='lance'){{
      av=parseFloat((a.querySelector('.lc')?.textContent||'0').replace(/[^\\d.]/g,''))||0;
      bv=parseFloat((b.querySelector('.lc')?.textContent||'0').replace(/[^\\d.]/g,''))||0;
      return bv-av;
    }}else if(col==='data'){{
      av=a.querySelector('.dt')?.textContent||''; bv=b.querySelector('.dt')?.textContent||'';
    }}else{{av=a.textContent.trim();bv=b.textContent.trim();}}
    return sasc?av.localeCompare(bv,'pt'):bv.localeCompare(av,'pt');
  }});
  rows.forEach(r=>tbody.appendChild(r));
}}
function abrirModal(){{
  document.getElementById('mc').textContent=DATA.filter(d=>!excl.has(d.id)).length;
  document.getElementById('mo').classList.add('open');
}}
function closeMo(){{document.getElementById('mo').classList.remove('open');}}
function doExp(){{
  closeMo();
  const sel=DATA.filter(d=>!excl.has(d.id));
  const lines=[FNS.join(',')];
  sel.forEach(row=>{{
    const vals=FNS.map(f=>'"'+(row[f]??'').toString().replace(/\\n/g,' ').replace(/"/g,'""')+'"');
    lines.push(vals.join(','));
  }});
  const blob=new Blob(['\\uFEFF'+lines.join('\\n')],{{type:'text/csv;charset=utf-8;'}});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=FN; a.click();
  toast('Exportados '+sel.length+' imóveis → '+FN);
}}
function toast(msg,d=4000){{
  const el=document.getElementById('toast');
  el.textContent=msg; el.style.display='block';
  setTimeout(()=>el.style.display='none',d);
}}
document.addEventListener('DOMContentLoaded',sortTab);
</script>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Viewer: {OUTPUT_HTML}")


# ── Progresso ─────────────────────────────────────────────────────────────────
REPORT_EVERY = 5 * 60  # 5 minutos

def print_progress(stats: dict, total_imoveis: int):
    done  = stats.get("done", 0)
    total = max(stats.get("total", 1), 1)
    pct   = int(done / total * 100)
    bar   = "█" * (pct // 5) + "░" * (20 - pct // 5)
    ts    = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'═'*60}")
    print(f"  {ts}  PROGRESSO [{bar}] {pct}%")
    print(f"  Sites: {done}/{stats.get('total',0)}"
          f"  ✓ OK:{stats.get('sites_ok',0)}"
          f"  ✗ Err:{stats.get('sites_err',0)}")
    print(f"  Imóveis capturados: {total_imoveis}")
    print(f"{'═'*60}\n")
    sys.stdout.flush()

def save_progress(stats: dict, total_imoveis: int):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({**stats, "total_imoveis": total_imoveis,
                   "updated": datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("="*60)
    print("  SCRAPER — LEILOEIROS REGULAR (TODAS AS JUNTAS)")
    print(f"  Data: {TODAY}")
    print(f"  CSV entrada: {INPUT_CSV}")
    print("="*60 + "\n")

    leiloeiros = load_leiloeiros()

    stats: dict = {"total": len(leiloeiros), "done": 0,
                   "sites_ok": 0, "sites_err": 0}
    all_imoveis: list[dict] = []
    last_report  = time.time()

    # Gera viewer vazio inicial
    render_html([], stats)

    print(f"Domínios únicos a scraping: {len(leiloeiros)}\n")

    for idx, lei in enumerate(leiloeiros, 1):
        stats["done"] = idx
        try:
            items = scrape_leiloeiro(lei)
            all_imoveis.extend(items)
            if items: stats["sites_ok"] += 1
            else:     stats["sites_err"] += 1
        except Exception as e:
            log.warning(f"ERRO {lei.get('site','?')}: {e}")
            stats["sites_err"] += 1

        # ── Relatório a cada 5 minutos ──
        if time.time() - last_report >= REPORT_EVERY:
            print_progress(stats, len(all_imoveis))
            save_csv(all_imoveis)
            render_html(all_imoveis, stats)
            save_progress(stats, len(all_imoveis))
            last_report = time.time()

        time.sleep(0.7)

    # ── Final ──
    print_progress(stats, len(all_imoveis))
    save_csv(all_imoveis)
    render_html(all_imoveis, stats)
    save_progress(stats, len(all_imoveis))

    log.info(f"=== CONCLUÍDO! {len(all_imoveis)} imóveis de {stats['sites_ok']} sites ===")
    print(f"\nViewer: {OUTPUT_HTML}")
    print(f"CSV:    {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
