"""
scraper_jucemat.py — Pipeline completo JUCEMAT-MT

1. Coleta lista de leiloeiros de https://www.jucemat.mt.gov.br/leiloeiros
   (página renderizada por JavaScript - usa Playwright)
2. Salva CSV em csv/jucemat_leiloeiros_<data>.csv
3. Para cada leiloeiro com site, visita e extrai imóveis (Playwright genérico)
4. Insere no banco de dados (leiloeiros + imoveis)
5. Reporta progresso a cada 5 minutos
6. Gera relatório de dificuldades ao final

Uso:
    python scraper_jucemat.py
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
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── UTF-8 no Windows ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# ─── Configurações ─────────────────────────────────────────────────────────────
JUCEMAT_URL = "https://www.jucemat.mt.gov.br/leiloeiros"
JUCEMAT_BASE = "https://www.jucemat.mt.gov.br"
LEILOES_DIR = Path(__file__).parent
CSV_DIR     = LEILOES_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)
DB_URL_SYNC = os.getenv("DATABASE_URL_SYNC",
    "postgresql://leilao:leilao123@localhost:5432/leilao_db")

DELAY   = 2.5
TIMEOUT = 25000
REPORT_INTERVAL = 300

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ─── Regex ────────────────────────────────────────────────────────────────────
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

CARD_SELECTORS = [
    'div.card.shadow-sm', 'div.card-lote', '.lote-card', '.lote-item',
    '.card-imovel', '.imovel-card', '.imovel-item', '.auction-item',
    '.lot-card', '.lot-item', '.listing-card', '.property-card',
    '.col-lote', '.leilao-item', '.oferta-item',
    '[data-lote]', '[data-imovel]',
    'article.lote', 'article.imovel',
    'div.card',
]

# ─── Estado global ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_progresso: dict[str, int] = {}
_erros: list[dict] = []          # log de dificuldades
_iniciado_em = datetime.now()


def _registrar(nome: str, qtd: int = 1):
    with _lock:
        _progresso[nome] = _progresso.get(nome, 0) + qtd


def _registrar_erro(tipo: str, url: str, detalhe: str):
    with _lock:
        _erros.append({'tipo': tipo, 'url': url, 'detalhe': detalhe, 'ts': datetime.now().isoformat()})


def _imprimir_relatorio():
    elapsed = (datetime.now() - _iniciado_em).seconds // 60
    print(f"\n{'='*60}")
    print(f"RELATÓRIO JUCEMAT-MT — {datetime.now().strftime('%H:%M:%S')} (+{elapsed}min)")
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


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _uid(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:24]


def _normalizar_site(raw: str) -> Optional[str]:
    if not raw or any(x in raw for x in ['@', 'javascript', 'mailto', 'tel:']):
        return None
    raw = raw.strip().rstrip('/')
    if raw.startswith('http'):
        return raw
    if raw.startswith('www.') or ('.' in raw and len(raw) > 5):
        return 'https://' + raw
    return None


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


# ─────────────────────────────────────────────────────────────────────────────
# 1. PARSE DA PÁGINA JUCEMAT (via Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def parsear_jucemat(pw_page) -> list[dict]:
    print(f"[JUCEMAT] Baixando {JUCEMAT_URL} (Playwright)...")
    try:
        pw_page.goto(JUCEMAT_URL, wait_until='networkidle', timeout=35000)
        pw_page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[ERRO] Falha ao carregar JUCEMAT: {e}", file=sys.stderr)
        _registrar_erro('pagina_principal', JUCEMAT_URL, str(e))
        return []

    html = pw_page.content()
    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.find_all('div', class_=lambda c: c and 'featured-box' in c)
    print(f"[JUCEMAT] {len(cards)} leiloeiros encontrados.")

    leiloeiros = []
    seen_names: set[str] = set()

    for card in cards:
        # Nome
        h2 = card.find('h2')
        nome = re.sub(r'\s+', ' ', h2.get_text(strip=True)).strip() if h2 else ''
        if not nome or len(nome) < 3:
            continue

        # Matrícula
        mat = card.find('span', class_='label')
        matricula = mat.get_text(strip=True) if mat else ''

        # Foto (background-image na div.box-content)
        box = card.find('div', class_='box-content')
        foto_url = None
        if box:
            style = box.get('style', '')
            m_img = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', style)
            if m_img:
                raw_img = m_img.group(1)
                if raw_img and 'avatar' not in raw_img.lower():
                    foto_url = urljoin(JUCEMAT_BASE, raw_img)
        # Fallback: img tag
        if not foto_url:
            img_tag = card.find('img', class_='avatar')
            if img_tag and img_tag.get('src'):
                foto_url = urljoin(JUCEMAT_BASE, img_tag['src'])

        # Campos dos <li>
        endereco = telefone = email = site_raw = posse = situacao = ''
        for li in card.find_all('li'):
            i_tag = li.find('i')
            txt = li.get_text(strip=True)
            if not i_tag:
                continue
            cls = ' '.join(i_tag.get('class', []))
            if 'map-marker' in cls:
                endereco = re.sub(r'^[^\w]*', '', txt).strip()
            elif 'phone' in cls:
                telefone = re.sub(r'^[^\d(]*', '', txt).strip()
            elif 'fa-at' in cls:
                # Email pode ser obfuscado - tenta link mailto
                a_tag = li.find('a', href=True)
                if a_tag and 'mailto:' in a_tag['href']:
                    email = a_tag['href'].replace('mailto:', '').strip()
                else:
                    m_email = re.search(r'[\w.+-]+@[\w.-]+\.\w+', txt)
                    email = m_email.group(0) if m_email else txt.replace('at:', '').strip()
            elif 'link' in cls:
                a_tag = li.find('a', href=True)
                site_raw = a_tag['href'] if a_tag else txt.replace('link:', '').strip()
            elif 'calendar' in cls:
                posse = re.sub(r'Posse\s*:?\s*', '', txt).strip()
            elif 'check' in cls:
                situacao = re.sub(r'Situação\s*:?\s*', '', txt).strip().lower()

        site = _normalizar_site(site_raw)

        # Deduplica por nome+matricula (alguns leiloeiros aparecem com dois sites)
        key = f"{nome}|{matricula}"
        if key in seen_names:
            # Adiciona o segundo site como alternativo ao primeiro
            for l in leiloeiros:
                if l['nome'] == nome and l['matricula'] == matricula and site and not l.get('site'):
                    l['site'] = site
                    break
            continue
        seen_names.add(key)

        leiloeiros.append({
            'nome': nome,
            'matricula': matricula,
            'endereco': endereco,
            'telefone': telefone,
            'email': email,
            'site': site,
            'site_raw': site_raw,
            'foto_url': foto_url,
            'posse': posse,
            'situacao': situacao,
            'uf': 'MT',
            'cidade': 'Cuiabá',
            'junta_comercial': 'JUCEMAT',
            'fonte': JUCEMAT_URL,
        })

    com_site = sum(1 for l in leiloeiros if l.get('site'))
    print(f"[JUCEMAT] {len(leiloeiros)} únicos, {com_site} com site.")
    return leiloeiros


# ─────────────────────────────────────────────────────────────────────────────
# 2. CSV
# ─────────────────────────────────────────────────────────────────────────────

def salvar_csv(leiloeiros: list[dict]) -> Path:
    data_str = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = CSV_DIR / f"jucemat_leiloeiros_{data_str}.csv"
    campos = ["matricula","nome","situacao","cidade","uf","site","email",
              "telefone","endereco","posse","foto_url","junta_comercial","fonte"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(leiloeiros)
    print(f"[CSV] Salvo: {csv_path} ({len(leiloeiros)} linhas)")
    return csv_path


# ─────────────────────────────────────────────────────────────────────────────
# 3. SCRAPING DOS SITES (Playwright genérico)
# ─────────────────────────────────────────────────────────────────────────────

def _card_to_imovel(card, page_url: str, nome: str, base_url: str = '') -> Optional[dict]:
    texto = _texto(card)
    if len(texto) < 15:
        return None
    link_el = card.find('a', href=True)
    url_imovel = urljoin(base_url or page_url, link_el['href']) if link_el else page_url
    titulo = None
    for t_sel in ['h1','h2','h3','h4','h5','.titulo','.title','.nome','[class*="titulo"]']:
        el = card.select_one(t_sel)
        if el:
            titulo = _texto(el)[:300]
            if titulo and len(titulo) > 5:
                break
    if not titulo:
        titulo = (texto[:200].split('\n')[0]).strip() or 'Imóvel'
    skip_words = ['veículo','carro','moto','caminhão','ônibus','equipamento','máquina','sucata']
    if any(w in titulo.lower() for w in skip_words) and not any(k in titulo.lower() for k in ['apartamento','casa','terreno','imóvel','lote','sala','galpão']):
        return None
    precos = RE_PRECO.findall(texto)
    area_m = RE_AREA.search(texto)
    uf_m = RE_UF.search(texto)
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
        'valor_minimo': _parse_preco(precos[0]) if precos else None,
        'valor_avaliacao': _parse_preco(precos[1]) if len(precos) > 1 else None,
        'area_total': Decimal(area_m.group(1).replace(',', '.')) if area_m else None,
        'quartos': int(RE_QUARTOS.search(texto).group(1)) if RE_QUARTOS.search(texto) else None,
        'banheiros': int(RE_BANHEI.search(texto).group(1)) if RE_BANHEI.search(texto) else None,
        'vagas': int(RE_VAGAS.search(texto).group(1)) if RE_VAGAS.search(texto) else None,
        'estado': uf_m.group(1) if uf_m else 'MT',
        'cep': cep_m.group(0) if cep_m else None,
        'data_primeiro_leilao': datas['data_primeiro_leilao'],
        'data_segundo_leilao': datas['data_segundo_leilao'],
        'data_encerramento': datas['data_encerramento'],
        'imagem_principal': img_url,
        'numero_processo': proc_m.group(0) if proc_m else None,
        'leiloeiro': nome,
    }


def _extrair_cards_soup(soup: BeautifulSoup, page_url: str, nome: str, base_url: str = '') -> list[dict]:
    results = []
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if len(cards) >= 2:
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


def scrape_site_leiloeiro(nome: str, site_url: str, browser) -> list[dict]:
    imoveis = []
    base = f"https://{urlparse(site_url).netloc}"
    ctx = browser.new_context(user_agent=UA, viewport={'width': 1366, 'height': 768}, locale='pt-BR')
    pw_page = ctx.new_page()
    pw_page.set_extra_http_headers({"Accept-Language": "pt-BR,pt;q=0.9"})

    try:
        pw_page.goto(site_url, wait_until='networkidle', timeout=TIMEOUT)
        pw_page.wait_for_timeout(2000)
        html = pw_page.content()
        soup = BeautifulSoup(html, 'html.parser')

        # Try card selectors first
        cards = _extrair_cards_soup(soup, site_url, nome, base)
        imoveis.extend(cards)

        # If no cards, look for lote links
        if not cards:
            lote_links = list(set([
                urljoin(base, a['href'])
                for a in soup.find_all('a', href=True)
                if re.search(r'/(lote[s]?|imovel|oferta|produto|leilao|bem)/', a['href'], re.I)
                and urlparse(urljoin(base, a['href'])).netloc == urlparse(base).netloc
            ]))[:20]

            print(f"  [{nome}] {len(lote_links)} lote links")
            for lurl in lote_links[:15]:
                try:
                    time.sleep(DELAY)
                    pw_page.goto(lurl, wait_until='networkidle', timeout=TIMEOUT)
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
                    arquivos = _extrair_arquivos(soup2, lurl)
                    imoveis.append({
                        'id_externo': _uid(lurl),
                        'titulo': titulo,
                        'url_original': lurl,
                        'tipo_imovel': _detectar_tipo(titulo + ' ' + texto[:500]),
                        'tipo_leilao': 'judicial' if 'judicial' in texto.lower() else 'extrajudicial',
                        'valor_minimo': _parse_preco(precos[0]) if precos else None,
                        'valor_avaliacao': _parse_preco(precos[1]) if len(precos) > 1 else None,
                        'estado': uf_m.group(1) if uf_m else 'MT',
                        'cep': (RE_CEP.search(texto).group(0) if RE_CEP.search(texto) else None),
                        'data_primeiro_leilao': datas['data_primeiro_leilao'],
                        'data_segundo_leilao': datas['data_segundo_leilao'],
                        'data_encerramento': datas['data_encerramento'],
                        'arquivos': json.dumps(arquivos) if arquivos else None,
                        'leiloeiro': nome,
                    })
                except Exception as e:
                    _registrar_erro('lote_detalhe', lurl, str(e)[:200])

    except PWTimeout:
        msg = f"Timeout ({TIMEOUT}ms) ao carregar {site_url}"
        print(f"  [{nome}] TIMEOUT")
        _registrar_erro('timeout', site_url, msg)
    except Exception as e:
        err_str = str(e)[:200]
        if 'ERR_NAME_NOT_RESOLVED' in err_str:
            _registrar_erro('dns_falha', site_url, 'DNS não resolvido — domínio offline')
        elif 'ERR_CONNECTION_REFUSED' in err_str:
            _registrar_erro('conexao_recusada', site_url, 'Servidor recusou conexão')
        elif '403' in err_str or '401' in err_str:
            _registrar_erro('acesso_negado', site_url, f'HTTP {err_str[:20]}')
        elif 'cloudflare' in err_str.lower() or '403' in err_str:
            _registrar_erro('cloudflare', site_url, 'Bloqueado por Cloudflare/WAF')
        else:
            _registrar_erro('outro', site_url, err_str)
        print(f"  [{nome}] ERRO: {err_str[:80]}")
    finally:
        try:
            pw_page.close()
            ctx.close()
        except Exception:
            pass

    return imoveis


# ─────────────────────────────────────────────────────────────────────────────
# 4. BANCO DE DADOS
# ─────────────────────────────────────────────────────────────────────────────

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


def _upsert_leiloeiro(session, dados: dict):
    col_names = set(Leiloeiro.__table__.columns.keys())
    existing = session.query(Leiloeiro).filter(
        Leiloeiro.nome == dados['nome'], Leiloeiro.uf == 'MT'
    ).first()
    if existing:
        for k, v in dados.items():
            if k in col_names and v is not None:
                setattr(existing, k, v)
        return existing
    lei = Leiloeiro(**{k: v for k, v in dados.items() if k in col_names})
    session.add(lei)
    session.flush()
    return lei


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
        'cep': dados.get('cep'), 'estado': dados.get('estado', 'MT'),
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


# ─────────────────────────────────────────────────────────────────────────────
# 5. PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    t = threading.Thread(target=_thread_relatorio, daemon=True)
    t.start()

    print(f"\n{'='*60}")
    print(f"SCRAPER JUCEMAT-MT — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])

        # ── 1. Coleta leiloeiros ──────────────────────────────────────────────
        parse_ctx = browser.new_context(user_agent=UA)
        parse_page = parse_ctx.new_page()
        leiloeiros = parsear_jucemat(parse_page)
        parse_page.close()
        parse_ctx.close()

        if not leiloeiros:
            print("[FATAL] Nenhum leiloeiro extraído.", file=sys.stderr)
            browser.close()
            return

        # ── 2. CSV ────────────────────────────────────────────────────────────
        csv_path = salvar_csv(leiloeiros)

        com_site = [l for l in leiloeiros if l.get('site')]
        sem_site = [l for l in leiloeiros if not l.get('site')]
        cancelados = [l for l in leiloeiros if 'cancelad' in l.get('situacao', '').lower()]
        print(f"\n[INFO] {len(com_site)} com site | {len(sem_site)} sem site | {len(cancelados)} cancelados\n")

        # ── 3. Banco ──────────────────────────────────────────────────────────
        session = None
        if DB_AVAILABLE:
            try:
                engine = create_engine(DB_URL_SYNC, echo=False, pool_pre_ping=True)
                SessionLocal = sessionmaker(bind=engine)
                session = SessionLocal()
                print(f"[DB] Conectado: {DB_URL_SYNC.split('@')[-1]}")
            except Exception as e:
                print(f"[AVISO] Falha DB: {e}", file=sys.stderr)

        # ── 4. Upsert leiloeiros ──────────────────────────────────────────────
        if session:
            print("[DB] Upserting leiloeiros JUCEMAT...")
            for l in leiloeiros:
                try:
                    _upsert_leiloeiro(session, {
                        'matricula': l.get('matricula'),
                        'nome': l['nome'],
                        'uf': 'MT',
                        'junta_comercial': 'JUCEMAT',
                        'situacao': l.get('situacao', 'regular'),
                        'cidade': 'Cuiabá',
                        'email': l.get('email'),
                        'telefone': l.get('telefone'),
                        'site': l.get('site'),
                        'logo_url': l.get('foto_url'),
                        'fonte_url': JUCEMAT_URL,
                    })
                except Exception as e:
                    session.rollback()
                    print(f"  [AVISO] {l['nome']}: {e}", file=sys.stderr)
            try:
                session.commit()
                print(f"[DB] {len(leiloeiros)} leiloeiros sincronizados.")
            except Exception as e:
                session.rollback()
                print(f"[AVISO] Commit leiloeiros: {e}", file=sys.stderr)

        # ── 5. Scrape dos sites ───────────────────────────────────────────────
        print(f"\n[SCRAPE] {len(com_site)} sites para visitar...\n")
        total_imoveis = 0

        for i, lei in enumerate(com_site, 1):
            nome = lei['nome']
            site = lei['site']
            sit  = lei.get('situacao', '')
            print(f"[{i}/{len(com_site)}] {nome} | {sit}")
            print(f"  Site: {site}")

            try:
                imoveis = scrape_site_leiloeiro(nome, site, browser)
            except Exception as e:
                print(f"  [ERRO GERAL] {e}", file=sys.stderr)
                _registrar_erro('geral', site, str(e)[:200])
                imoveis = []

            imoveis = _filtrar_por_data(imoveis)

            if not imoveis:
                print(f"  [RESULTADO] 0 imóveis válidos")
                continue

            print(f"  [RESULTADO] {len(imoveis)} imóveis válidos")

            if session:
                inseridos_lote = 0
                try:
                    fonte = _get_or_create_fonte(session, nome, site)
                    lei_db = session.query(Leiloeiro).filter(
                        Leiloeiro.nome == nome, Leiloeiro.uf == 'MT'
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

    if session:
        session.close()

    # ── 6. Relatório final ────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("COLETA FINALIZADA")
    _imprimir_relatorio()
    print(f"CSV: {csv_path}")
    print(f"Total imóveis: {total_imoveis}")
    print(f"{'='*60}\n")

    _gerar_relatorio_dificuldades(leiloeiros, total_imoveis, csv_path)
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────────────────────────
# 6. RELATÓRIO DE DIFICULDADES
# ─────────────────────────────────────────────────────────────────────────────

def _gerar_relatorio_dificuldades(leiloeiros: list[dict], total_imoveis: int, csv_path: Path):
    """Gera seção markdown e appenda ao captura_dados_leiloes_v2.md."""
    from collections import Counter

    # Categoriza erros
    erros_por_tipo = Counter(e['tipo'] for e in _erros)
    leiloeiros_com_site = sum(1 for l in leiloeiros if l.get('site'))
    leiloeiros_cancelados = sum(1 for l in leiloeiros if 'cancelad' in l.get('situacao','').lower())
    leiloeiros_sem_site = sum(1 for l in leiloeiros if not l.get('site'))
    sites_sucesso = len(_progresso)

    # Exemplos de erros por tipo
    erros_exemplos: dict[str, list[str]] = {}
    for e in _erros:
        erros_exemplos.setdefault(e['tipo'], []).append(e['url'])

    agora = datetime.now().strftime('%d/%m/%Y %H:%M')

    md = f"""
