"""
scraper_jucesc.py
=================
Coleta imóveis de leiloeiros REGULAR credenciados pela JUCESC.

Fluxo:
  1. Scrape https://leiloeiros.jucesc.sc.gov.br/site/  → lista de Regular
  2. Cruza com leiloeiros_regulares.csv para obter sites
  3. Deriva site do domínio do e-mail quando ausente
  4. Visita cada site com Playwright (multi-estratégia)
  5. Salva CSV  → csv/leiloeiros_jucesc_YYYY-MM-DD.csv
                   csv/imoveis_jucesc_YYYY-MM-DD.csv
  6. Importa para SQLite  (imoveis_leiloeiros.db)
  7. Importa para PostgreSQL Docker (leilao_db)
  8. Relatório por leiloeiro a cada 5 min + relatório final

Uso:
  python scraper_jucesc.py [--sem-banco] [--max-sites N] [--reset]
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
BASE        = Path(__file__).resolve().parent
CSV_DIR     = BASE / "csv"
DB_FILE     = BASE / "imoveis_leiloeiros.db"
SCRAPER_ROOT = Path(os.environ.get("SITE_ROOT", str(Path(__file__).resolve().parent.parent / "leilao-scraper" / "leilao-scraper")))
PROGRESS_FILE = BASE / "scraper_jucesc_progress.json"
LOG_FILE     = BASE / "scraper_jucesc.log"

JUCESC_URL = "https://leiloeiros.jucesc.sc.gov.br/site/index.php?titulo=LEILOEIRO+POR+ANTIGUIDADE"
LEILOEIROS_CSV = BASE / "leiloeiros_regulares.csv"
TODAY = datetime.now().strftime("%Y-%m-%d")

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
    "imoveis": [],           # lista de dicts
    "por_leiloeiro": {},     # nome → count
    "sites_ok": 0,
    "sites_err": 0,
    "sites_sem_leilao": 0,
    "erros": [],             # list of (site, msg)
    "leiloeiro_atual": "",
    "inicio": datetime.now().isoformat(),
}

# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    linha = f"[{ts}] {msg}"
    print(linha)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

# ── Helpers de texto ───────────────────────────────────────────────────────────
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
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2: return m2.group(0)
    return None

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","sítio","hectare","rural","chácara"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência","vila"]): return "casa"
    if any(k in txt for k in ["terreno","gleba","lote urbano"]): return "terreno"
    if any(k in txt for k in ["galpão","armazém","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","ponto comercial"]): return "comercial"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","hasta","praça","tjsc","jfsc"]): return "judicial"
    if any(k in txt for k in ["banco","caixa","financiamento","retomada"]): return "bancario"
    return "extrajudicial"

RE_PRICE = re.compile(r"R[\$\s]+(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)")
RE_AREA  = re.compile(r"(\d+[\.,]?\d*)\s*m[²2]", re.IGNORECASE)
RE_PROC  = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
RE_CEP   = re.compile(r"\d{5}-?\d{3}")
RE_UF    = re.compile(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b")
RE_DATE  = re.compile(r"\d{2}[\/\.\-]\d{2}[\/\.\-]\d{4}")
RE_DOC_KW = re.compile(r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|memorial|processo", re.IGNORECASE)

# ── 1. Coleta lista JUCESC ─────────────────────────────────────────────────────
def fetch_jucesc_regulares() -> list[dict]:
    """Retorna lista de leiloeiros REGULAR da JUCESC."""
    log("Buscando lista oficial JUCESC...")
    try:
        r = requests.get(JUCESC_URL, headers=HEADERS, timeout=30, verify=False)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log(f"[WARN] Falha ao buscar JUCESC: {e}. Usando dados do CSV local.")
        return []

    regulares = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4: continue
        aarc     = tds[0].get_text(strip=True)
        nome     = tds[1].get_text(strip=True)
        data_mat = tds[2].get_text(strip=True)
        situacao = tds[3].get_text(strip=True)
        if situacao.strip().lower() == "regular" and nome:
            regulares.append({
                "aarc": aarc, "nome": nome,
                "data_matricula": data_mat, "situacao": situacao,
            })

    log(f"  JUCESC: {len(regulares)} leiloeiros REGULAR encontrados")
    return regulares

# ── 2. Cruza com CSV local ────────────────────────────────────────────────────
def load_csv_jucesc() -> list[dict]:
    """Carrega dados dos leiloeiros JUCESC do CSV local."""
    leiloeiros = []
    try:
        with open(LEILOEIROS_CSV, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                junta = (row.get("junta_comercial") or "").upper()
                uf    = (row.get("uf") or "").upper()
                sit   = (row.get("situacao") or "").lower()
                if ("JUCESC" in junta or uf == "SC") and "regular" in sit:
                    leiloeiros.append(row)
    except Exception as e:
        log(f"[WARN] Falha ao ler CSV local: {e}")
    log(f"  CSV local: {len(leiloeiros)} leiloeiros JUCESC REGULAR")
    return leiloeiros

def derivar_site_do_email(email: str) -> str | None:
    """Tenta derivar URL do site a partir do domínio do e-mail."""
    if not email: return None
    email = email.split()[0].strip()  # pega só o primeiro se houver múltiplos
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m: return None
    dominio = m.group(1)
    # Ignora domínios gratuitos
    ignorados = {"gmail.com","hotmail.com","yahoo.com","yahoo.com.br",
                 "outlook.com","terra.com.br","uol.com.br","bol.com.br","ig.com.br"}
    if dominio in ignorados: return None
    return f"https://www.{dominio}"

def merge_leiloeiros(jucesc_list: list[dict], csv_list: list[dict]) -> list[dict]:
    """Junta dados da JUCESC com o CSV local."""
    # Indexa CSV por nome normalizado
    csv_by_nome = {}
    for row in csv_list:
        nome_norm = re.sub(r"\s+", " ", row.get("nome","")).strip().upper()
        csv_by_nome[nome_norm] = row

    resultado = []
    nomes_vistos = set()

    for lei in jucesc_list:
        nome_norm = lei["nome"].strip().upper()
        if nome_norm in nomes_vistos: continue
        nomes_vistos.add(nome_norm)
        csv_row = csv_by_nome.get(nome_norm, {})

        site = (csv_row.get("site") or "").strip()
        email = (csv_row.get("email") or "").strip()
        sites_alt = csv_row.get("sites_alternativos") or "[]"
        try:
            alt_list = json.loads(sites_alt) if sites_alt else []
        except:
            alt_list = []

        # Prefere site direto; fallback para alternativo; fallback para email
        if not site and alt_list:
            site = alt_list[0]
        if not site:
            site = derivar_site_do_email(email) or ""

        resultado.append({
            "nome": lei["nome"],
            "aarc": lei.get("aarc",""),
            "data_matricula": lei.get("data_matricula",""),
            "site": site,
            "email": email,
            "telefone": csv_row.get("telefone",""),
            "junta": "JUCESC",
            "uf": "SC",
        })

    # Adiciona do CSV quem não estava na JUCESC online (nova matrícula ou site offline)
    for row in csv_list:
        nome_norm = re.sub(r"\s+", " ", row.get("nome","")).strip().upper()
        if nome_norm in nomes_vistos: continue
        nomes_vistos.add(nome_norm)
        site = (row.get("site") or "").strip()
        email = (row.get("email") or "").strip()
        sites_alt = row.get("sites_alternativos") or "[]"
        try:
            alt_list = json.loads(sites_alt) if sites_alt else []
        except:
            alt_list = []
        if not site and alt_list:
            site = alt_list[0]
        if not site:
            site = derivar_site_do_email(email) or ""
        resultado.append({
            "nome": row.get("nome",""),
            "aarc": row.get("matricula",""),
            "data_matricula": row.get("data_matricula",""),
            "site": site,
            "email": email,
            "telefone": row.get("telefone",""),
            "junta": "JUCESC",
            "uf": "SC",
        })

    com_site = [l for l in resultado if l["site"]]
    log(f"  Total merged: {len(resultado)} | com site: {len(com_site)}")
    return resultado

# ── 3. Scraping de sites de leiloeiros ────────────────────────────────────────
LISTING_PATHS = [
    "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/",
    "/leilao", "/leiloes/", "/proximos-leiloes", "/proximos_leiloes",
    "/leiloes/imoveis", "/imoveis-leilao", "/em-leilao",
    "/leiloes/imoveis-rurais", "/leiloes/imoveis-urbanos",
    "/catalogo", "/catalogo/",
]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","leilao"]

DOC_KW = re.compile(
    r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|"
    r"memorial|escritura|penhora|registro|processo",
    re.IGNORECASE
)
PDF_EXT = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

def is_imovel(titulo: str, url: str = "") -> bool:
    """Retorna True se o item parece ser um imóvel (não veículo/moto/produto)."""
    txt = (titulo + " " + url).lower()
    nao_imovel = [
        "veículo","veiculo","automóvel","automovel","moto","motocicl",
        "caminhão","caminhao","trator","máquina","maquina","equipamento",
        "eletro","celular","notebook","sucata","eletrodom",
    ]
    imovel_kw = [
        "imóvel","imovel","apart","casa","terreno","galpão","sala","loja",
        "gleba","rural","lote","fazenda","sítio","comercial","prédio",
        "sobrado","flat","kitnet","chácara","conjunto",
    ]
    if any(k in txt for k in nao_imovel): return False
    if any(k in txt for k in imovel_kw): return True
    return True  # sem pistas → inclui (filtro conservador)

def extract_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Extrai links de documentos (edital, matrícula, laudo) da página."""
    arquivos = []
    seen = set()
    # Tags <a href>
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#","javascript","mailto","tel")): continue
        url_abs = urljoin(page_url, href)
        if url_abs in seen: continue
        text = (a.get_text() + " " + href).lower()
        if PDF_EXT.search(href) or DOC_KW.search(text):
            tipo = "edital" if "edital" in text else \
                   "matricula" if "matric" in text or "matríc" in text else \
                   "laudo" if "laudo" in text else \
                   "certidao" if "certid" in text else "pdf"
            nome = a.get_text(strip=True)[:80] or tipo.capitalize()
            arquivos.append({"tipo": tipo, "url": url_abs, "nome": nome})
            seen.add(url_abs)
        if len(arquivos) >= 15: break
    # Padrão onclick="ExibeDoc('/path.pdf')"
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

