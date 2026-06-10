"""
scraper_jucisrs.py
==================
Coleta imóveis de leiloeiros REGULAR credenciados pela JUCISRS (RS).

Fluxo:
  1. POST https://sistemas.jucisrs.rs.gov.br/leiloeiros/busca/listar
     → lista completa (245 Regular, 121 Cancelados, 16 Suspensos)
  2. Filtra apenas blocos sem "(Cancelado)" e sem "(Suspenso)"
  3. Extrai: nome, matrícula, cidade, UF, site, email, telefone
  4. Visita cada site com requests (HTTP) → Playwright fallback
  5. Salva CSV → csv/leiloeiros_jucisrs_YYYY-MM-DD.csv
                  csv/imoveis_jucisrs_YYYY-MM-DD.csv
  6. Importa para SQLite + PostgreSQL Docker
  7. Relatório a cada 5 min + relatório final

Uso:
  python scraper_jucisrs.py [--sem-banco] [--max-sites N] [--max-paginas N] [--reset]
"""
import sys, io, os
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re, csv, json, time, hashlib, sqlite3, argparse, threading, subprocess
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse, urljoin

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ── Configuração ──────────────────────────────────────────────────────────────
BASE          = Path(__file__).resolve().parent
CSV_DIR       = BASE / "csv"
DB_FILE       = BASE / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE / "scraper_jucisrs_progress.json"
LOG_FILE      = BASE / "scraper_jucisrs.log"

JUCISRS_URL   = "https://sistemas.jucisrs.rs.gov.br/leiloeiros/busca/listar"
JUCISRS_HOME  = "https://sistemas.jucisrs.rs.gov.br/leiloeiros/"
TODAY         = datetime.now().strftime("%Y-%m-%d")

FIELDNAMES = [
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

_lock   = threading.Lock()
_estado = {
    "imoveis": [],
    "por_leiloeiro": {},
    "sites_ok": 0,
    "sites_err": 0,
    "sites_sem_leilao": 0,
    "erros": [],
    "leiloeiro_atual": "",
    "inicio": datetime.now().isoformat(),
}

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    linha = f"[{ts}] {msg}"
    print(linha)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

# ── Helpers ───────────────────────────────────────────────────────────────────
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
            import datetime as dt; dt.date(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError: return None
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        try:
            import datetime as dt; dt.date(y, mo, d)
            return m2.group(0)
        except ValueError: return None
    return None

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","hectare","rural","chácara","gleba"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência"]): return "casa"
    if any(k in txt for k in ["terreno","lote urbano"]): return "terreno"
    if any(k in txt for k in ["galpão","armazém","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","comercial"]): return "comercial"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","hasta","praça","tjrs","jfrs"]): return "judicial"
    if any(k in txt for k in ["banco","caixa","financiamento","retomada"]): return "bancario"
    return "extrajudicial"

