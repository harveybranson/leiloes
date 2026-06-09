"""
scraper_tjam_jucea.py
=====================
Captura de imóveis de leilão do Amazonas, de DUAS fontes:

  FONTE A — Loja TJAM no Superbid (store id 16418), via API interna limpa
            offer-query.superbid.net/offers/  (Tribunal de Justiça do Amazonas).
  FONTE B — Sites próprios dos leiloeiros credenciados pela JUCEA (PDF "Leiloeiros
            Amazonas"), filtrando SOMENTE situação REGULAR (exclui IRREGULAR /
            cancelados / suspensos).

Regras (captura_dados_leiloes_v2.md):
  - Só leiloeiros REGULAR.
  - Captura: fotos, título, info do imóvel, anexos (edital/matrícula/laudo), datas.
  - Valida que a data do 1º leilão (1ª praça) é posterior à data da captura — ou que
    o leilão ainda está aberto (data de encerramento >= hoje). Descarta o que já passou.
  - Gera CSV de leiloeiros (nome + site) e CSV de imóveis em /csv.
  - Importa imóveis válidos para SQLite (imoveis_leiloeiros.db, junta=JUCEA) e
    PostgreSQL Docker (leilao_db), com dedup por URL canônica.
  - Relatório por leiloeiro a cada 5 min + relatório final de dificuldades.

Uso:
  python scraper_tjam_jucea.py [--sem-banco] [--so-tjam] [--max-sites N] [--max-paginas N]
"""
import sys, io, os
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re, csv, json, time, html, hashlib, sqlite3, argparse, threading, subprocess
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal
from urllib.parse import urlparse, urljoin

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ── Configuração ────────────────────────────────────────────────────────────────
BASE          = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR       = BASE / "csv"
DB_FILE       = BASE / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE / "scraper_tjam_jucea_progress.json"
LOG_FILE      = BASE / "scraper_tjam_jucea.log"
TODAY         = datetime.now().strftime("%Y-%m-%d")
HOJE          = date.today()

# API Superbid (loja TJAM)
TJAM_STORE_ID = 16418
OFFER_API     = "https://offer-query.superbid.net/offers/"
TJAM_BASE_PARAMS = {
    "filter": f"stores.id:{TJAM_STORE_ID}",
    "locale": "pt_BR",
    "orderBy": "endDate:asc",
    "portalId": "[2,15]",
    "preOrderBy": "orderByFirstOpenedOffers",
    "requestOrigin": "store",
    "searchType": "opened",
    "timeZoneId": "America/Sao_Paulo",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
    "Origin": "https://tjam.superbid.net",
    "Referer": "https://tjam.superbid.net/",
}

FIELDNAMES_IMOVEIS = [
    "id_externo","leiloeiro","leiloeiro_site","titulo","tipo_imovel","tipo_leilao",
    "estado","cidade","cep","endereco_completo",
    "valor_minimo","valor_avaliacao","area_total","quartos",
    "data_primeiro_leilao","data_segundo_leilao","data_encerramento",
    "url_original","imagem_principal","numero_processo","vara","comarca",
    "arquivos","descricao","fonte_slug",
]