def extrair_imovel_do_card(card_html: str, card_url: str, lei: dict, base_url: str) -> dict | None:
    """Extrai dados de um card HTML individual."""
    soup = BeautifulSoup(card_html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    # Título
    titulo = ""
    for sel in ["h1","h2","h3",".titulo",".title",".lote-titulo","[class*='titulo']","[class*='title']"]:
        el = soup.select_one(sel)
        if el:
            titulo = el.get_text(strip=True)[:200]
            break
    if not titulo:
        titulo = texto[:120]

    if not is_imovel(titulo, card_url): return None

    # Preços
    precos = RE_PRICE.findall(texto)
    v_min = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None

    # Área
    area_m = RE_AREA.search(texto)
    area = area_m.group(1).replace(",",".") if area_m else None

    # Quartos
    q_m = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None

    # Datas
    datas = RE_DATE.findall(texto)
    data1 = parse_date(datas[0]) if datas else None
    data2 = parse_date(datas[1]) if len(datas) > 1 else None

    # UF e cidade
    uf_m = RE_UF.search(texto)
    uf = uf_m.group() if uf_m else "SC"

    # Cidade — tenta padrão "Cidade/UF"
    cid_m = re.search(r"([A-ZÀ-Ú][a-zà-ú]+(?:\s[A-ZÀ-Úa-zà-ú]+){0,3})\s*/\s*SC", texto)
    cidade = cid_m.group(1).strip() if cid_m else ""

    # CEP
    cep_m = RE_CEP.search(texto)
    cep = cep_m.group() if cep_m else ""

    # Processo
    proc_m = RE_PROC.search(texto)
    processo = proc_m.group() if proc_m else ""

    # Imagem
    imgs = soup.find_all("img", src=True)
    img_principal = ""
    for img in imgs:
        src = img.get("src","") or img.get("data-src","")
        src_abs = urljoin(base_url, src)
        if src and not any(k in src.lower() for k in ["logo","icon","banner","avatar"]):
            img_principal = src_abs
            break

    # Endereço
    end_el = soup.select_one(
        "[class*='endere'],[class*='local'],[class*='address'],[itemprop='address']"
    )
    endereco = end_el.get_text(strip=True)[:300] if end_el else ""

    # Descrição
    desc_el = soup.select_one(
        "[class*='descri'],[class*='desc'],[class*='detail'],[class*='detalhe']"
    )
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else texto[:300]

    # Documentos
    arquivos = extract_arquivos(soup, card_url)

    return {
        "id_externo": make_id(card_url),
        "leiloeiro": lei["nome"],
        "leiloeiro_site": lei["site"],
        "titulo": titulo,
        "tipo_imovel": infer_tipo(titulo, desc),
        "tipo_leilao": infer_tipo_leilao(titulo, desc),
        "estado": uf,
        "cidade": cidade,
        "cep": cep,
        "endereco_completo": endereco,
        "valor_minimo": v_min,
        "valor_avaliacao": v_aval,
        "area_total": area,
        "quartos": quartos,
        "data_primeiro_leilao": data1,
        "data_segundo_leilao": data2,
        "data_encerramento": None,
        "url_original": card_url,
        "imagem_principal": img_principal,
        "numero_processo": processo,
        "arquivos": json.dumps(arquivos, ensure_ascii=False),
        "descricao": desc,
    }

def scrape_site_httpx(lei: dict, max_paginas: int = 8) -> list[dict]:
    """Scraping via requests (HTTP estático). Retorna lista de imóveis."""
    base = lei["site"].rstrip("/")
    session = requests.Session()
    session.headers.update(HEADERS)
    imoveis = []
    lote_urls = set()

    # Descobre URL de listagem
    listagem_urls = [base + p for p in LISTING_PATHS] + [base]
    listagem_url  = None
    for url in listagem_urls:
        try:
            r = session.get(url, timeout=20, allow_redirects=True, verify=False)
            if r.status_code == 200 and any(k in r.text.lower() for k in LISTING_KW):
                listagem_url = url
                break
        except Exception:
            continue

    if not listagem_url:
        return []

    # Pagina
    for pag in range(1, max_paginas + 1):
        if pag == 1:
            url_pg = listagem_url
        else:
            # Tenta variações de paginação
            candidates = [
                f"{listagem_url}?pagina={pag}",
                f"{listagem_url}?page={pag}",
                f"{listagem_url}?pag={pag}",
                f"{listagem_url}/{pag}/",
                f"{listagem_url}/{pag}",
            ]
            url_pg = candidates[0]

        try:
            r = session.get(url_pg, timeout=20, verify=False)
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, "html.parser")

            # Extrai links de lotes
            novos = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                href_abs = urljoin(base, href)
                txt = (a.get_text() + href).lower()
                if any(k in txt or k in href_abs for k in ["lote","imovel","oferta","arrematacao"]):
                    if href_abs not in lote_urls and urlparse(href_abs).netloc:
                        lote_urls.add(href_abs)
                        novos += 1

            if novos == 0 and pag > 1:
                break
            time.sleep(1)
        except Exception:
            break

    # Visita cada lote
    for url in list(lote_urls)[:200]:
        try:
            r = session.get(url, timeout=20, verify=False)
            if r.status_code != 200: continue
            im = extrair_imovel_do_card(r.text, url, lei, base)
            if im:
                imoveis.append(im)
            time.sleep(0.8)
        except Exception:
            continue

    return imoveis

