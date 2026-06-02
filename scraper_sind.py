"""
scraper_sind.py — Pipeline completo SINDLEILOEIRO-SP

1. Coleta lista de leiloeiros de https://www.sindleiloeiro.com.br/leiloeiros
2. Salva CSV em csv/sind_leiloeiros_<data>.csv
3. Para cada leiloeiro com site, visita o site e extrai imóveis
4. Insere no banco de dados (leiloeiros + imoveis)
5. Reporta progresso a cada 5 minutos

Uso:
    python scraper_sind.py
"""

import sys
import io
import os
import csv
import json
import re
import time
import hashlib
import threading
import urllib3
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ─── UTF-8 no Windows ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Caminho do leilao-scraper para importar modelos ─────────────────────────
_SCRAPER_ROOT = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\leilao-scraper")
if str(_SCRAPER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_ROOT))

try:
    from database.models import Leiloeiro, Imovel, Fonte, TipoImovel, TipoLeilao, StatusLeilao, CategoriaItem
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker, Session
    DB_AVAILABLE = True
except ImportError as e:
    print(f"[AVISO] Banco indisponível: {e}", file=sys.stderr)
    DB_AVAILABLE = False

# ─── Configurações ─────────────────────────────────────────────────────────────
SIND_URL    = "https://www.sindleiloeiro.com.br/leiloeiros"
SIND_BASE   = "https://www.sindleiloeiro.com.br"
LEILOES_DIR = Path(__file__).parent
CSV_DIR     = LEILOES_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)

DB_URL_SYNC = os.getenv("DATABASE_URL_SYNC",
    "postgresql://leilao:leilao123@localhost:5432/leilao_db")

