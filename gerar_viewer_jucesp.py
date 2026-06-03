"""
Gera viewer_imoveis_jucesp.html com dados de múltiplas fontes:
1. imoveis_completo CSV (dados ricos já coletados)
2. Scraping adicional de sites de leiloeiros JUCESP acessíveis
"""
import sys, io, csv, json, re, time, hashlib, warnings
from datetime import date, datetime
from urllib.parse import urlparse, urljoin
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"
TODAY    = date.today()

FIELDNAMES = ["id","leiloeiro","site","titulo","descricao","endereco","cidade","uf",
              "lance_inicial","avaliacao","data_leilao","url","tipo","imagem"]

# ── Session ────────────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.3)))
    s.mount("http://",  HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.3)))
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
                       "Accept-Language": "pt-BR,pt;q=0.9"})
    return s
SES = make_session()

def safe_get(url, timeout=12):
    try:
        r = SES.get(url, timeout=timeout, verify=False, allow_redirects=True)
        if r.status_code < 400: return r
    except: pass

def money_to_float(txt):
    if not txt: return None
    txt = re.sub(r"[^\d,.]", "", str(txt))
    if "," in txt and "." in txt: txt = txt.replace(".", "").replace(",", ".")
    elif "," in txt: txt = txt.replace(",", ".")
    try: return float(txt)
    except: return None

def parse_date_br(txt):
    if not txt: return None
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", str(txt))
    if m:
        d,mo,y = m.group(1), m.group(2), m.group(3)
        if len(y)==2: y = "20"+y
        try: return date(int(y),int(mo),int(d)).isoformat()
        except: pass
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(txt))
    if m2: return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None

def make_id(s): return hashlib.md5(str(s).encode()).hexdigest()[:12]

def deve_inserir(data_str):
    if not data_str: return True
    try: return date.fromisoformat(data_str[:10]) >= TODAY
    except: return True


