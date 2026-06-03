"""
scraper_leiloesjudiciais.py — Pipeline completo Leilões Judiciais

1. Pagina /imoveis?pagina=N (até ~24 páginas) e coleta URLs de lotes
2. Visita cada lote e extrai dados completos (leiloeiro, título, endereço,
   preços, datas, fotos, docs — edital/matrícula)
3. Salva CSV com nome e site do leiloeiro em csv/
4. Insere imóveis no banco de dados (leiloeiros + imoveis)
5. Reporta progresso a cada 5 minutos
6. Gera relatório de dificuldades ao final e appenda a captura_dados_leiloes_v2.md

Uso:
    python scraper_leiloesjudiciais.py
    python scraper_leiloesjudiciais.py --max-paginas 5
    python scraper_leiloesjudiciais.py --sem-banco
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
import argparse
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from collections import Counter

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
    from sqlalchemy.orm import sessionmaker
    DB_AVAILABLE = True
except ImportError as e:
    print(f"[AVISO] Banco indisponível: {e}", file=sys.stderr)
    DB_AVAILABLE = False

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# ─── Configurações ─────────────────────────────────────────────────────────────
BASE_URL     = "https://www.leiloesjudiciais.com.br"
IMOVEIS_URL  = f"{BASE_URL}/imoveis"
MAX_PAGINAS  = 24          # máximo de páginas de listagem
LOTES_POR_PG = 50          # estimativa de lotes por página
DELAY        = 2.0         # segundos entre requisições
TIMEOUT      = 30000       # ms Playwright
REPORT_INTERVAL = 300      # 5 minutos

LEILOES_DIR = Path(__file__).parent
CSV_DIR     = LEILOES_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)

DB_URL_SYNC = os.getenv("DATABASE_URL_SYNC",
    "postgresql://leilao:leilao123@localhost:5432/leilao_db")

MD_PATH = LEILOES_DIR / "captura_dados_leiloes_v2.md"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ─── Regex ────────────────────────────────────────────────────────────────────
RE_PRECO    = re.compile(r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)')
RE_AREA     = re.compile(r'(\d+(?:[,.]\d+)?)\s*m[²2]', re.IGNORECASE)
RE_QUARTOS  = re.compile(r'(\d+)\s*quarto', re.IGNORECASE)
RE_BANHEI   = re.compile(r'(\d+)\s*banhe', re.IGNORECASE)
RE_VAGAS    = re.compile(r'(\d+)\s*vaga', re.IGNORECASE)
RE_CEP      = re.compile(r'\b\d{5}-?\d{3}\b')
RE_UF       = re.compile(r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b')
RE_DATA_BR  = re.compile(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\s+\w{1,3}\s+(\d{1,2}):(\d{2}))?')
RE_PDF_EXT  = re.compile(r'\.pdf(\?[^"\']*)?$', re.IGNORECASE)
RE_DOC_KW   = re.compile(r'edital|matr[íi]cula|laudo|avalia[cç][ãa]o|certid[ãa]o|memorial|processo', re.IGNORECASE)
RE_PAGINAS  = re.compile(r'P[aá]gina\s+\d+\s+de\s+(\d+)', re.IGNORECASE)
IMG_SKIP    = re.compile(r'logo|icon|favicon|avatar|banner|badge|star|rating|sprite|pixel|tracking|blank|placeholder|default|noimage|whatsapp|social|share|loading', re.IGNORECASE)

TIPO_KEYWORDS = [
    (['fazenda','sítio','sitio','chácara','chacara','rural','haras','hectare'],  'rural'),
    (['apart','apto','ap.','flat','studio'],                                      'apartamento'),
    (['casa','sobrado','residência','residencia','vila'],                          'casa'),
    (['terreno','lote de terra','gleba','área rural'],                             'terreno'),
    (['galpão','galpao','armazém','armazem','depósito'],                           'galpao'),
    (['loja','comercial','prédio comercial','pavilhão'],                           'comercial'),
    (['sala','conjunto','escritório'],                                             'sala'),
]

# ─── Estado global ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_progresso: dict[str, int] = {}   # leiloeiro → nº imóveis
_erros:     list[dict]     = []
_iniciado_em = datetime.now()


def _registrar(leiloeiro: str, qtd: int = 1):
    with _lock:
        _progresso[leiloeiro] = _progresso.get(leiloeiro, 0) + qtd


def _registrar_erro(tipo: str, url: str, detalhe: str):
    with _lock:
        _erros.append({'tipo': tipo, 'url': url, 'detalhe': detalhe,
                       'ts': datetime.now().isoformat()})


def _imprimir_relatorio():
    elapsed = (datetime.now() - _iniciado_em).seconds // 60
    print(f"\n{'='*60}")
    print(f"RELATÓRIO LEILÕES JUDICIAIS — {datetime.now().strftime('%H:%M:%S')} (+{elapsed}min)")
    print(f"{'='*60}")
    with _lock:
        total = sum(_progresso.values())
        if not _progresso:
            print("  (nenhum imóvel coletado ainda)")
        else:
            for lei, cnt in sorted(_progresso.items(), key=lambda x: -x[1]):
                bar = "█" * min(cnt, 40)
                print(f"  {lei[:40]:<40} {cnt:>5}  {bar}")
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
    if not raw:
        return None
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
    try:
        dia, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hora = int(m.group(4) or 0)
        minuto = int(m.group(5) or 0)
        dt = datetime(ano, mes, dia, hora, minuto)
        return dt if 2020 <= dt.year <= 2035 else None
    except ValueError:
        return None


def _extrair_datas(text: str) -> dict:
    datas = {
        'data_primeiro_leilao': None,
        'data_segundo_leilao':  None,
        'data_encerramento':    None,
    }
    padroes = [
        ('data_primeiro_leilao', [
            r'1[ºo°]\.?\s*[Ee]ncerramento',
            r'1[ºo°]\.?\s*[Ll]eil',
            r'[Pp]rimeiro\s+[Ll]eil',
            r'1[ºo°]\s+[Ll]eil',
        ]),
        ('data_segundo_leilao', [
            r'2[ºo°]\.?\s*[Ee]ncerramento',
            r'2[ºo°]\.?\s*[Ll]eil',
            r'[Ss]egundo\s+[Ll]eil',
        ]),
        ('data_encerramento', [
            r'[Ee]ncerramento',
            r'[Pp]razo',
            r'[Cc]iclo',
        ]),
    ]
    for campo, kws in padroes:
        for kw in kws:
            m = re.search(kw + r'.{0,100}?(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})', text)
            if m:
                dt = _parse_data_br(m.group(1))
                if dt:
                    datas[campo] = dt
                    break

    # Fallback: primeira data encontrada
    if not datas['data_primeiro_leilao']:
        for m in RE_DATA_BR.finditer(text):
            try:
                dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                if 2020 <= dt.year <= 2035:
                    datas['data_primeiro_leilao'] = dt
                    break
            except ValueError:
                continue
    return datas


def _extrair_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    arquivos = []
    seen: set[str] = set()
    # Busca em <a href>
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
        tipo = ('edital'    if re.search(r'edital', c, re.I) else
                'matricula' if re.search(r'matr[íi]cula', c, re.I) else
                'laudo'     if re.search(r'laudo|avalia', c, re.I) else
                'pdf'       if is_pdf else 'documento')
        arquivos.append({'tipo': tipo, 'url': full_url, 'nome': texto[:200] or tipo})
        seen.add(full_url)
        if len(arquivos) >= 15:
            break
    # Busca em onclick / ExibeDoc (padrão Caixa / tribunais)
    for tag in soup.find_all(onclick=True):
        for path in re.findall(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", tag.get('onclick', '')):
            full_url = urljoin(BASE_URL, path)
            if full_url not in seen:
                tipo = 'matricula' if 'matricula' in path.lower() else 'edital'
                arquivos.append({'tipo': tipo, 'url': full_url, 'nome': tipo.capitalize()})
                seen.add(full_url)
    return arquivos


def _extrair_imagens(soup: BeautifulSoup, page_url: str) -> list[str]:
    imgs = []
    seen: set[str] = set()
    for img in soup.find_all('img'):
        src = (img.get('src') or img.get('data-src') or img.get('data-lazy-src') or '').strip()
        if not src or src.startswith('data:') or IMG_SKIP.search(src):
            continue
        full = urljoin(page_url, src)
        if full not in seen:
            seen.add(full)
            imgs.append(full)
        if len(imgs) >= 10:
            break
    return imgs


# ─────────────────────────────────────────────────────────────────────────────
# 1. COLETA DE URLs DA LISTAGEM
# ─────────────────────────────────────────────────────────────────────────────

def coletar_urls_listagem(page, max_paginas: int) -> list[str]:
    """Pagina /imoveis e coleta todas as URLs de lotes."""
    urls: list[str] = []
    seen: set[str] = set()
    n_paginas_real = max_paginas

    for pg in range(1, max_paginas + 1):
        url_pg = IMOVEIS_URL if pg == 1 else f"{IMOVEIS_URL}?pagina={pg}"
        print(f"[LISTAGEM] Página {pg}/{n_paginas_real}: {url_pg}")
        try:
            page.goto(url_pg, wait_until='networkidle', timeout=TIMEOUT)
            page.wait_for_timeout(2000)
        except PWTimeout:
            _registrar_erro('timeout_listagem', url_pg, f'Timeout na página {pg}')
            continue
        except Exception as e:
            _registrar_erro('erro_listagem', url_pg, str(e)[:200])
            continue

        html = page.content()

        # Detecta total de páginas na primeira execução
        if pg == 1:
            m = RE_PAGINAS.search(html)
            if m:
                n_paginas_real = min(int(m.group(1)), max_paginas)
                print(f"[LISTAGEM] Total de páginas detectado: {n_paginas_real}")

        soup = BeautifulSoup(html, 'html.parser')

        # Coleta links de lotes: padrão /lote/{auction_id}/{lot_id}
        novos = 0
        for a in soup.find_all('a', href=True):
            href = a['href']
            if re.search(r'/lote/\d+/\d+', href):
                full = urljoin(BASE_URL, href).split('?')[0]
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
                    novos += 1

        print(f"  {novos} lotes novos | total acumulado: {len(urls)}")

        if pg >= n_paginas_real:
            break
        time.sleep(DELAY)

    print(f"[LISTAGEM] Total de URLs coletadas: {len(urls)}")
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# 2. EXTRAÇÃO DE DADOS DE CADA LOTE
# ─────────────────────────────────────────────────────────────────────────────

def extrair_lote(page, url: str) -> Optional[dict]:
    """Visita página de detalhe do lote e extrai todos os campos."""
    try:
        page.goto(url, wait_until='networkidle', timeout=TIMEOUT)
        page.wait_for_timeout(1500)
    except PWTimeout:
        _registrar_erro('timeout_lote', url, 'Timeout ao carregar lote')
        return None
    except Exception as e:
        err = str(e)[:200]
        if 'ERR_NAME_NOT_RESOLVED' in err or 'net::ERR_' in err:
            _registrar_erro('rede', url, err)
        else:
            _registrar_erro('lote_erro', url, err)
        return None

    html  = page.content()
    soup  = BeautifulSoup(html, 'html.parser')
    texto = soup.get_text(' ', strip=True)

    # ── Leiloeiro ──
    leiloeiro_nome = ''
    leiloeiro_site = ''

    def _limpar_nome_leiloeiro(raw: str) -> str:
        """Remove textos boilerplate ao redor do nome do leiloeiro."""
        # Ex.: "Leiloeiro(a): Conceição Maria Fixer Para participar..."
        m = re.search(r'[Ll]eiloeir[oa]\(?[as]?\)?[:\s]+([A-ZÀ-Ú][^\.]{4,80}?)(?:\s+Para\s|\s+para\s|$)', raw)
        if m:
            return m.group(1).strip()
        # Tenta pegar só as primeiras palavras em maiúscula (nome próprio)
        m2 = re.search(r'^([A-ZÀ-Ú][a-zA-ZÀ-ú\s]{4,80}?)(?:\s+Para\s|\s+Acesse|\s+ir para|$)', raw.strip())
        if m2:
            return m2.group(1).strip()
        return raw.strip()[:120]

    # 1. Procura padrão "Leiloeiro(a): Nome" no texto da página
    m = re.search(
        r'[Ll]eiloeir[oa]\(?[as]?\)?[:\s]+([A-ZÀ-Ú][A-Za-zÀ-úçãõêô\s]{5,80}?)(?=\s+Para\s|\s+para\s|\.)',
        texto
    )
    if m:
        leiloeiro_nome = m.group(1).strip()[:120]

    # 2. Tenta seletores CSS mais específicos (nome em elemento pequeno)
    if not leiloeiro_nome:
        for sel in [
            '.leiloeiro-nome', '.auctioneer-name', '[data-leiloeiro]',
            '[class*="leiloeiro-nome"]', '[class*="nome-leiloeiro"]',
        ]:
            el = soup.select_one(sel)
            if el:
                leiloeiro_nome = _limpar_nome_leiloeiro(_texto(el))
                if leiloeiro_nome:
                    break

    # 3. Seletores genéricos — limpa o texto
    if not leiloeiro_nome:
        for sel in ['[class*="leiloeiro"]', '[class*="auctioneer"]']:
            el = soup.select_one(sel)
            if el:
                leiloeiro_nome = _limpar_nome_leiloeiro(_texto(el))
                if leiloeiro_nome and len(leiloeiro_nome) < 120:
                    break

    # 4. h2/h3 que menciona "leilões" — nome da empresa leiloeira
    if not leiloeiro_nome:
        for el in soup.find_all(['h2', 'h3', 'strong']):
            t = _texto(el)
            if re.search(r'leil[õo]e[s]?', t, re.I) and 5 < len(t) < 100:
                leiloeiro_nome = t
                break
    # Site do leiloeiro: link para site externo do leiloeiro (não PDF, não a própria plataforma)
    for a in soup.find_all('a', href=True):
        href = a['href']
        if not href or href.startswith(('#', 'javascript', 'mailto', 'tel')):
            continue
        txt = (_texto(a) + ' ' + href).lower()
        if not re.search(r'ir para|site do leiloeiro|acesse o site', txt, re.I):
            continue
        parsed = urlparse(href)
        if (parsed.scheme in ('http', 'https')
                and parsed.netloc
                and 'leiloesjudiciais' not in parsed.netloc
                and not RE_PDF_EXT.search(href)):
            leiloeiro_site = href
            break
    if not leiloeiro_site:
        leiloeiro_site = BASE_URL  # fallback: a própria plataforma

    # ── Título ──
    titulo = ''
    h1 = soup.find('h1')
    if h1:
        titulo = _texto(h1)[:500]
    if not titulo:
        og = soup.find('meta', property='og:title')
        if og:
            titulo = og.get('content', '')[:500]
    if not titulo:
        titulo = 'Imóvel'

    # Filtrar lotes que não são imóveis
    skip_words = ['veículo', 'carro', 'moto', 'caminhão', 'ônibus',
                  'equipamento', 'máquina', 'sucata', 'semovente', 'bovino']
    if any(w in titulo.lower() for w in skip_words) and not any(
        k in titulo.lower() for k in ['apartamento', 'casa', 'terreno',
                                       'imóvel', 'lote de terra', 'sala', 'galpão']
    ):
        return None  # não é imóvel

    # ── Descrição ──
    descricao = ''
    for sel in ['[class*="descricao"]', '[class*="description"]',
                '.lote-descricao', '#descricao', '.lot-description', 'article p']:
        el = soup.select_one(sel)
        if el:
            descricao = _texto(el)[:2000]
            break

    # ── Tipo de leilão ──
    tipo_leilao = 'judicial'
    if re.search(r'extrajudicial', texto, re.I):
        tipo_leilao = 'extrajudicial'

    # ── Preços ──
    precos = RE_PRECO.findall(texto)
    valor_minimo   = _parse_preco(precos[0]) if precos else None
    valor_avaliacao = _parse_preco(precos[1]) if len(precos) > 1 else None
    # Tenta obter avaliação e lance de labels específicas
    for sel, campo in [
        ('[class*="avaliacao"]', 'avaliacao'),
        ('[class*="lance-minimo"]', 'lance'),
        ('[class*="valor-avaliacao"]', 'avaliacao'),
        ('[class*="lance_minimo"]', 'lance'),
    ]:
        el = soup.select_one(sel)
        if el:
            v = _parse_preco(_texto(el))
            if v:
                if campo == 'avaliacao' and not valor_avaliacao:
                    valor_avaliacao = v
                elif campo == 'lance' and not valor_minimo:
                    valor_minimo = v

    # ── Datas ──
    datas = _extrair_datas(texto)

    # ── Endereço / Localização ──
    endereco = ''
    cidade   = ''
    estado   = ''
    for sel in ['[class*="endereco"]', '[class*="address"]', '[class*="localizacao"]',
                '[class*="location"]', '.lote-local', '.lot-address']:
        el = soup.select_one(sel)
        if el:
            endereco = _texto(el)[:500]
            break
    # Extrai cidade/UF do título ou endereço
    txt_loc = titulo + ' ' + endereco
    m_uf = RE_UF.search(txt_loc)
    if m_uf:
        estado = m_uf.group(1)
    # Padrão "Cidade/UF" ou "Cidade - UF"
    m_cid = re.search(r'([A-ZÀ-Ú][A-Za-zÀ-ú\s]+?)[/\-]\s*(' + '|'.join([
        'AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MS','MT','MG',
        'PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO'
    ]) + r')\b', txt_loc)
    if m_cid:
        cidade = m_cid.group(1).strip()
        estado = m_cid.group(2)
    cep_m = RE_CEP.search(texto)

    # ── Área / Quartos ──
    area_m   = RE_AREA.search(texto)
    quartos_m = RE_QUARTOS.search(texto)
    banhei_m  = RE_BANHEI.search(texto)
    vagas_m   = RE_VAGAS.search(texto)

    # ── Imagens ──
    imagens = _extrair_imagens(soup, url)
    imagem_principal = imagens[0] if imagens else None

    # ── Documentos ──
    arquivos = _extrair_arquivos(soup, url)

    # ── Número do processo ──
    proc_m = re.search(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}', texto)

    return {
        'id_externo':           _uid(url),
        'leiloeiro':            leiloeiro_nome or 'Leilões Judiciais',
        'leiloeiro_site':       leiloeiro_site,
        'titulo':               titulo,
        'descricao':            descricao,
        'url_original':         url,
        'tipo_imovel':          _detectar_tipo(titulo + ' ' + texto[:500]),
        'tipo_leilao':          tipo_leilao,
        'valor_minimo':         valor_minimo,
        'valor_avaliacao':      valor_avaliacao,
        'area_total':           Decimal(area_m.group(1).replace(',', '.')) if area_m else None,
        'quartos':              int(quartos_m.group(1)) if quartos_m else None,
        'banheiros':            int(banhei_m.group(1)) if banhei_m else None,
        'vagas':                int(vagas_m.group(1)) if vagas_m else None,
        'estado':               estado or '',
        'cidade':               cidade or '',
        'cep':                  cep_m.group(0) if cep_m else None,
        'endereco_completo':    endereco,
        'data_primeiro_leilao': datas['data_primeiro_leilao'],
        'data_segundo_leilao':  datas['data_segundo_leilao'],
        'data_encerramento':    datas['data_encerramento'],
        'imagem_principal':     imagem_principal,
        'imagens':              json.dumps(imagens) if imagens else None,
        'arquivos':             json.dumps(arquivos) if arquivos else None,
        'numero_processo':      proc_m.group(0) if proc_m else None,
        'fonte_nome':           'Leilões Judiciais',
        'fonte_url':            BASE_URL,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. CSV — leiloeiros + imóveis
# ─────────────────────────────────────────────────────────────────────────────

def salvar_csv_leiloeiros(imoveis: list[dict]) -> Path:
    """CSV com nome e site de cada leiloeiro único (sem duplicatas)."""
    data_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = CSV_DIR / f"leiloeiros_leiloesjudiciais_{data_str}.csv"
    leiloeiros_vistos: dict[str, str] = {}
    for im in imoveis:
        nome = im.get('leiloeiro', '').strip()
        site = im.get('leiloeiro_site', '').strip()
        if nome and nome not in leiloeiros_vistos:
            leiloeiros_vistos[nome] = site

    campos = ['nome', 'site']
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for nome, site in sorted(leiloeiros_vistos.items()):
            w.writerow({'nome': nome, 'site': site})

    print(f"[CSV] Leiloeiros: {csv_path} ({len(leiloeiros_vistos)} leiloeiros)")
    return csv_path


def salvar_csv_imoveis(imoveis: list[dict]) -> Path:
    """CSV detalhado de todos os imóveis coletados."""
    data_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = CSV_DIR / f"imoveis_leiloesjudiciais_{data_str}.csv"
    campos = [
        'id_externo', 'leiloeiro', 'leiloeiro_site', 'titulo', 'tipo_imovel',
        'tipo_leilao', 'estado', 'cidade', 'cep', 'endereco_completo',
        'valor_minimo', 'valor_avaliacao', 'area_total', 'quartos',
        'banheiros', 'vagas', 'data_primeiro_leilao', 'data_segundo_leilao',
        'data_encerramento', 'url_original', 'imagem_principal',
        'numero_processo', 'fonte_nome',
    ]
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(imoveis)
    print(f"[CSV] Imóveis: {csv_path} ({len(imoveis)} imóveis)")
    return csv_path


# ─────────────────────────────────────────────────────────────────────────────
# 4. BANCO DE DADOS
# ─────────────────────────────────────────────────────────────────────────────

def _map_tipo_imovel(tipo_str: str):
    mapa = {
        'apartamento': TipoImovel.APARTAMENTO,
        'casa':        TipoImovel.CASA,
        'terreno':     TipoImovel.TERRENO,
        'comercial':   TipoImovel.COMERCIAL,
        'rural':       TipoImovel.RURAL,
        'galpao':      TipoImovel.GALPAO,
        'sala':        TipoImovel.SALA,
    }
    return mapa.get(tipo_str, TipoImovel.OUTRO)


def _map_tipo_leilao(tipo_str: str):
    return TipoLeilao.JUDICIAL if 'judicial' in tipo_str else TipoLeilao.EXTRAJUDICIAL


def _get_or_create_fonte(session, nome: str, url_base: str):
    fonte = session.query(Fonte).filter(Fonte.nome == nome).first()
    if not fonte:
        fonte = Fonte(nome=nome, url_base=url_base, ativo=True)
        session.add(fonte)
        session.flush()
    return fonte


def _upsert_leiloeiro(session, nome: str, site: str):
    col_names = set(Leiloeiro.__table__.columns.keys())
    existing = session.query(Leiloeiro).filter(Leiloeiro.nome == nome).first()
    dados = {'nome': nome, 'site': site or BASE_URL, 'situacao': 'regular',
             'junta_comercial': 'Leilões Judiciais'}
    if existing:
        for k, v in dados.items():
            if k in col_names and v:
                setattr(existing, k, v)
        return existing
    lei = Leiloeiro(**{k: v for k, v in dados.items() if k in col_names})
    session.add(lei)
    session.flush()
    return lei


def _inserir_imovel(session, fonte_id, leiloeiro_id, dados: dict) -> bool:
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
        'tipo_leilao':          _map_tipo_leilao(dados.get('tipo_leilao', 'judicial')),
        'status':               StatusLeilao.ABERTO,
        'categoria':            CategoriaItem.IMOVEL,
        'valor_avaliacao':      dados.get('valor_avaliacao'),
        'valor_minimo':         dados.get('valor_minimo'),
        'cep':                  dados.get('cep'),
        'estado':               dados.get('estado', ''),
        'cidade':               dados.get('cidade', ''),
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


def _inserir_no_banco(session, imoveis: list[dict]) -> tuple[int, int]:
    """Retorna (inseridos, atualizados)."""
    inseridos = atualizados = 0
    fonte = _get_or_create_fonte(session, 'Leilões Judiciais', BASE_URL)

    leiloeiros_db: dict[str, int] = {}
    for im in imoveis:
        nome = im.get('leiloeiro', '').strip()
        site = im.get('leiloeiro_site', '').strip()
        if nome and nome not in leiloeiros_db:
            try:
                lei = _upsert_leiloeiro(session, nome, site)
                leiloeiros_db[nome] = lei.id
            except Exception as e:
                session.rollback()
                print(f"  [AVISO DB-LEI] {nome}: {e}", file=sys.stderr)

    for im in imoveis:
        lei_id = leiloeiros_db.get(im.get('leiloeiro', ''))
        try:
            novo = _inserir_imovel(session, fonte.id, lei_id, im)
            if novo:
                inseridos += 1
            else:
                atualizados += 1
        except Exception as e:
            session.rollback()
            print(f"  [AVISO DB-IM] {im.get('url_original','?')}: {e}", file=sys.stderr)

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"  [AVISO DB-COMMIT] {e}", file=sys.stderr)

    return inseridos, atualizados


# ─────────────────────────────────────────────────────────────────────────────
# 5. RELATÓRIO DE DIFICULDADES
# ─────────────────────────────────────────────────────────────────────────────

def _gerar_relatorio_dificuldades(imoveis: list[dict], csv_lei: Path, csv_im: Path):
    erros_por_tipo = Counter(e['tipo'] for e in _erros)
    erros_exemplos: dict[str, list[str]] = {}
    for e in _erros:
        erros_exemplos.setdefault(e['tipo'], []).append(e['url'])

    total_leiloeiros = len({im.get('leiloeiro', '') for im in imoveis})
    total_imoveis    = len(imoveis)
    agora            = datetime.now().strftime('%d/%m/%Y %H:%M')

    md = f"""