---

## 28. Estudo de caso: JUCEMAT-MT ({agora})

Coleta realizada em `https://www.jucemat.mt.gov.br/leiloeiros` — Junta Comercial do Estado de Mato Grosso.

### 28.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros extraídos | {len(leiloeiros)} |
| Com site cadastrado | {leiloeiros_com_site} |
| Sem site cadastrado | {leiloeiros_sem_site} |
| Cancelados/Inativos | {leiloeiros_cancelados} |
| Sites com imóveis encontrados | {sites_sucesso} |
| Total de imóveis inseridos | {total_imoveis} |
| CSV gerado | `{csv_path.name}` |

### 28.2. Principais dificuldades enfrentadas

#### 28.2.1. Página JavaScript-only (SPA não-padrão)

**Problema:** A página `jucemat.mt.gov.br/leiloeiros` retorna HTML quase vazio com `requests`/`httpx`. O conteúdo dos 136 leiloeiros é injetado via JavaScript após o carregamento. `requests` via HTTP direto captura apenas o shell da página.

**Impacto:** Impossível usar scraping HTTP simples; `robots.txt` permite acesso, mas o conteúdo não está no HTML estático.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)` para aguardar a hidratação JS.

**Solução recomendada para escalabilidade:**
```python
# Verificar se existe endpoint JSON interno (Rails/Turbolinks)
r = requests.get('https://www.jucemat.mt.gov.br/leiloeiros.json',
                 headers={'Accept': 'application/json'})
