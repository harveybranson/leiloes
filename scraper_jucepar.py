"""
scraper_jucepar.py
==================
Coleta imóveis de leiloeiros REGULAR credenciados pela JUCEPAR (Paraná).

Fonte: PDFs da JUCEPAR parseados + CSV leiloeiros_jucepar_YYYY-MM-DD.csv
       (já filtrados: situação REGULAR, com site)

Fluxo:
  1. Lê CSV leiloeiros_jucepar_YYYY-MM-DD.csv (gerado dos PDFs)
  2. Visita cada site: requests HTTP → Playwright como fallback
  3. Extrai imóveis: título, tipo, cidade/UF, preço, datas, imagem, docs
  4. Salva CSV → csv/imoveis_jucepar_YYYY-MM-DD.csv
  5. Importa para SQLite  (imoveis_leiloeiros.db)
  6. Importa para PostgreSQL Docker (leilao_db)
  7. Relatório por leiloeiro a cada 5 min + relatório final de dificuldades

Uso:
  python scraper_jucepar.py [--sem-banco] [--max-sites N] [--max-paginas N] [--reset]
"""
import sys, io, os
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re, csv, json, time, hashlib, sqlite3, argparse, threading
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urljoin

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ── Configuração ───────────────────────────────────────────────────────────────
BASE          = Path(__file__).resolve().parent
CSV_DIR       = BASE / "csv"
DB_FILE       = BASE / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE / "scraper_jucepar_progress.json"
LOG_FILE      = BASE / "scraper_jucepar.log"
TODAY         = datetime.now().strftime("%Y-%m-%d")

FIELDNAMES_IMOVEIS = [
    "id_externo","leiloeiro","leiloeiro_site","titulo","tipo_imovel","tipo_leilao",
    "estado","cidade","cep","endereco_completo",
    "valor_minimo","valor_avaliacao","area_total","quartos",
    "data_primeiro_leilao","data_segundo_leilao","data_encerramento",
    "url_original","imagem_principal","numero_processo","arquivos","descricao",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
}

# Estado global compartilhado entre threads
_lock   = threading.Lock()
_estado = {
    "imoveis": [],
    "por_leiloeiro": {},
    "sites_ok": 0,
    "sites_err": 0,
    "sites_sem_leilao": 0,
    "erros": [],          # (site, erro)
    "dificuldades": [],   # registro detalhado de problemas
    "leiloeiro_atual": "",
    "inicio": datetime.now().isoformat(),
}

# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    linha = f"[{ts}] {msg}"
    print(linha, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

# ── Helpers ────────────────────────────────────────────────────────────────────
def make_id(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:24]

def clean_money(s: str) -> float | None:
    if not s: return None
    s = re.sub(r"[^\d,.]", "", s)
    s = s.replace(".", "").replace(",", ".")
    try: return float(s) if s else None
    except: return None

def parse_date(s: str) -> str | None:
    if not s: return None
    m = re.search(r"(\d{2})[/\-\.](\d{2})[/\-\.](\d{4})", s)
    if m:
        y, mo, d = int(m.group(3)), int(m.group(2)), int(m.group(1))
        try:
            import datetime as dt
            dt.date(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        return m2.group(0)
    return None

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","sitio","hectare","rural","chácara","gleba","haras"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet","apto"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência","residencia","vila"]): return "casa"
    if any(k in txt for k in ["terreno","lote urbano","lote vago","área nua"]): return "terreno"
    if any(k in txt for k in ["galpão","galpao","armazém","armazem","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","ponto comercial","escritório","escritorio"]): return "comercial"
    if any(k in txt for k in ["prédio","predio","edifício","edificio"]): return "comercial"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","hasta","praça","tjpr","jfpr","tribunal"]): return "judicial"
    if any(k in txt for k in ["banco","caixa","financiamento","retomada","hipoteca"]): return "bancario"
    return "extrajudicial"

RE_PRICE = re.compile(r"R[\$\s]+(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)")
RE_AREA  = re.compile(r"(\d+[\.,]?\d*)\s*m[²2]", re.IGNORECASE)
RE_PROC  = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
RE_CEP   = re.compile(r"\d{5}-?\d{3}")
RE_UF    = re.compile(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b")
RE_DATE  = re.compile(r"\d{2}[\/\.\-]\d{2}[\/\.\-]\d{4}")
DOC_KW   = re.compile(
    r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|"
    r"memorial|escritura|penhora|registro|processo",
    re.IGNORECASE
)
PDF_EXT  = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

LISTING_PATHS = [
    "", "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/",
    "/leilao", "/leiloes/", "/proximos-leiloes", "/proximos_leiloes",
    "/leiloes/imoveis", "/imoveis-leilao", "/em-leilao",
    "/catalogo", "/catalogo/", "/ofertas", "/imoveis-judiciais",
    "/leiloes/imoveis-rurais", "/leiloes/imoveis-urbanos",
    "/imoveis-extrajudiciais", "/leiloes/imoveis-extrajudiciais",
]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","arrematação","leilao"]
LOTE_KW    = ["lote","imovel","oferta","arrematacao","detalhe","detail","lot-"]

# ── 1. Leitura do CSV de leiloeiros ────────────────────────────────────────────
def ler_leiloeiros_csv() -> list[dict]:
    csvs = sorted(CSV_DIR.glob("leiloeiros_jucepar_*.csv"), reverse=True)
    if not csvs:
        log("[ERRO] Nenhum CSV leiloeiros_jucepar_*.csv encontrado em " + str(CSV_DIR))
        return []

    csv_path = csvs[0]
    log(f"Lendo leiloeiros de: {csv_path.name}")
    leiloeiros = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            # Normaliza campos
            nome = (row.get("nome") or "").strip()
            site = (row.get("site") or "").strip().rstrip("/")
            situacao = (row.get("situacao") or "").strip().upper()
            if not nome or situacao != "REGULAR":
                continue
            leiloeiros.append({
                "nome": nome,
                "matricula": row.get("matricula","").strip(),
                "site": site if site else None,
                "email": row.get("email","").strip(),
                "telefone": row.get("telefone","").strip(),
                "cidade_leiloeiro": row.get("cidade","").strip(),
                "uf_leiloeiro": row.get("uf","PR").strip() or "PR",
                "situacao": situacao,
                "junta": row.get("junta","JUCEPAR").strip(),
            })

    log(f"  {len(leiloeiros)} leiloeiros REGULAR carregados")
    return leiloeiros


def derivar_site_do_email(email: str) -> str | None:
    if not email: return None
    email = email.split()[0].strip()
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m: return None
    dominio = m.group(1)
    ignorados = {
        "gmail.com","hotmail.com","yahoo.com","yahoo.com.br",
        "outlook.com","terra.com.br","uol.com.br","bol.com.br",
        "ig.com.br","icloud.com","ymail.com",
    }
    if dominio in ignorados: return None
    return f"https://www.{dominio}"


# ── 2. Funções de extração ─────────────────────────────────────────────────────
def is_imovel(titulo: str, url: str = "") -> bool:
    txt = (titulo + " " + url).lower()
    nao_imovel = [
        "veículo","veiculo","automóvel","automovel","moto ","motocicl",
        "caminhão","caminhao","trator","máquina","maquina","equipamento",
        "eletrodom","celular","notebook","sucata",
    ]
    imovel_kw = [
        "imóvel","imovel","apart","casa","terreno","galpão","galpao",
        "sala","loja","gleba","rural","fazenda","sítio","sitio",
        "comercial","prédio","predio","sobrado","flat","kitnet",
        "chácara","chacara","conjunto","edificio","edifício",
    ]
    if any(k in txt for k in nao_imovel): return False
    if any(k in txt for k in imovel_kw): return True
    return True  # assume imóvel se não há indicador contrário


def extract_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    arquivos = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#","javascript","mailto","tel")): continue
        url_abs = urljoin(page_url, href)
        if url_abs in seen: continue
        text = (a.get_text() + " " + href).lower()
        if PDF_EXT.search(href) or DOC_KW.search(text):
            tipo = "edital" if "edital" in text else \
                   "matricula" if ("matric" in text or "matríc" in text) else \
                   "laudo" if "laudo" in text else \
                   "certidao" if "certid" in text else "pdf"
            nome = a.get_text(strip=True)[:80] or tipo.capitalize()
            arquivos.append({"tipo": tipo, "url": url_abs, "nome": nome})
            seen.add(url_abs)
        if len(arquivos) >= 15: break
    # onclick ExibeDoc (padrão tribunais / Caixa)
    for tag in soup.find_all(onclick=True):
        m = re.search(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", tag.get("onclick",""))
        if m:
            path = m.group(1)
            url_abs = urljoin(page_url, path)
            if url_abs not in seen:
                tipo = "matricula" if "matricula" in path.lower() else "edital"
                arquivos.append({"tipo": tipo, "url": url_abs, "nome": tipo.capitalize()})
                seen.add(url_abs)
    return arquivos


def extrair_imovel_do_card(html: str, card_url: str, lei: dict, base_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    # Título: tenta vários seletores
    titulo = ""
    for sel in [
        "h1","h2","h3",
        ".titulo",".title",".lote-titulo",".lote-title",
        "[class*='titulo']","[class*='title']","[class*='lote']","[class*='imovel']",
        ".card-title",".property-title",".auction-title",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if len(t) > 5:
                titulo = t[:200]
                break
    if not titulo:
        # Tenta meta description
        meta = soup.find("meta", {"name": re.compile(r"description|title", re.I)})
        if meta:
            titulo = (meta.get("content","") or "")[:200]
    if not titulo:
        titulo = texto[:120]

    if not is_imovel(titulo, card_url): return None
    if len(titulo.strip()) < 4: return None

    # Preços
    precos = RE_PRICE.findall(texto)
    v_min  = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None

    # Área e quartos
    area_m  = RE_AREA.search(texto)
    area    = area_m.group(1).replace(",",".") if area_m else None
    q_m     = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None

    # Datas
    datas = RE_DATE.findall(texto)
    data1 = parse_date(datas[0]) if datas else None
    data2 = parse_date(datas[1]) if len(datas) > 1 else None

    # Localização
    uf_m  = RE_UF.search(texto)
    uf    = uf_m.group() if uf_m else lei.get("uf_leiloeiro","PR")
    cid_m = re.search(rf"([A-ZÀ-Úa-zà-ú\s]+)\s*[/\-]\s*{uf}\b", texto)
    cidade = cid_m.group(1).strip() if cid_m else lei.get("cidade_leiloeiro","")

    cep_m = RE_CEP.search(texto)
    cep   = cep_m.group() if cep_m else ""
    proc_m = RE_PROC.search(texto)
    processo = proc_m.group() if proc_m else ""

    # Endereço
    end_el = soup.select_one(
        "[class*='endere'],[class*='local'],[class*='address'],"
        "[itemprop='address'],[class*='localizacao'],[class*='location']"
    )
    endereco = end_el.get_text(strip=True)[:300] if end_el else ""

    # Descrição
    desc_el = soup.select_one(
        "[class*='descri'],[class*='desc'],[class*='detail'],"
        "[class*='detalhe'],[class*='about'],[class*='info']"
    )
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else texto[:300]

    # Imagem principal
    img_principal = ""
    for img in soup.find_all("img", src=True):
        src = img.get("src","") or img.get("data-src","") or img.get("data-lazy-src","")
        if not src: continue
        src_abs = urljoin(base_url, src)
        kw_skip = ["logo","icon","banner","avatar","sprite","placeholder","blank","pixel"]
        if not any(k in src.lower() for k in kw_skip) and (src.startswith("http") or src.startswith("/")):
            img_principal = src_abs
            break

    arquivos = extract_arquivos(soup, card_url)

    return {
        "id_externo":          make_id(card_url),
        "leiloeiro":           lei["nome"],
        "leiloeiro_site":      lei.get("site",""),
        "titulo":              titulo,
        "tipo_imovel":         infer_tipo(titulo, desc),
        "tipo_leilao":         infer_tipo_leilao(titulo, desc),
        "estado":              uf,
        "cidade":              cidade,
        "cep":                 cep,
        "endereco_completo":   endereco,
        "valor_minimo":        v_min,
        "valor_avaliacao":     v_aval,
        "area_total":          area,
        "quartos":             quartos,
        "data_primeiro_leilao": data1,
        "data_segundo_leilao":  data2,
        "data_encerramento":    None,
        "url_original":        card_url,
        "imagem_principal":    img_principal,
        "numero_processo":     processo,
        "arquivos":            json.dumps(arquivos, ensure_ascii=False),
        "descricao":           desc,
    }


# ── 3. Scrapers ────────────────────────────────────────────────────────────────
def _discover_listing(session_or_page, base: str, use_playwright: bool = False) -> str | None:
    """Encontra a URL da página de listagem de imóveis."""
    for path in LISTING_PATHS:
        url = base + path
        try:
            if use_playwright:
                session_or_page.goto(url, timeout=25000, wait_until="domcontentloaded")
                session_or_page.wait_for_timeout(2000)
                html = session_or_page.content()
            else:
                r = session_or_page.get(url, timeout=18, allow_redirects=True, verify=False)
                if r.status_code != 200: continue
                html = r.text
            if any(k in html.lower() for k in LISTING_KW):
                return url
        except Exception:
            continue
    return None


def _collect_lote_urls(soup: BeautifulSoup, base: str, existing: set) -> list[str]:
    novos = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_abs = urljoin(base, href)
        txt = (a.get_text(" ") + " " + href).lower()
        if any(k in txt or k in href_abs.lower() for k in LOTE_KW):
            parsed = urlparse(href_abs)
            if parsed.netloc and href_abs not in existing:
                existing.add(href_abs)
                novos.append(href_abs)
    return novos


def scrape_site_http(lei: dict, max_paginas: int = 10) -> list[dict]:
    base = (lei["site"] or "").rstrip("/")
    if not base: return []
    session = requests.Session()
    session.headers.update(HEADERS)
    imoveis = []
    lote_urls: set = set()

    listagem = _discover_listing(session, base, use_playwright=False)
    if not listagem:
        return []

    for pag in range(1, max_paginas + 1):
        urls_pg = [
            listagem if pag == 1 else f"{listagem}?pagina={pag}",
            listagem if pag == 1 else f"{listagem}?pag={pag}",
            listagem if pag == 1 else f"{listagem}/{pag}",
            listagem if pag == 1 else f"{listagem}?page={pag}",
        ]
        achou = False
        for url_pg in urls_pg:
            try:
                r = session.get(url_pg, timeout=20, verify=False)
                if r.status_code != 200: continue
                soup = BeautifulSoup(r.text, "html.parser")
                novos = _collect_lote_urls(soup, base, lote_urls)
                if novos: achou = True
            except Exception:
                continue
            if pag > 1 and not achou: break
        time.sleep(0.8)

    for url in list(lote_urls)[:300]:
        try:
            r = session.get(url, timeout=20, verify=False)
            if r.status_code != 200: continue
            im = extrair_imovel_do_card(r.text, url, lei, base)
            if im: imoveis.append(im)
            time.sleep(0.6)
        except Exception:
            continue

    return imoveis


def scrape_site_playwright(lei: dict, max_paginas: int = 10) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[WARN] Playwright não instalado.")
        return []

    base = (lei["site"] or "").rstrip("/")
    if not base: return []
    imoveis = []
    lote_urls: set = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            ignore_https_errors=True,
        )
        page = context.new_page()

        listagem = _discover_listing(page, base, use_playwright=True)
        if not listagem:
            browser.close()
            return []

        for pag in range(1, max_paginas + 1):
            url_pg = listagem if pag == 1 else f"{listagem}?pagina={pag}"
            try:
                page.goto(url_pg, timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(2000)
                soup = BeautifulSoup(page.content(), "html.parser")
                novos = _collect_lote_urls(soup, base, lote_urls)
                if not novos and pag > 1: break
                time.sleep(1.5)
            except Exception:
                break

        for url in list(lote_urls)[:300]:
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                im = extrair_imovel_do_card(page.content(), url, lei, base)
                if im: imoveis.append(im)
                time.sleep(1)
            except Exception:
                continue

        browser.close()
    return imoveis


def is_js_heavy(html: str) -> bool:
    markers = ["__next_data__","__nuxt__","react-root","vue-app","ng-app",
               "window.__initial_state__","data-reactroot","_app-props"]
    html_lower = html.lower()
    if any(m in html_lower for m in markers): return True
    soup = BeautifulSoup(html, "html.parser")
    if len(soup.get_text().strip()) < 300: return True
    return False


def scrape_leiloeiro(lei: dict, max_paginas: int = 10) -> tuple[list[dict], str]:
    if not lei.get("site"):
        return [], "sem_site"

    site = lei["site"]
    log(f"  → Scraping: {lei['nome']} | {site}")

    # Testa acesso HTTP
    status_http = None
    html_inicial = ""
    try:
        r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
        status_http = r.status_code
        html_inicial = r.text
        if r.status_code in (404, 410, 403, 503):
            with _lock:
                _estado["dificuldades"].append({
                    "leiloeiro": lei["nome"], "site": site,
                    "tipo": f"HTTP_{r.status_code}",
                    "detalhe": f"Status {r.status_code} na home"
                })
            return [], "offline"
    except requests.exceptions.SSLError as e:
        log(f"    [SSL] {e}")
        with _lock:
            _estado["dificuldades"].append({
                "leiloeiro": lei["nome"], "site": site,
                "tipo": "SSL_ERROR", "detalhe": str(e)[:120]
            })
        # Tenta sem verificação já está configurado
    except requests.exceptions.ConnectionError as e:
        log(f"    [CONN] {e}")
        with _lock:
            _estado["dificuldades"].append({
                "leiloeiro": lei["nome"], "site": site,
                "tipo": "CONNECTION_ERROR", "detalhe": str(e)[:120]
            })
        imoveis = scrape_site_playwright(lei, max_paginas)
        return imoveis, "ok" if imoveis else "sem_leilao"
    except Exception as e:
        log(f"    [HTTP ERR] {e}. Tentando Playwright...")
        with _lock:
            _estado["dificuldades"].append({
                "leiloeiro": lei["nome"], "site": site,
                "tipo": "HTTP_TIMEOUT", "detalhe": str(e)[:120]
            })
        imoveis = scrape_site_playwright(lei, max_paginas)
        return imoveis, "ok" if imoveis else "sem_leilao"

    # Decide estratégia
    if is_js_heavy(html_inicial):
        log(f"    JS-heavy detectado (SPA/Next.js). Usando Playwright...")
        with _lock:
            _estado["dificuldades"].append({
                "leiloeiro": lei["nome"], "site": site,
                "tipo": "JS_HEAVY", "detalhe": "SPA detectado — usando Playwright"
            })
        imoveis = scrape_site_playwright(lei, max_paginas)
    else:
        imoveis = scrape_site_http(lei, max_paginas)
        if not imoveis:
            log(f"    HTTP sem resultados. Tentando Playwright...")
            imoveis = scrape_site_playwright(lei, max_paginas)
            if imoveis:
                with _lock:
                    _estado["dificuldades"].append({
                        "leiloeiro": lei["nome"], "site": site,
                        "tipo": "HTTP_VAZIO_PW_OK",
                        "detalhe": f"HTTP retornou 0 lotes, Playwright encontrou {len(imoveis)}"
                    })

    if not imoveis:
        with _lock:
            _estado["dificuldades"].append({
                "leiloeiro": lei["nome"], "site": site,
                "tipo": "SEM_LOTES",
                "detalhe": "Nenhum lote/imóvel encontrado mesmo com Playwright"
            })

    status = "ok" if imoveis else "sem_leilao"
    return imoveis, status


# ── 4. Progresso e relatórios ───────────────────────────────────────────────────
def salvar_progresso():
    with _lock:
        data = {
            "atualizado": datetime.now().isoformat(),
            "total_imoveis": len(_estado["imoveis"]),
            "por_leiloeiro": dict(_estado["por_leiloeiro"]),
            "sites_ok": _estado["sites_ok"],
            "sites_err": _estado["sites_err"],
            "sites_sem_leilao": _estado["sites_sem_leilao"],
            "erros": _estado["erros"][-10:],
            "dificuldades": _estado["dificuldades"][-30:],
        }
    try:
        PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def relatorio_5min():
    with _lock:
        por_lei = dict(_estado["por_leiloeiro"])
        total   = len(_estado["imoveis"])
        atual   = _estado["leiloeiro_atual"]
        ok      = _estado["sites_ok"]
        err     = _estado["sites_err"]
        sem     = _estado["sites_sem_leilao"]

    log(f"\n{'='*65}")
    log(f"RELATÓRIO PARCIAL | Total: {total} imóveis | Atual: {atual}")
    log(f"  OK: {ok} | Sem leilão: {sem} | Erro: {err}")
    if por_lei:
        log("  Imóveis por leiloeiro (com resultados):")
        for nome, cnt in sorted(por_lei.items(), key=lambda x: -x[1])[:20]:
            log(f"    {nome[:45]:<45} {cnt:>4} imóveis")
    log(f"{'='*65}\n")


def thread_relatorio(stop_evt: threading.Event):
    while not stop_evt.wait(300):  # 5 minutos
        relatorio_5min()


# ── 5. Salvar CSVs ─────────────────────────────────────────────────────────────
def salvar_csv_imoveis(imoveis: list[dict]) -> Path:
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"imoveis_jucepar_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_IMOVEIS, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    log(f"[CSV] Imóveis: {path} ({len(imoveis)} registros)")
    return path


# ── 6. SQLite ───────────────────────────────────────────────────────────────────
def importar_sqlite(imoveis: list[dict]) -> int:
    log(f"\n[SQLite] Importando {len(imoveis)} imóveis em {DB_FILE}...")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imoveis (
            id TEXT PRIMARY KEY,
            leiloeiro TEXT, junta TEXT, site TEXT,
            titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
            lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
            url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leiloeiro ON imoveis(leiloeiro)")
    conn.commit()

    ins = dup = 0
    agora = datetime.now().isoformat(timespec="seconds")
    for r in imoveis:
        def _d(v):
            try: return float(Decimal(str(v).replace(",","."))) if v else None
            except: return None
        try:
            conn.execute(
                "INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("id_externo",""), r.get("leiloeiro",""), "JUCEPAR",
                    r.get("leiloeiro_site",""),
                    r.get("titulo","")[:500], r.get("descricao","")[:300],
                    r.get("endereco_completo","")[:200],
                    r.get("cidade",""), r.get("estado","PR"),
                    _d(r.get("valor_minimo")), _d(r.get("valor_avaliacao")),
                    r.get("data_primeiro_leilao",""),
                    r.get("url_original",""), r.get("tipo_imovel",""),
                    r.get("imagem_principal",""), agora,
                )
            )
            ins += 1
        except sqlite3.IntegrityError:
            dup += 1
        except Exception as e:
            log(f"  [SQLite ERR] {e}")

    conn.commit()
    conn.close()
    log(f"  SQLite: {ins} inseridos, {dup} já existiam")
    return ins


# ── 7. PostgreSQL ───────────────────────────────────────────────────────────────
def psql(sql: str, timeout: int = 30) -> str:
    import subprocess
    proc = subprocess.run(
        ["docker","exec","leilao_postgres","psql","-U","leilao","-d","leilao_db",
         "--no-align","--tuples-only","-c", sql],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    return proc.stdout + proc.stderr


def importar_postgres(imoveis: list[dict]) -> tuple[int,int]:
    """
    Importa via arquivo SQL temporário copiado ao container (docker cp + psql -f).
    Evita WinError 206 (linha de comando > 32 KB no Windows ao usar psql -c).
    """
    import subprocess, tempfile, os
    log(f"\n[PostgreSQL] Importando {len(imoveis)} imóveis via arquivo SQL...")

    TIPOS_IMOVEL_VALIDOS = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
    TIPOS_LEILAO_VALIDOS = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

    def esc(v, max_len=None):
        s = str(v or "").replace("\x00","").replace("'","''")
        if max_len: s = s[:max_len]
        return s

    def _d(v):
        try: return str(float(Decimal(str(v).replace(",","."))) ) if v else "NULL"
        except: return "NULL"

    def _dt(v):
        if not v: return "NULL"
        return f"'{str(v)[:10]}'"

    def _i(v):
        try: return str(int(v)) if v else "NULL"
        except: return "NULL"

    # Escreve SQL em arquivo temporário no host
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".sql",
                                      encoding="utf-8", delete=False)
    try:
        tmp.write("BEGIN;\n")
        tmp.write("""INSERT INTO fontes (nome, url_base, ativo, criado_em)
VALUES ('JUCEPAR','https://www.jucepar.pr.gov.br/',true,NOW())
ON CONFLICT (nome) DO NOTHING;\n""")

        for r in imoveis:
            ti = r.get("tipo_imovel","outro").upper()
            if ti not in TIPOS_IMOVEL_VALIDOS: ti = "OUTRO"
            tl = r.get("tipo_leilao","extrajudicial").upper()
            if tl not in TIPOS_LEILAO_VALIDOS: tl = "EXTRAJUDICIAL"

            tmp.write(f"""INSERT INTO imoveis (
    fonte_id, id_externo, titulo, descricao, url_original,
    tipo_imovel, tipo_leilao, status, categoria,
    cidade, estado, cep, endereco_completo,
    valor_minimo, valor_avaliacao, area_total, quartos,
    data_primeiro_leilao, data_segundo_leilao,
    imagem_principal, arquivos, numero_processo,
    leiloeiro, ativo, classificado, geocodificado, criado_em, atualizado_em
) VALUES (
    (SELECT id FROM fontes WHERE nome='JUCEPAR' LIMIT 1),
    '{esc(r.get("id_externo",""),200)}','{esc(r.get("titulo",""),500)}',
    '{esc(r.get("descricao",""),2000)}','{esc(r.get("url_original",""),1000)}',
    '{ti}','{tl}','ABERTO','IMOVEL',
    '{esc(r.get("cidade",""),200)}','{esc(r.get("estado","PR"),2)}',
    '{esc(r.get("cep",""),10)}','{esc(r.get("endereco_completo",""),500)}',
    {_d(r.get("valor_minimo"))},{_d(r.get("valor_avaliacao"))},
    {_d(r.get("area_total"))},{_i(r.get("quartos"))},
    {_dt(r.get("data_primeiro_leilao"))},{_dt(r.get("data_segundo_leilao"))},
    '{esc(r.get("imagem_principal",""),1000)}','{esc(r.get("arquivos","[]"),4000)}',
    '{esc(r.get("numero_processo",""),100)}','{esc(r.get("leiloeiro",""),300)}',
    true,false,false,NOW(),NOW()
) ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
    titulo=EXCLUDED.titulo, valor_minimo=EXCLUDED.valor_minimo,
    data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
    imagem_principal=EXCLUDED.imagem_principal,
    arquivos=EXCLUDED.arquivos, atualizado_em=NOW();\n""")

        tmp.write("COMMIT;\n")
        tmp_path = tmp.name
    finally:
        tmp.close()

    try:
        # Copia arquivo para o container e executa
        cp = subprocess.run(
            ["docker","cp", tmp_path, "leilao_postgres:/tmp/pg_import.sql"],
            capture_output=True, text=True
        )
        if cp.returncode != 0:
            log(f"  [ERRO docker cp] {cp.stderr[:200]}")
            return 0, 0

        proc = subprocess.run(
            ["docker","exec","leilao_postgres","psql","-U","leilao","-d","leilao_db",
             "-f","/tmp/pg_import.sql"],
            capture_output=True, text=True, encoding="utf-8", timeout=300,
        )
        out = proc.stdout + proc.stderr
        ins_pg = out.count("INSERT 0 1")
        upd_pg = out.count("UPDATE 1")
        err_lines = [l for l in out.splitlines() if "ERROR" in l or "error" in l.lower()]
        err_pg = len(err_lines)
        if err_pg:
            log(f"  [PG erros] {err_lines[:3]}")
        log(f"  PostgreSQL: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")
        return ins_pg, upd_pg
    finally:
        os.unlink(tmp_path)


# ── 8. Relatório final de dificuldades ─────────────────────────────────────────
def gerar_relatorio_dificuldades() -> str:
    with _lock:
        difs     = list(_estado["dificuldades"])
        por_lei  = dict(_estado["por_leiloeiro"])
        total    = len(_estado["imoveis"])
        ok       = _estado["sites_ok"]
        err      = _estado["sites_err"]
        sem      = _estado["sites_sem_leilao"]
        t_inicio = _estado["inicio"]

    duracao = (datetime.now() - datetime.fromisoformat(t_inicio)).total_seconds()
    h, m, s = int(duracao//3600), int((duracao%3600)//60), int(duracao%60)

    linhas = [
        "",
        "---",
        "",
        f"## Sessão de Scraping JUCEPAR — {TODAY}",
        "",
        "### Resumo",
        f"- **Duração:** {h}h {m}m {s}s",
        f"- **Total imóveis coletados:** {total}",
        f"- **Sites com resultados:** {ok}",
        f"- **Sites sem leilão ativo:** {sem}",
        f"- **Sites com erro:** {err}",
        "",
        "### Imóveis por leiloeiro",
        "",
        "| Leiloeiro | Imóveis |",
        "|-----------|---------|",
    ]
    for nome, cnt in sorted(por_lei.items(), key=lambda x: -x[1]):
        linhas.append(f"| {nome} | {cnt} |")

    # Categoriza dificuldades
    cats = {}
    for d in difs:
        t = d["tipo"]
        cats.setdefault(t, []).append(d)

    linhas += [
        "",
        "### Principais Dificuldades Encontradas",
        "",
    ]

    descs = {
        "HTTP_404":          ("Sites fora do ar (404)", "Site foi cancelado ou URL mudou"),
        "HTTP_403":          ("Sites com bloqueio 403", "Servidor rejeita o scraper — precisaria de headers adicionais ou Playwright"),
        "HTTP_503":          ("Servidor indisponível (503)", "Sobrecarga ou manutenção — tentar novamente depois"),
        "SSL_ERROR":         ("Erros de certificado SSL", "Certificado expirado ou inválido — tentar com `verify=False`"),
        "CONNECTION_ERROR":  ("Sites offline/DNS inválido", "Domínio não existe ou está sem DNS"),
        "HTTP_TIMEOUT":      ("Timeout de conexão", "Site lento demais — aumentar timeout ou usar retry"),
        "JS_HEAVY":          ("Sites SPA/JavaScript (JS-heavy)", "Site renderiza conteúdo via JavaScript — usa Playwright automaticamente"),
        "HTTP_VAZIO_PW_OK":  ("HTTP retorna 0 lotes mas Playwright encontra", "Anti-bot básico contra requests Python — Playwright contorna"),
        "SEM_LOTES":         ("Sem lotes/imóveis ativos", "Leiloeiro sem leilão em andamento no momento do scraping"),
    }

    for cat, itens in sorted(cats.items(), key=lambda x: -len(x[1])):
        titulo_d, desc_d = descs.get(cat, (cat, "Ver log para detalhes"))
        linhas += [
            f"#### {titulo_d} ({len(itens)} sites)",
            f"**Causa:** {desc_d}",
            "",
            "Sites afetados:",
        ]
        for it in itens[:10]:
            linhas.append(f"- `{it['site']}` ({it['leiloeiro']}): {it['detalhe'][:80]}")
        if len(itens) > 10:
            linhas.append(f"- ... e mais {len(itens)-10} sites")
        linhas.append("")

    linhas += [
        "### Sugestões de Melhoria",
        "",
        "1. **Sites offline (404/Connection):** manter lista de sites atualizada; remover leiloeiros IRREGULAR/SUSPENSO automaticamente.",
        "2. **Bloqueio 403/Cloudflare:** implementar FlareSolverr (Docker `:8191`) para sites com Cloudflare Managed Challenge — ver seção 14 deste documento.",
        "3. **SPA/JS-heavy:** Playwright já é acionado automaticamente, mas alguns SPAs Next.js App Router exigem `wait_until='networkidle'` com timeout de 60s.",
        "4. **Sem lotes ativos:** criar agenda de re-scraping; muitos leiloeiros têm leilões esporádicos — verificar novamente em 7-14 dias.",
        "5. **Paginação:** alguns sites usam `?page=N` em vez de `?pagina=N` — ampliar lista de variantes de paginação.",
        "6. **Imagens:** alguns sites usam `data-lazy-src` ou carregam imagens via CSS — adicionar extração de backgrounds CSS.",
        "7. **Documentos (edital/matrícula):** muitos sites usam botões JS com `onclick` ou APIs internas — implementar extratores específicos por domínio.",
        "8. **Rate limiting:** adicionar delay adaptativo baseado no tempo de resposta do servidor.",
        "9. **Leiloeiros sem site:** 30+ leiloeiros REGULAR sem website identificado — buscar pelo nome no Google para encontrar sites atualizados.",
        "10. **Deduplicação:** alguns leiloeiros compartilham site (ex.: Nogari, Pestana, Vardana) — usar `id_externo` baseado em URL para evitar duplicatas.",
        "",
    ]

    return "\n".join(linhas)


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Scraper JUCEPAR — imóveis de leiloeiros PR")
    ap.add_argument("--sem-banco",    action="store_true", help="Não importa para bancos")
    ap.add_argument("--max-sites",    type=int, default=999, help="Limite de sites")
    ap.add_argument("--max-paginas",  type=int, default=10, help="Páginas por site")
    ap.add_argument("--reset",        action="store_true", help="Ignora progresso anterior")
    ap.add_argument("--apenas-csv",   action="store_true", help="Salva CSV mas não importa DB")
    args = ap.parse_args()

    log("="*65)
    log(f"JUCEPAR Scraper iniciado — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("="*65)

    # 1. Carrega leiloeiros do CSV
    leiloeiros = ler_leiloeiros_csv()
    if not leiloeiros:
        log("[ERRO] Nenhum leiloeiro carregado. Abortando.")
        return

    # Deduplica por site
    sites_vistos: set = set()
    com_site = []
    sem_site = []
    for l in leiloeiros:
        site = (l.get("site") or "").rstrip("/")
        if not site:
            sem_site.append(l)
            continue
        if site not in sites_vistos:
            sites_vistos.add(site)
            com_site.append(l)

    log(f"\nTotal REGULAR: {len(leiloeiros)}")
    log(f"  Com site (únicos): {len(com_site)}")
    log(f"  Sem site (pulados): {len(sem_site)}")

    # 2. Thread de relatório a cada 5 min
    stop_evt = threading.Event()
    t_report = threading.Thread(target=thread_relatorio, args=(stop_evt,), daemon=True)
    t_report.start()

    todos_imoveis: list[dict] = []
    limitar = min(args.max_sites, len(com_site))

    log(f"\nIniciando scraping de {limitar} sites...\n")

    for idx, lei in enumerate(com_site[:limitar], 1):
        with _lock:
            _estado["leiloeiro_atual"] = lei["nome"]

        log(f"\n[{idx}/{limitar}] {lei['nome']} | {lei.get('site','')}")

        try:
            imoveis, status = scrape_leiloeiro(lei, args.max_paginas)
        except Exception as e:
            imoveis, status = [], "erro"
            with _lock:
                _estado["erros"].append((lei.get("site",""), str(e)))
                _estado["dificuldades"].append({
                    "leiloeiro": lei["nome"], "site": lei.get("site",""),
                    "tipo": "EXCECAO", "detalhe": str(e)[:120]
                })
            log(f"  [ERRO] {e}")

        with _lock:
            if status == "ok":
                _estado["sites_ok"] += 1
            elif status == "erro":
                _estado["sites_err"] += 1
            else:
                _estado["sites_sem_leilao"] += 1
            _estado["imoveis"].extend(imoveis)
            _estado["por_leiloeiro"][lei["nome"]] = len(imoveis)

        todos_imoveis.extend(imoveis)
        log(f"  Status: {status} | {len(imoveis)} imóveis | Total: {len(todos_imoveis)}")

        salvar_progresso()
        time.sleep(2)

    stop_evt.set()

    # 3. Relatório final
    log(f"\n{'='*65}")
    log(f"SCRAPING CONCLUÍDO: {len(todos_imoveis)} imóveis de {limitar} sites")
    relatorio_5min()

    # 4. Salvar CSV
    if todos_imoveis:
        salvar_csv_imoveis(todos_imoveis)
    else:
        log("[WARN] Nenhum imóvel coletado.")

    # 5. Importar bancos
    if not args.sem_banco and not args.apenas_csv and todos_imoveis:
        importar_sqlite(todos_imoveis)
        try:
            importar_postgres(todos_imoveis)
        except Exception as e:
            log(f"[PostgreSQL] Falhou: {e}")

    # 6. Relatório de dificuldades → anexar ao captura_dados_leiloes_v2.md
    relatorio = gerar_relatorio_dificuldades()
    log("\n" + relatorio)

    md_path = BASE / "captura_dados_leiloes_v2.md"
    try:
        existing = md_path.read_text(encoding="utf-8")
        # Evita duplicar seção
        marker = f"## Sessão de Scraping JUCEPAR — {TODAY}"
        if marker not in existing:
            md_path.write_text(existing + "\n" + relatorio, encoding="utf-8")
            log(f"[MD] Relatório de dificuldades anexado a {md_path.name}")
        else:
            log(f"[MD] Seção já existia, não duplicado.")
    except Exception as e:
        log(f"[MD] Falha ao atualizar MD: {e}")

    log("\n[CONCLUÍDO] Veja o relatório completo em scraper_jucepar.log")


if __name__ == "__main__":
    main()