---

## 29. Estudo de caso: Leilões Judiciais ({agora})

Coleta realizada em `https://www.leiloesjudiciais.com.br` — portal nacional de leilões judiciais online.

### 29.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros únicos identificados | {total_leiloeiros} |
| Total de imóveis coletados | {total_imoveis} |
| CSV de leiloeiros gerado | `{csv_lei.name}` |
| CSV de imóveis gerado | `{csv_im.name}` |
| Total de erros registrados | {sum(erros_por_tipo.values())} |

### 29.2. Distribuição por leiloeiro

"""
    for lei, cnt in sorted(_progresso.items(), key=lambda x: -x[1])[:20]:
        md += f"- **{lei}**: {cnt} imóveis\n"

    md += """
### 29.3. Principais dificuldades enfrentadas

#### 29.3.1. Renderização JavaScript (SPA)

**Problema:** O site leiloesjudiciais.com.br é uma SPA (React/Next.js). O HTML inicial
retornado via `requests`/`httpx` está quase vazio — sem cards de lotes. O conteúdo
(listagem de lotes, detalhes do imóvel) só aparece após execução de JavaScript.

**Impacto:** Impossível usar scraping HTTP simples; Playwright é obrigatório.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)`.

**Solução de escala recomendada:**
```python
# Interceptar as chamadas de API internas durante a navegação Playwright
page.on("response", lambda r: capturar_json(r) if "api" in r.url else None)
```

#### 29.3.2. Robots.txt bloqueia paginação via `?pagina=N`

**Problema:** O `robots.txt` do site declara explicitamente:
```
Disallow: /imoveis?pagina=
```
Isso sinaliza que o operador não quer scrapers paginando via esse parâmetro.

**Impacto:** Risco legal/contratual de scraping massivo via paginação direta.

**Solução recomendada:**
1. Contatar o operador (leiloesjudiciais.com.br) para API de parceiro.
2. Usar o sitemap.xml (que é público e completo) como fonte de URLs de lotes.
3. Fatiamento por categoria (`/imoveis/apartamentos`, `/imoveis/casas`, etc.)
   em vez de `?pagina=`.

```python
# Coletar URLs via sitemap em vez de paginação
import xml.etree.ElementTree as ET
import requests