# ── Fonte 1: imoveis_completo CSV ─────────────────────────────────────────────
def load_from_completo() -> list[dict]:
    path = CSV_DIR / "imoveis_completo_20260601_1708.csv"
    if not path.exists():
        print(f"[AVISO] Nao encontrado: {path}")
        return []

    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # Nomes normalizados dos leiloeiros JUCESP regulares
    reg_path = BASE_DIR / "leiloeiros_regulares.csv"
    regulares_nomes = set()
    if reg_path.exists():
        with open(reg_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("junta_comercial","").upper() == "JUCESP":
                    regulares_nomes.add(re.sub(r"[^a-z]","",r["nome"].lower()))

    result = []
    seen_ids = set()
    for im in rows:
        lei = im.get("leiloeiro","")
        # Deve ter JUCESP ou ser de leiloeiro regular SP
        is_jucesp = "jucesp" in lei.lower()
        if not is_jucesp:
            # Tenta match por nome
            clean = re.sub(r"[^a-z]","", lei.lower())
            is_jucesp = any(clean in n or n in clean for n in regulares_nomes if len(n)>8)

        if not is_jucesp:
            continue

        # Filtro de data
        data_str = parse_date_br(im.get("data_primeiro_leilao","") or im.get("data_encerramento",""))
        if not deve_inserir(data_str):
            continue

        lance = money_to_float(im.get("valor_minimo",""))
        aval  = money_to_float(im.get("valor_avaliacao",""))

        # Extrai site do leiloeiro a partir do URL original
        site = ""
        url_orig = im.get("url_original","")
        if url_orig:
            parsed = urlparse(url_orig)
            site = f"{parsed.scheme}://{parsed.netloc}"

        row = {
            "id": make_id(im.get("id","") + url_orig),
            "leiloeiro": re.sub(r"(?i)\boficial\b|\bJUCESP\b|\d+","", lei).strip()[:60],
            "site": site,
            "titulo": (im.get("titulo","") or "")[:150],
            "descricao": (im.get("descricao","") or "")[:400],
            "endereco": im.get("cidade",""),
            "cidade": im.get("cidade",""),
            "uf": im.get("estado","SP"),
            "lance_inicial": lance,
            "avaliacao": aval,
            "data_leilao": data_str,
            "url": url_orig,
            "tipo": im.get("tipo_imovel","imovel").lower(),
            "imagem": im.get("imagem_principal",""),
        }
        if row["id"] not in seen_ids:
            seen_ids.add(row["id"])
            result.append(row)

    print(f"[COMPLETO CSV] {len(result)} imoveis JUCESP validos")
    return result


# ── Fonte 2: scraping adicional de sites acessíveis ───────────────────────────
EMAIL_SERVICES = {"gmail","hotmail","yahoo","terra","bol","uol","outlook","ig","live","icloud","msn","globo"}
LEILAO_KW = ["leil","lance","arrema","lote","hasta","imovel","judicial"]

def load_leiloeiros_com_site():
    reg_path = BASE_DIR / "leiloeiros_regulares.csv"
    result = {}
    with open(reg_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("junta_comercial","").upper() != "JUCESP": continue
            site = (r.get("site","") or "").strip().rstrip("/")
            if not site:
                email = r.get("email","").lower()
                m = re.search(r"@([\w.\-]+)", email)
                if m:
                    dom = m.group(1)
                    base = dom.split(".")[-2] if len(dom.split("."))>=2 else dom
                    if base not in EMAIL_SERVICES and any(k in dom for k in LEILAO_KW):
                        site = f"https://{dom}"
            if not site: continue
            if not site.startswith("http"): site = "https://" + site
            key = urlparse(site).netloc.replace("www.","").lower()
            if key and key not in result:
                result[key] = {"nome": r["nome"], "site": site, "cidade": r.get("cidade","")}
    return list(result.values())

def scrape_frazao() -> list[dict]:
    """Scrapa frazaoleiloes.com.br que tem conteúdo rico."""
    base = "https://frazaoleiloes.com.br"
    out = []

    sections = ["/judicial/leiloes", "/itau/leiloes", "/santander/leiloes", "/alienacao-fiduciaria/leiloes"]
    for path in sections:
        r = safe_get(base + path)
        if not r: continue
        try: r.encoding = r.apparent_encoding or "utf-8"
        except: pass
        soup = BeautifulSoup(r.text, "html.parser")

        # Extrai lotes da página
        for card_sel in ["[class*=lote]","[class*=card-item]","[class*=auction]",".item","article"]:
            cards = soup.select(card_sel)
            if len(cards) > 2:
                for card in cards[:50]:
                    text = card.get_text(" ",strip=True)
                    if len(text) < 40: continue
                    titulo = ""
                    for ts in ["h1","h2","h3","h4",".titulo",".title","strong"]:
                        el = card.select_one(ts)
                        if el and len(el.get_text(strip=True)) > 5:
                            titulo = el.get_text(strip=True)[:150]
                            break
                    if not titulo: titulo = text[:100]

                    link = base + path
                    a = card.select_one("a[href]")
                    if a and a["href"] and not a["href"].startswith("#"):
                        link = urljoin(base, a["href"])

                    lance = None
                    m = re.search(r"R\$\s*([\d.,]+)", text, re.I)
                    if m: lance = money_to_float(m.group(1))

                    data_str = None
                    m2 = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})", text)
                    if m2: data_str = parse_date_br(m2.group(1))

                    if not deve_inserir(data_str): continue

                    im = {
                        "id": make_id(titulo + link),
                        "leiloeiro": "Frazao Leiloes",
                        "site": base,
                        "titulo": titulo,
                        "descricao": text[:300],
                        "endereco": "",
                        "cidade": "Sao Paulo",
                        "uf": "SP",
                        "lance_inicial": lance,
                        "avaliacao": None,
                        "data_leilao": data_str,
                        "url": link,
                        "tipo": "imovel",
                        "imagem": "",
                    }
                    out.append(im)
                if out: break

    print(f"[FRAZAO SCRAPE] {len(out)} imoveis")
    return out


