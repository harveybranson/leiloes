"""
scraper_jucems.py
=================
Coleta imóveis de leiloeiros REGULAR credenciados pela JUCEMS.

Fluxo:
  1. Parse do arquivo .txt (JUCEMS) + scrape de https://www.jucems.ms.gov.br/
  2. Filtra apenas SituaçãO: Regular
  3. Deriva site a partir do campo "Site:" ou do domínio do e-mail
  4. Visita cada site com requests (HTTP) → Playwright como fallback
  5. Salva CSV → csv/leiloeiros_jucems_YYYY-MM-DD.csv
                  csv/imoveis_jucems_YYYY-MM-DD.csv
  6. Importa para SQLite  (imoveis_leiloeiros.db)
  7. Importa para PostgreSQL Docker (leilao_db)
  8. Relatório por leiloeiro a cada 5 min + relatório final

Uso:
  python scraper_jucems.py [--sem-banco] [--max-sites N] [--reset]
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
BASE         = Path(__file__).resolve().parent
CSV_DIR      = BASE / "csv"
DB_FILE      = BASE / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE / "scraper_jucems_progress.json"
LOG_FILE     = BASE / "scraper_jucems.log"
TXT_FILE     = BASE / "jucems_leiloeiros.txt"

JUCEMS_URL   = "https://www.jucems.ms.gov.br/empresas/controles-especiais/agentes-auxiliares/leiloeiros/"
TODAY        = datetime.now().strftime("%Y-%m-%d")

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
    "erros": [],
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
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        try:
            import datetime as dt
            dt.date(y, mo, d)
            return m2.group(0)
        except ValueError:
            return None
    return None

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","hectare","rural","chácara","gleba"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência","vila"]): return "casa"
    if any(k in txt for k in ["terreno","lote urbano"]): return "terreno"
    if any(k in txt for k in ["galpão","armazém","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","ponto comercial"]): return "comercial"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","hasta","praça","tjms","jfms"]): return "judicial"
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
    r"memorial|escritura|penhora|registro|processo",
    re.IGNORECASE
)
PDF_EXT  = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

# ── 1. Parser do arquivo .txt ──────────────────────────────────────────────────
def parse_txt_jucems(txt_path: Path) -> list[dict]:
    """
    Parseia o arquivo .txt da JUCEMS linha a linha.
    Robusto a encoding corrompido (usa `.` em vez de caracteres acentuados nas regex).
    """
    log(f"Parseando arquivo TXT: {txt_path.name}")
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log(f"[ERRO] Falha ao ler TXT: {e}")
        return []

    leiloeiros = []
    # Divide em blocos por linha em branco dupla
    blocos = re.split(r"\n\s*\n", text)

    for bloco in blocos:
        bloco = bloco.strip()
        if not bloco or len(bloco) < 20:
            continue

        linhas = [l.strip() for l in bloco.splitlines() if l.strip()]
        if not linhas:
            continue

        # ── Situação (regex sem acentos para tolerar encoding corrompido)
        # Captura: "Situação: Regular" / "Situacao: Regular" / "Situa?o: Regular"
        sit_m = re.search(r"Situa.{0,4}o\s*:\s*(.+)", bloco, re.IGNORECASE)
        if not sit_m:
            continue
        situacao = sit_m.group(1).strip()
        if "regular" not in situacao.lower():
            continue

        # ── Nome: primeira linha que seja quase toda maiúsculas e sem ":" ──
        nome = ""
        for linha in linhas:
            if ":" in linha:
                continue
            if len(linha) < 5:
                continue
            # Verifica se >= 70% dos caracteres são letra maiúscula ou espaço
            letras = [c for c in linha if c.isalpha()]
            if letras and sum(1 for c in letras if c.isupper()) / len(letras) >= 0.7:
                nome = linha.strip()
                break
        if not nome:
            continue

        # ── Matrícula (Matr.cula tolera replacement char)
        mat_m = re.search(r"Matr.cula\s*:\s*(\d+)", bloco, re.IGNORECASE)
        matricula = mat_m.group(1) if mat_m else ""

        # ── Site
        site_m = re.search(r"Site\s*:\s*(https?://\S+|www\.\S+)", bloco, re.IGNORECASE)
        site = site_m.group(1).strip().rstrip(",;)") if site_m else ""
        if site and not site.startswith("http"):
            site = "https://" + site

        # ── E-mail
        email_m = re.search(r"E-?mails?\s*:\s*(\S+@\S+)", bloco, re.IGNORECASE)
        if not email_m:
            # Busca qualquer padrão de email no bloco
            email_m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", bloco)
        email = email_m.group(0 if not hasattr(email_m, 'lastindex') or not email_m.lastindex else 1).strip().rstrip(",;") if email_m else ""

        # ── Telefone
        tel_m = re.search(r"Fone\s*:\s*(.+?)(?:\n|E-|Site)", bloco, re.IGNORECASE | re.DOTALL)
        telefone = tel_m.group(1).strip()[:100] if tel_m else ""

        # ── Cidade e UF
        cid_m = re.search(
            r"([\w\sÀ-ÿ]+)\s*\(\s*(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*\)",
            bloco, re.IGNORECASE
        )
        cidade_leiloeiro = cid_m.group(1).strip() if cid_m else ""
        uf_leiloeiro = cid_m.group(2).strip().upper() if cid_m else "MS"

        # ── Deriva site do e-mail se não tiver site
        if not site and email:
            site = derivar_site_do_email(email) or ""

        leiloeiros.append({
            "nome": nome,
            "matricula": matricula,
            "site": site,
            "email": email,
            "telefone": telefone,
            "cidade_leiloeiro": cidade_leiloeiro,
            "uf_leiloeiro": uf_leiloeiro,
            "situacao": situacao,
            "junta": "JUCEMS",
            "fonte": "txt",
        })

    log(f"  TXT: {len(leiloeiros)} leiloeiros Regular encontrados")
    return leiloeiros


def derivar_site_do_email(email: str) -> str | None:
    if not email: return None
    email = email.split()[0].strip()
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m: return None
    dominio = m.group(1)
    ignorados = {"gmail.com","hotmail.com","yahoo.com","yahoo.com.br",
                 "outlook.com","terra.com.br","uol.com.br","bol.com.br","ig.com.br"}
    if dominio in ignorados: return None
    return f"https://www.{dominio}"


# ── 2. Scrape da página JUCEMS ──────────────────────────────────────────────────
def fetch_jucems_regulares() -> list[dict]:
    """Tenta buscar lista oficial da JUCEMS online. Fallback silencioso."""
    log(f"Buscando lista oficial JUCEMS em {JUCEMS_URL} ...")
    try:
        r = requests.get(JUCEMS_URL, headers=HEADERS, timeout=30, verify=False)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log(f"[WARN] Falha ao buscar JUCEMS online: {e}. Usando apenas TXT.")
        return []

    regulares = []
    # Tenta tabela HTML
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3: continue
        # Tenta detectar colunas de nome e situação
        texto_tr = " ".join(td.get_text(strip=True) for td in tds)
        if "regular" in texto_tr.lower():
            # Extrai nome (geralmente primeira coluna substantiva)
            for td in tds:
                txt = td.get_text(strip=True)
                if re.match(r"^[A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ\s]{5,}$", txt):
                    regulares.append({"nome": txt, "junta": "JUCEMS", "fonte": "web"})
                    break

    # Tenta lista de parágrafos/divs
    if not regulares:
        for el in soup.find_all(["p","li","div"]):
            txt = el.get_text(strip=True)
            if "regular" in txt.lower() and len(txt) > 10:
                regulares.append({"nome": txt[:200], "junta": "JUCEMS", "fonte": "web"})

    log(f"  JUCEMS online: {len(regulares)} registros encontrados")
    return regulares


# ── 3. Merge listas ────────────────────────────────────────────────────────────
def merge_leiloeiros(txt_list: list[dict], web_list: list[dict]) -> list[dict]:
    """Junta lista do TXT com a da web, deduplicando por nome."""
    nomes_vistos = set()
    resultado = []

    for lei in txt_list:
        nome_norm = re.sub(r"\s+", " ", lei["nome"]).strip().upper()
        if nome_norm in nomes_vistos: continue
        nomes_vistos.add(nome_norm)
        resultado.append(lei)

    for lei in web_list:
        nome_norm = re.sub(r"\s+", " ", lei.get("nome","")).strip().upper()
        if nome_norm in nomes_vistos: continue
        nomes_vistos.add(nome_norm)
        # Se veio da web sem site, tenta derivar do email
        if not lei.get("site") and lei.get("email"):
            lei["site"] = derivar_site_do_email(lei["email"]) or ""
        resultado.append(lei)

    com_site = [l for l in resultado if l.get("site")]
    log(f"  Total merged: {len(resultado)} | com site: {len(com_site)}")
    return resultado


# ── 4. Extração de imóveis ─────────────────────────────────────────────────────
LISTING_PATHS = [
    "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/",
    "/leilao", "/leiloes/", "/proximos-leiloes", "/proximos_leiloes",
    "/leiloes/imoveis", "/imoveis-leilao", "/em-leilao",
    "/leiloes/imoveis-rurais", "/leiloes/imoveis-urbanos",
    "/catalogo", "/catalogo/", "/ofertas",
]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","leilao"]


def is_imovel(titulo: str, url: str = "") -> bool:
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
    return True


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
    # onclick ExibeDoc (padrão tribunais)
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
    v_min  = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None

    # Área e quartos
    area_m = RE_AREA.search(texto)
    area   = area_m.group(1).replace(",",".") if area_m else None
    q_m    = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None

    # Datas
    datas  = RE_DATE.findall(texto)
    data1  = parse_date(datas[0]) if datas else None
    data2  = parse_date(datas[1]) if len(datas) > 1 else None

    # Localização
    uf_m  = RE_UF.search(texto)
    uf    = uf_m.group() if uf_m else lei.get("uf_leiloeiro","MS")
    # Tenta padrão "Cidade/UF"
    cid_m = re.search(rf"([A-ZÀ-Úa-zà-ú]+(?:\s[A-ZÀ-Úa-zà-ú]+){{0,3}})\s*/\s*{uf}", texto)
    cidade = cid_m.group(1).strip() if cid_m else ""

    cep_m = RE_CEP.search(texto)
    cep   = cep_m.group() if cep_m else ""
    proc_m = RE_PROC.search(texto)
    processo = proc_m.group() if proc_m else ""

    # Imagem
    imgs = soup.find_all("img", src=True)
    img_principal = ""
    for img in imgs:
        src = img.get("src","") or img.get("data-src","")
        src_abs = urljoin(base_url, src)
        if src and not any(k in src.lower() for k in ["logo","icon","banner","avatar","sprite"]):
            img_principal = src_abs
            break

    # Endereço e descrição
    end_el = soup.select_one("[class*='endere'],[class*='local'],[class*='address'],[itemprop='address']")
    endereco = end_el.get_text(strip=True)[:300] if end_el else ""
    desc_el  = soup.select_one("[class*='descri'],[class*='desc'],[class*='detail'],[class*='detalhe']")
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else texto[:300]

    arquivos = extract_arquivos(soup, card_url)

    return {
        "id_externo": make_id(card_url),
        "leiloeiro": lei["nome"],
        "leiloeiro_site": lei.get("site",""),
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
    base = lei["site"].rstrip("/")
    session = requests.Session()
    session.headers.update(HEADERS)
    imoveis = []
    lote_urls = set()

    # Descobre URL de listagem
    listagem_url = None
    for path in [""] + LISTING_PATHS:
        try:
            url = base + path
            r = session.get(url, timeout=20, allow_redirects=True, verify=False)
            if r.status_code == 200 and any(k in r.text.lower() for k in LISTING_KW):
                listagem_url = url
                break
        except Exception:
            continue

    if not listagem_url:
        return []

    for pag in range(1, max_paginas + 1):
        url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
        try:
            r = session.get(url_pg, timeout=20, verify=False)
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, "html.parser")

            novos = 0
            for a in soup.find_all("a", href=True):
                href_abs = urljoin(base, a["href"])
                txt = (a.get_text() + a["href"]).lower()
                if any(k in txt or k in href_abs for k in ["lote","imovel","oferta","arrematacao"]):
                    if href_abs not in lote_urls and urlparse(href_abs).netloc:
                        lote_urls.add(href_abs)
                        novos += 1

            if novos == 0 and pag > 1:
                break
            time.sleep(1)
        except Exception:
            break

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

        for pag in range(1, max_paginas + 1):
            url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
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
    markers = ["__next_data__","__nuxt__","react-root","vue-app","ng-app",
               "window.__INITIAL_STATE__","data-reactroot"]
    html_lower = html.lower()
    if any(m in html_lower for m in markers): return True
    # Verifica se tem muito pouco conteúdo textual
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text()
    if len(texto.strip()) < 300: return True
    return False


def scrape_leiloeiro(lei: dict, max_paginas: int = 8) -> tuple[list[dict], str]:
    """Scrape um leiloeiro. Retorna (imoveis, status)."""
    if not lei.get("site"):
        return [], "sem_site"

    site = lei["site"]
    log(f"  → Scraping: {lei['nome']} | {site}")

    # Tenta HTTP primeiro
    try:
        r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
        html = r.text
        if r.status_code in (404, 410):
            return [], "offline"
    except Exception as e:
        log(f"    [WARN] HTTP falhou ({e}). Tentando Playwright...")
        imoveis = scrape_site_playwright(lei, max_paginas)
        status = "ok" if imoveis else "sem_leilao"
        return imoveis, status

    # Verifica se precisa de Playwright
    if is_js_heavy(html):
        log(f"    JS-heavy detectado. Usando Playwright...")
        imoveis = scrape_site_playwright(lei, max_paginas)
    else:
        imoveis = scrape_site_httpx(lei, max_paginas)
        if not imoveis:
            log(f"    HTTP sem resultados. Tentando Playwright...")
            imoveis = scrape_site_playwright(lei, max_paginas)

    status = "ok" if imoveis else "sem_leilao"
    return imoveis, status


# ── 5. Progresso e relatórios ───────────────────────────────────────────────────
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
    try:
        PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def relatorio_5min():
    """Imprime tabela de imóveis por leiloeiro."""
    with _lock:
        por_lei = dict(_estado["por_leiloeiro"])
        total   = len(_estado["imoveis"])
        atual   = _estado["leiloeiro_atual"]

    log(f"\n{'='*60}")
    log(f"RELATÓRIO PARCIAL | Total: {total} imóveis | Agora: {atual}")
    for nome, cnt in sorted(por_lei.items(), key=lambda x: -x[1]):
        log(f"  {nome[:40]:<40} {cnt:>4} imóveis")
    log(f"{'='*60}\n")


def thread_relatorio(stop_evt: threading.Event):
    while not stop_evt.wait(300):  # 5 minutos
        relatorio_5min()


# ── 6. Salvar CSVs ─────────────────────────────────────────────────────────────
def salvar_csv_leiloeiros(leiloeiros: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"leiloeiros_jucems_{TODAY}.csv"
    campos = ["nome","matricula","site","email","telefone","cidade_leiloeiro","uf_leiloeiro","situacao","junta"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(leiloeiros)
    log(f"[CSV] Leiloeiros: {path} ({len(leiloeiros)} registros)")
    return path


def salvar_csv_imoveis(imoveis: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"imoveis_jucems_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_IMOVEIS, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    log(f"[CSV] Imóveis: {path} ({len(imoveis)} registros)")
    return path


# ── 7. SQLite ───────────────────────────────────────────────────────────────────
def importar_sqlite(imoveis: list[dict]):
    log(f"\n[SQLite] Importando {len(imoveis)} imóveis...")
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
                    r.get("id_externo",""), r.get("leiloeiro",""), "JUCEMS",
                    r.get("leiloeiro_site",""),
                    r.get("titulo","")[:500], r.get("descricao","")[:300],
                    r.get("endereco_completo","")[:200],
                    r.get("cidade",""), r.get("estado","MS"),
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


# ── 8. PostgreSQL ───────────────────────────────────────────────────────────────
def psql(sql: str, timeout: int = 30) -> str:
    import subprocess
    proc = subprocess.run(
        ["docker", "exec", "leilao_postgres",
         "psql", "-U", "leilao", "-d", "leilao_db",
         "--no-align", "--tuples-only", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    return proc.stdout


def importar_postgres(imoveis: list[dict]):
    import subprocess
    log(f"\n[PostgreSQL] Importando {len(imoveis)} imóveis...")

    # Garante fonte
    psql("""
        INSERT INTO fontes (nome, url_base, ativo, criado_em)
        VALUES ('JUCEMS','https://www.jucems.ms.gov.br/',true,NOW())
        ON CONFLICT (nome) DO NOTHING;
    """)
    fonte_id_raw = psql("SELECT id FROM fontes WHERE nome='JUCEMS' LIMIT 1;").strip()
    if not fonte_id_raw.isdigit():
        log(f"[ERRO] Não foi possível obter fonte_id JUCEMS. Saída: {repr(fonte_id_raw)}")
        return 0, 0

    FONTE_ID = int(fonte_id_raw)
    log(f"  fonte_id JUCEMS = {FONTE_ID}")

    TIPOS_IMOVEL_VALIDOS = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
    TIPOS_LEILAO_VALIDOS = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

    ins_pg = upd_pg = err_pg = 0

    for i in range(0, len(imoveis), 50):
        batch = imoveis[i:i+50]
        values = []

        for r in batch:
            def esc(v, max_len=None):
                s = str(v or "").replace("'", "''")
                if max_len: s = s[:max_len]
                return s

            tipo_i = r.get("tipo_imovel","outro").upper()
            if tipo_i not in TIPOS_IMOVEL_VALIDOS: tipo_i = "OUTRO"
            tipo_l = r.get("tipo_leilao","extrajudicial").upper()
            if tipo_l not in TIPOS_LEILAO_VALIDOS: tipo_l = "EXTRAJUDICIAL"

            def _d(v):
                try: return float(Decimal(str(v).replace(",","."))) if v else None
                except: return None

            vmin   = _d(r.get("valor_minimo"))
            vaval  = _d(r.get("valor_avaliacao"))
            area   = _d(r.get("area_total"))
            quartos = r.get("quartos","")
            try: quartos = int(quartos) if quartos else None
            except: quartos = None

            d1 = r.get("data_primeiro_leilao","")
            d2 = r.get("data_segundo_leilao","")

            values.append(f"""(
                {FONTE_ID},
                '{esc(r.get("id_externo",""), 200)}',
                '{esc(r.get("titulo",""), 500)}',
                '{esc(r.get("descricao",""), 500)}',
                '{esc(r.get("url_original",""), 1000)}',
                '{tipo_i}',
                '{tipo_l}',
                'ABERTO',
                'IMOVEL',
                '{esc(r.get("cidade",""), 200)}',
                '{esc(r.get("estado","MS"), 2)}',
                '{esc(r.get("cep",""), 10)}',
                '{esc(r.get("endereco_completo",""), 500)}',
                {vmin if vmin is not None else 'NULL'},
                {vaval if vaval is not None else 'NULL'},
                {area if area is not None else 'NULL'},
                {quartos if quartos is not None else 'NULL'},
                {f"'{d1}'" if d1 else 'NULL'},
                {f"'{d2}'" if d2 else 'NULL'},
                '{esc(r.get("imagem_principal",""), 1000)}',
                '{esc(r.get("arquivos","[]"), 4000)}',
                '{esc(r.get("numero_processo",""), 100)}',
                '{esc(r.get("leiloeiro",""), 300)}',
                true, false, false,
                NOW(), NOW()
            )""")

        if not values:
            continue

        sql = f"""
        INSERT INTO imoveis (
            fonte_id, id_externo, titulo, descricao, url_original,
            tipo_imovel, tipo_leilao, status, categoria,
            cidade, estado, cep, endereco_completo,
            valor_minimo, valor_avaliacao, area_total, quartos,
            data_primeiro_leilao, data_segundo_leilao,
            imagem_principal, arquivos, numero_processo,
            leiloeiro, ativo, classificado, geocodificado,
            criado_em, atualizado_em
        ) VALUES {', '.join(values)}
        ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
            titulo = EXCLUDED.titulo,
            valor_minimo = EXCLUDED.valor_minimo,
            data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
            imagem_principal = EXCLUDED.imagem_principal,
            arquivos = EXCLUDED.arquivos,
            atualizado_em = NOW();
        """

        try:
            proc = subprocess.run(
                ["docker", "exec", "leilao_postgres",
                 "psql", "-U", "leilao", "-d", "leilao_db", "-c", sql],
                capture_output=True, text=True, encoding="utf-8", timeout=60
            )
            out = proc.stdout + proc.stderr
            if "INSERT" in out:
                m = re.search(r"INSERT \d+ (\d+)", out)
                n = int(m.group(1)) if m else len(batch)
                ins_pg += n
            elif "UPDATE" in out or "conflict" in out.lower():
                upd_pg += len(batch)
            elif proc.returncode != 0:
                err_pg += len(batch)
                log(f"  [ERR lote {i}] {out[:200]}")
        except Exception as e:
            err_pg += len(batch)
            log(f"  [ERR lote {i}] {e}")

        if (i // 50) % 2 == 0:
            log(f"  Lote {i//50+1}: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")

    log(f"  PostgreSQL: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")
    return ins_pg, upd_pg


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-banco", action="store_true", help="Não importa para bancos")
    ap.add_argument("--max-sites", type=int, default=999, help="Limite de sites a visitar")
    ap.add_argument("--max-paginas", type=int, default=8, help="Páginas por site")
    ap.add_argument("--reset", action="store_true", help="Ignora progresso anterior")
    args = ap.parse_args()

    log("="*60)
    log(f"JUCEMS Scraper iniciado — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("="*60)

    # 1. Leiloeiros do TXT
    txt_list = parse_txt_jucems(TXT_FILE) if TXT_FILE.exists() else []

    # 2. Leiloeiros do site JUCEMS
    web_list = fetch_jucems_regulares()

    # 3. Merge
    todos = merge_leiloeiros(txt_list, web_list)

    # Remove duplicatas de site (ipcleiloes aparece 2x etc.)
    sites_vistos = set()
    leiloeiros_unicos = []
    sem_site = []
    for l in todos:
        site = l.get("site","").rstrip("/")
        if not site:
            sem_site.append(l)
            continue
        if site not in sites_vistos:
            sites_vistos.add(site)
            leiloeiros_unicos.append(l)

    log(f"\nTotal leiloeiros Regular: {len(todos)}")
    log(f"  Com site (únicos): {len(leiloeiros_unicos)}")
    log(f"  Sem site: {len(sem_site)}")

    # Salva CSV de leiloeiros
    salvar_csv_leiloeiros(todos)

    # 4. Scraping
    stop_evt = threading.Event()
    t_report = threading.Thread(target=thread_relatorio, args=(stop_evt,), daemon=True)
    t_report.start()

    todos_imoveis = []
    limitar = min(args.max_sites, len(leiloeiros_unicos))

    for idx, lei in enumerate(leiloeiros_unicos[:limitar], 1):
        with _lock:
            _estado["leiloeiro_atual"] = lei["nome"]

        log(f"\n[{idx}/{limitar}] {lei['nome']} | {lei.get('site','')}")

        try:
            imoveis, status = scrape_leiloeiro(lei, args.max_paginas)
        except Exception as e:
            imoveis, status = [], "erro"
            with _lock:
                _estado["erros"].append((lei.get("site",""), str(e)))
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

    # 5. Salvar CSVs
    log(f"\n{'='*60}")
    log(f"SCRAPING CONCLUÍDO: {len(todos_imoveis)} imóveis de {len(leiloeiros_unicos)} sites")
    relatorio_5min()

    if todos_imoveis:
        salvar_csv_imoveis(todos_imoveis)
    else:
        log("[WARN] Nenhum imóvel coletado.")

    # 6. Importar para bancos
    if not args.sem_banco and todos_imoveis:
        importar_sqlite(todos_imoveis)
        importar_postgres(todos_imoveis)

    # Sumário final de erros
    log("\n[ERROS REGISTRADOS]")
    with _lock:
        erros = list(_estado["erros"])
    if erros:
        for site, msg in erros:
            log(f"  {site}: {msg}")
    else:
        log("  Nenhum erro registrado.")

    log("\n[CONCLUSÃO]")
    log(f"  Leiloeiros processados: {len(leiloeiros_unicos)}")
    log(f"  Sites com imóveis: {_estado['sites_ok']}")
    log(f"  Sites sem leilão ativo: {_estado['sites_sem_leilao']}")
    log(f"  Sites com erro: {_estado['sites_err']}")
    log(f"  Total de imóveis: {len(todos_imoveis)}")
    log(f"  CSV: csv/imoveis_jucems_{TODAY}.csv")
    log(f"  CSV: csv/leiloeiros_jucems_{TODAY}.csv")


if __name__ == "__main__":
    main()