# Status 406 → não há API JSON pública
# Alternativa: usar Playwright com interceptação de XHR
```

#### 28.2.2. Sites de leiloeiros offline (DNS falha)
"""

    if erros_exemplos.get('dns_falha'):
        urls_dns = erros_exemplos['dns_falha'][:5]
        md += f"""
**Problema:** {erros_por_tipo.get('dns_falha', 0)} domínios não resolveram (DNS failure). Sites cadastrados na JUCEMAT mas fora do ar ou com domínio expirado.

**Exemplos:**
"""
        for u in urls_dns:
            md += f"- `{u}`\n"
        md += """
**Solução recomendada:**
1. Tentar com `http://` quando `https://` falha.
2. Verificar WHOIS do domínio — se expirado, marcar leiloeiro como `site_invalido=true` no banco.
3. Agenda de revalidação semanal dos domínios.

```python
# Validação de domínio antes do scraping
import socket
def dominio_existe(url: str) -> bool:
    try:
        socket.getaddrinfo(urlparse(url).netloc, None, timeout=3)
        return True
    except socket.gaierror:
        return False
```
"""

    if erros_exemplos.get('timeout'):
        md += f"""
#### 28.2.3. Timeouts em sites lentos

**Problema:** {erros_por_tipo.get('timeout', 0)} sites ultrapassaram o timeout de {TIMEOUT}ms. Alguns sites de leiloeiros de MT usam hospedagens compartilhadas com alta latência.