def scrape_site_playwright(lei: dict, max_paginas: int = 8) -> list[dict]:
    """Scraping via Playwright (SPA / JS-heavy). Retorna lista de imóveis."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[WARN] Playwright não instalado. Pulando site JS-heavy.")
        return []

    base = lei["site"].rstrip("/")
    imoveis = []
    lote_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Descobre URL de listagem
        listagem_url = None
        for path in [""] + LISTING_PATHS:
            try:
                url = base + path
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                if any(k in page.content().lower() for k in LISTING_KW):
                    listagem_url = url
                    break
            except Exception:
                continue

        if not listagem_url:
            browser.close()
            return []

        # Pagina
        for pag in range(1, max_paginas + 1):
            if pag == 1:
                url_pg = listagem_url
            else:
                url_pg = f"{listagem_url}?pagina={pag}"

            try:
                page.goto(url_pg, timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(2000)
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")

                novos = 0
                for a in soup.find_all("a", href=True):
                    href_abs = urljoin(base, a["href"])
                    txt = (a.get_text() + a["href"]).lower()
                    if any(k in txt or k in href_abs for k in ["lote","imovel","oferta"]):
                        if href_abs not in lote_urls and urlparse(href_abs).netloc:
                            lote_urls.add(href_abs)
                            novos += 1

                if novos == 0 and pag > 1:
                    break
                time.sleep(2)
            except Exception:
                break

        # Visita lotes
        for url in list(lote_urls)[:200]:
            try:
                page.goto(url, timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(1500)
                im = extrair_imovel_do_card(page.content(), url, lei, base)
                if im:
                    imoveis.append(im)
                time.sleep(1)
            except Exception:
                continue

        browser.close()
    return imoveis

def is_js_heavy(html: str) -> bool:
    """Detecta se o site é SPA/JS-heavy."""
    low = html.lower()
    if len(re.findall(r"<div", low)) < 10: return True
    kws = ["react","vue","angular","next.js","nuxt","__next_data__","window.__state"]
    return sum(1 for k in kws if k in low) >= 2

def scrape_leiloeiro(lei: dict) -> list[dict]:
    """Scraping inteligente: tenta HTTP primeiro, Playwright se necessário."""
    site = lei["site"]
    if not site: return []

    log(f"  -> {lei['nome']} | {site}")

    # Teste rápido com requests
    try:
        r = requests.get(site, headers=HEADERS, timeout=20, verify=False)
        usa_playwright = is_js_heavy(r.text) or r.status_code != 200
    except Exception as e:
        log(f"     [WARN] Falha HTTP: {e}")
        usa_playwright = True

    if usa_playwright:
        log(f"     Usando Playwright (JS-heavy)")
        imoveis = scrape_site_playwright(lei)
    else:
        imoveis = scrape_site_httpx(lei)

    return imoveis

# ── 4. Relatório periódico ────────────────────────────────────────────────────
def relatorio():
    """Imprime relatório a cada 5 minutos enquanto o scraping roda."""
    while True:
        time.sleep(300)  # 5 minutos
        with _lock:
            if not _estado["por_leiloeiro"]: continue
            total = sum(_estado["por_leiloeiro"].values())
            print("\n" + "="*60)
            print(f"RELATÓRIO PARCIAL  {datetime.now().strftime('%H:%M:%S')}")
            print(f"Total imóveis: {total}  |  Sites ok: {_estado['sites_ok']}  "
                  f"|  Erros: {_estado['sites_err']}  |  Sem leilão: {_estado['sites_sem_leilao']}")
            print(f"Processando: {_estado['leiloeiro_atual']}")
            print("-"*60)
            for nome, cnt in sorted(_estado["por_leiloeiro"].items(), key=lambda x:-x[1]):
                print(f"  {cnt:>4}  {nome}")
            print("="*60 + "\n")

# ── 5. Persistência CSV ────────────────────────────────────────────────────────
def salvar_csv_leiloeiros(leiloeiros: list[dict]) -> Path:
    CSV_DIR.mkdir(exist_ok=True)
    out = CSV_DIR / f"leiloeiros_jucesc_{TODAY}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["nome","aarc","data_matricula","site","email","telefone","junta","uf"])
        w.writeheader()
        for lei in leiloeiros:
            w.writerow(lei)
    log(f"[CSV] Leiloeiros: {out}")
    return out

def salvar_csv_imoveis(imoveis: list[dict]) -> Path:
    CSV_DIR.mkdir(exist_ok=True)
    out = CSV_DIR / f"imoveis_jucesc_{TODAY}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_IMOVEIS, extrasaction="ignore")
        w.writeheader()
        for im in imoveis:
            w.writerow(im)
    log(f"[CSV] Imóveis: {out} ({len(imoveis)} registros)")
    return out

# ── 6. Importação SQLite ───────────────────────────────────────────────────────
def importar_sqlite(imoveis: list[dict]) -> tuple[int, int]:
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
    for im in imoveis:
        try:
            conn.execute(
                "INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    im["id_externo"], im["leiloeiro"], "JUCESC", im["leiloeiro_site"],
                    im["titulo"], im.get("descricao","")[:300],
                    im.get("endereco_completo",""), im.get("cidade",""), im.get("estado","SC"),
                    im.get("valor_minimo"), im.get("valor_avaliacao"),
                    im.get("data_primeiro_leilao",""),
                    im["url_original"], im.get("tipo_imovel",""),
                    im.get("imagem_principal",""), agora,
                )
            )
            ins += 1
        except sqlite3.IntegrityError:
            dup += 1
        except Exception as e:
            log(f"[SQLite ERR] {e}")

    conn.commit()
    conn.close()
    return ins, dup

# ── 7. Importação PostgreSQL ───────────────────────────────────────────────────
def importar_postgres(imoveis: list[dict]) -> tuple[int, int]:
    """Importa via docker exec para garantir o banco correto."""
    import subprocess, tempfile

    if not imoveis: return 0, 0

    # Monta SQL de upsert em lotes
    ins = dup = 0
    for im in imoveis:
        titulo = (im.get("titulo","") or "")[:500].replace("'","''")
        desc   = (im.get("descricao","") or "")[:500].replace("'","''")
        lei    = (im.get("leiloeiro","") or "")[:300].replace("'","''")
        site   = (im.get("leiloeiro_site","") or "")[:500].replace("'","''")
        url    = (im.get("url_original","") or "")[:1000].replace("'","''")
        img    = (im.get("imagem_principal","") or "")[:1000].replace("'","''")
        arq    = (im.get("arquivos","[]") or "[]").replace("'","''")
        cidade = (im.get("cidade","") or "")[:200].replace("'","''")
        end    = (im.get("endereco_completo","") or "")[:500].replace("'","''")
        cep    = (im.get("cep","") or "")[:10].replace("'","''")
        proc   = (im.get("numero_processo","") or "")[:100].replace("'","''")
        uf     = (im.get("estado","SC") or "SC")[:2]
        tipo_i = (im.get("tipo_imovel","outro") or "outro").lower()
        tipo_l = (im.get("tipo_leilao","extrajudicial") or "extrajudicial").lower()
        id_ext = im.get("id_externo","")[:200].replace("'","''")
        vmin   = im.get("valor_minimo") or "NULL"
        vaval  = im.get("valor_avaliacao") or "NULL"
        area   = im.get("area_total") or "NULL"
        quartos = im.get("quartos") or "NULL"
        d1 = f"'{im['data_primeiro_leilao']}'" if im.get("data_primeiro_leilao") else "NULL"
        d2 = f"'{im['data_segundo_leilao']}'" if im.get("data_segundo_leilao") else "NULL"

        sql = f"""
        INSERT INTO imoveis (
            fonte_id, id_externo, titulo, descricao, url_original,
            tipo_imovel, tipo_leilao, status, categoria,
            cidade, estado, cep, logradouro, endereco_completo,
            valor_minimo, valor_avaliacao, area_total, quartos,
            data_primeiro_leilao, data_segundo_leilao,
            imagem_principal, arquivos, numero_processo,
            leiloeiro, ativo, classificado, geocodificado,
            criado_em, atualizado_em
        ) VALUES (
            (SELECT id FROM fontes WHERE nome='JUCESC' LIMIT 1),
            '{id_ext}', '{titulo}', '{desc}', '{url}',
            '{tipo_i}', '{tipo_l}', 'ABERTO', 'IMOVEL',
            '{cidade}', '{uf}', '{cep}', '', '{end}',
            {vmin}, {vaval}, {area}, {quartos},
            {d1}, {d2},
            '{img}', '{arq}', '{proc}',
            '{lei}', true, false, false,
            NOW(), NOW()
        )
        ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
            atualizado_em = NOW(),
            titulo = EXCLUDED.titulo,
            valor_minimo = EXCLUDED.valor_minimo,
            data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
            arquivos = EXCLUDED.arquivos;
        """
        try:
            result = subprocess.run(
                ["docker","exec","leilao_postgres",
                 "psql","-U","leilao","-d","leilao_db","-c", sql],
                capture_output=True, text=True, timeout=30
            )
            if "INSERT" in result.stdout or "UPDATE" in result.stdout:
                ins += 1
            elif result.returncode != 0:
                dup += 1
        except Exception as e:
            log(f"[PG ERR] {e}")

    return ins, dup

def garantir_fonte_postgres():
    """Garante que a fonte JUCESC existe no banco."""
    sql = """
    INSERT INTO fontes (nome, url_base, ativo, criado_em)
    VALUES ('JUCESC','https://leiloeiros.jucesc.sc.gov.br/site/',true,NOW())
    ON CONFLICT (nome) DO NOTHING;
    """
    try:
        import subprocess
        subprocess.run(
            ["docker","exec","leilao_postgres",
             "psql","-U","leilao","-d","leilao_db","-c", sql],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        log(f"[WARN] Não foi possível garantir fonte: {e}")

# ── 8. Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sem-banco", action="store_true",
                        help="Não importa para PostgreSQL")
    parser.add_argument("--max-sites", type=int, default=999,
                        help="Limite de sites a visitar")
    parser.add_argument("--reset", action="store_true",
                        help="Ignora progresso anterior")
    args = parser.parse_args()

    log("="*60)
    log("JUCESC Scraper — Imóveis de Leiloeiros REGULAR")
    log("="*60)

    # Inicia thread de relatório periódico
    t = threading.Thread(target=relatorio, daemon=True)
    t.start()

    # Carrega leiloeiros
    jucesc_list = fetch_jucesc_regulares()
    csv_list    = load_csv_jucesc()
    leiloeiros  = merge_leiloeiros(jucesc_list, csv_list)

    com_site = [l for l in leiloeiros if l["site"]]
    log(f"\nTotal: {len(leiloeiros)} leiloeiros | {len(com_site)} com site")

    # Salva CSV de leiloeiros
    salvar_csv_leiloeiros(leiloeiros)

    # Progresso anterior
    visitados = set()
    if not args.reset and PROGRESS_FILE.exists():
        try:
            prog = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            visitados = set(prog.get("visitados", []))
            _estado["imoveis"] = prog.get("imoveis", [])
            _estado["por_leiloeiro"] = prog.get("por_leiloeiro", {})
            log(f"[INFO] Retomando: {len(visitados)} sites já visitados, "
                f"{len(_estado['imoveis'])} imóveis")
        except Exception:
            pass

    # Garante fonte no Postgres
    if not args.sem_banco:
        garantir_fonte_postgres()

    # Scraping
    sites_para_visitar = [l for l in com_site if l["site"] not in visitados][:args.max_sites]
    log(f"\nIniciando scraping de {len(sites_para_visitar)} sites...\n")

    for i, lei in enumerate(sites_para_visitar, 1):
        with _lock:
            _estado["leiloeiro_atual"] = lei["nome"]

        log(f"[{i}/{len(sites_para_visitar)}] {lei['nome']}")
        try:
            imoveis = scrape_leiloeiro(lei)
            with _lock:
                _estado["imoveis"].extend(imoveis)
                _estado["por_leiloeiro"][lei["nome"]] = \
                    _estado["por_leiloeiro"].get(lei["nome"], 0) + len(imoveis)
                if imoveis:
                    _estado["sites_ok"] += 1
                    log(f"     -> {len(imoveis)} imóveis")
                else:
                    _estado["sites_sem_leilao"] += 1
                    log(f"     -> 0 imóveis (sem leilão ativo)")
        except Exception as e:
            with _lock:
                _estado["sites_err"] += 1
                _estado["erros"].append((lei["site"], str(e)))
            log(f"     [ERRO] {e}")

        visitados.add(lei["site"])

        # Salva progresso
        try:
            PROGRESS_FILE.write_text(json.dumps({
                "visitados": list(visitados),
                "imoveis": _estado["imoveis"],
                "por_leiloeiro": _estado["por_leiloeiro"],
                "atualizado": datetime.now().isoformat(),
            }, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

        time.sleep(2)

    # Resultado final
    all_imoveis = _estado["imoveis"]
    log(f"\n{'='*60}")
    log(f"SCRAPING CONCLUÍDO — {len(all_imoveis)} imóveis coletados")
    log(f"{'='*60}")

    # CSV de imóveis
    csv_imoveis = salvar_csv_imoveis(all_imoveis)

    # SQLite
    log("\n[SQLite] Importando...")
    ins_sq, dup_sq = importar_sqlite(all_imoveis)
    log(f"  {ins_sq} inseridos, {dup_sq} duplicados")

    # PostgreSQL
    if not args.sem_banco:
        log("\n[PostgreSQL] Importando via Docker...")
        ins_pg, dup_pg = importar_postgres(all_imoveis)
        log(f"  {ins_pg} inseridos/atualizados, {dup_pg} conflitos")

    # Relatório final
    log("\n" + "="*60)
    log("RELATÓRIO FINAL POR LEILOEIRO")
    log("="*60)
    for nome, cnt in sorted(_estado["por_leiloeiro"].items(), key=lambda x:-x[1]):
        log(f"  {cnt:>4}  {nome}")

    total = sum(_estado["por_leiloeiro"].values())
    log(f"\nTotal geral: {total} imóveis")
    log(f"Sites com imóveis: {_estado['sites_ok']}")
    log(f"Sites sem leilão ativo: {_estado['sites_sem_leilao']}")
    log(f"Sites com erro: {_estado['sites_err']}")

    if _estado["erros"]:
        log("\nErros encontrados:")
        for site, msg in _estado["erros"]:
            log(f"  {site}: {msg}")

    # Salva relatório de dificuldades
    gerar_relatorio_md(all_imoveis, leiloeiros)

    log(f"\nCSV imóveis: {csv_imoveis}")
    log("Scraping JUCESC concluído.")

def gerar_relatorio_md(imoveis: list[dict], leiloeiros: list[dict]):
    """Gera texto Markdown do relatório para ser adicionado ao captura_dados_leiloes_v2.md"""
    com_site  = [l for l in leiloeiros if l["site"]]
    sem_site  = [l for l in leiloeiros if not l["site"]]
    com_imov  = len(_estado["por_leiloeiro"])
    total_imov = sum(_estado["por_leiloeiro"].values())
    dist = sorted(_estado["por_leiloeiro"].items(), key=lambda x:-x[1])

    today = datetime.now().strftime("%d/%m/%Y %H:%M")

    linhas_lei = "\n".join(f"- **{nome}**: {cnt} imóveis" for nome, cnt in dist[:20])
    linhas_err = "\n".join(f"- `{s}`: {m}" for s, m in (_estado["erros"] or [])[:10])

    # Tipos de erro observados
    erros_obs = set()
    for _, msg in _estado["erros"]:
        if "timeout" in msg.lower(): erros_obs.add("Timeout de conexão (site lento / offline)")
        if "ssl" in msg.lower(): erros_obs.add("Erro SSL / certificado inválido")
        if "connect" in msg.lower(): erros_obs.add("Falha de conexão (site fora do ar)")
        if "playwright" in msg.lower(): erros_obs.add("Playwright não iniciou corretamente")
    if not erros_obs: erros_obs.add("Sem erros críticos registrados")

    md = f"""