# ── Leiloeiros REGULAR do PDF "Leiloeiros Amazonas" (JUCEA) ───────────────────────
# Apenas SITUAÇÃO: REGULAR. Site derivado do campo SITE: ou do domínio do e-mail
# institucional (e-mails pessoais — gmail/hotmail/yahoo — não geram site).
LEILOEIROS_JUCEA = [
    {"nome": "HUGO MOREIRA PIMENTA",                "matricula": "009/2007", "site": "https://www.leilaomanaus.lel.br",      "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "JIMMY ASAMI",                          "matricula": "010/2009", "site": "https://www.asamileiloes.com.br",       "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "WESLEY SILVA RAMOS",                   "matricula": "011/2009", "site": "https://www.wrleiloes.com.br",          "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "LUIZ DE CHIRICO JÚNIOR",               "matricula": "012/2009", "site": "https://www.leiloesdonorte.com.br",     "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "FELIPE GUIMARÃES CARRIJO",             "matricula": "017/2012", "site": "https://www.leilo.com.br",              "cidade_leiloeiro": "Goiânia", "uf_leiloeiro": "GO"},
    {"nome": "BRIAN GALVÃO FROTA",                   "matricula": "018/2015", "site": "https://www.amazonasleiloes.com.br",    "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "SANDRO DE OLIVEIRA",                   "matricula": "020/2020", "site": "https://www.norteleiloes.com.br",       "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "MARIANA GOUVÊA LESSA",                 "matricula": "021/2020", "site": "",                                      "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "DEONIZIA KIRATCH",                     "matricula": "022/2020", "site": "https://www.deonizialeiloes.com.br",    "cidade_leiloeiro": "Porto Velho", "uf_leiloeiro": "RO"},
    {"nome": "ALEX WILLIAN HOPPE",                   "matricula": "023/2020", "site": "https://www.hoppeleiloes.com.br",       "cidade_leiloeiro": "Canoinhas", "uf_leiloeiro": "SC"},
    {"nome": "DANIEL ELIAS GARCIA",                  "matricula": "028/2020", "site": "https://www.dgleiloes.com.br",          "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "TIAGO TESSLER BLECHER",                "matricula": "030/2019", "site": "https://www.webleiloes.com.br",         "cidade_leiloeiro": "Brasília", "uf_leiloeiro": "DF"},
    {"nome": "DAVI BORGES DE AQUINO",                "matricula": "031/2023", "site": "https://www.alfaleiloes.com",           "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "THAIS SILVA MOREIRA DE SOUZA",         "matricula": "006/2017", "site": "https://www.tmleiloes.com.br",          "cidade_leiloeiro": "São Paulo", "uf_leiloeiro": "SP"},
    {"nome": "FERNANDO CAETANO MOREIRA FILHO",       "matricula": "036/2024", "site": "https://www.fernandoleiloeiro.com.br",  "cidade_leiloeiro": "Contagem", "uf_leiloeiro": "MG"},
    {"nome": "RENAN SOUZA SILVA",                    "matricula": "034/2024", "site": "https://www.souzasilvaleiloes.com.br",  "cidade_leiloeiro": "São Paulo", "uf_leiloeiro": "SP"},
    {"nome": "IRANI FLORES",                         "matricula": "047/2025", "site": "https://www.leilaobrasil.com.br",       "cidade_leiloeiro": "São Paulo", "uf_leiloeiro": "SP"},
    {"nome": "PAULO CESAR AGOSTINHO",                "matricula": "048/2025", "site": "https://www.agostinholeiloes.com.br",   "cidade_leiloeiro": "Belo Horizonte", "uf_leiloeiro": "MG"},
    {"nome": "LEONARDO VIEIRA AMARAL",               "matricula": "050/2025", "site": "https://www.leilaonet.com.br",          "cidade_leiloeiro": "São Paulo", "uf_leiloeiro": "SP"},
    # Regulares sem site resolvível (e-mail pessoal) — entram na lista de leiloeiros,
    # mas seus imóveis vêm pela loja TJAM (fonte A) quando existirem.
    {"nome": "DANIELLY FERNANDES DA SILVA NAZARETH", "matricula": "013/2011", "site": "",                                      "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "RICARDO MARCELO GOMES DE OLIVEIRA",    "matricula": "014/2011", "site": "",                                      "cidade_leiloeiro": "Manaus", "uf_leiloeiro": "AM"},
    {"nome": "LUCAS RAFAEL ANTUNES MOREIRA",         "matricula": "039/2024", "site": "",                                      "cidade_leiloeiro": "Belo Horizonte", "uf_leiloeiro": "MG"},
    {"nome": "JONAS GABRIEL ANTUNES MOREIRA",        "matricula": "037/2024", "site": "",                                      "cidade_leiloeiro": "Pará de Minas", "uf_leiloeiro": "MG"},
]
for _l in LEILOEIROS_JUCEA:
    _l.update({"situacao": "Regular", "junta": "JUCEA"})

# Leiloeiros IRREGULAR (registrados aqui só para documentação/relatório — NÃO scrape)
IRREGULARES = [
    "MARCOS AUGUSTO DA SILVA MENEZES","SÔNIA PRISCILA DA SILVA MENEZES","EVANIR ROCHA MUNIZ",
    "VICENTE DE PAULO A. COTA FILHO","CONCEIÇÃO DE MARIA COSTA LOPES","CESAR AUGUSTO BAGATINI",
    "ERICO SOBRAL SOARES","PATRÍCIA PIMENTEL GROCOSKI COSTA","JOSIANE LIMA",
    "JOSE ROBERTO NEVES AMORIM","FRANCISCO FREITAS MENEZES","ADOLPHO MAURO MALES NAZARETH",
]

# ── Estado global ────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_estado = {
    "imoveis": [], "por_leiloeiro": {}, "fonte_atual": "",
    "sites_ok": 0, "sites_err": 0, "sites_sem_leilao": 0,
    "descartados_data": 0, "erros": [], "inicio": datetime.now().isoformat(),
}

# ── Logging ──────────────────────────────────────────────────────────────────────
def log(msg: str):
    linha = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(linha, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

# ── Helpers ──────────────────────────────────────────────────────────────────────
def make_id(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:24]

def slug_do_site(site: str) -> str:
    if not site: return ""
    host = urlparse(site).netloc or site
    host = re.sub(r"^www\.", "", host.lower())
    return re.sub(r"[^a-z0-9]", "", host)[:60]

def clean_money(s) -> float | None:
    if s is None: return None
    if isinstance(s, (int, float)): return float(s)
    s = re.sub(r"[^\d,.]", "", str(s))
    s = s.replace(".", "").replace(",", ".")
    try: return float(s) if s else None
    except: return None

def parse_date(s: str) -> str | None:
    """Retorna 'YYYY-MM-DD' a partir de 'dd/mm/aaaa', 'aaaa-mm-dd' ou 'aaaa-mm-dd HH:MM:SS'."""
    if not s: return None
    s = str(s)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try: date(int(m.group(1)), int(m.group(2)), int(m.group(3))); return m.group(0)
        except ValueError: return None
    m = re.search(r"(\d{2})[/\-.](\d{2})[/\-.](\d{4})", s)
    if m:
        y, mo, d = int(m.group(3)), int(m.group(2)), int(m.group(1))
        try: date(y, mo, d); return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError: return None
    return None

def _to_date(s: str | None) -> date | None:
    d = parse_date(s)
    if not d: return None
    try: return date.fromisoformat(d)
    except Exception: return None

def imovel_valido_por_data(data_primeiro: str | None, data_encerramento: str | None) -> bool:
    """
    Válido se o 1º leilão é posterior a hoje OU se o leilão ainda está aberto
    (encerramento hoje ou depois). Descarta o que já se encerrou.
    Datas desconhecidas → mantém (será filtrado por 'devoltaparaofuturo' no banco).
    """
    d1, denc = _to_date(data_primeiro), _to_date(data_encerramento)
    if d1 is None and denc is None:
        return True
    if d1 and d1 >= HOJE:
        return True
    if denc and denc >= HOJE:
        return True
    return False

def strip_html(s: str) -> str:
    if not s: return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","sitio","hectare","rural","chácara","chacara","gleba"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet","duplex"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência","residencia","vila"]): return "casa"
    if any(k in txt for k in ["terreno","lote urbano"]): return "terreno"
    if any(k in txt for k in ["galpão","galpao","armazém","armazem","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","ponto comercial","comercial"]): return "comercial"
    if any(k in txt for k in ["vaga","garagem","box"]): return "vaga"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "", judicial=False) -> str:
    if judicial: return "judicial"
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","execucao","hasta","praça","praca","vara","tjam"]): return "judicial"
    if any(k in txt for k in ["banco","caixa","financiamento","retomada"]): return "bancario"
    return "extrajudicial"

# ════════════════════════════════════════════════════════════════════════════════
# FONTE A — Loja TJAM no Superbid (API offer-query)
# ════════════════════════════════════════════════════════════════════════════════
def _nome_regular(nome: str) -> bool:
    """True se o leiloeiro (auctioneer da API) bate com um REGULAR da JUCEA."""
    if not nome: return False
    n = re.sub(r"\s+", " ", nome).strip().upper()
    for l in LEILOEIROS_JUCEA:
        if l["nome"].upper() == n:
            return True
    # match aproximado por sobrenome+nome
    for l in LEILOEIROS_JUCEA:
        a = set(l["nome"].upper().split()); b = set(n.split())
        if len(a & b) >= 2:
            return True
    return False

def mapear_offer(o: dict) -> dict | None:
    prod = o.get("product") or {}
    aon  = o.get("auction") or {}
    jud  = prod.get("judicial") or {}
    loc  = prod.get("location") or {}

    titulo = (prod.get("shortDesc") or o.get("offerDescription", {}).get("offerDescription") or "").strip()[:300]
    titulo = strip_html(titulo)
    desc   = strip_html((o.get("offerDescription") or {}).get("offerDescription") or prod.get("detailedDescription") or "")[:1500]

    # productType: 13 = Imóveis. Mantém só imóveis.
    pt = prod.get("productType") or {}
    if pt.get("id") not in (13, None) and "imó" not in (pt.get("description","").lower()):
        # não é imóvel
        return None

    auctioneer = aon.get("auctioneer") or o.get("manager", {}).get("name") or ""

    # Datas (eventPipeline = praças)
    stages = (o.get("eventPipeline") or {}).get("stages") or []
    d1 = parse_date(stages[0]["beginDate"]) if len(stages) >= 1 else parse_date(aon.get("beginDate"))
    d2 = parse_date(stages[1]["beginDate"]) if len(stages) >= 2 else None
    denc = parse_date(o.get("endDate")) or parse_date(aon.get("endDate"))

    # Localização (cidade vem "Manaus - AM")
    cidade_raw = (loc.get("city") or "").strip()
    cidade = re.sub(r"\s*-\s*[A-Z]{2}$", "", cidade_raw).strip()
    uf_m = re.search(r"\b([A-Z]{2})\b", cidade_raw)
    uf = uf_m.group(1) if uf_m else (aon.get("address", {}).get("stateCode") or "AM")

    # Valores
    od = o.get("offerDetail") or {}
    v_min = clean_money(od.get("initialBidValue") or o.get("price"))
    v_aval = None
    if len(stages) >= 1 and stages[0].get("initialBidValue"):
        v_aval = clean_money(stages[0]["initialBidValue"])  # 1ª praça = avaliação cheia

    # Imagem + galeria
    img = prod.get("thumbnailUrl") or ""
    imagens = [g.get("link") for g in (prod.get("galleryJson") or []) if g.get("link")]

    # Anexos
    arquivos = []
    for at in (prod.get("attachments") or []):
        link = at.get("link");
        if not link: continue
        nome = at.get("originalFileName") or "documento"
        low = nome.lower()
        tipo = ("edital" if "edital" in low else
                "matricula" if ("matric" in low or "cri" in low or "registro" in low) else
                "avaliacao" if ("avalia" in low or "laudo" in low) else
                "penhora" if "penhora" in low else
                "escritura" if "escritura" in low else "documento")
        arquivos.append({"tipo": tipo, "url": link, "nome": nome})

    offer_id = o.get("id")
    url = f"https://tjam.superbid.net/oferta/{offer_id}"

    return {
        "id_externo": f"tjam-{offer_id}",
        "leiloeiro": auctioneer,
        "leiloeiro_site": "https://tjam.superbid.net",
        "titulo": titulo,
        "tipo_imovel": infer_tipo(titulo, desc),
        "tipo_leilao": infer_tipo_leilao(titulo, desc, judicial=bool(jud)),
        "estado": uf,
        "cidade": cidade,
        "cep": "",
        "endereco_completo": strip_html(desc)[:400],
        "valor_minimo": v_min,
        "valor_avaliacao": v_aval,
        "area_total": None,
        "quartos": None,
        "data_primeiro_leilao": d1,
        "data_segundo_leilao": d2,
        "data_encerramento": denc,
        "url_original": url,
        "imagem_principal": img,
        "imagens": json.dumps(imagens, ensure_ascii=False),
        "numero_processo": jud.get("processNumber", ""),
        "vara": jud.get("vara", ""),
        "comarca": jud.get("district", ""),
        "arquivos": json.dumps(arquivos, ensure_ascii=False),
        "descricao": desc,
        "fonte_slug": "tjam_superbid",
    }

def scrape_tjam_superbid() -> list[dict]:
    log("="*60)
    log("FONTE A — Loja TJAM no Superbid (store 16418)")
    with _lock: _estado["fonte_atual"] = "TJAM Superbid"
    session = requests.Session(); session.headers.update(HEADERS)
    imoveis, page, page_size = [], 1, 30
    descartados = 0
    while True:
        params = dict(TJAM_BASE_PARAMS); params.update({"pageNumber": page, "pageSize": page_size})
        try:
            r = session.get(OFFER_API, params=params, timeout=30, verify=False)
            r.encoding = "utf-8"
            data = r.json()
        except Exception as e:
            log(f"  [ERRO] página {page}: {e}")
            break
        offers = data.get("offers") or []
        total = data.get("total", 0)
        if page == 1:
            log(f"  Total de ofertas abertas na loja TJAM: {total}")
        if not offers:
            break
        for o in offers:
            auctioneer = (o.get("auction") or {}).get("auctioneer") or o.get("manager", {}).get("name") or ""
            if not _nome_regular(auctioneer):
                log(f"    · pulando oferta {o.get('id')} — leiloeiro '{auctioneer}' não-regular/desconhecido")
                continue
            im = mapear_offer(o)
            if not im:
                continue
            if not imovel_valido_por_data(im["data_primeiro_leilao"], im["data_encerramento"]):
                descartados += 1
                log(f"    · descartado por data (1ª praça {im['data_primeiro_leilao']} / enc {im['data_encerramento']}): {im['titulo'][:50]}")
                continue
            imoveis.append(im)
            with _lock:
                _estado["por_leiloeiro"][im["leiloeiro"]] = _estado["por_leiloeiro"].get(im["leiloeiro"], 0) + 1
            log(f"    ✓ {im['leiloeiro'][:28]:<28} | {im['titulo'][:55]} | 1ª praça {im['data_primeiro_leilao']}")
        if len(offers) < page_size or page * page_size >= total:
            break
        page += 1
        time.sleep(0.8)
    with _lock:
        _estado["descartados_data"] += descartados
        _estado["sites_ok"] += 1 if imoveis else 0
    log(f"  FONTE A: {len(imoveis)} imóveis válidos, {descartados} descartados por data")
    return imoveis

# ════════════════════════════════════════════════════════════════════════════════
# FONTE B — Sites próprios dos leiloeiros REGULAR (scraper genérico)
# ════════════════════════════════════════════════════════════════════════════════
RE_PRICE = re.compile(r"R[\$\s]+(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)")
RE_AREA  = re.compile(r"(\d+[\.,]?\d*)\s*m[²2]", re.IGNORECASE)
RE_PROC  = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
RE_CEP   = re.compile(r"\d{5}-?\d{3}")
RE_UF    = re.compile(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b")
RE_DATE  = re.compile(r"\d{2}[\/\.\-]\d{2}[\/\.\-]\d{4}")
DOC_KW   = re.compile(r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|penhora|escritura|processo", re.IGNORECASE)
PDF_EXT  = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

LISTING_PATHS = ["", "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/", "/leilao",
                 "/leiloes/", "/proximos-leiloes", "/leiloes/imoveis", "/catalogo", "/ofertas", "/bens"]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","leilao","praça","praca"]

def is_imovel(titulo: str, url: str = "") -> bool:
    txt = (titulo + " " + url).lower()
    nao = ["veículo","veiculo","automóvel","automovel","moto","motocicl","caminhão","caminhao",
           "trator","máquina","maquina","equipamento","eletro","celular","notebook","sucata"]
    if any(k in txt for k in nao): return False
    return True

def extract_arquivos(soup, page_url):
    arquivos, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#","javascript","mailto","tel")): continue
        url_abs = urljoin(page_url, href)
        if url_abs in seen: continue
        text = (a.get_text() + " " + href).lower()
        if PDF_EXT.search(href) or DOC_KW.search(text):
            tipo = ("edital" if "edital" in text else
                    "matricula" if ("matric" in text or "matríc" in text) else
                    "laudo" if ("laudo" in text or "avalia" in text) else
                    "certidao" if "certid" in text else "pdf")
            nome = a.get_text(strip=True)[:80] or tipo.capitalize()
            arquivos.append({"tipo": tipo, "url": url_abs, "nome": nome}); seen.add(url_abs)
        if len(arquivos) >= 15: break
    return arquivos

def extrair_imovel_do_card(card_html, card_url, lei, base_url):
    soup = BeautifulSoup(card_html, "html.parser")
    texto = soup.get_text(" ", strip=True)
    titulo = ""
    for sel in ["h1","h2","h3",".titulo",".title",".lote-titulo","[class*='titulo']","[class*='title']"]:
        el = soup.select_one(sel)
        if el:
            titulo = el.get_text(strip=True)[:200]; break
    if not titulo: titulo = texto[:120]
    if not is_imovel(titulo, card_url): return None

    precos = RE_PRICE.findall(texto)
    v_min = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None
    area_m = RE_AREA.search(texto)
    area = area_m.group(1).replace(",",".") if area_m else None
    q_m = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None
    datas = RE_DATE.findall(texto)
    data1 = parse_date(datas[0]) if datas else None
    data2 = parse_date(datas[1]) if len(datas) > 1 else None
    uf_m = RE_UF.search(texto); uf = uf_m.group() if uf_m else lei.get("uf_leiloeiro","AM")
    cid_m = re.search(rf"([A-ZÀ-Úa-zà-ú]+(?:\s[A-ZÀ-Úa-zà-ú]+){{0,3}})\s*/\s*{uf}", texto)
    cidade = cid_m.group(1).strip() if cid_m else ""
    cep_m = RE_CEP.search(texto); cep = cep_m.group() if cep_m else ""
    proc_m = RE_PROC.search(texto); processo = proc_m.group() if proc_m else ""

    img_principal = ""
    for img in soup.find_all("img", src=True):
        src = img.get("src","") or img.get("data-src","")
        if src and not any(k in src.lower() for k in ["logo","icon","banner","avatar","sprite"]):
            img_principal = urljoin(base_url, src); break

    end_el = soup.select_one("[class*='endere'],[class*='local'],[class*='address'],[itemprop='address']")
    endereco = end_el.get_text(strip=True)[:300] if end_el else ""
    desc_el = soup.select_one("[class*='descri'],[class*='desc'],[class*='detail'],[class*='detalhe']")
    desc = desc_el.get_text(" ", strip=True)[:600] if desc_el else texto[:400]
    arquivos = extract_arquivos(soup, card_url)

    return {
        "id_externo": make_id(card_url), "leiloeiro": lei["nome"], "leiloeiro_site": lei.get("site",""),
        "titulo": titulo, "tipo_imovel": infer_tipo(titulo, desc), "tipo_leilao": infer_tipo_leilao(titulo, desc),
        "estado": uf, "cidade": cidade, "cep": cep, "endereco_completo": endereco,
        "valor_minimo": v_min, "valor_avaliacao": v_aval, "area_total": area, "quartos": quartos,
        "data_primeiro_leilao": data1, "data_segundo_leilao": data2, "data_encerramento": data1,
        "url_original": card_url, "imagem_principal": img_principal, "imagens": "[]",
        "numero_processo": processo, "vara": "", "comarca": "",
        "arquivos": json.dumps(arquivos, ensure_ascii=False), "descricao": desc,
        "fonte_slug": slug_do_site(lei.get("site","")) or "jucea",
    }

def is_js_heavy(html_txt):
    markers = ["__next_data__","__nuxt__","react-root","vue-app","ng-app","window.__initial_state__","data-reactroot"]
    h = html_txt.lower()
    if any(m in h for m in markers): return True
    if len(BeautifulSoup(html_txt, "html.parser").get_text().strip()) < 300: return True
    return False

def coletar_lote_urls(soup, base):
    urls = set()
    for a in soup.find_all("a", href=True):
        href_abs = urljoin(base, a["href"])
        txt = (a.get_text() + a["href"]).lower()
        if any(k in txt or k in href_abs.lower() for k in ["lote","imovel","imóvel","oferta","arremat","leilao/","praca"]):
            if urlparse(href_abs).netloc:
                urls.add(href_abs.split("#")[0])
    return urls

def scrape_site_httpx(lei, max_paginas=6):
    base = lei["site"].rstrip("/")
    session = requests.Session(); session.headers.update(HEADERS)
    lote_urls, listagem_url = set(), None
    for path in LISTING_PATHS:
        try:
            url = base + path
            r = session.get(url, timeout=20, allow_redirects=True, verify=False)
            if r.status_code == 200 and any(k in r.text.lower() for k in LISTING_KW):
                listagem_url = url
                lote_urls |= coletar_lote_urls(BeautifulSoup(r.text, "html.parser"), base)
                break
        except Exception:
            continue
    if not listagem_url:
        return [], "offline"
    for pag in range(2, max_paginas + 1):
        try:
            r = session.get(f"{listagem_url}?pagina={pag}", timeout=20, verify=False)
            if r.status_code != 200: break
            novos = coletar_lote_urls(BeautifulSoup(r.text, "html.parser"), base)
            if not (novos - lote_urls): break
            lote_urls |= novos
            time.sleep(0.6)
        except Exception:
            break
    imoveis = []
    for url in list(lote_urls)[:120]:
        try:
            r = session.get(url, timeout=20, verify=False)
            if r.status_code != 200: continue
            im = extrair_imovel_do_card(r.text, url, lei, base)
            if im: imoveis.append(im)
            time.sleep(0.5)
        except Exception:
            continue
    return imoveis, ("ok" if imoveis else "sem_leilao")

def scrape_site_playwright(lei, max_paginas=6):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("    [WARN] Playwright indisponível."); return [], "erro"
    base = lei["site"].rstrip("/"); lote_urls, imoveis = set(), []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], ignore_https_errors=True)
            page = ctx.new_page()
            listagem_url = None
            for path in LISTING_PATHS:
                try:
                    page.goto(base + path, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    if any(k in page.content().lower() for k in LISTING_KW):
                        listagem_url = base + path
                        lote_urls |= coletar_lote_urls(BeautifulSoup(page.content(), "html.parser"), base)
                        break
                except Exception:
                    continue
            if not listagem_url:
                browser.close(); return [], "offline"
            for pag in range(2, max_paginas + 1):
                try:
                    page.goto(f"{listagem_url}?pagina={pag}", timeout=30000, wait_until="networkidle")
                    page.wait_for_timeout(1500)
                    novos = coletar_lote_urls(BeautifulSoup(page.content(), "html.parser"), base)
                    if not (novos - lote_urls): break
                    lote_urls |= novos
                except Exception:
                    break
            for url in list(lote_urls)[:120]:
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1200)
                    im = extrair_imovel_do_card(page.content(), url, lei, base)
                    if im: imoveis.append(im)
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        log(f"    [WARN] Playwright erro: {str(e)[:120]}")
        return imoveis, ("ok" if imoveis else "erro")
    return imoveis, ("ok" if imoveis else "sem_leilao")

def scrape_leiloeiro_site(lei, max_paginas=6):
    site = lei.get("site","")
    if not site: return [], "sem_site"
    log(f"  → {lei['nome']} | {site}")
    try:
        r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
        if r.status_code in (404, 410): return [], "offline"
        html_txt = r.text
    except Exception as e:
        log(f"    [WARN] HTTP falhou ({str(e)[:80]}). Tentando Playwright...")
        return scrape_site_playwright(lei, max_paginas)
    if is_js_heavy(html_txt):
        log("    JS-heavy → Playwright")
        return scrape_site_playwright(lei, max_paginas)
    imoveis, status = scrape_site_httpx(lei, max_paginas)
    if not imoveis:
        log("    HTTP sem resultado → Playwright")
        return scrape_site_playwright(lei, max_paginas)
    return imoveis, status

# ── Relatórios ───────────────────────────────────────────────────────────────────
def salvar_progresso():
    with _lock:
        data = {
            "atualizado": datetime.now().isoformat(),
            "total_imoveis": len(_estado["imoveis"]),
            "por_leiloeiro": _estado["por_leiloeiro"],
            "sites_ok": _estado["sites_ok"], "sites_err": _estado["sites_err"],
            "sites_sem_leilao": _estado["sites_sem_leilao"],
            "descartados_data": _estado["descartados_data"],
            "fonte_atual": _estado["fonte_atual"], "erros": _estado["erros"][-10:],
        }
    try: PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass

def relatorio_5min():
    with _lock:
        por = dict(_estado["por_leiloeiro"]); total = len(_estado["imoveis"]); atual = _estado["fonte_atual"]
    log("\n" + "="*64)
    log(f"RELATÓRIO PARCIAL | {datetime.now().strftime('%H:%M:%S')} | {total} imóveis | em: {atual}")
    log("-"*64)
    if por:
        for nome, cnt in sorted(por.items(), key=lambda x: -x[1]):
            log(f"  {nome[:44]:<44} {cnt:>4} imóveis")
    else:
        log("  (ainda sem imóveis capturados)")
    log("="*64 + "\n")

def thread_relatorio(stop_evt):
    while not stop_evt.wait(300):
        relatorio_5min()
        salvar_progresso()

# ── CSVs ─────────────────────────────────────────────────────────────────────────
def salvar_csv_leiloeiros():
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"leiloeiros_tjam_jucea_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["nome","site","matricula","situacao","cidade_leiloeiro","uf_leiloeiro","junta"])
        for l in LEILOEIROS_JUCEA:
            w.writerow([l["nome"], l.get("site",""), l.get("matricula",""), l["situacao"],
                        l.get("cidade_leiloeiro",""), l.get("uf_leiloeiro",""), l["junta"]])
    log(f"[CSV] Leiloeiros: {path.name} ({len(LEILOEIROS_JUCEA)} regulares)")
    return path

def salvar_csv_imoveis(imoveis):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"imoveis_tjam_jucea_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_IMOVEIS, extrasaction="ignore")
        w.writeheader(); w.writerows(imoveis)
    log(f"[CSV] Imóveis: {path.name} ({len(imoveis)} registros)")
    return path

# ── SQLite ───────────────────────────────────────────────────────────────────────
def importar_sqlite(imoveis):
    log(f"\n[SQLite] Importando {len(imoveis)} imóveis...")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS imoveis (
        id TEXT PRIMARY KEY, leiloeiro TEXT, junta TEXT, site TEXT,
        titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
        lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
        url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT)""")
    ins = dup = 0
    agora = datetime.now().isoformat(timespec="seconds")
    def _d(v):
        try: return float(Decimal(str(v).replace(",","."))) if v not in (None,"") else None
        except: return None
    for r in imoveis:
        try:
            conn.execute("INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r.get("id_externo",""), r.get("leiloeiro",""), "JUCEA", r.get("leiloeiro_site",""),
                (r.get("titulo") or "")[:500], (r.get("descricao") or "")[:300],
                (r.get("endereco_completo") or "")[:200], r.get("cidade",""), r.get("estado","AM"),
                _d(r.get("valor_minimo")), _d(r.get("valor_avaliacao")), r.get("data_primeiro_leilao") or "",
                r.get("url_original",""), r.get("tipo_imovel",""), r.get("imagem_principal",""), agora))
            ins += 1
        except sqlite3.IntegrityError:
            dup += 1
        except Exception as e:
            log(f"  [SQLite ERR] {str(e)[:100]}")
    conn.commit(); conn.close()
    log(f"  SQLite: {ins} inseridos, {dup} já existiam")
    return ins

# ── PostgreSQL ───────────────────────────────────────────────────────────────────
def psql_run(sql, timeout=60):
    return subprocess.run(["docker","exec","leilao_postgres","psql","-U","leilao","-d","leilao_db","-c",sql],
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)

def get_fonte_id(slug, url_base):
    s = slug.replace("'","''")
    psql_run(f"INSERT INTO fontes (nome,url_base,ativo,criado_em) VALUES ('{s}','{url_base}',true,NOW()) ON CONFLICT (nome) DO NOTHING;")
    out = subprocess.run(["docker","exec","leilao_postgres","psql","-U","leilao","-d","leilao_db",
                          "--no-align","--tuples-only","-c",f"SELECT id FROM fontes WHERE nome='{s}' LIMIT 1;"],
                         capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    return int(out) if out.isdigit() else None

TIPOS_IMOVEL = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
TIPOS_LEILAO = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

def importar_postgres(imoveis):
    log(f"\n[PostgreSQL] Importando {len(imoveis)} imóveis...")
    # agrupa por fonte_slug
    por_fonte = {}
    for r in imoveis:
        por_fonte.setdefault(r.get("fonte_slug","jucea"), []).append(r)

    ins_tot = err_tot = 0
    for slug, regs in por_fonte.items():
        url_base = regs[0].get("leiloeiro_site","") or "https://tjam.superbid.net"
        fonte_id = get_fonte_id(slug, url_base)
        if not fonte_id:
            log(f"  [ERRO] sem fonte_id para {slug}"); err_tot += len(regs); continue
        log(f"  fonte '{slug}' (id={fonte_id}): {len(regs)} imóveis")
        for r in regs:
            def esc(v, n=None):
                s = str(v if v is not None else "").replace("\x00","").replace("'","''")
                return s[:n] if n else s
            def _d(v):
                try: return float(Decimal(str(v).replace(",","."))) if v not in (None,"") else None
                except: return None
            ti = (r.get("tipo_imovel") or "outro").upper(); ti = ti if ti in TIPOS_IMOVEL else "OUTRO"
            tl = (r.get("tipo_leilao") or "extrajudicial").upper(); tl = tl if tl in TIPOS_LEILAO else "EXTRAJUDICIAL"
            vmin, vaval, area = _d(r.get("valor_minimo")), _d(r.get("valor_avaliacao")), _d(r.get("area_total"))
            quartos = r.get("quartos"); quartos = int(quartos) if str(quartos or "").isdigit() else None
            d1, d2, denc = r.get("data_primeiro_leilao"), r.get("data_segundo_leilao"), r.get("data_encerramento")
            sql = f"""INSERT INTO imoveis (
                fonte_id,id_externo,titulo,descricao,url_original,tipo_imovel,tipo_leilao,status,categoria,
                cidade,estado,cep,endereco_completo,valor_minimo,valor_avaliacao,area_total,quartos,
                data_primeiro_leilao,data_segundo_leilao,data_encerramento,
                imagem_principal,imagens,arquivos,numero_processo,vara,comarca,leiloeiro,
                ativo,classificado,geocodificado,criado_em,atualizado_em
            ) VALUES (
                {fonte_id},'{esc(r.get("id_externo"),200)}','{esc(r.get("titulo"),500)}','{esc(r.get("descricao"),4000)}',
                '{esc(r.get("url_original"),1000)}','{ti}','{tl}','ABERTO','IMOVEL',
                '{esc(r.get("cidade"),200)}','{esc(r.get("estado","AM"),2)}','{esc(r.get("cep"),10)}','{esc(r.get("endereco_completo"),500)}',
                {vmin if vmin is not None else 'NULL'},{vaval if vaval is not None else 'NULL'},{area if area is not None else 'NULL'},
                {quartos if quartos is not None else 'NULL'},
                {f"'{d1}'" if d1 else 'NULL'},{f"'{d2}'" if d2 else 'NULL'},{f"'{denc}'" if denc else 'NULL'},
                '{esc(r.get("imagem_principal"),1000)}','{esc(r.get("imagens","[]"),8000)}','{esc(r.get("arquivos","[]"),8000)}',
                '{esc(r.get("numero_processo"),100)}','{esc(r.get("vara"),200)}','{esc(r.get("comarca"),200)}','{esc(r.get("leiloeiro"),300)}',
                true,false,false,NOW(),NOW()
            ) ON CONFLICT (fonte_id,id_externo) DO UPDATE SET
                titulo=EXCLUDED.titulo, valor_minimo=EXCLUDED.valor_minimo,
                data_primeiro_leilao=EXCLUDED.data_primeiro_leilao, imagem_principal=EXCLUDED.imagem_principal,
                arquivos=EXCLUDED.arquivos, atualizado_em=NOW();"""
            try:
                proc = psql_run(sql, timeout=40)
                if proc.returncode == 0:
                    ins_tot += 1
                else:
                    err_tot += 1
                    log(f"    [ERR] {proc.stderr[:160].strip()}")
            except Exception as e:
                err_tot += 1; log(f"    [ERR] {str(e)[:120]}")
    log(f"  PostgreSQL: {ins_tot} upserts OK, {err_tot} erros")
    return ins_tot, err_tot

# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-banco", action="store_true")
    ap.add_argument("--so-tjam", action="store_true", help="Só fonte A (loja TJAM)")
    ap.add_argument("--max-sites", type=int, default=999)
    ap.add_argument("--max-paginas", type=int, default=6)
    args = ap.parse_args()

    log("="*64)
    log(f"SCRAPER TJAM + JUCEA iniciado — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Leiloeiros REGULAR: {len(LEILOEIROS_JUCEA)} | IRREGULAR excluídos: {len(IRREGULARES)}")
    log("="*64)

    salvar_csv_leiloeiros()

    stop_evt = threading.Event()
    t = threading.Thread(target=thread_relatorio, args=(stop_evt,), daemon=True); t.start()

    todos = []

    # FONTE A — loja TJAM
    try:
        tjam = scrape_tjam_superbid()
        todos.extend(tjam)
        with _lock: _estado["imoveis"].extend(tjam)
    except Exception as e:
        log(f"[ERRO FONTE A] {e}")
        with _lock: _estado["erros"].append(("tjam_superbid", str(e)))
    salvar_progresso()

    # FONTE B — sites dos leiloeiros
    if not args.so_tjam:
        com_site = [l for l in LEILOEIROS_JUCEA if l.get("site")][:args.max_sites]
        log(f"\n{'='*60}\nFONTE B — {len(com_site)} sites de leiloeiros REGULAR\n{'='*60}")
        for idx, lei in enumerate(com_site, 1):
            with _lock: _estado["fonte_atual"] = lei["nome"]
            log(f"\n[{idx}/{len(com_site)}] {lei['nome']}")
            try:
                imoveis, status = scrape_leiloeiro_site(lei, args.max_paginas)
            except Exception as e:
                imoveis, status = [], "erro"
                with _lock: _estado["erros"].append((lei.get("site",""), str(e)[:200]))
                log(f"  [ERRO] {str(e)[:120]}")
            # filtro de data
            validos, descartados = [], 0
            for im in imoveis:
                if imovel_valido_por_data(im.get("data_primeiro_leilao"), im.get("data_encerramento")):
                    validos.append(im)
                else:
                    descartados += 1
            with _lock:
                if status == "ok": _estado["sites_ok"] += 1
                elif status in ("erro",): _estado["sites_err"] += 1
                else: _estado["sites_sem_leilao"] += 1
                _estado["imoveis"].extend(validos)
                _estado["por_leiloeiro"][lei["nome"]] = len(validos)
                _estado["descartados_data"] += descartados
            todos.extend(validos)
            log(f"  Status: {status} | {len(validos)} válidos (+{descartados} desc. data) | Total: {len(todos)}")
            salvar_progresso()
            time.sleep(1)

    stop_evt.set()

    # Dedup global por URL
    vistos, unicos = set(), []
    for im in todos:
        u = (im.get("url_original") or im.get("id_externo") or "").strip()
        if u and u in vistos: continue
        vistos.add(u); unicos.append(im)
    log(f"\n{'='*64}\nSCRAPING CONCLUÍDO: {len(unicos)} imóveis únicos (de {len(todos)} coletados)")
    relatorio_5min()

    if unicos:
        salvar_csv_imoveis(unicos)

    if not args.sem_banco and unicos:
        n_sqlite = importar_sqlite(unicos)
        n_pg, err_pg = importar_postgres(unicos)
        # Verificação CSV↔banco
        log(f"\n[VERIFICAÇÃO] coletados válidos={len(unicos)} | SQLite ins+dup tratados | PG upserts={n_pg} erros={err_pg}")

    log("\n[ERROS REGISTRADOS]")
    with _lock: erros = list(_estado["erros"])
    for site, msg in erros: log(f"  {site}: {msg[:150]}")
    if not erros: log("  Nenhum erro.")

    log("\n[CONCLUSÃO]")
    log(f"  Imóveis únicos válidos: {len(unicos)}")
    log(f"  Descartados por data: {_estado['descartados_data']}")
    log(f"  Sites OK: {_estado['sites_ok']} | sem leilão: {_estado['sites_sem_leilao']} | erro: {_estado['sites_err']}")
    log(f"  CSV imóveis: csv/imoveis_tjam_jucea_{TODAY}.csv")
    log(f"  CSV leiloeiros: csv/leiloeiros_tjam_jucea_{TODAY}.csv")
    salvar_progresso()

if __name__ == "__main__":
    main()