**Exemplos:**
"""
        for u in erros_exemplos['timeout'][:5]:
            md += f"- `{u}`\n"
        md += """
**Solução recomendada:**
1. Usar `wait_until='domcontentloaded'` em vez de `'networkidle'` para sites lentos.
2. Retry com timeout progressivo (15s → 30s → 45s).
3. Fallback para `requests` quando o conteúdo está no HTML estático.

```python
def goto_resiliente(page, url, timeouts=(15000, 30000, 45000)):
    for t in timeouts:
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=t)
            return
        except PWTimeout:
            continue
    raise PWTimeout(f"Falhou após {timeouts[-1]}ms")
```
"""

    if erros_exemplos.get('acesso_negado') or erros_exemplos.get('cloudflare'):
        urls_403 = (erros_exemplos.get('acesso_negado', []) + erros_exemplos.get('cloudflare', []))[:5]
        md += f"""
#### 28.2.4. Bloqueio por WAF / Cloudflare (HTTP 403)

**Problema:** {erros_por_tipo.get('acesso_negado', 0) + erros_por_tipo.get('cloudflare', 0)} sites retornaram 403 ou bloquearam o Playwright headless.

**Exemplos:**
"""
        for u in urls_403:
            md += f"- `{u}`\n"
        md += """
**Solução recomendada:**
1. Para Cloudflare Managed Challenge: usar FlareSolverr (ver seção 14).
2. Para WAF simples: adicionar headers `Referer` e `Accept` realistas.
3. `playwright-stealth` para reduzir fingerprint de automação.