DELAY          = 3.0
TIMEOUT        = 20
REPORT_INTERVAL = 300  # 5 min

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── Regex helpers ────────────────────────────────────────────────────────────
RE_PRECO   = re.compile(r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)')
RE_AREA    = re.compile(r'(\d+(?:[,.]\d+)?)\s*m[²2]', re.IGNORECASE)
RE_QUARTOS = re.compile(r'(\d+)\s*quarto', re.IGNORECASE)
RE_BANHEI  = re.compile(r'(\d+)\s*banhe', re.IGNORECASE)
RE_VAGAS   = re.compile(r'(\d+)\s*vaga', re.IGNORECASE)
RE_CEP     = re.compile(r'\b\d{5}-?\d{3}\b')
RE_UF      = re.compile(
    r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b')
RE_DATA_BR = re.compile(
    r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\s+\S{1,3}\s+(\d{1,2}):(\d{2}))?')
RE_PROCESSO = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
RE_PDF_EXT  = re.compile(r'\.pdf(\?[^"\']*)?$', re.IGNORECASE)
RE_DOC_KW   = re.compile(
    r'edital|matr[íi]cula|laudo|avalia[cç][ãa]o|certid[ãa]o|'
    r'memorial|escritura|penhora|registro|processo', re.IGNORECASE)
IMG_SKIP = re.compile(
    r'logo|icon|favicon|avatar|banner|badge|star|rating|sprite|'
    r'pixel|tracking|blank|placeholder|default|noimage|no-image|'
    r'selo|stamp|whatsapp|social|share|loading', re.IGNORECASE)

LISTING_KEYWORDS = [
    'imovel','imoveis','imóvel','imóveis','lotes','lote','leilao','leilão',
    'leiloes','leilões','lance','lances','catalogo','catálogo','bem','bens',
    'apartamento','casa','terreno','oportunidade','propriedade',
]
CARD_SELECTORS = [
    '.card-lote','.lote-card','.lote-item','.item-lote','.card-imovel',
    '.imovel-card','.imovel-item','.card-bem','.auction-item','.lot-item',
    '.lot-card','.bem-item','.product-item','.listing-item','.listing-card',
    '.col-lote','.leilao-item','.oferta-item','.card-oferta',
    '[data-id]','[data-lote]','[data-bem]','[data-item]',
    '.card-property','.property-card','.property-item',
    'article.lote','article.imovel','li.lote','li.imovel',
]
TIPO_KEYWORDS = [
    (['apart','apto','ap.','flat','studio'],               'apartamento'),
    (['casa','sobrado','residência','residencia','vila'],   'casa'),
    (['terreno','lote ','gleba','área rural','chacara'],   'terreno'),
    (['galpão','galpao','armazém','armazem','depósito'],   'galpao'),
    (['sala','conjunto','escritório'],                     'sala'),
    (['loja','comercial','prédio comercial','pavilhão'],   'comercial'),
    (['fazenda','sítio','sitio','chácara','rural','haras'],'rural'),
]
SIGLAS_UF = {
    'AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MS','MT',
    'MG','PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO',
}

# ─── Estado global de progresso ───────────────────────────────────────────────
_lock = threading.Lock()
_progresso: dict[str, int] = {}
_iniciado_em = datetime.now()


def _registrar(nome: str, qtd: int = 1):
    with _lock:
        _progresso[nome] = _progresso.get(nome, 0) + qtd


def _imprimir_relatorio():
    elapsed = (datetime.now() - _iniciado_em).seconds // 60
    print(f"\n{'='*60}")
    print(f"RELATÓRIO DE PROGRESSO — {datetime.now().strftime('%H:%M:%S')} (+{elapsed}min)")
    print(f"{'='*60}")
    with _lock:
        total = sum(_progresso.values())
        if not _progresso:
            print("  (nenhum imóvel coletado ainda)")
        else:
            for nome, cnt in sorted(_progresso.items(), key=lambda x: -x[1]):
                bar = "█" * min(cnt, 40)
                print(f"  {nome[:40]:<40} {cnt:>5}  {bar}")
        print(f"  TOTAL: {total} imóveis")
    print(f"{'='*60}\n")
    sys.stdout.flush()


def _thread_relatorio():
    while True:
        time.sleep(REPORT_INTERVAL)
        _imprimir_relatorio()


# ─────────────────────────────────────────────────────────────────────────────
# 1. DECODE do email Cloudflare
# ─────────────────────────────────────────────────────────────────────────────

def _decode_cf_email(encoded: str) -> str:
    """Decodifica email protegido por Cloudflare via XOR."""
    try:
        r = int(encoded[:2], 16)
        return ''.join(chr(int(encoded[i:i+2], 16) ^ r) for i in range(2, len(encoded), 2))
    except Exception:
        return ''


# ─────────────────────────────────────────────────────────────────────────────
# 2. PARSE DA PÁGINA SINDLEILOEIRO
# ─────────────────────────────────────────────────────────────────────────────

def _normalizar_site(href: str) -> Optional[str]:
    if not href or href.startswith('#') or href.startswith('javascript') or href.startswith('mailto') or href.startswith('tel'):
        return None
    href = href.strip().rstrip('/')
    if href.startswith('http'):
        return href
    if href.startswith('www.') or '.' in href:
        return 'https://' + href
    return None


def parsear_sind() -> list[dict]:
    """Extrai todos os leiloeiros da página SINDLEILOEIRO-SP."""
    print(f"[SIND] Baixando {SIND_URL} ...")
    try:
        r = requests.get(SIND_URL, headers=HEADERS, timeout=30, verify=False)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERRO] Falha ao baixar SIND: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.content, 'html.parser')
    articles = soup.find_all('article')
    print(f"[SIND] {len(articles)} leiloeiros encontrados.")

    leiloeiros = []
    for art in articles:
        # Nome
        h3 = art.find('h3')
        nome = re.sub(r'\s+', ' ', h3.get_text(strip=True)).strip() if h3 else ''
        if not nome or len(nome) < 3:
            continue

        # Foto
        img = art.find('img')
        foto_url = None
        if img and img.get('src'):
            foto_url = urljoin(SIND_BASE, img['src'].split('?')[0])

        # Campos dos <li>
        lis = art.find_all('li')
        endereco = ''
        cep = ''
        telefone = ''
        jucesp = ''
        email = ''
        site = None

        for li in lis:
            texto = li.get_text(' ', strip=True)

            # Email (Cloudflare obfuscado)
            cf_span = li.find('span', class_='__cf_email__')
            if cf_span and cf_span.get('data-cfemail'):
                email = _decode_cf_email(cf_span['data-cfemail'])
            elif 'Email' in texto or '@' in texto:
                m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', texto)
                if m:
                    email = m.group(0)

            # Site — link interno ao <li>
            for a in li.find_all('a', href=True):
                href = a['href']
                if 'tel:' not in href and 'cdn-cgi' not in href and 'sindleiloeiro' not in href and 'mailto' not in href:
                    site = _normalizar_site(href)

            # Campos por texto
            if 'CEP:' in texto:
                m_cep = RE_CEP.search(texto)
                cep = m_cep.group(0) if m_cep else ''
            elif 'Telefone' in texto:
                telefone = re.sub(r'Telefone\s*:?\s*', '', texto).strip()
            elif 'JUCESP' in texto or 'Nº:' in texto or 'N°:' in texto:
                m_j = re.search(r'(\d+)', re.sub(r'JUCESP\s*[Nn][°oº]?:?\s*', '', texto))
                jucesp = m_j.group(1) if m_j else texto
            elif 'Site:' in texto or 'Email:' in texto:
                pass  # handled above
            elif texto and not endereco and len(texto) > 5:
                # First non-empty li without keyword = address
                if not any(k in texto for k in ['CEP','Tel','JUCESP','Site','Email','@']):
                    endereco = texto

        leiloeiros.append({
            'nome': nome,
            'matricula': jucesp,
            'endereco': endereco,
            'cep': cep,
            'telefone': telefone,
            'email': email,
            'site': site,
            'foto_url': foto_url,
            'situacao': 'regular',
            'uf': 'SP',
            'cidade': 'São Paulo',
            'junta_comercial': 'JUCESP',
            'fonte': SIND_URL,
        })

    com_site = sum(1 for l in leiloeiros if l.get('site'))
    print(f"[SIND] {len(leiloeiros)} extraídos, {com_site} com site.")
    return leiloeiros