---

## 33. Scraping JUCESC — Leiloeiros Regulares de SC ({today})

Coleta realizada em `https://leiloeiros.jucesc.sc.gov.br/site/` — portal de leiloeiros oficiais do Estado de Santa Catarina.
Script: `scraper_jucesc.py` + `importar_leiloesjudiciais.py` (padrão adaptado).

### 33.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros REGULAR na JUCESC | {len(leiloeiros)} |
| Leiloeiros com site identificado | {len(com_site)} |
| Leiloeiros sem site (só e-mail) | {len(sem_site)} |
| Leiloeiros com imóveis capturados | {com_imov} |
| Total de imóveis coletados | {total_imov} |
| Sites com erro | {_estado['sites_err']} |
| Sites sem leilão ativo | {_estado['sites_sem_leilao']} |
| CSV leiloeiros | `csv/leiloeiros_jucesc_{TODAY}.csv` |
| CSV imóveis | `csv/imoveis_jucesc_{TODAY}.csv` |

### 33.2. Distribuição por leiloeiro (top 20)

{linhas_lei}

### 33.3. Principais dificuldades encontradas

#### 33.3.1. JUCESC não expõe URL do site de cada leiloeiro

**Problema:** O portal `leiloeiros.jucesc.sc.gov.br/site/` lista apenas 4 colunas:
AARC, Nome, Data de Matrícula e Situação. Não há coluna de site, e-mail ou telefone.