RE_PRICE = re.compile(r"R[\$\s]+(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)")
RE_AREA  = re.compile(r"(\d+[\.,]?\d*)\s*m[²2]", re.IGNORECASE)
RE_PROC  = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
RE_CEP   = re.compile(r"\d{5}-?\d{3}")
RE_UF    = re.compile(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b")
RE_DATE  = re.compile(r"\d{2}[\/\.\-]\d{2}[\/\.\-]\d{4}")
DOC_KW   = re.compile(
    r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|"
    r"memorial|escritura|penhora|registro|processo", re.IGNORECASE)
PDF_EXT  = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

# ── 1. Fetch JUCISRS listing ──────────────────────────────────────────────────
def fetch_jucisrs_regulares() -> list[dict]:
    """Busca e parseia a lista completa de leiloeiros Regular da JUCISRS."""
    log(f"Buscando lista JUCISRS em {JUCISRS_URL} ...")
    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        sess.get(JUCISRS_HOME, timeout=20, verify=False)
        r = sess.post(JUCISRS_URL,
                      data={"Nome": "", "CodMunicipio": "0"},
                      timeout=30, verify=False)
        r.encoding = "latin-1"
        html = r.text
    except Exception as e:
        log(f"[ERRO] Falha ao buscar JUCISRS: {e}")
        return []

    # Divide por <hr> — cada bloco = 1 leiloeiro
    blocks = re.split(r"<hr>", html, flags=re.IGNORECASE)
    log(f"  Total blocos HR: {len(blocks)}")

    regulares = []
    cancelados = suspensos = 0

    for block in blocks[4:]:   # primeiros 4 são cabeçalho
        if not block.strip():
            continue
        low = block.lower()
        if "cancelado" in low:
            cancelados += 1
            continue
        if "suspenso" in low:
            suspensos += 1
            continue

        # Limpa HTML
        clean = re.sub(r"<[^>]+>", " ", block)
        clean = re.sub(r"&nbsp;", " ", clean)
        clean = re.sub(r"&amp;", "&", clean)
        clean = re.sub(r"&#\d+;", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) < 10:
            continue

        # ── Matrícula e nome
        # Padrão: "173 - ADEMIR MIGUEL CORRÊA ..."
        mat_m = re.match(r"^(\d+)\s*[-–]\s*([A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇÑ][A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇÑa-záéíóúàâêôãõüçñ\s\.]+)", clean)
        if not mat_m:
            # Tenta sem número de matrícula
            mat_m2 = re.match(r"^([A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇÑ][A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇÑa-záéíóúàâêôãõüçñ\s\.]{4,})", clean)
            if not mat_m2:
                continue
            matricula = ""
            nome = mat_m2.group(1).strip()
        else:
            matricula = mat_m.group(1).strip()
            nome = mat_m.group(2).strip()

        # Remove palavras finais de corte erradas
        nome = re.sub(r"\s+(Posse|www|http|Rua|Av\.|Avenida)\s*:?\s*.*$", "", nome, flags=re.IGNORECASE).strip()
        if len(nome) < 5:
            continue

        # ── Site
        site_m = re.search(r"(https?://\S+|www\.\S+\.\S+)", clean)
        site = site_m.group(1).strip().rstrip(",;)") if site_m else ""
        # Remove links do Diário Oficial (não são sites de leiloeiro)
        if "diariooficial" in site.lower() or "jucesp" in site.lower():
            site = ""
        if site and not site.startswith("http"):
            site = "https://" + site

        # ── Email
        email_m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", clean)
        email = email_m.group(0).strip() if email_m else ""

        # ── Telefone
        tel_m = re.search(r"Telefone\s*:?\s*([+\d\s\(\)\.,-]{7,40})", clean, re.IGNORECASE)
        telefone = tel_m.group(1).strip()[:80] if tel_m else ""

        # ── Cidade e UF
        cid_m = re.search(
            r"([A-ZÀ-Úa-zà-ú][A-ZÀ-Úa-zà-ú\s\.]+)\s*[-–]\s*(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b",
            clean
        )
        cidade = cid_m.group(1).strip() if cid_m else ""
        uf     = cid_m.group(2).strip() if cid_m else "RS"

        # ── CEP
        cep_m = RE_CEP.search(clean)
        cep = cep_m.group() if cep_m else ""

        # Deriva site do email se não tiver
        if not site and email:
            site = _derivar_site_email(email) or ""

        regulares.append({
            "nome": nome,
            "matricula": matricula,
            "site": site,
            "email": email,
            "telefone": telefone,
            "cidade_leiloeiro": cidade,
            "uf_leiloeiro": uf,
            "cep": cep,
            "situacao": "Regular",
            "junta": "JUCISRS",
        })

    log(f"  Regular: {len(regulares)} | Cancelados: {cancelados} | Suspensos: {suspensos}")
    return regulares


def _derivar_site_email(email: str) -> str | None:
    if not email: return None
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m: return None
    dom = m.group(1)
    ignorados = {"gmail.com","hotmail.com","yahoo.com","yahoo.com.br",
                 "outlook.com","terra.com.br","uol.com.br","bol.com.br","ig.com.br",
                 "tjrs.jus.br","rs.gov.br"}
    if dom in ignorados: return None
    return f"https://www.{dom}"


# ── 2. Extração de imóveis ────────────────────────────────────────────────────
LISTING_PATHS = [
    "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/",
    "/leilao", "/leiloes/", "/proximos-leiloes", "/proximos_leiloes",
    "/leiloes/imoveis", "/imoveis-leilao", "/em-leilao",
    "/catalogo", "/catalogo/", "/ofertas",
]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","leilao"]

def is_imovel(titulo: str, url: str = "") -> bool:
    txt = (titulo + " " + url).lower()
    nao = ["veículo","veiculo","automóvel","automovel","moto","motocicl",
           "caminhão","caminhao","trator","máquina","maquina","equipamento",
           "eletro","celular","notebook","sucata","eletrodom"]
    imovel = ["imóvel","imovel","apart","casa","terreno","galpão","sala","loja",
              "gleba","rural","lote","fazenda","sítio","comercial","prédio",
              "sobrado","flat","kitnet","chácara","conjunto"]
    if any(k in txt for k in nao): return False
    if any(k in txt for k in imovel): return True
    return True

def extract_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    arquivos, seen = [], set()
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
            arquivos.append({"tipo": tipo, "url": url_abs, "nome": a.get_text(strip=True)[:80] or tipo.capitalize()})
            seen.add(url_abs)
        if len(arquivos) >= 15: break
    for tag in soup.find_all(onclick=True):
        m = re.search(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", tag.get("onclick",""))
        if m:
            url_abs = urljoin(page_url, m.group(1))
            if url_abs not in seen:
                tipo = "matricula" if "matricula" in m.group(1).lower() else "edital"
                arquivos.append({"tipo": tipo, "url": url_abs, "nome": tipo.capitalize()})
                seen.add(url_abs)
    return arquivos

def extrair_imovel(card_html: str, card_url: str, lei: dict, base_url: str) -> dict | None:
    soup = BeautifulSoup(card_html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    titulo = ""
    for sel in ["h1","h2","h3",".titulo",".title",".lote-titulo","[class*='titulo']","[class*='title']"]:
        el = soup.select_one(sel)
        if el:
            titulo = el.get_text(strip=True)[:200]
            break
    if not titulo:
        titulo = texto[:120]

    if not is_imovel(titulo, card_url): return None

    precos = RE_PRICE.findall(texto)
    v_min  = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None

    area_m  = RE_AREA.search(texto)
    area    = area_m.group(1).replace(",",".") if area_m else None
    q_m     = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None

    datas  = RE_DATE.findall(texto)
    data1  = parse_date(datas[0]) if datas else None
    data2  = parse_date(datas[1]) if len(datas) > 1 else None

    uf_m   = RE_UF.search(texto)
    uf     = uf_m.group() if uf_m else lei.get("uf_leiloeiro","RS")
    cid_m  = re.search(rf"([A-ZÀ-Úa-zà-ú]+(?:\s[A-ZÀ-Úa-zà-ú]+){{0,3}})\s*/\s*{uf}", texto)
    cidade = cid_m.group(1).strip() if cid_m else lei.get("cidade_leiloeiro","")

    cep_m  = RE_CEP.search(texto)
    cep    = cep_m.group() if cep_m else ""
    proc_m = RE_PROC.search(texto)
    proc   = proc_m.group() if proc_m else ""

    imgs   = soup.find_all("img", src=True)
    img    = ""
    for i in imgs:
        src = i.get("src","") or i.get("data-src","")
        if src and not any(k in src.lower() for k in ["logo","icon","banner","avatar","sprite"]):
            img = urljoin(base_url, src)
            break

    end_el = soup.select_one("[class*='endere'],[class*='local'],[class*='address'],[itemprop='address']")
    end    = end_el.get_text(strip=True)[:300] if end_el else ""
    desc_el= soup.select_one("[class*='descri'],[class*='desc'],[class*='detail'],[class*='detalhe']")
    desc   = desc_el.get_text(" ", strip=True)[:500] if desc_el else texto[:300]

    return {
        "id_externo": make_id(card_url),
        "leiloeiro": lei["nome"],
        "leiloeiro_site": lei.get("site",""),
        "titulo": titulo,
        "tipo_imovel": infer_tipo(titulo, desc),
        "tipo_leilao": infer_tipo_leilao(titulo, desc),
        "estado": uf, "cidade": cidade, "cep": cep,
        "endereco_completo": end,
        "valor_minimo": v_min, "valor_avaliacao": v_aval,
        "area_total": area, "quartos": quartos,
        "data_primeiro_leilao": data1, "data_segundo_leilao": data2,
        "data_encerramento": None,
        "url_original": card_url,
        "imagem_principal": img,
        "numero_processo": proc,
        "arquivos": json.dumps(extract_arquivos(soup, card_url), ensure_ascii=False),
        "descricao": desc,
    }

def scrape_httpx(lei: dict, max_pags: int = 8) -> list[dict]:
    base = lei["site"].rstrip("/")
    sess = requests.Session(); sess.headers.update(HEADERS)
    imoveis, lote_urls = [], set()

    listagem_url = None
    for path in [""] + LISTING_PATHS:
        try:
            r = sess.get(base + path, timeout=20, allow_redirects=True, verify=False)
            if r.status_code == 200 and any(k in r.text.lower() for k in LISTING_KW):
                listagem_url = base + path; break
        except Exception: continue
    if not listagem_url: return []

    for pag in range(1, max_pags + 1):
        url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
        try:
            r = sess.get(url_pg, timeout=20, verify=False)
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, "html.parser")
            novos = 0
            for a in soup.find_all("a", href=True):
                href_abs = urljoin(base, a["href"])
                txt = (a.get_text() + a["href"]).lower()
                if any(k in txt or k in href_abs for k in ["lote","imovel","oferta","arrematacao"]):
                    if href_abs not in lote_urls and urlparse(href_abs).netloc:
                        lote_urls.add(href_abs); novos += 1
            if novos == 0 and pag > 1: break
            time.sleep(1)
        except Exception: break

    for url in list(lote_urls)[:200]:
        try:
            r = sess.get(url, timeout=20, verify=False)
            if r.status_code != 200: continue
            im = extrair_imovel(r.text, url, lei, base)
            if im: imoveis.append(im)
            time.sleep(0.8)
        except Exception: continue
    return imoveis

def scrape_playwright(lei: dict, max_pags: int = 8) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[WARN] Playwright não instalado."); return []

    base = lei["site"].rstrip("/")
    imoveis, lote_urls = [], set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], ignore_https_errors=True)
        page = ctx.new_page()

        listagem_url = None
        for path in [""] + LISTING_PATHS:
            try:
                page.goto(base + path, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                if any(k in page.content().lower() for k in LISTING_KW):
                    listagem_url = base + path; break
            except Exception: continue
        if not listagem_url:
            browser.close(); return []

        for pag in range(1, max_pags + 1):
            url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
            try:
                page.goto(url_pg, timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(2000)
                soup = BeautifulSoup(page.content(), "html.parser")
                novos = 0
                for a in soup.find_all("a", href=True):
                    href_abs = urljoin(base, a["href"])
                    txt = (a.get_text() + a["href"]).lower()
                    if any(k in txt or k in href_abs for k in ["lote","imovel","oferta"]):
                        if href_abs not in lote_urls and urlparse(href_abs).netloc:
                            lote_urls.add(href_abs); novos += 1
                if novos == 0 and pag > 1: break
                time.sleep(2)
            except Exception: break

        for url in list(lote_urls)[:200]:
            try:
                page.goto(url, timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(1500)
                im = extrair_imovel(page.content(), url, lei, base)
                if im: imoveis.append(im)
                time.sleep(1)
            except Exception: continue

        browser.close()
    return imoveis

def is_js_heavy(html: str) -> bool:
    markers = ["__next_data__","__nuxt__","react-root","vue-app","ng-app","window.__INITIAL_STATE__"]
    if any(m in html.lower() for m in markers): return True
    return len(BeautifulSoup(html, "html.parser").get_text().strip()) < 300

def scrape_leiloeiro(lei: dict, max_pags: int = 8) -> tuple[list[dict], str]:
    if not lei.get("site"): return [], "sem_site"
    site = lei["site"]
    log(f"  → {lei['nome']} | {site}")
    try:
        r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
        if r.status_code in (404, 410): return [], "offline"
        html = r.text
    except Exception as e:
        log(f"    [WARN] HTTP falhou ({type(e).__name__}). Playwright...")
        imoveis = scrape_playwright(lei, max_pags)
        return imoveis, "ok" if imoveis else "sem_leilao"

    if is_js_heavy(html):
        log(f"    JS-heavy → Playwright")
        imoveis = scrape_playwright(lei, max_pags)
    else:
        imoveis = scrape_httpx(lei, max_pags)
        if not imoveis:
            log(f"    HTTP sem resultados → Playwright")
            imoveis = scrape_playwright(lei, max_pags)

    return imoveis, "ok" if imoveis else "sem_leilao"


# ── 3. Relatório periódico ────────────────────────────────────────────────────
def salvar_progresso():
    with _lock:
        data = {
            "atualizado": datetime.now().isoformat(),
            "total_imoveis": len(_estado["imoveis"]),
            "por_leiloeiro": _estado["por_leiloeiro"],
            "sites_ok": _estado["sites_ok"],
            "sites_err": _estado["sites_err"],
            "sites_sem_leilao": _estado["sites_sem_leilao"],
            "erros": _estado["erros"][-10:],
        }
    PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def relatorio():
    with _lock:
        por = dict(_estado["por_leiloeiro"])
        total = len(_estado["imoveis"])
        atual = _estado["leiloeiro_atual"]
    log(f"\n{'='*60}")
    log(f"RELATÓRIO | Total: {total} imóveis | Agora: {atual}")
    for nome, cnt in sorted(por.items(), key=lambda x: -x[1])[:20]:
        log(f"  {nome[:42]:<42} {cnt:>4}")
    log(f"{'='*60}\n")

def thread_relatorio(stop: threading.Event):
    while not stop.wait(300):
        relatorio()


# ── 4. CSVs ───────────────────────────────────────────────────────────────────
def salvar_csv_leiloeiros(leiloeiros: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"leiloeiros_jucisrs_{TODAY}.csv"
    campos = ["nome","matricula","site","email","telefone","cidade_leiloeiro","uf_leiloeiro","cep","situacao","junta"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader(); w.writerows(leiloeiros)
    log(f"[CSV] Leiloeiros: {path.name} ({len(leiloeiros)} registros)")

def salvar_csv_imoveis(imoveis: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"imoveis_jucisrs_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader(); w.writerows(imoveis)
    log(f"[CSV] Imóveis: {path.name} ({len(imoveis)} registros)")
    return path


# ── 5. SQLite ─────────────────────────────────────────────────────────────────
def importar_sqlite(imoveis: list[dict]):
    log(f"\n[SQLite] Importando {len(imoveis)} imóveis...")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS imoveis (
        id TEXT PRIMARY KEY, leiloeiro TEXT, junta TEXT, site TEXT,
        titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
        lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
        url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
    conn.commit()

    ins = dup = 0
    agora = datetime.now().isoformat(timespec="seconds")
    for r in imoveis:
        def _d(v):
            try: return float(Decimal(str(v).replace(",","."))) if v else None
            except: return None
        try:
            conn.execute("INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r.get("id_externo",""), r.get("leiloeiro",""), "JUCISRS",
                r.get("leiloeiro_site",""),
                r.get("titulo","")[:500], r.get("descricao","")[:300],
                r.get("endereco_completo","")[:200],
                r.get("cidade",""), r.get("estado","RS"),
                _d(r.get("valor_minimo")), _d(r.get("valor_avaliacao")),
                r.get("data_primeiro_leilao",""), r.get("url_original",""),
                r.get("tipo_imovel",""), r.get("imagem_principal",""), agora,
            ))
            ins += 1
        except sqlite3.IntegrityError: dup += 1
        except Exception as e: log(f"  [SQLite ERR] {e}")

    conn.commit(); conn.close()
    log(f"  SQLite: {ins} inseridos, {dup} já existiam")
    return ins


# ── 6. PostgreSQL (via container) ─────────────────────────────────────────────
def importar_postgres(imoveis: list[dict], csv_path: Path):
    log(f"\n[PostgreSQL] Importando {len(imoveis)} imóveis via Docker...")

    # Cria script de importação dentro do container
    docker_script = """
import csv, os, sys
from pathlib import Path
from decimal import Decimal
CSV_FILE = '/tmp/imoveis_jucisrs.csv'
rows = list(csv.DictReader(open(CSV_FILE, newline='', encoding='utf-8-sig')))
print(f'[INFO] {len(rows)} registros')

def _d(v):
    try: return float(Decimal(str(v).replace(',','.'))) if v else None
    except: return None

import psycopg2
db_url = os.environ.get('DATABASE_URL_SYNC','postgresql://leilao:leilao123@postgres:5432/leilao_db').replace('postgresql+asyncpg://','postgresql://')
conn = psycopg2.connect(db_url)
cur = conn.cursor()

cur.execute("INSERT INTO fontes (nome,url_base,ativo,criado_em) VALUES ('JUCISRS','https://sistemas.jucisrs.rs.gov.br/leiloeiros/',true,NOW()) ON CONFLICT (nome) DO NOTHING")
cur.execute("SELECT id FROM fontes WHERE nome='JUCISRS' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
print(f'  fonte_id={FONTE_ID}')

TIPOS_I = {'APARTAMENTO','CASA','TERRENO','COMERCIAL','RURAL','GALPAO','SALA','VAGA','OUTRO'}
TIPOS_L = {'JUDICIAL','EXTRAJUDICIAL','BANCARIO'}
ins=upd=err=0

for r in rows:
    ti = r.get('tipo_imovel','outro').upper()
    if ti not in TIPOS_I: ti='OUTRO'
    tl = r.get('tipo_leilao','extrajudicial').upper()
    if tl not in TIPOS_L: tl='EXTRAJUDICIAL'
    try:
        cur.execute(\"\"\"INSERT INTO imoveis (
            fonte_id,id_externo,titulo,descricao,url_original,
            tipo_imovel,tipo_leilao,status,categoria,
            cidade,estado,cep,endereco_completo,
            valor_minimo,valor_avaliacao,area_total,quartos,
            data_primeiro_leilao,data_segundo_leilao,
            imagem_principal,arquivos,numero_processo,
            leiloeiro,ativo,classificado,geocodificado,criado_em,atualizado_em
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'ABERTO','IMOVEL',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,false,false,NOW(),NOW())
        ON CONFLICT (fonte_id,id_externo) DO UPDATE SET
            titulo=EXCLUDED.titulo,valor_minimo=EXCLUDED.valor_minimo,
            data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
            imagem_principal=EXCLUDED.imagem_principal,
            arquivos=EXCLUDED.arquivos,atualizado_em=NOW()
        \"\"\", (
            FONTE_ID, r.get('id_externo','')[:200], r.get('titulo','')[:500],
            r.get('descricao','')[:500], r.get('url_original','')[:1000],
            ti, tl,
            r.get('cidade','')[:200], r.get('estado','RS')[:2], r.get('cep','')[:10],
            r.get('endereco_completo','')[:500],
            _d(r.get('valor_minimo')), _d(r.get('valor_avaliacao')),
            _d(r.get('area_total')), int(r['quartos']) if r.get('quartos') else None,
            r.get('data_primeiro_leilao','') or None, r.get('data_segundo_leilao','') or None,
            r.get('imagem_principal','')[:1000], r.get('arquivos','[]')[:4000],
            r.get('numero_processo','')[:100], r.get('leiloeiro','')[:300],
        ))
        if cur.rowcount==1: ins+=1
        else: upd+=1
    except Exception as e:
        err+=1; conn.rollback()
        if err<=3: print(f'  ERR: {str(e)[:80]}')
    else:
        if (ins+upd)%100==0: conn.commit()

conn.commit(); cur.close(); conn.close()
print(f'[OK] {ins} inseridos, {upd} atualizados, {err} erros')
"""

    script_path = BASE / "import_jucisrs_docker.py"
    script_path.write_text(docker_script, encoding="utf-8")

    # Copia CSV e script para o container
    try:
        subprocess.run(["docker","cp", str(csv_path), "leilao_api:/tmp/imoveis_jucisrs.csv"], check=True, timeout=60)
        subprocess.run(["docker","cp", str(script_path), "leilao_api:/tmp/import_jucisrs_docker.py"], check=True, timeout=30)
        proc = subprocess.run(
            ["docker","exec","leilao_api","python","/tmp/import_jucisrs_docker.py"],
            capture_output=True, text=True, encoding="utf-8", timeout=300
        )
        log(proc.stdout.strip())
        if proc.returncode != 0:
            log(f"  [ERR] {proc.stderr[:200]}")
    except Exception as e:
        log(f"  [ERR PostgreSQL] {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-banco", action="store_true")
    ap.add_argument("--max-sites", type=int, default=999)
    ap.add_argument("--max-paginas", type=int, default=8)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    log("="*60)
    log(f"JUCISRS Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("="*60)

    # 1. Busca lista
    regulares = fetch_jucisrs_regulares()
    if not regulares:
        log("[ERRO] Nenhum leiloeiro encontrado. Abortando.")
        return

    # Deduplicar sites
    sites_vistos = set()
    com_site = []
    sem_site = []
    for l in regulares:
        site = l.get("site","").rstrip("/")
        if not site:
            sem_site.append(l)
        elif site not in sites_vistos:
            sites_vistos.add(site)
            com_site.append(l)

    log(f"\nTotal Regular: {len(regulares)} | Com site: {len(com_site)} | Sem site: {len(sem_site)}")
    salvar_csv_leiloeiros(regulares)

    # 2. Scraping
    stop = threading.Event()
    t = threading.Thread(target=thread_relatorio, args=(stop,), daemon=True)
    t.start()

    todos_imoveis = []
    limitar = min(args.max_sites, len(com_site))

    for idx, lei in enumerate(com_site[:limitar], 1):
        with _lock:
            _estado["leiloeiro_atual"] = lei["nome"]
        log(f"\n[{idx}/{limitar}] {lei['nome']} | {lei['site']}")

        try:
            imoveis, status = scrape_leiloeiro(lei, args.max_paginas)
        except Exception as e:
            imoveis, status = [], "erro"
            with _lock: _estado["erros"].append((lei.get("site",""), str(e)))
            log(f"  [ERRO] {e}")

        with _lock:
            if status == "ok": _estado["sites_ok"] += 1
            elif status == "erro": _estado["sites_err"] += 1
            else: _estado["sites_sem_leilao"] += 1
            _estado["imoveis"].extend(imoveis)
            _estado["por_leiloeiro"][lei["nome"]] = len(imoveis)

        todos_imoveis.extend(imoveis)
        log(f"  {status} | {len(imoveis)} imóveis | Total: {len(todos_imoveis)}")
        salvar_progresso()
        time.sleep(2)

    stop.set()

    log(f"\n{'='*60}")
    log(f"CONCLUÍDO: {len(todos_imoveis)} imóveis de {len(com_site)} sites")
    relatorio()

    if todos_imoveis:
        csv_path = salvar_csv_imoveis(todos_imoveis)
    else:
        log("[WARN] Nenhum imóvel coletado."); csv_path = None

    if not args.sem_banco and todos_imoveis and csv_path:
        importar_sqlite(todos_imoveis)
        importar_postgres(todos_imoveis, csv_path)

        # Pós-processamento
        log("\n[Pós-processamento]")
        for cmd in [
            ["docker","exec","leilao_api","bash","-c","cd /app && python run.py classificar --limite 2000"],
            ["docker","exec","leilao_api","bash","-c","cd /app && python run.py normalizar-cidades"],
            ["docker","exec","leilao_api","bash","-c","cd /app && python run.py deduplicar"],
        ]:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
                log(f"  {' '.join(cmd[-1:])}: {proc.stdout[-100:].strip()}")
            except Exception as e:
                log(f"  [ERR] {e}")

    # Erros finais
    log("\n[ERROS]")
    with _lock: erros = list(_estado["erros"])
    for site, msg in erros[:10]:
        log(f"  {site}: {msg}")
    if not erros: log("  Nenhum erro registrado.")

    log(f"\n[RESUMO FINAL]")
    log(f"  Sites processados: {len(com_site)}")
    log(f"  Sites OK: {_estado['sites_ok']} | Sem leilão: {_estado['sites_sem_leilao']} | Erro: {_estado['sites_err']}")
    log(f"  Total imóveis: {len(todos_imoveis)}")
    log(f"  CSV leiloeiros: csv/leiloeiros_jucisrs_{TODAY}.csv")
    log(f"  CSV imóveis: csv/imoveis_jucisrs_{TODAY}.csv")


if __name__ == "__main__":
    main()