# ─────────────────────────────────────────────────────────────────────────────
# 3. CSV
# ─────────────────────────────────────────────────────────────────────────────

def salvar_csv(leiloeiros: list[dict]) -> Path:
    data_str = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = CSV_DIR / f"sind_leiloeiros_{data_str}.csv"
    campos = ["matricula","nome","situacao","cidade","uf","site","email",
              "telefone","endereco","cep","foto_url","junta_comercial","fonte"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(leiloeiros)
    print(f"[CSV] Salvo: {csv_path} ({len(leiloeiros)} linhas)")
    return csv_path


# ─────────────────────────────────────────────────────────────────────────────
# 4. SCRAPING DE SITES DOS LEILOEIROS
# ─────────────────────────────────────────────────────────────────────────────

def _uid(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:24]


def _parse_preco(raw: str) -> Optional[Decimal]:
    try:
        cleaned = re.sub(r'[^\d,]', '', raw)
        if ',' in cleaned:
            cleaned = cleaned.replace('.', '').replace(',', '.')
        return Decimal(cleaned) if cleaned else None
    except (InvalidOperation, ValueError):
        return None


def _texto(el) -> str:
    return el.get_text(' ', strip=True) if el else ''


def _detectar_tipo(text: str) -> str:
    t = text.lower()
    for kws, tipo in TIPO_KEYWORDS:
        if any(kw in t for kw in kws):
            return tipo
    return 'outro'


def _parse_data_br(texto: str) -> Optional[datetime]:
    if not texto:
        return None
    m = RE_DATA_BR.search(texto.strip())
    if not m:
        return None
    dia, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hora = int(m.group(4) or 0)
    minuto = int(m.group(5) or 0)
    try:
        dt = datetime(ano, mes, dia, hora, minuto)
        return dt if 2020 <= dt.year <= 2035 else None
    except ValueError:
        return None


def _extrair_datas(text: str) -> dict:
    datas = {'data_primeiro_leilao': None, 'data_segundo_leilao': None, 'data_encerramento': None}
    padroes = [
        ('data_primeiro_leilao', [r'1[ºo°]\.?\s*[Ll]eil', r'[Pp]rimeiro\s+[Ll]eil', r'[Dd]ata\s+do\s+[Ll]eil']),
        ('data_segundo_leilao', [r'2[ºo°]\.?\s*[Ll]eil', r'[Ss]egundo\s+[Ll]eil']),
        ('data_encerramento', [r'[Ee]ncerramento', r'[Pp]razo', r'[Ff]im\s+do']),
    ]
    for campo, kws in padroes:
        for kw in kws:
            m = re.search(kw + r'.{0,80}?(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})', text)
            if m:
                dt = _parse_data_br(m.group(1))
                if dt:
                    datas[campo] = dt
                    break
    if not datas['data_primeiro_leilao']:
        for match in RE_DATA_BR.findall(text):
            try:
                dt = datetime(int(match[2]), int(match[1]), int(match[0]))
                if 2020 <= dt.year <= 2035:
                    datas['data_primeiro_leilao'] = dt
                    break
            except ValueError:
                continue
    return datas


def _extrair_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    arquivos = []
    seen: set[str] = set()
    for a in soup.find_all('a', href=True):
        href = (a.get('href') or '').strip()
        if not href or href.startswith(('#', 'javascript', 'mailto', 'tel')):
            continue
        full_url = urljoin(page_url, href)
        if full_url in seen:
            continue
        texto = a.get_text(strip=True)
        combined = f"{texto} {href}".lower()
        is_pdf = bool(RE_PDF_EXT.search(href))
        has_kw = bool(RE_DOC_KW.search(combined))
        if not (is_pdf or has_kw):
            continue
        c = combined
        if re.search(r'edital', c, re.I): tipo = 'edital'
        elif re.search(r'matr[íi]cula', c, re.I): tipo = 'matricula'
        elif re.search(r'laudo|avalia', c, re.I): tipo = 'laudo'
        elif re.search(r'certid[ãa]o', c, re.I): tipo = 'certidao'
        elif is_pdf: tipo = 'pdf'
        else: tipo = 'documento'
        arquivos.append({'tipo': tipo, 'url': full_url, 'nome': texto[:200] or tipo})
        seen.add(full_url)
        if len(arquivos) >= 15:
            break
    return arquivos


def _extrair_imagens(soup: BeautifulSoup, page_url: str) -> tuple[Optional[str], list[str]]:
    imgs = []
    seen: set[str] = set()
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ''
        src = src.strip()
        if not src or src.startswith('data:'):
            continue
        if IMG_SKIP.search(src):
            continue
        full = urljoin(page_url, src)
        if full not in seen:
            seen.add(full)
            imgs.append(full)
    principal = imgs[0] if imgs else None
    return principal, imgs


def _encontrar_paginas_listagem(soup: BeautifulSoup, base_url: str) -> list[str]:
    candidatos = []
    parsed_base = urlparse(base_url)
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('#') or href.startswith('javascript') or href.startswith('mailto'):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc != parsed_base.netloc:
            continue
        texto_link = (a.get_text(strip=True) + ' ' + href).lower()
        if any(kw in texto_link for kw in LISTING_KEYWORDS):
            candidatos.append(full)
    seen: set[str] = set()
    result = []
    for u in candidatos:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result[:10]


def _extrair_cards_da_pagina(soup: BeautifulSoup, page_url: str, leiloeiro_nome: str) -> list[dict]:
    cards_encontrados = []
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if len(cards) >= 2:
            for card in cards[:50]:
                texto = _texto(card)
                if len(texto) < 20:
                    continue
                link_el = card.find('a', href=True)
                url_imovel = urljoin(page_url, link_el['href']) if link_el else page_url
                titulo = None
                for t_sel in ['h1','h2','h3','h4','.titulo','.title','[class*="titulo"]']:
                    el = card.select_one(t_sel)
                    if el:
                        titulo = _texto(el)[:300]
                        if titulo:
                            break
                if not titulo:
                    titulo = (texto[:150].split('\n')[0]).strip() or 'Imóvel sem título'
                precos = RE_PRECO.findall(texto)
                valor_min = _parse_preco(precos[0]) if precos else None
                valor_aval = _parse_preco(precos[1]) if len(precos) > 1 else None
                area_m = RE_AREA.search(texto)
                area = Decimal(area_m.group(1).replace(',', '.')) if area_m else None
                quartos_m = RE_QUARTOS.search(texto)
                banheiros_m = RE_BANHEI.search(texto)
                vagas_m = RE_VAGAS.search(texto)
                uf_m = RE_UF.search(texto)
                estado = uf_m.group(1) if uf_m else 'SP'
                cep_m = RE_CEP.search(texto)
                cep = cep_m.group(0) if cep_m else None
                datas = _extrair_datas(texto)
                tipo_str = _detectar_tipo(titulo + ' ' + texto[:500])
                img = card.find('img')
                img_url = None
                if img:
                    src = img.get('src') or img.get('data-src') or img.get('data-lazy-src', '')
                    if src and not src.startswith('data:') and not IMG_SKIP.search(src):
                        img_url = urljoin(page_url, src)
                proc_m = RE_PROCESSO.search(texto)
                processo = proc_m.group(0) if proc_m else None
                cards_encontrados.append({
                    'id_externo': _uid(url_imovel),
                    'titulo': titulo,
                    'url_original': url_imovel,
                    'tipo_imovel': tipo_str,
                    'tipo_leilao': 'extrajudicial',
                    'valor_minimo': valor_min,
                    'valor_avaliacao': valor_aval,
                    'area_total': area,
                    'quartos': int(quartos_m.group(1)) if quartos_m else None,
                    'banheiros': int(banheiros_m.group(1)) if banheiros_m else None,
                    'vagas': int(vagas_m.group(1)) if vagas_m else None,
                    'estado': estado,
                    'cep': cep,
                    'data_primeiro_leilao': datas['data_primeiro_leilao'],
                    'data_segundo_leilao': datas['data_segundo_leilao'],
                    'data_encerramento': datas['data_encerramento'],
                    'imagem_principal': img_url,
                    'numero_processo': processo,
                    'leiloeiro': leiloeiro_nome,
                })
            if cards_encontrados:
                break
    return cards_encontrados


def _scrape_detalhe(url: str, dados: dict) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            return dados
        soup = BeautifulSoup(r.content, 'html.parser')
        texto = soup.get_text(' ', strip=True)
        if len(dados.get('titulo', '')) < 15:
            h1 = soup.find('h1')
            if h1:
                dados['titulo'] = _texto(h1)[:300]
        for sel in ['[class*="descricao"]','[class*="description"]','[class*="detalhes"]',
                    '[class*="obs"]','main p','article p']:
            el = soup.select_one(sel)
            if el:
                t = _texto(el)
                if len(t) > 80:
                    dados['descricao'] = t[:8000]
                    break
        precos = RE_PRECO.findall(texto)
        if precos and not dados.get('valor_minimo'):
            dados['valor_minimo'] = _parse_preco(precos[0])
        if len(precos) > 1 and not dados.get('valor_avaliacao'):
            dados['valor_avaliacao'] = _parse_preco(precos[1])
        cep_m = RE_CEP.search(texto)
        if cep_m and not dados.get('cep'):
            dados['cep'] = cep_m.group(0)
        datas = _extrair_datas(texto)
        for k, v in datas.items():
            if v and not dados.get(k):
                dados[k] = v
        img_principal, imgs = _extrair_imagens(soup, url)
        if not dados.get('imagem_principal') and img_principal:
            dados['imagem_principal'] = img_principal
        dados['imagens'] = json.dumps(imgs[:20])
        arquivos = _extrair_arquivos(soup, url)
        if arquivos:
            dados['arquivos'] = json.dumps(arquivos)
        proc_m = RE_PROCESSO.search(texto)
        if proc_m and not dados.get('numero_processo'):
            dados['numero_processo'] = proc_m.group(0)
        if 'judicial' in texto.lower() or 'processo' in texto.lower():
            dados['tipo_leilao'] = 'judicial'
    except Exception:
        pass
    return dados


def scrape_site_leiloeiro(nome: str, site_url: str) -> list[dict]:
    imoveis = []
    visitados: set[str] = set()
    print(f"\n[{nome}] Visitando {site_url} ...")
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            print(f"  [SKIP] HTTP {r.status_code}")
            return []
    except Exception as e:
        print(f"  [ERRO] {e}")
        return []

    soup_home = BeautifulSoup(r.content, 'html.parser')
    cards_home = _extrair_cards_da_pagina(soup_home, site_url, nome)
    paginas_listagem = _encontrar_paginas_listagem(soup_home, site_url)
    paginas_listagem = [p for p in paginas_listagem if p not in visitados]
    visitados.add(site_url)

    todas_listagem_cards: list[dict] = []
    for url_lista in paginas_listagem[:8]:
        if url_lista in visitados:
            continue
        visitados.add(url_lista)
        try:
            time.sleep(DELAY)
            r2 = requests.get(url_lista, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r2.status_code != 200:
                continue
            soup2 = BeautifulSoup(r2.content, 'html.parser')
            cards = _extrair_cards_da_pagina(soup2, url_lista, nome)
            if cards:
                todas_listagem_cards.extend(cards)
                print(f"  [OK] {url_lista} → {len(cards)} cards")
        except Exception as e:
            print(f"  [ERRO listagem] {url_lista}: {e}")

    todos_cards = todas_listagem_cards if todas_listagem_cards else cards_home
    if not todos_cards and cards_home:
        todos_cards = cards_home

    if not todos_cards:
        print(f"  [INFO] Nenhum card encontrado em {nome}")
        return []

    print(f"  [INFO] {len(todos_cards)} cards. Visitando detalhes...")

    for card in todos_cards[:30]:
        url_det = card.get('url_original', '')
        if url_det and url_det != site_url and url_det not in visitados:
            visitados.add(url_det)
            time.sleep(DELAY)
            card = _scrape_detalhe(url_det, card)

        data_1 = card.get('data_primeiro_leilao')
        data_enc = card.get('data_encerramento')
        data_ref = data_1 or data_enc
        if data_ref:
            if isinstance(data_ref, datetime):
                data_ref = data_ref.date()
            if data_ref < date.today():
                continue

        imoveis.append(card)

    print(f"  [OK] {nome}: {len(imoveis)} imóveis válidos (data >= hoje)")
    return imoveis


# ─────────────────────────────────────────────────────────────────────────────
# 5. BANCO DE DADOS
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_fonte(session: Session, nome: str, url_base: str) -> "Fonte":
    fonte = session.query(Fonte).filter(Fonte.nome == nome).first()
    if not fonte:
        fonte = Fonte(nome=nome, url_base=url_base, ativo=True)
        session.add(fonte)
        session.flush()
    return fonte


def _upsert_leiloeiro(session: Session, dados: dict) -> "Leiloeiro":
    existing = session.query(Leiloeiro).filter(
        Leiloeiro.nome == dados['nome'],
        Leiloeiro.uf == dados.get('uf', 'SP')
    ).first()
    col_names = set(Leiloeiro.__table__.columns.keys())
    if existing:
        for k, v in dados.items():
            if k in col_names and v is not None:
                setattr(existing, k, v)
        return existing
    lei = Leiloeiro(**{k: v for k, v in dados.items() if k in col_names})
    session.add(lei)
    session.flush()
    return lei


def _map_tipo_imovel(tipo_str: str) -> "TipoImovel":
    mapa = {
        'apartamento': TipoImovel.APARTAMENTO,
        'casa': TipoImovel.CASA,
        'terreno': TipoImovel.TERRENO,
        'comercial': TipoImovel.COMERCIAL,
        'rural': TipoImovel.RURAL,
        'galpao': TipoImovel.GALPAO,
        'sala': TipoImovel.SALA,
        'vaga': TipoImovel.VAGA,
    }
    return mapa.get(tipo_str, TipoImovel.OUTRO)


def _map_tipo_leilao(tipo_str: str) -> "TipoLeilao":
    if 'judicial' in tipo_str:
        return TipoLeilao.JUDICIAL
    if 'bancario' in tipo_str or 'bancário' in tipo_str:
        return TipoLeilao.BANCARIO
    return TipoLeilao.EXTRAJUDICIAL


def inserir_imovel(session: Session, fonte_id: int, leiloeiro_id: Optional[int],
                   dados: dict) -> bool:
    existing = session.query(Imovel).filter(
        Imovel.fonte_id == fonte_id,
        Imovel.id_externo == dados['id_externo']
    ).first()
    campos = {
        'id_externo':           dados['id_externo'],
        'fonte_id':             fonte_id,
        'titulo':               dados.get('titulo', 'Imóvel')[:500],
        'descricao':            dados.get('descricao'),
        'url_original':         dados.get('url_original', '')[:1000],
        'tipo_imovel':          _map_tipo_imovel(dados.get('tipo_imovel', 'outro')),
        'tipo_leilao':          _map_tipo_leilao(dados.get('tipo_leilao', 'extrajudicial')),
        'status':               StatusLeilao.ABERTO,
        'categoria':            CategoriaItem.IMOVEL,
        'valor_avaliacao':      dados.get('valor_avaliacao'),
        'valor_minimo':         dados.get('valor_minimo'),
        'cep':                  dados.get('cep'),
        'estado':               dados.get('estado', 'SP'),
        'cidade':               dados.get('cidade'),
        'endereco_completo':    dados.get('endereco_completo'),
        'area_total':           dados.get('area_total'),
        'quartos':              dados.get('quartos'),
        'banheiros':            dados.get('banheiros'),
        'vagas':                dados.get('vagas'),
        'data_primeiro_leilao': dados.get('data_primeiro_leilao'),
        'data_segundo_leilao':  dados.get('data_segundo_leilao'),
        'data_encerramento':    dados.get('data_encerramento'),
        'imagem_principal':     dados.get('imagem_principal'),
        'imagens':              dados.get('imagens'),
        'arquivos':             dados.get('arquivos'),
        'numero_processo':      dados.get('numero_processo'),
        'leiloeiro':            dados.get('leiloeiro'),
        'leiloeiro_id':         leiloeiro_id,
    }
    if existing:
        for k, v in campos.items():
            if v is not None:
                setattr(existing, k, v)
        return False
    session.add(Imovel(**campos))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 6. PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    t = threading.Thread(target=_thread_relatorio, daemon=True)
    t.start()

    print(f"\n{'='*60}")
    print(f"SCRAPER SINDLEILOEIRO-SP — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    # ── 1. Coleta leiloeiros ──────────────────────────────────────────────────
    leiloeiros = parsear_sind()
    if not leiloeiros:
        print("[FATAL] Nenhum leiloeiro extraído.", file=sys.stderr)
        return

    # ── 2. CSV ────────────────────────────────────────────────────────────────
    csv_path = salvar_csv(leiloeiros)

    com_site = [l for l in leiloeiros if l.get('site')]
    sem_site = [l for l in leiloeiros if not l.get('site')]
    print(f"\n[INFO] {len(com_site)} com site, {len(sem_site)} sem site.")

    # ── 3. Banco ──────────────────────────────────────────────────────────────
    session: Optional[Session] = None
    if DB_AVAILABLE:
        try:
            engine = create_engine(DB_URL_SYNC, echo=False, pool_pre_ping=True)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            print(f"[DB] Conectado: {DB_URL_SYNC.split('@')[-1]}")
        except Exception as e:
            print(f"[AVISO] Falha DB: {e}", file=sys.stderr)
            session = None

    # ── 4. Upsert leiloeiros ──────────────────────────────────────────────────
    if session:
        print("\n[DB] Upserting leiloeiros SIND...")
        for l in leiloeiros:
            try:
                _upsert_leiloeiro(session, {
                    'matricula': l.get('matricula'),
                    'nome': l['nome'],
                    'uf': 'SP',
                    'junta_comercial': 'JUCESP',
                    'situacao': l.get('situacao', 'regular'),
                    'cidade': 'São Paulo',
                    'email': l.get('email'),
                    'telefone': l.get('telefone'),
                    'site': l.get('site'),
                    'logo_url': l.get('foto_url'),
                    'fonte_url': SIND_URL,
                })
            except Exception as e:
                session.rollback()
                print(f"  [AVISO] {l['nome']}: {e}", file=sys.stderr)
        try:
            session.commit()
            print(f"[DB] {len(leiloeiros)} leiloeiros sincronizados.")
        except Exception as e:
            session.rollback()
            print(f"[AVISO] Commit: {e}", file=sys.stderr)

    # ── 5. Scrape dos sites ───────────────────────────────────────────────────
    print(f"\n[SCRAPE] {len(com_site)} sites para visitar...")
    total_imoveis = 0

    for i, lei in enumerate(com_site, 1):
        nome = lei['nome']
        site = lei['site']
        print(f"\n[{i}/{len(com_site)}] {nome}")
        print(f"  Site: {site}")

        try:
            imoveis = scrape_site_leiloeiro(nome, site)
        except Exception as e:
            print(f"  [ERRO] {e}", file=sys.stderr)
            imoveis = []

        if not imoveis:
            continue

        if session:
            inseridos_lote = 0
            try:
                fonte = _get_or_create_fonte(session, nome, site)
                lei_db = session.query(Leiloeiro).filter(
                    Leiloeiro.nome == nome, Leiloeiro.uf == 'SP'
                ).first()
                lei_id = lei_db.id if lei_db else None
                for imovel_data in imoveis:
                    novo = inserir_imovel(session, fonte.id, lei_id, imovel_data)
                    if novo:
                        inseridos_lote += 1
                session.commit()
                print(f"  [DB] {inseridos_lote} inseridos, {len(imoveis)-inseridos_lote} atualizados")
            except Exception as e:
                session.rollback()
                print(f"  [AVISO DB] {e}", file=sys.stderr)

        _registrar(nome, len(imoveis))
        total_imoveis += len(imoveis)

        if i < len(com_site):
            time.sleep(DELAY * 2)

    # ── 6. Relatório final ────────────────────────────────────────────────────
    if session:
        session.close()

    print(f"\n{'='*60}")
    print("COLETA FINALIZADA")
    _imprimir_relatorio()
    print(f"CSV dos leiloeiros: {csv_path}")
    print(f"Total de imóveis coletados: {total_imoveis}")
    print(f"{'='*60}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