**Impacto:** Impossível descobrir o site de cada leiloeiro só pela JUCESC.

**Solução aplicada:** Cruzamento com `leiloeiros_regulares.csv` (FENAJU),
que contém `site`, `email` e `telefone` para boa parte dos leiloeiros SC.

**Solução recomendada:**
1. Automatizar cruzamento com FENAJU via `https://www.fenaju.org.br/leiloeiro/{slug}`.
2. Derivar site do domínio do e-mail quando não genérico (gmail, hotmail, etc.).
3. Usar Google Custom Search API para descoberta automática de sites restantes.

```python
def descobrir_site_por_nome(nome: str) -> str | None:
    slug = nome.lower().replace(" ", "-")
    candidatos = [
        f"https://www.{slug.split('-')[0]}leiloes.com.br",
        f"https://www.{slug}leiloes.com.br",
    ]
    for url in candidatos:
        try:
            r = requests.head(url, timeout=5)
            if r.status_code < 400:
                return url
        except Exception:
            pass
    return None
```

#### 33.3.2. Alta proporção de leiloeiros sem site próprio (~{len(sem_site)}/{len(leiloeiros)})

**Problema:** Muitos leiloeiros JUCESC não possuem site próprio — atuam via
plataformas terceiras (leiloesjudiciais.com.br, formulaleiloes.com.br, etc.)
ou apenas presencialmente/por telefone.