```python
from playwright_stealth import Stealth
stealth = Stealth()
await stealth.apply_stealth_async(page)
```
"""

    md += f"""
#### 28.2.5. Leiloeiros cancelados/inativos com site cadastrado

**Problema:** {leiloeiros_cancelados} leiloeiros têm situação "Cancelada" ou "Suspensa" mas ainda têm URL de site registrada. Scraping esses sites consome tempo e pode encontrar dados desatualizados.

**Solução recomendada:**
1. Filtrar leiloeiros com `situacao in ('cancelad', 'suspend')` antes do scraping.
2. Manter no banco com `ativo=false` para histórico.
3. Verificar periodicamente se situação mudou.

```python
# Filtrar apenas regulares para scraping de imóveis
com_site_ativos = [l for l in leiloeiros
                   if l.get('site') and 'cancelad' not in l.get('situacao','').lower()
                   and 'suspend' not in l.get('situacao','').lower()]
```

#### 28.2.6. Sites sem cards de imóvel detectáveis

**Problema:** A maioria dos leiloeiros de MT usa sites customizados sem padrão de card reconhecível pelo scraper genérico. Sites com layout tabular, PDF-only ou sem listagem pública de lotes.

**Causas identificadas:**
- Sites institucionais (apresentação do leiloeiro, sem listagem de lotes)
- Lotes publicados apenas em PDF/edital
- Sistemas proprietários com login obrigatório
- Leiloeiros que operam exclusivamente via plataformas (leilaoimovel.com.br, superbid, etc.)

