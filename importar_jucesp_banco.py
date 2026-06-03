"""
Enriquece e importa imóveis JUCESP Atuante Regular no banco de dados.

Fontes:
  1. imoveis_completo CSV (cp1252) — dados já ricos
  2. CSV extra do scraping — enriquecido via fetch de URL

Execução:
  python importar_jucesp_banco.py
  python importar_jucesp_banco.py --dry-run     # sem commit
  python importar_jucesp_banco.py --skip-enrich # pula fetch de URLs extras
"""
import sys, csv, json, re, hashlib, warnings, time, argparse
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import psycopg2
from psycopg2.extras import execute_values
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL   = "postgresql://leilao:leilao123@localhost:5432/leilao_db"
BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"

COMPLETO_CSV = CSV_DIR / "imoveis_completo_20260601_1708.csv"
EXTRA_CSV    = CSV_DIR / "imoveis_jucesp_2026-06-02.csv"
LEIS_CSV     = BASE_DIR / "leiloeiros_regulares.csv"

BATCH        = 200
ENRICH_WORKERS = 6
ENRICH_TIMEOUT = 12
TODAY        = date.today()

ESTADOS_VALIDOS = {
    "AC","AL","AM","AP","BA","CE","DF","ES","GO","MA","MG","MS",
    "MT","PA","PB","PE","PI","PR","RJ","RN","RO","RR","RS","SC",
    "SE","SP","TO"
}

# ── HTTP session ──────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    r = Retry(total=2, backoff_factor=0.3, status_forcelist=[429,500,502,503])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://",  HTTPAdapter(max_retries=r))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })
    return s
SES = make_session()

# ── Helpers ───────────────────────────────────────────────────────────────────
def mkid(s):
    return hashlib.md5(str(s).encode()).hexdigest()[:16]

def dec(v, default=None):
    if v is None or str(v).strip() in ("","None","null"): return default
    t = re.sub(r"[^\d,.]", "", str(v))
    if "," in t and "." in t: t = t.replace(".", "").replace(",", ".")
    elif "," in t: t = t.replace(",", ".")
    try:
        f = float(t)
        return Decimal(str(f)).quantize(Decimal("0.01")) if f > 0 else default
    except: return default

def parse_dt(txt):
    if not txt or str(txt).strip() in ("","None","null","nan"): return None
    for pat in [r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})",
                r"(\d{4})-(\d{2})-(\d{2})"]:
        m = re.search(pat, str(txt))
        if m:
            gs = m.groups()
            if len(gs[0]) == 4:  # ISO
                y, mo, d = gs
            else:
                d, mo, y = gs
            if len(y) == 2: y = "20" + y
            try: return datetime(int(y), int(mo), int(d))
            except: pass
    return None

def fut(dt):
    if dt is None: return True
    return dt.date() >= TODAY

def norm_tipo(s):
    s = str(s or "").lower()
    if any(x in s for x in ["apart","apto","flat","studio"]): return "APARTAMENTO"
    if any(x in s for x in ["casa","sobrado","chale"]): return "CASA"
    if any(x in s for x in ["terreno","lote","gleba","area"]): return "TERRENO"
    if any(x in s for x in ["comercial","loja","ponto"]): return "COMERCIAL"
    if any(x in s for x in ["galpao","galpão","armazem"]): return "GALPAO"
    if any(x in s for x in ["sala","escritorio"]): return "SALA"
    if any(x in s for x in ["rural","chacara","sitio","fazenda","chácara","sítio"]): return "RURAL"
    if any(x in s for x in ["vaga","garagem"]): return "VAGA"
    return "OUTRO"

def norm_tipo_leilao(s, lei_nome=""):
    s = str(s or "").lower()
    lei = str(lei_nome or "").lower()
    if any(x in s+lei for x in ["judicial","processo","tj","trt","tst","vara","comarca"]): return "JUDICIAL"
    if any(x in s+lei for x in ["banco","caixa","itau","bradesco","santander","bb ","bmg","fiduci"]): return "BANCARIO"
    return "EXTRAJUDICIAL"

def calc_desconto(avaliacao, minimo):
    if avaliacao and minimo and avaliacao > 0:
        return Decimal(str(round((1 - float(minimo)/float(avaliacao))*100, 2)))
    return None