**Solução recomendada:** Rastrear esses leiloeiros nas plataformas de leilão judicial
via nome: `leiloesjudiciais.com.br/leiloeiro/{nome-slugificado}`.

#### 33.3.3. Sites JS-heavy (SPAs React/Vue/Next.js)

**Problema:** Sites modernos de leiloeiro rendem 0 lotes via `requests` —
o conteúdo só aparece após execução do JavaScript.

**Solução aplicada:** Detecção automática via `is_js_heavy()` + fallback para Playwright.

**Solução recomendada:** Priorizar interceptação da API JSON interna no Playwright:
```python
page.on("response", lambda r: capturar_json(r) if "/api/" in r.url else None)
```

#### 33.3.4. Sites fora do ar ou domínios expirados

**Problema:** Alguns domínios derivados do e-mail não existem ou retornam 404/SSL error.

**Erros observados:**
{linhas_err if linhas_err else "- Nenhum erro crítico registrado"}

**Solução recomendada:** Pré-filtro de domínios antes do scraping:
```python
def dominio_ativo(url: str, timeout=5) -> bool:
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(urlparse(url).netloc)
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False
```

#### 33.3.5. Campos incompletos — cidade, estado, imagem

**Problema:** Sites sem estrutura padronizada (HTML livre) não têm campos
semânticos para `cidade`, `estado` e `imagem`. Muitos imóveis ficam com
`cidade=NULL` mesmo quando a informação está no texto.