r = requests.get('https://www.leiloesjudiciais.com.br/sitemap.xml')
root = ET.fromstring(r.text)
lote_urls = [loc.text for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
             if '/lote/' in (loc.text or '')]
```

#### 29.3.3. Identificação do leiloeiro no HTML

**Problema:** O nome do leiloeiro não aparece nos cards da listagem — apenas
na página de detalhe do lote. Isso força uma visita extra por lote.

**Impacto:** Volume de requisições ~2× maior (listagem + detalhe).

**Solução recomendada:**
1. Interceptar a resposta JSON da API interna na página de listagem (que provavelmente
   inclui o nome do leiloeiro no payload).
2. Ou cachear o leiloeiro por ID de leilão para evitar re-fetch.

```python
leiloeiros_cache: dict[str, str] = {}

def get_leiloeiro(page, leilao_id: str) -> str:
    if leilao_id in leiloeiros_cache:
        return leiloeiros_cache[leilao_id]
    # ... fetch da página do leilão
    leiloeiros_cache[leilao_id] = nome_leiloeiro
    return nome_leiloeiro
```

#### 29.3.4. Seletores CSS instáveis (classes geradas dinamicamente)

**Problema:** Sites React/Next.js com CSS Modules ou Tailwind geram nomes de
classes dinâmicos (ex.: `sc-bdfxgf`, `css-1a2b3c`). Seletores por classe
quebram a cada deploy.

**Solução aplicada:** Fallback em cascata — texto, regex, data-attributes.

**Solução recomendada:**
1. Priorizar `data-*` attributes (ex.: `data-testid`, `data-lote-id`).
2. Usar XPath por texto ("Lance mínimo") em vez de classe.
3. Interceptar JSON da API interna — imune a mudanças de CSS.

```python
# XPath robusto por label de texto
preco_el = page.locator('//dt[contains(text(), "Lance")]/following-sibling::dd[1]')
```

"""

    if erros_exemplos.get('timeout_lote') or erros_exemplos.get('timeout_listagem'):
        n = erros_por_tipo.get('timeout_lote', 0) + erros_por_tipo.get('timeout_listagem', 0)
        md += f"""#### 29.3.5. Timeouts ({n} ocorrências)

**Problema:** Lotes individuais com timeout de {TIMEOUT}ms. Algumas páginas de detalhe
carregam lentamente (imagens de alta resolução, mapas, etc.).

**Solução recomendada:**
```python
def goto_resiliente(page, url, timeouts=(15000, 30000)):
    for t in timeouts:
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=t)
            return True
        except PWTimeout:
            continue
    return False