def scrape_generic_sites(leiloeiros: list[dict]) -> list[dict]:
    """Tenta scraping genérico em cada site."""
    out = []
    LISTING_PATHS = ["","/leiloes","/imoveis","/lotes","/lotes/imovel",
                     "/catalogo","/agenda","/judicial/leiloes"]

    for lei in leiloeiros:
        base = lei["site"].rstrip("/")
        print(f"  Testando {lei['nome'][:40]}: {base}")
        site_items = []

        visited = set()
        # Pega home para detectar links
        r = safe_get(base)
        if r:
            try: r.encoding = r.apparent_encoding or "utf-8"
            except: pass
            soup = BeautifulSoup(r.text, "html.parser")

            # Coleta links de leiloes
            pages = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                txt = (a.get_text() + href).lower()
                if any(k in txt for k in ["imov","lote","leil","judic","hasta"]):
                    full = urljoin(base, href)
                    if urlparse(full).netloc == urlparse(base).netloc:
                        pages.add(full)

            for path in LISTING_PATHS[:5]:
                pages.add(base + path)

            for page_url in sorted(pages, key=len)[:8]:
                if page_url in visited: continue
                visited.add(page_url)

                pr = safe_get(page_url)
                if not pr: continue
                try: pr.encoding = pr.apparent_encoding or "utf-8"
                except: pass
                text = pr.text
                low = text.lower()

                if not any(k in low for k in ["imov","lote","leil","judic"]): continue

                # Tenta JSON embutido
                sp2 = BeautifulSoup(text, "html.parser")

                # __NEXT_DATA__
                nd = sp2.find("script", id="__NEXT_DATA__")
                if nd:
                    try:
                        data = json.loads(nd.string or "")
                        items = extract_from_json(data, page_url, lei)
                        site_items.extend(items)
                    except: pass

                # JSON embutido em variáveis
                for pat in [r'window\.__INITIAL_STATE__\s*=\s*({.+?});',
                             r'var\s+lotes\s*=\s*(\[.+?\]);',
                             r'var\s+imoveis\s*=\s*(\[.+?\]);']:
                    m = re.search(pat, text, re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group(1))
                            site_items.extend(extract_from_json(data, page_url, lei))
                        except: pass

                # HTML cards
                if not site_items:
                    items = extract_html_cards(sp2, page_url, lei)
                    site_items.extend(items)

                time.sleep(0.3)

        # Deduplicar site
        seen = set()
        for item in site_items:
            if item["id"] not in seen and deve_inserir(item.get("data_leilao")):
                seen.add(item["id"])
                out.append(item)

        if site_items:
            print(f"    -> {len(seen)} imoveis encontrados")

        time.sleep(0.5)

    return out


def extract_from_json(obj, url, lei, depth=0):
    if depth > 5: return []
    out = []
    if isinstance(obj, list):
        for item in obj[:100]: out += extract_from_json(item, url, lei, depth+1)
    elif isinstance(obj, dict):
        kl = {k.lower() for k in obj}
        has_t = any(k in kl for k in ["titulo","title","name","nome"])
        has_p = any(k in kl for k in ["lance","preco","valor","price","vlr"])
        if has_t and has_p:
            im = _dict_to_imovel(obj, url, lei)
            if im.get("titulo"): out.append(im)
        else:
            for v in obj.values(): out += extract_from_json(v, url, lei, depth+1)
    return out

def _gval(obj, *keys):
    for k in keys:
        for dk in obj:
            if dk.lower() == k:
                v = obj[dk]
                if isinstance(v,(str,int,float)) and str(v).strip():
                    return str(v)
    return ""

def _dict_to_imovel(obj, url, lei):
    titulo = _gval(obj,"titulo","title","name","nome","descricao")[:150]
    lance  = money_to_float(_gval(obj,"lance","lance_inicial","preco","valor","price","vlr"))
    data   = parse_date_br(_gval(obj,"data","dataleilao","data_leilao","date","startdate"))
    end    = _gval(obj,"endereco","address","logradouro","local")[:200]
    cid    = _gval(obj,"cidade","city","municipio") or lei.get("cidade","")
    link   = _gval(obj,"url","link","href") or url
    img    = _gval(obj,"imagem","image","foto","thumbnail","img")
    return {
        "id": make_id(titulo+link),
        "leiloeiro": lei["nome"][:60],
        "site": lei["site"],
        "titulo": titulo,
        "descricao": "",
        "endereco": end,
        "cidade": cid[:80],
        "uf": _gval(obj,"uf","estado","state") or "SP",
        "lance_inicial": lance,
        "avaliacao": money_to_float(_gval(obj,"avaliacao","valoravaliacao")),
        "data_leilao": data,
        "url": link,
        "tipo": "imovel",
        "imagem": img,
    }

