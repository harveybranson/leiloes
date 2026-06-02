"""
scraper_sind_v2.py — Re-scraping SINDLEILOEIRO com Playwright (sites JS-heavy)

Complemento do scraper_sind.py: visita os leiloeiros que retornaram 0 imóveis,
usando Playwright + adaptadores de plataforma para extrair mais dados.

Plataformas suportadas:
  - Damásio/Rico: /lotes/imovel → div.card.shadow-sm
  - Destak:       home page → lote links → detalhe
  - Sodré Santoro: /imoveis/lotes → Playwright
  - Rico Leilões:  /lotes/imovel → Playwright
  - Genérico:     Playwright + seletores ampliados

Uso:
    python scraper_sind_v2.py
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
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── UTF-8 no Windows ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
LEILOES_DIR = Path(__file__).parent
CSV_DIR     = LEILOES_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)
DB_URL_SYNC = os.getenv("DATABASE_URL_SYNC",
    "postgresql://leilao:leilao123@localhost:5432/leilao_db")
DELAY   = 2.5
TIMEOUT = 25000  # ms para Playwright
REPORT_INTERVAL = 300

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9",
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

# ─── Regex helpers ────────────────────────────────────────────────────────────
RE_PRECO   = re.compile(r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)')
RE_AREA    = re.compile(r'(\d+(?:[,.]\d+)?)\s*m[²2]', re.IGNORECASE)
RE_QUARTOS = re.compile(r'(\d+)\s*quarto', re.IGNORECASE)
RE_BANHEI  = re.compile(r'(\d+)\s*banhe', re.IGNORECASE)
RE_VAGAS   = re.compile(r'(\d+)\s*vaga', re.IGNORECASE)
RE_CEP     = re.compile(r'\b\d{5}-?\d{3}\b')
RE_UF      = re.compile(r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b')
RE_DATA_BR = re.compile(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\s+\S{1,3}\s+(\d{1,2}):(\d{2}))?')
RE_PROCESSO = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
RE_PDF_EXT  = re.compile(r'\.pdf(\?[^"\']*)?$', re.IGNORECASE)
RE_DOC_KW   = re.compile(r'edital|matr[íi]cula|laudo|avalia[cç][ãa]o|certid[ãa]o|memorial|processo', re.IGNORECASE)
IMG_SKIP = re.compile(r'logo|icon|favicon|avatar|banner|badge|star|rating|sprite|pixel|tracking|blank|placeholder|default|noimage|whatsapp|social|share|loading', re.IGNORECASE)

TIPO_KEYWORDS = [
    (['apart','apto','ap.','flat','studio'],               'apartamento'),
    (['casa','sobrado','residência','residencia','vila'],   'casa'),
    (['terreno','lote ','gleba','área rural','chacara'],   'terreno'),
    (['galpão','galpao','armazém','armazem','depósito'],   'galpao'),
    (['sala','conjunto','escritório'],                     'sala'),
    (['loja','comercial','prédio comercial','pavilhão'],   'comercial'),
    (['fazenda','sítio','sitio','chácara','rural','haras'],'rural'),
]

# Card selectors — extended with generic class patterns
CARD_SELECTORS_EXTENDED = [
    'div.card.shadow-sm', 'div.card-lote', '.lote-card', '.lote-item',
    '.card-imovel', '.imovel-card', '.imovel-item', '.auction-item',
    '.lot-card', '.lot-item', '.listing-card', '.property-card',
    '.col-lote', '.leilao-item', '.oferta-item',
    '[data-lote]', '[data-imovel]',
    'article.lote', 'article.imovel', 'li.lote', 'li.imovel',
    # Generic Bootstrap cards that contain auction data
    'div.card',
]

# ─── Sites com zero resultados na primeira passagem ────────────────────────────
# Mapeamento: domínio → URL de listagem + seletor específico
PLATFORM_MAP = {
    'damasioleiloes.com.br':   {'listing': '/lotes/imovel',  'selector': 'div.card.shadow-sm'},
    'ricoleiloes.com.br':      {'listing': '/lotes/imovel',  'selector': 'div.card.shadow-sm'},
    'sodresantoro.com.br':     {'listing': '/imoveis/lotes', 'selector': None},
    'destakleiloes.com.br':    {'listing': '/',              'selector': None, 'type': 'destak'},
    'leilaovip.com.br':        {'listing': '/agenda',        'selector': None},
    'vipleiloes.com.br':       {'listing': '/',              'selector': None, 'redirect': 'https://www.leilaovip.com.br/agenda'},
    'gustavoreisleiloes.com.br': {'listing': '/',            'selector': None},
    'impactoleiloes.com.br':   {'listing': '/',              'selector': None},
    'crisleiloes.com.br':      {'listing': '/',              'selector': None},
    'franklinleiloes.com.br':  {'listing': '/',              'selector': None},
}

# ─── Estado global ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_progresso: dict[str, int] = {}
_iniciado_em = datetime.now()


def _registrar(nome: str, qtd: int = 1):
    with _lock:
        _progresso[nome] = _progresso.get(nome, 0) + qtd


def _imprimir_relatorio():
    elapsed = (datetime.now() - _iniciado_em).seconds // 60
    print(f"\n{'='*60}")
    print(f"RELATÓRIO V2 — {datetime.now().strftime('%H:%M:%S')} (+{elapsed}min)")
    print(f"{'='*60}")
    with _lock:
        total = sum(_progresso.values())
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


# ─── Helpers ───────────────────────────────────────────────────────────────────

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
        ('data_encerramento', [r'[Ee]ncerramento', r'[Pp]razo']),
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
        tipo = ('edital' if re.search(r'edital', c, re.I) else
                'matricula' if re.search(r'matr[íi]cula', c, re.I) else
                'laudo' if re.search(r'laudo|avalia', c, re.I) else
                'pdf' if is_pdf else 'documento')
        arquivos.append({'tipo': tipo, 'url': full_url, 'nome': texto[:200] or tipo})
        seen.add(full_url)
        if len(arquivos) >= 15:
            break
    return arquivos


def _card_to_imovel(card, page_url: str, leiloeiro_nome: str, base_url: str = '') -> Optional[dict]:
    """Converte um elemento HTML de card em dict de imóvel."""
    texto = _texto(card)
    if len(texto) < 15:
        return None
    link_el = card.find('a', href=True)
    url_imovel = urljoin(base_url or page_url, link_el['href']) if link_el else page_url
    # Título
    titulo = None
    for t_sel in ['h1','h2','h3','h4','h5','.titulo','.title','.nome','.name',
                  '[class*="titulo"]','[class*="title"]','[class*="nome"]']:
        el = card.select_one(t_sel)
        if el:
            titulo = _texto(el)[:300]
            if titulo and len(titulo) > 5:
                break
    if not titulo:
        titulo = (texto[:200].split('\n')[0]).strip() or 'Imóvel'
    # Skip if not real property
    skip_words = ['veículo', 'carro', 'moto', 'caminhão', 'ônibus', 'equipamento', 'máquina']
    if any(w in titulo.lower() for w in skip_words) and not any(k in titulo.lower() for k in ['apartamento','casa','terreno','imóvel','lote','sala','galpão']):
        return None
    precos = RE_PRECO.findall(texto)
    valor_min  = _parse_preco(precos[0]) if precos else None
    valor_aval = _parse_preco(precos[1]) if len(precos) > 1 else None
    area_m = RE_AREA.search(texto)
    area = Decimal(area_m.group(1).replace(',', '.')) if area_m else None
    quartos_m  = RE_QUARTOS.search(texto)
    banheiros_m = RE_BANHEI.search(texto)
    vagas_m    = RE_VAGAS.search(texto)
    uf_m = RE_UF.search(texto)
    estado = uf_m.group(1) if uf_m else 'SP'
    cep_m = RE_CEP.search(texto)
    datas = _extrair_datas(texto)
    img = card.find('img')
    img_url = None
    if img:
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src', '')
        if src and not src.startswith('data:') and not IMG_SKIP.search(src):
            img_url = urljoin(base_url or page_url, src)
    proc_m = RE_PROCESSO.search(texto)
    return {
        'id_externo': _uid(url_imovel),
        'titulo': titulo[:500],
        'url_original': url_imovel,
        'tipo_imovel': _detectar_tipo(titulo + ' ' + texto[:500]),
        'tipo_leilao': 'judicial' if 'judicial' in texto.lower() else 'extrajudicial',
        'valor_minimo': valor_min,
        'valor_avaliacao': valor_aval,
        'area_total': area,
        'quartos': int(quartos_m.group(1)) if quartos_m else None,
        'banheiros': int(banheiros_m.group(1)) if banheiros_m else None,
        'vagas': int(vagas_m.group(1)) if vagas_m else None,
        'estado': estado,
        'cep': cep_m.group(0) if cep_m else None,
        'data_primeiro_leilao': datas['data_primeiro_leilao'],
        'data_segundo_leilao': datas['data_segundo_leilao'],
        'data_encerramento': datas['data_encerramento'],
        'imagem_principal': img_url,
        'numero_processo': proc_m.group(0) if proc_m else None,
        'leiloeiro': leiloeiro_nome,
    }


def _extrair_cards_soup(soup: BeautifulSoup, page_url: str, nome: str,
                         base_url: str = '', min_cards: int = 2) -> list[dict]:
    """Extrai imóveis de soup com seletores estendidos."""
    results = []
    for sel in CARD_SELECTORS_EXTENDED:
        cards = soup.select(sel)
        if len(cards) >= min_cards:
            for card in cards[:60]:
                item = _card_to_imovel(card, page_url, nome, base_url)
                if item:
                    results.append(item)
            if results:
                print(f"    Selector [{sel}]: {len(results)} imóveis")
                break
    return results


def _filtrar_por_data(imoveis: list[dict]) -> list[dict]:
    validos = []
    for im in imoveis:
        data_1 = im.get('data_primeiro_leilao')
        data_enc = im.get('data_encerramento')
        data_ref = data_1 or data_enc
        if data_ref:
            if isinstance(data_ref, datetime):
                data_ref = data_ref.date()
            if data_ref < date.today():
                continue
        validos.append(im)
    return validos


# ─── Scrapers por plataforma ───────────────────────────────────────────────────

def scrape_damasio_platform(nome: str, site_url: str, pw_page) -> list[dict]:
    """Plataforma Damásio/Rico: /lotes/imovel com div.card.shadow-sm."""
    base = f"https://{urlparse(site_url).netloc}"
    listing_url = base + '/lotes/imovel'
    imoveis = []
    try:
        pw_page.goto(listing_url, wait_until='networkidle', timeout=TIMEOUT)
        html = pw_page.content()
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('div.card.shadow-sm')
        if not cards:
            cards = soup.find_all(lambda t: t.get('class') and 'card' in ' '.join(t.get('class',[])) and 'shadow' in ' '.join(t.get('class',[])))
        print(f"  [{nome}] {listing_url}: {len(cards)} cards")
        for card in cards:
            item = _card_to_imovel(card, listing_url, nome, base)
            if item:
                imoveis.append(item)
        # Try more pages
        for pg in range(2, 6):
            try:
                url_pg = listing_url + f'?page={pg}'
                pw_page.goto(url_pg, wait_until='networkidle', timeout=TIMEOUT)
                soup2 = BeautifulSoup(pw_page.content(), 'html.parser')
                cards2 = soup2.select('div.card.shadow-sm')
                if not cards2:
                    break
                for card in cards2:
                    item = _card_to_imovel(card, url_pg, nome, base)
                    if item:
                        imoveis.append(item)
                if len(cards2) < 2:
                    break
            except Exception:
                break
    except Exception as e:
        print(f"  [{nome}] ERRO Damásio: {e}", file=sys.stderr)
    return imoveis


def scrape_destak(nome: str, site_url: str, pw_page) -> list[dict]:
    """Destak: coleta lote links da home e visita cada um."""
    imoveis = []
    try:
        pw_page.goto('https://www.destakleiloes.com.br', wait_until='networkidle', timeout=TIMEOUT)
        html = pw_page.content()
        soup = BeautifulSoup(html, 'html.parser')
        lote_urls = list(set([a['href'] for a in soup.find_all('a', href=True)
                              if '/lote/' in a['href'] and 'destakleiloes' in a['href']]))
        print(f"  [{nome}] Destak home: {len(lote_urls)} lote links")
        for lurl in lote_urls[:20]:
            try:
                time.sleep(DELAY)
                pw_page.goto(lurl, wait_until='networkidle', timeout=TIMEOUT)
                html2 = pw_page.content()
                soup2 = BeautifulSoup(html2, 'html.parser')
                texto = soup2.get_text(' ', strip=True)
                h1 = soup2.find('h1')
                titulo = _texto(h1)[:300] if h1 else lurl.split('/')[-2].replace('-', ' ').title()
                if not titulo:
                    continue
                precos = RE_PRECO.findall(texto)
                datas = _extrair_datas(texto)
                uf_m = RE_UF.search(texto)
                img_principal, imgs = None, []
                for img in soup2.find_all('img'):
                    src = img.get('src','')
                    if src and not IMG_SKIP.search(src) and 'destakleiloes' in src:
                        img_principal = img_principal or src
                        imgs.append(src)
                arquivos = _extrair_arquivos(soup2, lurl)
                imoveis.append({
                    'id_externo': _uid(lurl),
                    'titulo': titulo,
                    'url_original': lurl,
                    'tipo_imovel': _detectar_tipo(titulo + ' ' + texto[:500]),
                    'tipo_leilao': 'judicial' if 'judicial' in texto.lower() else 'extrajudicial',
                    'valor_minimo': _parse_preco(precos[0]) if precos else None,
                    'valor_avaliacao': _parse_preco(precos[1]) if len(precos) > 1 else None,
                    'estado': uf_m.group(1) if uf_m else 'SP',
                    'cep': (RE_CEP.search(texto).group(0) if RE_CEP.search(texto) else None),
                    'data_primeiro_leilao': datas['data_primeiro_leilao'],
                    'data_segundo_leilao': datas['data_segundo_leilao'],
                    'data_encerramento': datas['data_encerramento'],
                    'imagem_principal': img_principal,
                    'imagens': json.dumps(imgs[:15]),
                    'arquivos': json.dumps(arquivos) if arquivos else None,
                    'leiloeiro': nome,
                })
            except Exception as e:
                print(f"    Destak lote ERRO: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  [{nome}] ERRO Destak: {e}", file=sys.stderr)
    return imoveis


def scrape_playwright_generic(nome: str, site_url: str, pw_page,
                               listing_path: str = '/') -> list[dict]:
    """Scraper Playwright genérico: visita listing_url e extrai cards."""
    base = f"https://{urlparse(site_url).netloc}"
    listing_url = site_url if listing_path == '/' else base + listing_path
    imoveis = []
    try:
        pw_page.goto(listing_url, wait_until='networkidle', timeout=TIMEOUT)
        pw_page.wait_for_timeout(3000)
        html = pw_page.content()
        soup = BeautifulSoup(html, 'html.parser')
        # Try standard selectors
        cards = _extrair_cards_soup(soup, listing_url, nome, base)
        imoveis.extend(cards)
        # Also check for lote links
        if not cards:
            lote_links = list(set([a['href'] for a in soup.find_all('a', href=True)
                                   if re.search(r'/(lote[s]?|imovel|oferta|produto)/', a['href'])
                                   and urlparse(a['href']).netloc in ('', urlparse(site_url).netloc)]))[:20]
            print(f"  [{nome}] {len(lote_links)} lote links encontrados")
            for lurl in lote_links[:15]:
                full = urljoin(base, lurl)
                try:
                    time.sleep(DELAY)
                    pw_page.goto(full, wait_until='networkidle', timeout=TIMEOUT)
                    html2 = pw_page.content()
                    soup2 = BeautifulSoup(html2, 'html.parser')
                    texto = soup2.get_text(' ', strip=True)
                    h1 = soup2.find('h1')
                    titulo = _texto(h1)[:300] if h1 else ''
                    if not titulo or len(titulo) < 5:
                        continue
                    precos = RE_PRECO.findall(texto)
                    datas = _extrair_datas(texto)
                    uf_m = RE_UF.search(texto)
                    arquivos = _extrair_arquivos(soup2, full)
                    imoveis.append({
                        'id_externo': _uid(full),
                        'titulo': titulo,
                        'url_original': full,
                        'tipo_imovel': _detectar_tipo(titulo + ' ' + texto[:500]),
                        'tipo_leilao': 'judicial' if 'judicial' in texto.lower() else 'extrajudicial',
                        'valor_minimo': _parse_preco(precos[0]) if precos else None,
                        'valor_avaliacao': _parse_preco(precos[1]) if len(precos) > 1 else None,
                        'estado': uf_m.group(1) if uf_m else 'SP',
                        'cep': (RE_CEP.search(texto).group(0) if RE_CEP.search(texto) else None),
                        'data_primeiro_leilao': datas['data_primeiro_leilao'],
                        'data_segundo_leilao': datas['data_segundo_leilao'],
                        'data_encerramento': datas['data_encerramento'],
                        'arquivos': json.dumps(arquivos) if arquivos else None,
                        'leiloeiro': nome,
                    })
                except Exception as e:
                    pass
    except Exception as e:
        print(f"  [{nome}] ERRO genérico Playwright: {e}", file=sys.stderr)
    return imoveis


def scrape_leiloeiro_pw(nome: str, site_url: str, pw_page) -> list[dict]:
    """Dispatch para o scraper correto com base no domínio."""
    dom = urlparse(site_url).netloc.replace('www.', '')
    conf = PLATFORM_MAP.get(dom, {})
    ptype = conf.get('type', '')
    redirect = conf.get('redirect', '')
    listing = conf.get('listing', '/')

    actual_url = redirect if redirect else site_url

    print(f"  [{nome}] Plataforma: {dom} | tipo={ptype or 'genérico'} | listing={listing}")

    if ptype == 'destak':
        return scrape_destak(nome, actual_url, pw_page)
    elif dom in ('damasioleiloes.com.br', 'ricoleiloes.com.br'):
        return scrape_damasio_platform(nome, actual_url, pw_page)
    else:
        return scrape_playwright_generic(nome, actual_url, pw_page, listing)


# ─── BANCO DE DADOS ────────────────────────────────────────────────────────────

def _map_tipo_imovel(tipo_str: str) -> "TipoImovel":
    mapa = {'apartamento': TipoImovel.APARTAMENTO, 'casa': TipoImovel.CASA,
            'terreno': TipoImovel.TERRENO, 'comercial': TipoImovel.COMERCIAL,
            'rural': TipoImovel.RURAL, 'galpao': TipoImovel.GALPAO,
            'sala': TipoImovel.SALA, 'vaga': TipoImovel.VAGA}
    return mapa.get(tipo_str, TipoImovel.OUTRO)


def _map_tipo_leilao(tipo_str: str) -> "TipoLeilao":
    return TipoLeilao.JUDICIAL if 'judicial' in tipo_str else TipoLeilao.EXTRAJUDICIAL


def _get_or_create_fonte(session, nome, url_base):
    fonte = session.query(Fonte).filter(Fonte.nome == nome).first()
    if not fonte:
        fonte = Fonte(nome=nome, url_base=url_base, ativo=True)
        session.add(fonte)
        session.flush()
    return fonte


def inserir_imovel(session, fonte_id, leiloeiro_id, dados: dict) -> bool:
    existing = session.query(Imovel).filter(
        Imovel.fonte_id == fonte_id, Imovel.id_externo == dados['id_externo']
    ).first()
    campos = {
        'id_externo': dados['id_externo'], 'fonte_id': fonte_id,
        'titulo': dados.get('titulo', 'Imóvel')[:500],
        'descricao': dados.get('descricao'),
        'url_original': dados.get('url_original', '')[:1000],
        'tipo_imovel': _map_tipo_imovel(dados.get('tipo_imovel', 'outro')),
        'tipo_leilao': _map_tipo_leilao(dados.get('tipo_leilao', 'extrajudicial')),
        'status': StatusLeilao.ABERTO, 'categoria': CategoriaItem.IMOVEL,
        'valor_avaliacao': dados.get('valor_avaliacao'),
        'valor_minimo': dados.get('valor_minimo'),
        'cep': dados.get('cep'), 'estado': dados.get('estado', 'SP'),
        'cidade': dados.get('cidade'), 'endereco_completo': dados.get('endereco_completo'),
        'area_total': dados.get('area_total'), 'quartos': dados.get('quartos'),
        'banheiros': dados.get('banheiros'), 'vagas': dados.get('vagas'),
        'data_primeiro_leilao': dados.get('data_primeiro_leilao'),
        'data_segundo_leilao': dados.get('data_segundo_leilao'),
        'data_encerramento': dados.get('data_encerramento'),
        'imagem_principal': dados.get('imagem_principal'),
        'imagens': dados.get('imagens'), 'arquivos': dados.get('arquivos'),
        'numero_processo': dados.get('numero_processo'),
        'leiloeiro': dados.get('leiloeiro'), 'leiloeiro_id': leiloeiro_id,
    }
    if existing:
        for k, v in campos.items():
            if v is not None:
                setattr(existing, k, v)
        return False
    session.add(Imovel(**campos))
    return True


# ─── PIPELINE PRINCIPAL ────────────────────────────────────────────────────────

def main():
    t = threading.Thread(target=_thread_relatorio, daemon=True)
    t.start()

    print(f"\n{'='*60}")
    print(f"SCRAPER SIND v2 (Playwright) — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    # ── Carregar lista de leiloeiros do CSV gerado pelo v1 ──────────────────────
    csv_files = sorted(CSV_DIR.glob("sind_leiloeiros_*.csv"), reverse=True)
    if not csv_files:
        print("[ERRO] Nenhum CSV sind_leiloeiros encontrado. Rode scraper_sind.py primeiro.", file=sys.stderr)
        return

    leiloeiros = []
    with open(csv_files[0], encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('site'):
                leiloeiros.append({'nome': row['nome'], 'site': row['site']})

    # Deduplica por site (múltiplos leiloeiros no mesmo site)
    sites_vistos: dict[str, str] = {}
    leiloeiros_unicos = []
    for l in leiloeiros:
        dom = urlparse(l['site']).netloc.replace('www.', '')
        if dom not in sites_vistos:
            sites_vistos[dom] = l['nome']
            leiloeiros_unicos.append(l)
        else:
            print(f"  [SKIP DUP] {l['nome']} → {l['site']} (já coberto por {sites_vistos[dom]})")

    print(f"[INFO] {len(leiloeiros_unicos)} sites únicos para visitar com Playwright\n")

    # ── Banco ─────────────────────────────────────────────────────────────────
    session = None
    if DB_AVAILABLE:
        try:
            engine = create_engine(DB_URL_SYNC, echo=False, pool_pre_ping=True)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            print(f"[DB] Conectado: {DB_URL_SYNC.split('@')[-1]}")
        except Exception as e:
            print(f"[AVISO] Falha DB: {e}", file=sys.stderr)

    total_imoveis = 0

    # ── Playwright loop ──────────────────────────────────────────────────────
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])

        for i, lei in enumerate(leiloeiros_unicos, 1):
            nome = lei['nome']
            site = lei['site']
            print(f"\n[{i}/{len(leiloeiros_unicos)}] {nome}")
            print(f"  Site: {site}")

            # Página nova por leiloeiro para evitar contaminação de redirecionamentos
            context = browser.new_context(
                user_agent=UA,
                viewport={'width': 1366, 'height': 768},
                locale='pt-BR',
            )
            pw_page = context.new_page()
            pw_page.set_extra_http_headers({"Accept-Language": "pt-BR,pt;q=0.9"})

            try:
                imoveis = scrape_leiloeiro_pw(nome, site, pw_page)
            except Exception as e:
                print(f"  [ERRO GERAL] {e}", file=sys.stderr)
                imoveis = []
            finally:
                try:
                    pw_page.close()
                    context.close()
                except Exception:
                    pass

            imoveis = _filtrar_por_data(imoveis)

            if not imoveis:
                print(f"  [RESULTADO] 0 imóveis válidos")
                continue

            print(f"  [RESULTADO] {len(imoveis)} imóveis válidos (data >= hoje)")

            if session:
                inseridos_lote = 0
                try:
                    fonte = _get_or_create_fonte(session, nome, site)
                    lei_db = session.query(Leiloeiro).filter(
                        Leiloeiro.nome == nome, Leiloeiro.uf == 'SP'
                    ).first()
                    lei_id = lei_db.id if lei_db else None
                    for im in imoveis:
                        if inserir_imovel(session, fonte.id, lei_id, im):
                            inseridos_lote += 1
                    session.commit()
                    print(f"  [DB] {inseridos_lote} inseridos, {len(imoveis)-inseridos_lote} atualizados")
                except Exception as e:
                    session.rollback()
                    print(f"  [AVISO DB] {e}", file=sys.stderr)

            _registrar(nome, len(imoveis))
            total_imoveis += len(imoveis)
            time.sleep(DELAY)

        browser.close()

    # ── Relatório final ─────────────────────────────────────────────────────
    if session:
        session.close()

    print(f"\n{'='*60}")
    print("COLETA v2 FINALIZADA")
    _imprimir_relatorio()
    print(f"Total de imóveis adicionais: {total_imoveis}")
    print(f"{'='*60}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