**Solução recomendada:**
1. **Detectar plataforma** antes de scraping (ver seção 27.6).
2. **Scraping de PDFs**: para sites que só publicam editais, usar `pdfplumber` para extrair dados dos PDFs de edital.
3. **Login quando necessário**: usar `storage_state` do Playwright para persistir sessão (seção 4.3).
4. **Indexadores externos**: buscar o leiloeiro no leilaoimovel.com.br, leilaobrasil.com.br para encontrar seus lotes ativos.

```python
# Busca cross-platform: encontrar lotes do leiloeiro em outros portais
def buscar_leiloeiro_em_portais(nome_leiloeiro: str) -> list[str]:
    portais = [
        f"https://www.leilaoimovel.com.br/busca?q={nome_leiloeiro}",
        f"https://www.superbid.net/busca?q={nome_leiloeiro}",
    ]
    # ... scraping de cada portal
```

### 28.3. Resumo de erros por tipo

| Tipo de erro | Ocorrências | Causa raiz |
|---|---|---|
"""
    tipos_desc = {
        'dns_falha': 'Domínio offline/expirado',
        'timeout': 'Site lento ou sem resposta',
        'acesso_negado': 'HTTP 403 / autenticação',
        'cloudflare': 'Cloudflare / WAF',
        'conexao_recusada': 'Servidor recusa conexão',
        'lote_detalhe': 'Erro ao visitar página de lote',
        'geral': 'Erro genérico de navegação',
        'outro': 'Outros erros de rede',
    }
    for tipo, cnt in sorted(erros_por_tipo.items(), key=lambda x: -x[1]):
        desc = tipos_desc.get(tipo, tipo)
        md += f"| `{tipo}` | {cnt} | {desc} |\n"

    md += f"""