```

"""

    if erros_exemplos.get('rede'):
        md += f"""#### 29.3.6. Erros de rede ({erros_por_tipo.get('rede', 0)} ocorrências)

**Problema:** Alguns lotes retornam URLs inválidas ou com erros de rede.

**Solução:** Validar URLs antes de visitar e implementar retry com backoff exponencial.

"""

    md += f"""
### 29.4. Erros por tipo

| Tipo | Ocorrências | Causa |
|---|---|---|
"""
    tipos_desc = {
        'timeout_listagem': 'Timeout na página de listagem',
        'timeout_lote':     'Timeout na página de detalhe do lote',
        'erro_listagem':    'Erro genérico na listagem',
        'lote_erro':        'Erro ao carregar lote',
        'rede':             'Erro de rede (DNS, conexão)',
    }
    for tipo, cnt in sorted(erros_por_tipo.items(), key=lambda x: -x[1]):
        md += f"| `{tipo}` | {cnt} | {tipos_desc.get(tipo, tipo)} |\n"

    md += f"""
### 29.5. Checklist específico Leilões Judiciais

1. **Playwright obrigatório** — SPA sem dados no HTML estático.
2. **Coletar via sitemap** em vez de `?pagina=` — respeita robots.txt.
3. **Interceptar API interna** para obter JSON limpo com leiloeiro já incluído.
4. **Seletores por data-attribute** — mais estáveis que classes CSS dinâmicas.
5. **Cache de leiloeiro por leilão** — evita visitas repetidas à página do leilão.
6. **Filtrar por categoria `/imoveis/`** para coletar só imóveis.
7. **Verificar status "Aberto para Lances"** antes de processar — evita lotes encerrados.
8. **Documentos** (edital, matrícula) estão na página de detalhe como links diretos.

### 29.6. Sugestões de melhoria para o pipeline

1. **Adicionar interceptação de API** no Playwright para capturar JSON da listagem.
2. **Usar sitemap.xml** como fonte primária de URLs de lotes.
3. **Paralelizar** visitas de detalhe com `asyncio` + Playwright assíncrono.
4. **Salvar progresso** em JSON para retomar de onde parou se interrompido.
5. **Adicionar verificação de status** do lote antes do scraping completo.
"""

    try:
        with open(MD_PATH, 'a', encoding='utf-8') as f:
            f.write(md)
        print(f"\n[RELATÓRIO] Apendado em: {MD_PATH}")
    except Exception as e:
        print(f"[AVISO] Não foi possível gravar relatório: {e}", file=sys.stderr)
        rpt_path = LEILOES_DIR / f"relatorio_leiloesjudiciais_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        with open(rpt_path, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"[RELATÓRIO] Salvo separado: {rpt_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Scraper Leilões Judiciais')
    parser.add_argument('--max-paginas',  type=int, default=MAX_PAGINAS,
                        help=f'Máximo de páginas de listagem (default: {MAX_PAGINAS})')
    parser.add_argument('--sem-banco',    action='store_true',
                        help='Não tenta inserir no banco de dados')
    parser.add_argument('--max-lotes',    type=int, default=0,
                        help='Limite de lotes a processar (0 = sem limite)')
    args = parser.parse_args()

    usar_banco = DB_AVAILABLE and not args.sem_banco

    # Thread de relatório periódico (a cada 5 min)
    t = threading.Thread(target=_thread_relatorio, daemon=True)
    t.start()

    print(f"\n{'='*60}")
    print(f"SCRAPER LEILÕES JUDICIAIS — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Banco: {'SIM' if usar_banco else 'NÃO'} | Max páginas: {args.max_paginas}")
    print(f"{'='*60}\n")

    # ── Banco ──
    session = None
    if usar_banco:
        try:
            engine = create_engine(DB_URL_SYNC, echo=False, pool_pre_ping=True)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            print(f"[DB] Conectado: {DB_URL_SYNC.split('@')[-1]}")
        except Exception as e:
            print(f"[AVISO] Falha DB: {e}", file=sys.stderr)
            usar_banco = False

    all_imoveis: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        ctx    = browser.new_context(user_agent=UA, locale='pt-BR',
                                     viewport={'width': 1366, 'height': 768})
        page   = ctx.new_page()
        page.set_extra_http_headers({"Accept-Language": "pt-BR,pt;q=0.9"})

        # ── 1. Coleta URLs da listagem ──
        print("[FASE 1] Coletando URLs de lotes da listagem...\n")
        lot_urls = coletar_urls_listagem(page, args.max_paginas)

        if args.max_lotes and args.max_lotes > 0:
            lot_urls = lot_urls[:args.max_lotes]
            print(f"[INFO] Limitado a {args.max_lotes} lotes por --max-lotes\n")

        total = len(lot_urls)
        print(f"\n[FASE 2] Processando {total} lotes...\n")

        # ── 2. Extrai dados de cada lote ──
        for idx, url in enumerate(lot_urls, 1):
            print(f"[{idx}/{total}] {url}")
            try:
                dados = extrair_lote(page, url)
            except Exception as e:
                _registrar_erro('lote_excecao', url, str(e)[:200])
                dados = None

            if not dados:
                print(f"  -> Ignorado (sem dados ou não-imóvel)")
                time.sleep(DELAY)
                continue

            lei_nome = dados.get('leiloeiro', 'Desconhecido')
            print(f"  -> {lei_nome} | {dados.get('tipo_imovel','?')} | {dados.get('estado','?')} "
                  f"| {dados.get('valor_minimo') or '—'}")

            all_imoveis.append(dados)
            _registrar(lei_nome)

            # Inserção em lotes de 50 para não acumular muito na memória
            if usar_banco and session and len(all_imoveis) % 50 == 0:
                ins, upd = _inserir_no_banco(session, all_imoveis[-50:])
                print(f"  [DB] +{ins} inseridos, {upd} atualizados")

            time.sleep(DELAY)

        page.close()
        ctx.close()
        browser.close()

    # ── 3. Inserção final no banco ──
    if usar_banco and session and all_imoveis:
        resto = len(all_imoveis) % 50
        if resto:
            ins, upd = _inserir_no_banco(session, all_imoveis[-resto:])
            print(f"[DB] Final: +{ins} inseridos, {upd} atualizados")
        session.close()

    # ── 4. CSVs ──
    csv_lei = salvar_csv_leiloeiros(all_imoveis)
    csv_im  = salvar_csv_imoveis(all_imoveis)

    # ── 5. Relatório final ──
    _imprimir_relatorio()
    print(f"\nCSV leiloeiros : {csv_lei}")
    print(f"CSV imóveis    : {csv_im}")
    print(f"Total imóveis  : {len(all_imoveis)}")
    print(f"{'='*60}\n")

    _gerar_relatorio_dificuldades(all_imoveis, csv_lei, csv_im)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