**Solução recomendada:**
1. Geocoding reverso pós-scraping via Nominatim quando há endereço.
2. Regex de UF/cidade mais agressiva no texto completo.
3. Enriquecimento via API IBGE de geolocalização.

#### 33.3.6. Valores e datas ausentes

**Problema:** Sites de leiloeiros individuais frequentemente exibem valores e datas
em formatos não-padronizados (ex.: "Lance mínimo: R$ 120.000" em um parágrafo
sem seletor CSS específico).

**Solução aplicada:** Regex genérica `R[$]\\s*(\\d{1,3}(?:[.\\s]\\d{3})*(?:,\\d{2})?)`.

**Solução recomendada:** Extratores específicos por plataforma (ver seção 27.6)
para sites que usam o mesmo sistema base.

### 33.4. Sugestões de melhoria

1. **Integrar com leiloesjudiciais.com.br por nome** — capturar imóveis de
   leiloeiros JUCESC que não têm site próprio mas publicam nessa plataforma.
2. **Re-scraping semanal** — leiloeiros individuais têm leilões esporádicos;
   agendar via Celery beat:
   ```python
   "scrape-jucesc-weekly": {{
       "task": "scrapers.tasks.scrape_csv",
       "schedule": crontab(hour=4, minute=0, day_of_week="tuesday"),
       "args": ["csv/leiloeiros_jucesc_{TODAY}.csv"],
   }}
   ```