### 28.4. Checklist específico JUCEMAT-MT

1. **Usar Playwright** — página carrega via JavaScript, `requests` captura HTML vazio.
2. **`wait_for_timeout(2000)`** após `networkidle` — a hidratação dos 136 cards leva ~2s.
3. **Deduplicar por nome+matrícula** — alguns leiloeiros têm dois sites listados no mesmo card.
4. **Filtrar cancelados** antes do scraping para economizar tempo.
5. **Verificar DNS** antes de Playwright — evita timeout desnecessário de 25s para domínios mortos.
6. **Sites sem listagem pública**: registrar leiloeiro no banco mas não criar `fonte` sem imóveis.
7. **Foto do leiloeiro**: está no `background-image` do `div.box-content`, não em `<img src>`.
8. **Email obfuscado**: JUCEMAT não usa Cloudflare email obfuscation — emails estão em texto plano no DOM.

### 28.5. Lições aprendidas para próximas juntas comerciais

| Lição | Aplicação |
|---|---|
| Verificar se site é SPA antes de usar `requests` | Usar `len(soup.get_text(strip=True)) < 500` como detector |
| Muitos leiloeiros compartilham plataformas | Detectar plataforma pelo HTML antes de codar seletor específico |
| Sites de leiloeiros estaduais têm alta taxa de domínios inativos | Validar DNS antes de Playwright |
| Leiloeiros inativos inflam lista de sites a visitar | Filtrar por `situacao = 'regular'` na coleta |
| Dados estruturados JUCEMAT são bons (endereço, email, foto) | Aproveitar todos os campos do card para enriquecer tabela `leiloeiros` |
"""

    # Append to the v2 md file
    md_path = Path(__file__).resolve().parent / "captura_dados_leiloes_v2.md"
    try:
        with open(md_path, 'a', encoding='utf-8') as f:
            f.write(md)
        print(f"\n[RELATÓRIO] Apendado em: {md_path}")
    except Exception as e:
        print(f"[AVISO] Não foi possível gravar relatório: {e}", file=sys.stderr)
        # Fallback: salva separado
        rpt_path = LEILOES_DIR / f"relatorio_jucemat_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        with open(rpt_path, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"[RELATÓRIO] Salvo separado: {rpt_path}")


if __name__ == "__main__":
    main()