def extract_html_cards(soup, url, lei):
    SELS = ["[class*=lote-card]","[class*=card-lote]","[class*=imovel-card]",
            "[class*=property-card]","[class*=listing-item]","article.card",
            "[data-lote]","[data-id]","article","[class*=lote]:not(label)"]
    cards = []
    for sel in SELS:
        found = soup.select(sel)
        if len(found) > 1 and any(len(c.get_text(strip=True)) > 50 for c in found):
            cards = found[:100]
            break

    out = []
    for card in cards:
        text = card.get_text(" ", strip=True)
        if len(text) < 40: continue
        low = text.lower()
        if not any(k in low for k in ["m²","m2","terreno","apart","casa","imov","lote"]): continue

        titulo = ""
        for ts in ["h1","h2","h3","h4","strong"]:
            el = card.select_one(ts)
            if el and len(el.get_text(strip=True)) > 5:
                titulo = el.get_text(strip=True)[:150]
                break
        if not titulo: titulo = text[:100]

        link = url
        a = card.select_one("a[href]")
        if a and a.get("href","") and not a["href"].startswith("#"):
            link = urljoin(url, a["href"])

        lance = None
        m = re.search(r"R\$\s*([\d.,]+)", text, re.I)
        if m: lance = money_to_float(m.group(1))

        data_str = None
        m2 = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})", text)
        if m2: data_str = parse_date_br(m2.group(1))

        img = ""
        im_el = card.select_one("img[src]")
        if im_el: img = im_el.get("src","")

        out.append({
            "id": make_id(titulo+link),
            "leiloeiro": lei["nome"][:60],
            "site": lei["site"],
            "titulo": titulo,
            "descricao": text[:300],
            "endereco": lei.get("cidade",""),
            "cidade": lei.get("cidade",""),
            "uf": "SP",
            "lance_inicial": lance,
            "avaliacao": None,
            "data_leilao": data_str,
            "url": link,
            "tipo": "imovel",
            "imagem": img,
        })
    return out