3. **Normalizar cidades SC** após importação:
   ```bash
   python corrigir_cidades.py --todos
   python corrigir_cidades.py --deduplicar
   ```
4. **Adicionar leiloeiros JUCESC sem site à tabela `leiloeiros`** mesmo sem site —
   permite rastreá-los quando site aparecer.

### 33.5. Ordem de execução

```bash
cd C:\\Users\\arthur\\OneDrive\\Documentos\\Cursor\\leiloes

# 1. Scraping
python scraper_jucesc.py

# 2. Corrigir encoding de cidades SC
cd ..\\leilao-scraper\\leilao-scraper
python corrigir_cidades.py --todos
python corrigir_cidades.py --deduplicar

# 3. Pós-processamento
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 2000"
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"
docker restart leilao_api
```

### 33.6. Arquivos criados

```
leiloes/
├── scraper_jucesc.py                        ← scraper principal
├── scraper_jucesc_progress.json             ← progresso (retomável)
├── scraper_jucesc.log                       ← log completo
└── csv/
    ├── leiloeiros_jucesc_{TODAY}.csv        ← leiloeiros REGULAR com/sem site
    └── imoveis_jucesc_{TODAY}.csv           ← imóveis coletados
```
"""

    # Adiciona ao captura_dados_leiloes_v2.md
    md_file = BASE / "captura_dados_leiloes_v2.md"
    try:
        with open(md_file, "a", encoding="utf-8") as f:
            f.write(md)
        log(f"\n[MD] Relatório adicionado a {md_file}")
    except Exception as e:
        log(f"[WARN] Não foi possível escrever no MD: {e}")
        # Salva como arquivo separado
        rel_file = BASE / f"relatorio_jucesc_{TODAY}.md"
        rel_file.write_text(md, encoding="utf-8")
        log(f"[MD] Relatório salvo em {rel_file}")


if __name__ == "__main__":
    main()