def calc_preco_m2(valor, area):
    if valor and area and float(area) > 0:
        return Decimal(str(round(float(valor)/float(area), 2)))
    return None

def safe_str(s, max_len=None):
    s = str(s or "").strip()
    if max_len: s = s[:max_len]
    return s or None

def url_limpa(url):
    try:
        p = urlparse(str(url))
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except: return str(url)

def dominio(url):
    try: return urlparse(str(url)).netloc.lower().replace("www.","")
    except: return ""

def nome_fonte(url, lei_nome=""):
    d = dominio(url)
    name = re.sub(r"[^a-z0-9]","", (d or lei_nome or "").lower())[:25]
    return name or "fonte_desconhecida"

# ── Enriquecimento por URL ─────────────────────────────────────────────────────
PRICE_PAT  = re.compile(r"R\$\s*([\d.]+(?:,\d{1,2})?)")
DATE_PAT   = re.compile(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})")
AREA_PAT   = re.compile(r"([\d.,]+)\s*m[²2]", re.I)
PROC_PAT   = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}|\d{4,}\.\d{2}\.\d{6}-\d")
CEP_PAT    = re.compile(r"\d{5}-?\d{3}")

def enrich_url(url: str) -> dict:
    """Faz fetch da URL e extrai campos extras."""
    extra = {}
    try:
        r = SES.get(url, timeout=ENRICH_TIMEOUT, verify=False, allow_redirects=True)
        if r.status_code >= 400: return extra
        try: r.encoding = r.apparent_encoding or "utf-8"
        except: pass
        text = r.text
        soup = BeautifulSoup(text, "html.parser")

        # Título
        for sel in ["h1","h2.titulo","h2","[class*=title]","[class*=titulo]"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(" ", strip=True)
                if len(t) > 10:
                    extra["titulo"] = t[:300]
                    break

        # __NEXT_DATA__ ou JSON embutido
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd:
            try:
                data = json.loads(nd.string or "")
                _extract_from_json(data, extra)
            except: pass

        # Preços
        if "valor_minimo" not in extra:
            for pat in [r"Lance\s+Inicial[^R]*R\$\s*([\d.,]+)",
                        r"1[ªa]\s*Pra[çc]a[^R]*R\$\s*([\d.,]+)",
                        r"R\$\s*([\d.]+,\d{2})"]:
                m = re.search(pat, text, re.I)
                if m:
                    v = dec(m.group(1))
                    if v: extra["valor_minimo"] = v; break

        if "valor_avaliacao" not in extra:
            for pat in [r"[Aa]valia[çc][ãa]o[^R]*R\$\s*([\d.,]+)",
                        r"[Vv]alor\s+de\s+Refer[êe]ncia[^R]*R\$\s*([\d.,]+)"]:
                m = re.search(pat, text, re.I)
                if m:
                    v = dec(m.group(1))
                    if v: extra["valor_avaliacao"] = v; break

        # Datas
        for label, key in [("1[ªa]\\.?\\s*Pra[çc]a","data_primeiro_leilao"),
                            ("2[ªa]\\.?\\s*Pra[çc]a","data_segundo_leilao"),
                            ("[Ee]ncerramento","data_encerramento")]:
            m = re.search(rf"{label}[^0-9]{{0,30}}(\d{{1,2}}[/\-]\d{{1,2}}[/\-]\d{{4}})", text, re.I)
            if m and key not in extra:
                dt = parse_dt(m.group(1))
                if dt: extra[key] = dt

        # Área
        if "area_total" not in extra:
            m = AREA_PAT.search(text)
            if m:
                v = dec(m.group(1))
                if v: extra["area_total"] = v

        # Processo
        if "numero_processo" not in extra:
            m = PROC_PAT.search(text)
            if m: extra["numero_processo"] = m.group(0)[:100]

        # CEP
        if "cep" not in extra:
            m = CEP_PAT.search(text)
            if m: extra["cep"] = m.group(0)[:10]

        # Endereço
        for sel in ["[class*=endereco]","[class*=address]","[class*=localizacao]","[class*=location]"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(" ",strip=True)[:500]
                if len(t) > 5: extra["endereco_raw"] = t; break

        # Imagem principal
        if "imagem_principal" not in extra:
            for sel in ["img.foto-principal","[class*=foto-principal]","[class*=main-image]",
                        ".lote-foto img","article img"]:
                img = soup.select_one(sel)
                if img and img.get("src",""):
                    src = img["src"]
                    if src.startswith("http"):
                        extra["imagem_principal"] = src[:1000]; break

        # Imagens
        imgs = []
        for img in soup.select("img[src]"):
            src = img.get("src","")
            if src.startswith("http") and any(x in src for x in ["foto","imag","lote","bem","property"]):
                imgs.append(src)
        if imgs: extra["imagens_extra"] = list(dict.fromkeys(imgs))[:20]

        # Documentos (editais, matrículas)
        docs = []
        for a in soup.select("a[href]"):
            href = a.get("href","")
            label = a.get_text(strip=True).lower()
            if any(x in label+href.lower() for x in ["edital","matricula","laudo","pdf","doc"]):
                full = href if href.startswith("http") else f"{r.url.rstrip('/')}/{href.lstrip('/')}"
                tipo = "edital" if "edital" in label+href.lower() else \
                       "matricula" if "matricul" in label+href.lower() else "doc"
                docs.append({"tipo": tipo, "url": full[:1000], "nome": a.get_text(strip=True)[:100]})
        if docs: extra["docs_extra"] = docs[:10]

        # Quartos/banheiros/vagas
        for label, key in [("quarto","quartos"),("dormit","quartos"),
                             ("banheiro","banheiros"),("vaga","vagas"),("garagem","vagas")]:
            m = re.search(rf"(\d+)\s*{label}", text, re.I)
            if m and key not in extra:
                try: extra[key] = int(m.group(1))
                except: pass

        # Vara/comarca
        m = re.search(r"(\d[ªa]\s*Vara[^,\n]{0,80})", text, re.I)
        if m and "vara" not in extra: extra["vara"] = m.group(1).strip()[:300]
        m = re.search(r"[Cc]omarca\s+(?:de\s+)?([A-ZÀ-Ú][a-zà-ú ]+)", text)
        if m and "comarca" not in extra: extra["comarca"] = m.group(1).strip()[:300]

    except Exception: pass
    return extra


def _extract_from_json(obj, out: dict, depth=0):
    if depth > 5: return
    if isinstance(obj, list):
        for item in obj[:30]: _extract_from_json(item, out, depth+1)
    elif isinstance(obj, dict):
        kl = {k.lower(): k for k in obj}
        for src, dst in [("titulo","titulo"),("title","titulo"),("descricao","descricao"),
                          ("description","descricao"),("valor_minimo","valor_minimo"),
                          ("valorminimo","valor_minimo"),("price","valor_minimo"),
                          ("valor_avaliacao","valor_avaliacao"),("city","cidade"),
                          ("cidade","cidade"),("estado","estado"),("state","estado"),
                          ("cep","cep"),("bairro","bairro"),("logradouro","logradouro"),
                          ("processo","numero_processo"),("vara","vara"),("comarca","comarca"),
                          ("area_total","area_total"),("area","area_total"),
                          ("quartos","quartos"),("banheiros","banheiros"),("vagas","vagas"),
                          ("imagem","imagem_principal"),("image","imagem_principal"),
                          ("thumbnail","imagem_principal")]:
            if src in kl and dst not in out:
                v = obj[kl[src]]
                if isinstance(v, (str,int,float)) and str(v).strip():
                    out[dst] = v
        for v in obj.values():
            if isinstance(v, (dict,list)): _extract_from_json(v, out, depth+1)


# ── Carrega fontes ─────────────────────────────────────────────────────────────
def load_leiloeiros_db(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, nome, site FROM leiloeiros")
    leiloeiros = {}
    for lid, nome, site in cur.fetchall():
        n = re.sub(r"[^a-z]","", str(nome or "").lower())
        leiloeiros[n] = lid
        if site:
            d = dominio(site)
            if d: leiloeiros[d] = lid
    return leiloeiros

def get_ou_criar_fonte(conn, cache, url, lei_nome=""):
    d = dominio(url)
    key = d or lei_nome
    if key in cache: return cache[key]
    cur = conn.cursor()
    cur.execute("SELECT id FROM fontes WHERE url_base ILIKE %s LIMIT 1", (f"%{d}%",))
    row = cur.fetchone()
    if row:
        cache[key] = row[0]; return row[0]
    nome = nome_fonte(url, lei_nome)
    cur.execute(
        "INSERT INTO fontes (nome, url_base, ativo, criado_em) VALUES (%s, %s, TRUE, NOW()) RETURNING id",
        (nome, f"https://{d}" if d else lei_nome)
    )
    fid = cur.fetchone()[0]
    cache[key] = fid
    return fid

def get_leiloeiro_id(leis_db, lei_nome):
    if not lei_nome: return None
    clean = re.sub(r"[^a-z]","", lei_nome.lower())
    for k, v in leis_db.items():
        if len(clean) > 6 and (clean in k or k in clean):
            return v
    return None


# ── Carrega CSV completo (cp1252) ─────────────────────────────────────────────
def load_completo():
    rows = []
    seen = set()
    with open(COMPLETO_CSV, encoding="cp1252", errors="replace") as f:
        for row in csv.DictReader(f):
            url = (row.get("url_original","") or "").strip()
            if not url: continue
            uk = url_limpa(url)
            if uk in seen: continue
            seen.add(uk)
            rows.append(row)
    print(f"  completo CSV: {len(rows)} registros")
    return rows, seen

# ── Carrega CSV extra ─────────────────────────────────────────────────────────
def load_extra(existing_urls):
    rows = []
    if not EXTRA_CSV.exists(): return rows
    with open(EXTRA_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("url","") or "").strip()
            if not url: continue
            uk = url_limpa(url)
            if uk in existing_urls: continue
            existing_urls.add(uk)
            rows.append(row)
    print(f"  extra CSV: {len(rows)} novos registros")
    return rows


# ── Monta registro para inserção ───────────────────────────────────────────────
def row_completo_to_db(row, fonte_cache, leis_db, conn):
    url = url_limpa(row.get("url_original",""))
    lei_nome = re.sub(r"(?i)\boficial\b|\bJUCESP\b|\d+","",
                      row.get("leiloeiro","")).strip()

    dt1 = parse_dt(row.get("data_primeiro_leilao",""))
    dt2 = parse_dt(row.get("data_segundo_leilao",""))
    dte = parse_dt(row.get("data_encerramento",""))

    if not fut(dt1) and not fut(dte): return None

    av  = dec(row.get("valor_avaliacao",""))
    vm  = dec(row.get("valor_minimo",""))
    des = dec(row.get("desconto_percentual","")) or calc_desconto(av, vm)
    at  = dec(row.get("area_total",""))
    pm2 = calc_preco_m2(vm, at)
    sc  = row.get("score_oportunidade","")
    try: sc = int(float(sc)) if sc else None
    except: sc = None

    # Arquivos/docs
    arqs = row.get("arquivos","") or ""
    try:
        arqs_json = json.loads(arqs) if arqs.startswith("[") else []
    except: arqs_json = []

    edital_url   = row.get("edital_url","") or None
    matricula_url = row.get("matricula_url","") or None
    if not edital_url:
        for a in arqs_json:
            if isinstance(a,dict) and a.get("tipo","")=="edital":
                edital_url = a.get("url","")[:1000]; break
    if not matricula_url:
        for a in arqs_json:
            if isinstance(a,dict) and a.get("tipo","")=="matricula":
                matricula_url = a.get("url","")[:1000]; break

    # Imagens
    img_principal = row.get("imagem_principal","") or None
    imgs_json = json.dumps([img_principal] if img_principal else [])

    cidade = safe_str(row.get("cidade",""), 200)
    estado = safe_str(row.get("estado",""), 2)
    if estado and estado not in ESTADOS_VALIDOS: estado = None

    fonte_id = get_ou_criar_fonte(conn, fonte_cache, url, lei_nome)
    lei_id   = get_leiloeiro_id(leis_db, lei_nome)
    tipo_im  = norm_tipo(row.get("tipo_imovel","") or "")
    desc     = safe_str(row.get("descricao",""), 8000)
    titulo   = safe_str(row.get("titulo",""), 500) or f"Imóvel em leilão — {lei_nome}"

    return dict(
        id_externo      = mkid(url),
        fonte_id        = fonte_id,
        titulo          = titulo,
        descricao       = desc,
        url_original    = url[:1000],
        tipo_imovel     = tipo_im,
        tipo_leilao     = norm_tipo_leilao("", lei_nome + " " + (desc or "")),
        status          = "ABERTO",
        categoria       = "IMOVEL",
        valor_avaliacao = av,
        valor_minimo    = vm,
        desconto_percentual = des,
        cidade          = cidade,
        estado          = estado,
        area_total      = at,
        quartos         = _int(row.get("quartos","")),
        banheiros       = _int(row.get("banheiros","")),
        vagas           = _int(row.get("vagas","")),
        data_primeiro_leilao = dt1,
        data_segundo_leilao  = dt2,
        data_encerramento    = dte,
        imagem_principal= img_principal and img_principal[:1000],
        imagens         = imgs_json,
        arquivos        = arqs or None,
        edital_url      = edital_url,
        matricula_url   = matricula_url,
        numero_processo = safe_str(row.get("numero_processo",""), 100),
        vara            = safe_str(row.get("vara",""), 300),
        comarca         = safe_str(row.get("comarca",""), 300),
        leiloeiro       = safe_str(lei_nome, 300),
        leiloeiro_id    = lei_id,
        preco_m2        = pm2,
        score_oportunidade = sc,
        geocodificado   = bool(row.get("latitude","")),
        classificado    = True,
        ativo           = True,
        latitude        = dec(row.get("latitude","")),
        longitude       = dec(row.get("longitude","")),
        logradouro      = None,
        bairro          = None,
        cep             = None,
    )


def row_extra_to_db(row, enriched, fonte_cache, leis_db, conn):
    url = url_limpa(row.get("url",""))
    lei_nome = safe_str(row.get("lei",""), 200) or ""

    dt1 = parse_dt(enriched.get("data_primeiro_leilao") or row.get("data",""))
    dte = parse_dt(enriched.get("data_encerramento",""))

    if not fut(dt1) and not fut(dte): return None

    av  = dec(enriched.get("valor_avaliacao")) or dec(row.get("aval",""))
    vm  = dec(enriched.get("valor_minimo"))    or dec(row.get("lan",""))
    des = calc_desconto(av, vm)
    at  = dec(enriched.get("area_total",""))
    pm2 = calc_preco_m2(vm, at)

    img = (enriched.get("imagem_principal") or row.get("img","") or "")[:1000] or None
    imgs_extra = enriched.get("imagens_extra",[])
    imgs_json = json.dumps([img] + [x for x in imgs_extra if x != img] if img else imgs_extra)

    docs = enriched.get("docs_extra",[])
    edital_url    = next((d["url"] for d in docs if d["tipo"]=="edital"), None)
    matricula_url = next((d["url"] for d in docs if d["tipo"]=="matricula"), None)
    arquivos_json = json.dumps(docs) if docs else None

    titulo = safe_str(enriched.get("titulo") or row.get("tit",""), 500) or \
             f"Imóvel em leilão — {lei_nome}"

    cidade = safe_str(row.get("cid","") or enriched.get("cidade",""), 200)
    estado = safe_str(row.get("uf","") or enriched.get("estado",""), 2)
    if estado and estado not in ESTADOS_VALIDOS: estado = None

    desc_raw = enriched.get("descricao","")
    end_raw  = enriched.get("endereco_raw","")
    desc = safe_str(desc_raw or end_raw, 8000)

    fonte_id = get_ou_criar_fonte(conn, fonte_cache, url, lei_nome)
    lei_id   = get_leiloeiro_id(leis_db, lei_nome)
    tipo_im  = norm_tipo(row.get("tipo","") or enriched.get("tipo_imovel",""))

    return dict(
        id_externo      = mkid(url),
        fonte_id        = fonte_id,
        titulo          = titulo,
        descricao       = desc,
        url_original    = url[:1000],
        tipo_imovel     = tipo_im,
        tipo_leilao     = norm_tipo_leilao("", lei_nome),
        status          = "ABERTO",
        categoria       = "IMOVEL",
        valor_avaliacao = av,
        valor_minimo    = vm,
        desconto_percentual = des,
        cidade          = cidade,
        estado          = estado,
        area_total      = at,
        quartos         = _int(enriched.get("quartos","")),
        banheiros       = _int(enriched.get("banheiros","")),
        vagas           = _int(enriched.get("vagas","")),
        data_primeiro_leilao = dt1 or parse_dt(enriched.get("data_primeiro_leilao","")),
        data_segundo_leilao  = parse_dt(enriched.get("data_segundo_leilao","")),
        data_encerramento    = dte,
        imagem_principal= img,
        imagens         = imgs_json,
        arquivos        = arquivos_json,
        edital_url      = edital_url and edital_url[:1000],
        matricula_url   = matricula_url and matricula_url[:1000],
        numero_processo = safe_str(enriched.get("numero_processo",""), 100),
        vara            = safe_str(enriched.get("vara",""), 300),
        comarca         = safe_str(enriched.get("comarca",""), 300),
        leiloeiro       = safe_str(lei_nome, 300),
        leiloeiro_id    = lei_id,
        preco_m2        = pm2,
        score_oportunidade = None,
        geocodificado   = False,
        classificado    = False,
        ativo           = True,
        latitude        = None,
        longitude       = None,
        logradouro      = safe_str(enriched.get("logradouro",""), 500),
        bairro          = safe_str(enriched.get("bairro",""), 200),
        cep             = safe_str(enriched.get("cep",""), 10),
    )

def _int(v):
    try: return int(float(str(v or 0)))
    except: return None


# ── Inserção em batch ──────────────────────────────────────────────────────────
INSERT_SQL = """
INSERT INTO imoveis (
    id_externo, fonte_id, titulo, descricao, url_original,
    tipo_imovel, tipo_leilao, status, categoria,
    valor_avaliacao, valor_minimo, desconto_percentual,
    cidade, estado, area_total, quartos, banheiros, vagas,
    data_primeiro_leilao, data_segundo_leilao, data_encerramento,
    imagem_principal, imagens, arquivos, edital_url, matricula_url,
    numero_processo, vara, comarca, leiloeiro, leiloeiro_id,
    preco_m2, score_oportunidade, geocodificado, classificado, ativo,
    latitude, longitude, logradouro, bairro, cep,
    criado_em, atualizado_em
) VALUES (
    %(id_externo)s, %(fonte_id)s, %(titulo)s, %(descricao)s, %(url_original)s,
    %(tipo_imovel)s, %(tipo_leilao)s, %(status)s, %(categoria)s,
    %(valor_avaliacao)s, %(valor_minimo)s, %(desconto_percentual)s,
    %(cidade)s, %(estado)s, %(area_total)s, %(quartos)s, %(banheiros)s, %(vagas)s,
    %(data_primeiro_leilao)s, %(data_segundo_leilao)s, %(data_encerramento)s,
    %(imagem_principal)s, %(imagens)s, %(arquivos)s, %(edital_url)s, %(matricula_url)s,
    %(numero_processo)s, %(vara)s, %(comarca)s, %(leiloeiro)s, %(leiloeiro_id)s,
    %(preco_m2)s, %(score_oportunidade)s, %(geocodificado)s, %(classificado)s, %(ativo)s,
    %(latitude)s, %(longitude)s, %(logradouro)s, %(bairro)s, %(cep)s,
    NOW(), NOW()
)
ON CONFLICT DO NOTHING
"""

def inserir_batch(conn, records, dry_run):
    if not records: return 0, 0
    cur = conn.cursor()
    ok = dup = 0
    for rec in records:
        rec["tipo_imovel"] = rec.get("tipo_imovel","OUTRO")
        rec["tipo_leilao"] = rec.get("tipo_leilao","EXTRAJUDICIAL")
        rec["status"]      = rec.get("status","ABERTO")
        rec["categoria"]   = rec.get("categoria","IMOVEL")
        try:
            cur.execute(INSERT_SQL, rec)
            ok += cur.rowcount
        except Exception as e:
            conn.rollback()
            dup += 1
            # Re-abre cursor após rollback
            cur = conn.cursor()
    if not dry_run:
        conn.commit()
    return ok, dup


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  IMPORT JUCESP → BANCO")
    print(f"  dry-run: {args.dry_run}  skip-enrich: {args.skip_enrich}")
    print("=" * 60)

    conn = psycopg2.connect(DB_URL)
    fonte_cache = {}
    leis_db     = load_leiloeiros_db(conn)
    print(f"Leiloeiros no banco: {len(leis_db)}")

    # ── Fase 1: imoveis_completo ───────────────────────────────────────────────
    print("\n[FASE 1] Carregando imoveis_completo (dados ricos)...")
    completo_rows, seen_urls = load_completo()

    batch, ins_total, dup_total, skip_total = [], 0, 0, 0
    t0 = time.time()

    for i, row in enumerate(completo_rows, 1):
        rec = row_completo_to_db(row, fonte_cache, leis_db, conn)
        if rec is None:
            skip_total += 1
            continue
        batch.append(rec)
        if len(batch) >= BATCH:
            ok, dup = inserir_batch(conn, batch, args.dry_run)
            ins_total += ok; dup_total += dup
            batch = []
            elapsed = time.time() - t0
            print(f"  [{i}/{len(completo_rows)}] +{ins_total} inseridos | {dup_total} dup | "
                  f"{skip_total} skip | {elapsed:.0f}s")

    if batch:
        ok, dup = inserir_batch(conn, batch, args.dry_run)
        ins_total += ok; dup_total += dup

    print(f"\n  FASE 1 concluída: {ins_total} inseridos, {dup_total} dup, {skip_total} skip")

    # ── Fase 2: extras + enriquecimento ────────────────────────────────────────
    print("\n[FASE 2] Carregando registros extras...")
    extra_rows = load_extra(seen_urls)
    print(f"  {len(extra_rows)} para enriquecer e importar")

    if not extra_rows:
        print("  Nenhum registro extra.")
    else:
        enrich_results = {}
        if not args.skip_enrich:
            print(f"  Enriquecendo via URL ({ENRICH_WORKERS} workers)...")
            urls_to_enrich = [(r.get("url",""), r) for r in extra_rows if r.get("url","")]

            with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
                future_to_url = {ex.submit(enrich_url, u): u for u, _ in urls_to_enrich}
                done = 0
                for fut_obj in as_completed(future_to_url):
                    u = future_to_url[fut_obj]
                    try: enrich_results[u] = fut_obj.result()
                    except: enrich_results[u] = {}
                    done += 1
                    if done % 50 == 0:
                        print(f"    enriquecidos: {done}/{len(urls_to_enrich)}")

        print("  Importando extras enriquecidos...")
        batch2, ins2, dup2, skip2 = [], 0, 0, 0
        for i, row in enumerate(extra_rows, 1):
            url = row.get("url","")
            enriched = enrich_results.get(url, {})
            rec = row_extra_to_db(row, enriched, fonte_cache, leis_db, conn)
            if rec is None:
                skip2 += 1
                continue
            batch2.append(rec)
            if len(batch2) >= BATCH:
                ok, dup = inserir_batch(conn, batch2, args.dry_run)
                ins2 += ok; dup2 += dup; batch2 = []
                print(f"    [{i}/{len(extra_rows)}] +{ins2} inseridos")

        if batch2:
            ok, dup = inserir_batch(conn, batch2, args.dry_run)
            ins2 += ok; dup2 += dup

        ins_total += ins2; dup_total += dup2
        print(f"\n  FASE 2 concluída: {ins2} inseridos, {dup2} dup, {skip2} skip")

    # ── Resultado final ────────────────────────────────────────────────────────
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=TRUE")
    total_db = cur.fetchone()[0]
    conn.close()

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  CONCLUÍDO em {elapsed:.0f}s")
    print(f"  Inseridos : {ins_total}")
    print(f"  Duplicados: {dup_total}")
    print(f"  Total DB  : {total_db} imóveis ativos")
    if args.dry_run:
        print("  [DRY RUN — nada foi commitado]")
    print("=" * 60)


if __name__ == "__main__":
    main()