# ── HTML Viewer ────────────────────────────────────────────────────────────────
def render_html(imoveis: list[dict], stats: dict):
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    total = len(imoveis)
    pct   = min(100, int(stats.get("done",100) / max(stats.get("total",1),1) * 100))
    csv_name = f"imoveis_jucesp_{TODAY}.csv"

    rows_html = ""
    for im in imoveis:
        lance = "—"
        if im.get("lance_inicial"):
            try: lance = f"R$ {float(im['lance_inicial']):,.2f}".replace(",","X").replace(".",",").replace("X",".")
            except: lance = str(im["lance_inicial"])

        data  = im.get("data_leilao","") or "—"
        titulo = (im.get("titulo","") or "")[:90]
        end   = (im.get("endereco","") or im.get("cidade","") or "")[:55]
        url_  = im.get("url","") or im.get("site","")
        site_ = (im.get("site","") or "").replace("https://","").replace("http://","")[:35]
        uf_   = im.get("uf","SP") or "SP"
        tipo_ = (im.get("tipo","") or "").upper()[:12]
        img_  = im.get("imagem","") or ""

        img_html = f"<img class='thumb' src='{img_}' onerror=\"this.style.display='none'\">" if img_ else ""
        lei_name = im.get('leiloeiro','')[:40]
        site_href = im.get('site','')
        iid = im['id']
        rows_html += (
            f"\n<tr id='row-{iid}' class='imovel-row' data-uf='{uf_}' data-tipo='{tipo_}'>"
            f"<td><button class='btn-x' onclick=\"excluir('{iid}')\">X</button></td>"
            f"<td>{img_html}<a href='{url_}' target='_blank' class='titulo-link'>{titulo}</a>"
            f"<span class='badge'>{tipo_}</span></td>"
            f"<td class='lei'>{lei_name}</td>"
            f"<td class='end'>{end} <span class='uf'>{uf_}</span></td>"
            f"<td class='lance'>{lance}</td>"
            f"<td class='data'>{data}</td>"
            f"<td><a href='{site_href}' target='_blank' class='site-link'>{site_}</a></td>"
            f"</tr>"
        )

    stats_bar = (
        f"<span class='st'>Imoveis: <b>{total}</b></span>"
        f"<span class='st'>Fontes: <b>CSV + scraping</b></span>"
        f"<span class='st'>Progresso: <b>{pct}%</b></span>"
        f"<span class='st'>Data: <b>{now_str}</b></span>"
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Imoveis JUCESP Atuante Regular - {now_str}</title>
<style>
:root{{--bg:#0f172a;--s:#1e293b;--b:#334155;--a:#3b82f6;--d:#ef4444;--ok:#22c55e;--t:#e2e8f0;--m:#94a3b8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--t);font:'Segoe UI',sans-serif;font-size:.85rem}}
header{{background:var(--s);border-bottom:1px solid var(--b);padding:12px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
header h1{{font-size:1rem;color:#fff}}
.stats{{display:flex;gap:8px;flex-wrap:wrap}}
.st{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:3px 10px;color:var(--m);font-size:.78rem}}
.st b{{color:var(--t)}}
.pbar{{height:3px;background:var(--b)}}
.pbar div{{height:100%;background:var(--a);width:{pct}%}}
.tb{{padding:8px 18px;display:flex;gap:7px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--b);background:var(--s)}}
.tb input,.tb select{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:5px 9px;color:var(--t);font-size:.82rem}}
.tb input{{flex:1;min-width:160px}}.tb input:focus,.tb select:focus{{outline:none;border-color:var(--a)}}
.btn{{padding:5px 13px;border-radius:5px;border:none;cursor:pointer;font-size:.8rem;font-weight:600}}
.btn-d{{background:var(--d);color:#fff}}.btn-d:hover{{opacity:.85}}
.btn-ok{{background:var(--ok);color:#fff}}.btn-ok:hover{{opacity:.85}}
.cnt{{margin-left:auto;color:var(--m);font-size:.78rem}}
.tw{{overflow-x:auto;padding:0 18px 18px}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}
thead th{{background:var(--s);color:var(--m);text-align:left;padding:8px 6px;border-bottom:2px solid var(--b);white-space:nowrap;cursor:pointer}}
thead th:hover{{color:var(--t)}}
tbody tr{{border-bottom:1px solid var(--b)}}
tbody tr:hover{{background:rgba(59,130,246,.05)}}
tr.hidden{{display:none!important}}
tr.excluded{{opacity:.2;pointer-events:none}}
td{{padding:7px 6px;vertical-align:middle}}
.btn-x{{background:var(--d);color:#fff;border:none;border-radius:4px;width:22px;height:22px;cursor:pointer;font-size:.78rem}}
.titulo-link{{color:var(--a);text-decoration:none;font-weight:500}}.titulo-link:hover{{text-decoration:underline}}
.site-link{{color:var(--m);font-size:.76rem;text-decoration:none}}.site-link:hover{{color:var(--t)}}
.lance{{font-weight:700;color:var(--ok);white-space:nowrap}}
.lei{{color:var(--m);font-size:.78rem}}
.uf{{background:var(--b);border-radius:3px;padding:1px 4px;font-size:.7rem;color:var(--m)}}
.badge{{background:rgba(59,130,246,.15);color:var(--a);border-radius:3px;padding:1px 5px;font-size:.68rem;margin-left:4px}}
.thumb{{width:50px;height:38px;object-fit:cover;border-radius:3px;margin-right:6px;vertical-align:middle}}
.data{{white-space:nowrap;font-size:.8rem}}
.toast{{position:fixed;bottom:18px;right:18px;background:var(--ok);color:#fff;padding:9px 16px;border-radius:7px;font-size:.84rem;display:none;z-index:99;box-shadow:0 4px 12px rgba(0,0,0,.3)}}
#mo{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;align-items:center;justify-content:center}}
#mo.open{{display:flex}}
#mid{{background:var(--s);border:1px solid var(--b);border-radius:10px;padding:22px;max-width:440px;width:90%}}
#mid h2{{margin-bottom:8px;font-size:1rem}}
#mid p{{color:var(--m);margin-bottom:16px;font-size:.85rem}}
#mid .btns{{display:flex;gap:7px;justify-content:flex-end}}
</style>
</head>
<body>
<header>
  <h1>Imoveis — Leiloeiros JUCESP Atuante Regular</h1>
  <div class="stats">{stats_bar}</div>
</header>
<div class="pbar"><div></div></div>
<div class="tb">
  <input type="text" id="srch" placeholder="Filtrar titulo, leiloeiro, cidade..." oninput="fil()">
  <select id="sel-uf" onchange="fil()">
    <option value="">UF</option>
    <option>SP</option><option>RJ</option><option>MG</option><option>RS</option>
    <option>SC</option><option>PR</option><option>BA</option><option>GO</option>
  </select>
  <select id="sel-tipo" onchange="fil()">
    <option value="">Tipo</option>
    <option>CASA</option><option>APARTAMENTO</option><option>TERRENO</option>
    <option>COMERCIAL</option><option>RURAL</option><option>GALPAO</option>
  </select>
  <select id="sel-sort" onchange="sortTable()">
    <option value="data">Ordenar: Data</option>
    <option value="lance">Ordenar: Lance</option>
    <option value="desc">Ordenar: Desconto</option>
  </select>
  <button class="btn btn-d" onclick="excTodos()">X Excluir visiveis</button>
  <button class="btn btn-ok" onclick="abrirModal()">Exportar CSV / Banco</button>
  <span class="cnt" id="cnt">{total} registros</span>
</div>
<div class="tw">
  <table id="tab">
    <thead>
      <tr>
        <th></th>
        <th onclick="setSortCol('titulo')">Titulo</th>
        <th onclick="setSortCol('leiloeiro')">Leiloeiro</th>
        <th onclick="setSortCol('cidade')">Cidade/UF</th>
        <th onclick="setSortCol('lance')">Lance Inicial</th>
        <th onclick="setSortCol('data')">Data Leilao</th>
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
    <p>Serao exportados <b id="mc">0</b> imoveis (nao excluidos).</p>
    <div class="btns">
      <button class="btn" style="background:var(--b)" onclick="closeModal()">Cancelar</button>
      <button class="btn btn-ok" onclick="doExport()">Confirmar export</button>
    </div>
  </div>
</div>
<script>
const DATA = {json.dumps(imoveis, ensure_ascii=False, default=str)};
const FN = "{csv_name}";
const excl = new Set();
let sortCol = 'data', sortAsc = true;

function excluir(id) {{
  excl.add(id);
  const r = document.getElementById('row-'+id);
  if(r) r.classList.add('excluded');
  upCnt();
}}
function excTodos() {{
  document.querySelectorAll('#tbody tr:not(.hidden):not(.excluded)').forEach(r=>{{
    excl.add(r.id.replace('row-',''));
    r.classList.add('excluded');
  }});
  upCnt();
}}
function fil() {{
  const q = document.getElementById('srch').value.toLowerCase();
  const uf = document.getElementById('sel-uf').value;
  const tp = document.getElementById('sel-tipo').value;
  let v = 0;
  document.querySelectorAll('#tbody tr').forEach(r=>{{
    const txt = r.textContent.toLowerCase();
    const ruf = r.dataset.uf||'';
    const rtp = r.dataset.tipo||'';
    const show = (!q||txt.includes(q)) && (!uf||ruf===uf) && (!tp||rtp.includes(tp));
    r.classList.toggle('hidden',!show);
    if(show && !r.classList.contains('excluded')) v++;
  }});
  document.getElementById('cnt').textContent = v+' registros';
}}
function upCnt() {{
  const t = document.querySelectorAll('#tbody tr:not(.hidden):not(.excluded)').length;
  document.getElementById('cnt').textContent = t+' registros';
}}
function setSortCol(c) {{ sortCol=c; sortAsc=!sortAsc; sortTable(); }}
function sortTable() {{
  const sc = document.getElementById('sel-sort').value||sortCol;
  const tbody = document.getElementById('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a,b)=>{{
    let av='',bv='';
    if(sc==='data'){{ av=a.querySelector('.data')?.textContent||''; bv=b.querySelector('.data')?.textContent||''; }}
    else if(sc==='lance'){{ av=parseFloat((a.querySelector('.lance')?.textContent||'0').replace(/[^\d.]/g,''))||0; bv=parseFloat((b.querySelector('.lance')?.textContent||'0').replace(/[^\d.]/g,''))||0; return bv-av; }}
    else{{ av=a.textContent; bv=b.textContent; }}
    return av.localeCompare(bv);
  }});
  rows.forEach(r=>tbody.appendChild(r));
}}
function abrirModal() {{
  document.getElementById('mc').textContent = DATA.filter(d=>!excl.has(d.id)).length;
  document.getElementById('mo').classList.add('open');
}}
function closeModal() {{ document.getElementById('mo').classList.remove('open'); }}
function doExport() {{
  closeModal();
  const sel = DATA.filter(d=>!excl.has(d.id));
  const fields = {json.dumps(FIELDNAMES)};
  const lines = [fields.join(',')];
  sel.forEach(row=>{{
    const vals = fields.map(f=>'"'+(row[f]??'').toString().replace(/\\n/g,' ').replace(/"/g,'""')+'"');
    lines.push(vals.join(','));
  }});
  const blob = new Blob(['\\uFEFF'+lines.join('\\n')],{{type:'text/csv;charset=utf-8;'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = FN;
  a.click();
  toast('Exportados '+sel.length+' imoveis');
}}
function toast(msg,d=3500) {{
  const el=document.getElementById('toast');
  el.textContent=msg; el.style.display='block';
  setTimeout(()=>el.style.display='none',d);
}}
// Auto sort by date on load
document.addEventListener('DOMContentLoaded', ()=>sortTable());
</script>
</body>
</html>"""

    out_path = BASE_DIR / "viewer_imoveis_jucesp.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[HTML] Viewer salvo: {out_path}")
    return out_path


# ── Salvar CSV ─────────────────────────────────────────────────────────────────
def save_csv(imoveis):
    out = CSV_DIR / f"imoveis_jucesp_{TODAY}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    print(f"[CSV] {out} ({len(imoveis)} registros)")
    return out


# ── Progresso ──────────────────────────────────────────────────────────────────
REPORT_EVERY = 5 * 60

def print_progress(stats, total):
    done = stats.get("done",0); total_sites = stats.get("total",1)
    pct = int(done/max(total_sites,1)*100)
    bar = "#"*(pct//5) + "."*(20-pct//5)
    print(f"\n{'='*55}")
    print(f"  PROGRESSO [{bar}] {pct}%")
    print(f"  Sites: {done}/{total_sites}  OK:{stats.get('ok',0)}  Err:{stats.get('err',0)}")
    print(f"  Imoveis: {total}")
    print(f"  {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*55}\n")
    sys.stdout.flush()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  SCRAPER JUCESP — ATUANTE REGULAR")
    print(f"  Data: {TODAY}")
    print("=" * 55)

    all_imoveis = []
    seen_ids = set()

    def add_imoveis(items):
        for im in items:
            if im.get("id") and im["id"] not in seen_ids:
                seen_ids.add(im["id"])
                all_imoveis.append(im)

    # ── Fase 1: CSV existente ──────────────────────────────────────────────────
    print("\n[FASE 1] Carregando CSV existente...")
    items1 = load_from_completo()
    add_imoveis(items1)
    print(f"  Total acumulado: {len(all_imoveis)}")

    # Salva checkpoint
    save_csv(all_imoveis)
    render_html(all_imoveis, {"done": 20, "total": 100, "ok": 1, "err": 0})
    print_progress({"done": 20, "total": 100, "ok": 1, "err": 0}, len(all_imoveis))

    # ── Fase 2: Frazao (site com conteúdo rico) ────────────────────────────────
    print("\n[FASE 2] Scraping Frazao Leiloes...")
    try:
        items2 = scrape_frazao()
        add_imoveis(items2)
    except Exception as e:
        print(f"  ERRO Frazao: {e}")
    print(f"  Total acumulado: {len(all_imoveis)}")

    # ── Fase 3: Scraping genérico dos outros sites ─────────────────────────────
    print("\n[FASE 3] Scraping generico de leiloeiros...")
    leiloeiros = load_leiloeiros_com_site()
    # Exclui frazao (já feito)
    leiloeiros = [l for l in leiloeiros if "frazao" not in l["site"].lower()]
    print(f"  Sites a scraping: {len(leiloeiros)}")

    stats = {"total": len(leiloeiros), "done": 0, "ok": 0, "err": 0}
    last_report = time.time()

    for idx, lei in enumerate(leiloeiros, 1):
        stats["done"] = idx
        try:
            items = scrape_generic_sites([lei])
            add_imoveis(items)
            if items: stats["ok"] += 1
            else: stats["err"] += 1
        except Exception as e:
            print(f"  ERRO {lei['site']}: {e}")
            stats["err"] += 1

        if time.time() - last_report >= REPORT_EVERY:
            print_progress(stats, len(all_imoveis))
            save_csv(all_imoveis)
            render_html(all_imoveis, {"done": 20+idx, "total": 100, **stats})
            last_report = time.time()

    # ── Final ──────────────────────────────────────────────────────────────────
    print(f"\n[FINAL] {len(all_imoveis)} imoveis totais")
    save_csv(all_imoveis)
    path = render_html(all_imoveis, {"done": 100, "total": 100, "ok": stats["ok"], "err": stats["err"]})
    print(f"\nAbra o viewer: {path}")
    print("Concluido!")


if __name__ == "__main__":
    main()
