"""
scraper_jucis_df.py — Pipeline completo JUCIS-DF

1. Coleta lista de leiloeiros de https://jucis.df.gov.br/leiloeiros/
2. Salva CSV em csv/jucis_df_leiloeiros_<data>.csv
3. Para cada leiloeiro com site, visita o site e extrai imóveis
4. Insere no banco de dados (leiloeiros + imoveis)
5. Reporta progresso a cada 5 minutos

Uso:
    python scraper_jucis_df.py [--db <url>] [--sem-db]
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
import asyncio
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ─── UTF-8 no Windows ────────────────────────────────────────────────────────
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
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker, Session
    DB_AVAILABLE = True
except ImportError as e:
    print(f"[AVISO] Banco indisponível: {e}. Rodando apenas coleta + CSV.", file=sys.stderr)
    DB_AVAILABLE = False

# ─── Configurações ────────────────────────────────────────────────────────────
JUCIS_URL      = "https://jucis.df.gov.br/leiloeiros/"
LEILOES_DIR    = Path(__file__).parent
CSV_DIR        = LEILOES_DIR / "csv"
CSV_DIR.mkdir(exist_ok=True)

DB_URL_SYNC    = os.getenv(
    "DATABASE_URL_SYNC",
    "postgresql://leilao:leilao123@localhost:5432/leilao_db"
)
FONTE_URL_BASE = JUCIS_URL
DELAY          = 3.0        # segundos entre requests de sites
TIMEOUT        = 20         # timeout HTTP
REPORT_INTERVAL = 300       # 5 minutos

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
    r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b'
)
RE_DATA_BR = re.compile(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\s+\S{1,3}\s+(\d{1,2}):(\d{2}))?')
RE_PROCESSO = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
RE_PDF_EXT  = re.compile(r'\.pdf(\?[^"\']*)?$', re.IGNORECASE)
RE_DOC_KW   = re.compile(
    r'edital|matr[íi]cula|laudo|avalia[cç][ãa]o|certid[ãa]o|memorial|'
    r'escritura|penhora|registro|habite.?se|aprovação|aprovacao|pasta|processo',
    re.IGNORECASE,
)
IMG_SKIP = re.compile(
    r'logo|icon|favicon|avatar|banner|badge|star|rating|sprite|'
    r'pixel|tracking|blank|placeholder|default|noimage|no-image|'
    r'selo|stamp|whatsapp|social|share|loading',
    re.IGNORECASE,
)

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


# ─────────────────────────────────────────────────────────────────────────────
# Estado global de progresso (thread-safe via lock)
# ─────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_progresso: dict[str, int] = {}   # leiloeiro_nome → count_imoveis
_iniciado_em = datetime.now()
_total_inseridos = 0


def _registrar(leiloeiro_nome: str, qtd: int = 1):
    global _total_inseridos
    with _lock:
        _progresso[leiloeiro_nome] = _progresso.get(leiloeiro_nome, 0) + qtd
        _total_inseridos += qtd


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
                print(f"  {nome[:40]:<40} {cnt:>5} imóveis  {bar}")
        print(f"  TOTAL: {total} imóveis")
    print(f"{'='*60}\n")


def _thread_relatorio():
    """Dispara a cada 5 minutos indefinidamente."""
    while True:
        time.sleep(REPORT_INTERVAL)
        _imprimir_relatorio()


# ─────────────────────────────────────────────────────────────────────────────
# 1. PARSE DA PÁGINA JUCIS-DF
# ─────────────────────────────────────────────────────────────────────────────

def _get_text_br(el) -> str:
    """Extrai texto de elemento com <br/> como separador de linha."""
    return "\n".join(el.get_text("\n", strip=True).splitlines())


def _extrair_field(text: str, labels: list[str]) -> Optional[str]:
    """Procura 'Label: valor' no texto de um bloco."""
    for label in labels:
        pattern = re.compile(label + r'\s*:?\s*(.+?)(?:\n|$)', re.IGNORECASE)
        m = pattern.search(text)
        if m:
            val = m.group(1).strip()
            # Se o valor é só a label do próximo campo, ignora
            if val and not re.match(r'^(Matr|Endere|Telef|Site|E-mail|Preposto|Situação|Portaria)', val, re.I):
                return val
    return None


def _extrair_site(el) -> Optional[str]:
    """Extrai URL do site, preferindo <a href> sobre texto."""
    for a in el.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('http') and 'jucis' not in href.lower() and 'gov.br' not in href.lower():
            return href
    # fallback: texto "Site: ..."
    text = _get_text_br(el)
    m = re.search(r'[Ss]ite\s*:?\s*(https?://[^\s\n]+|www\.[^\s\n]+)', text)
    if m:
        url = m.group(1).strip().rstrip('.')
        if not url.startswith('http'):
            url = 'https://' + url
        return url
    return None


def parsear_jucis() -> list[dict]:
    """Retorna lista de dicts com dados de cada leiloeiro."""
    print(f"[JUCIS] Baixando {JUCIS_URL} ...")
    try:
        r = requests.get(JUCIS_URL, headers=HEADERS, timeout=30, verify=False)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERRO] Falha ao baixar JUCIS-DF: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.content, 'html.parser')

    # Encontra o div de conteúdo principal
    conteudo = None
    for div in soup.find_all('div'):
        text = div.get_text()
        if 'LISTA DE' in text and 'Matrícula' in text:
            inner = [c for c in div.children if c.name]
            if len(inner) > 5:
                conteudo = div
                break

    if not conteudo:
        print("[ERRO] Não encontrou bloco de leiloeiros no HTML.", file=sys.stderr)
        return []

    leiloeiros = []
    children = [c for c in conteudo.children if c.name]

    for el in children:
        texto = _get_text_br(el)
        if not texto.strip():
            continue

        # Nome: está em <strong> ou na primeira linha se contiver "Matrícula"
        nome_el = el.find('strong')
        if nome_el:
            nome = nome_el.get_text(strip=True).strip().lstrip().rstrip()
        else:
            # Primeira linha não vazia que não começa com campo conhecido
            linhas = [l.strip() for l in texto.splitlines() if l.strip()]
            if linhas and not re.match(r'^(Matr|Endere|Telef|Site|E-mail|Preposto|Situação|Portaria|LISTA)', linhas[0], re.I):
                nome = linhas[0]
            else:
                continue

        nome = re.sub(r'\s+', ' ', nome).strip()
        # Remove lixo residual como "Matrícula: N" que pode vazar do <strong>
        nome = re.split(r'\s*[Mm]atrícula', nome)[0].strip()
        if not nome or len(nome) < 3 or 'LISTA DE' in nome.upper():
            continue

        # Matrícula
        m_mat = re.search(r'[Mm]atrícula\s*:?\s*(\d+)', texto)
        matricula = m_mat.group(1) if m_mat else None

        # Endereço
        m_end = re.search(r'[Ee]ndere[çc]o\s*(?:[Cc]omercial\s*)?:?\s*(.+?)(?:\n[A-Z]|\nTelef|\nSite|\nE-mail|\nPORT|\nSitua|\nPrep|$)', texto, re.DOTALL)
        endereco = re.sub(r'\s+', ' ', m_end.group(1)).strip() if m_end else None

        # Telefone
        m_tel = re.search(r'[Tt]elef[oe]n[oe]s?\s*:?\s*(.+?)(?:\n|Site|E-mail|$)', texto)
        telefone = m_tel.group(1).strip() if m_tel else None

        # Email
        m_email = re.search(r'E-?mail\s*:?\s*([^\s\n]+)', texto, re.IGNORECASE)
        email = m_email.group(1).strip() if m_email else None
        # Verifica também links mailto
        for a in el.find_all('a', href=True):
            if a['href'].startswith('mailto:'):
                email = a['href'].replace('mailto:', '').strip()
                break

        # Site
        site = _extrair_site(el)

        # Situação funcional
        m_sit = re.search(r'[Ss]itua[cç][ãa]o\s+[Ff]uncional\s*:?\s*(\w+)', texto)
        situacao = m_sit.group(1).lower() if m_sit else 'desconhecido'

        # Portaria
        m_port = re.search(r'(PORTARIA[^\n]+)', texto)
        portaria = m_port.group(1).strip() if m_port else None

        # Preposto
        m_prep = re.search(r'[Pp]reposto\s*:?\s*(.+?)(?:\n|$)', texto)
        preposto = m_prep.group(1).strip() if m_prep else None

        leiloeiros.append({
            "nome": nome,
            "matricula": matricula,
            "endereco": endereco,
            "telefone": telefone,
            "email": email,
            "site": site,
            "situacao": situacao,
            "portaria": portaria,
            "preposto": preposto,
            "uf": "DF",
            "cidade": "Brasília",
            "junta_comercial": "JUCIS-DF",
            "fonte": JUCIS_URL,
        })

    print(f"[JUCIS] {len(leiloeiros)} leiloeiros extraídos.")
    return leiloeiros


# ─────────────────────────────────────────────────────────────────────────────
# 2. CSV DE LEILOEIROS
# ─────────────────────────────────────────────────────────────────────────────

def salvar_csv(leiloeiros: list[dict]) -> Path:
    data_str = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = CSV_DIR / f"jucis_df_leiloeiros_{data_str}.csv"
    campos = ["matricula","nome","situacao","cidade","uf","site","email","telefone",
              "endereco","portaria","preposto","junta_comercial","fonte"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(leiloeiros)
    print(f"[CSV] Salvo: {csv_path} ({len(leiloeiros)} linhas)")
    return csv_path


# ─────────────────────────────────────────────────────────────────────────────
# 3. HELPERS DE SCRAPING DE SITES
# ─────────────────────────────────────────────────────────────────────────────

def _uid(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:24]


def _parse_preco(raw: str) -> Optional[Decimal]:
    try:
        cleaned = re.sub(r'[^\d,]', '', raw)
        if ',' in cleaned:
            cleaned = cleaned.replace('.', '').replace(',', '.')
        return Decimal(cleaned)
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
        ('data_primeiro_leilao', [r'1[ºo°o]\.?\s*[Ll]eil', r'[Pp]rimeiro\s+[Ll]eil', r'[Dd]ata\s+do\s+[Ll]eil']),
        ('data_segundo_leilao', [r'2[ºo°o]\.?\s*[Ll]eil', r'[Ss]egundo\s+[Ll]eil']),
        ('data_encerramento', [r'[Ee]ncerramento', r'[Pp]razo', r'[Ff]im\s+do']),
    ]
    for campo, kws in padroes:
        for kw in kws:
            m = re.search(kw + r'.{0,80}?(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})', text)
            if m:
                dt = _parse_data_br(m.group(1))
                if dt:
                    trecho = text[m.start():m.start()+120]
                    hm = re.search(r'\b(\d{1,2}):(\d{2})\b', trecho)
                    if hm:
                        try:
                            dt = dt.replace(hour=int(hm.group(1)), minute=int(hm.group(2)))
                        except ValueError:
                            pass
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


def _is_js_heavy(html: str) -> bool:
    return len(BeautifulSoup(html, 'html.parser').get_text(strip=True)) < 300


def _encontrar_paginas_listagem(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Encontra links internos com keywords de listagem de imóveis."""
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
    # Deduplicar mantendo ordem
    seen: set[str] = set()
    result = []
    for u in candidatos:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result[:10]  # máx 10 páginas de listagem


def _extrair_cards_da_pagina(soup: BeautifulSoup, page_url: str, leiloeiro_nome: str) -> list[dict]:
    """Extrai imóveis de uma página de listagem."""
    cards_encontrados = []

    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if len(cards) >= 2:
            for card in cards[:50]:
                texto = _texto(card)
                if len(texto) < 20:
                    continue

                # URL do imóvel
                link_el = card.find('a', href=True)
                url_imovel = urljoin(page_url, link_el['href']) if link_el else page_url

                # Título
                titulo = None
                for t_sel in ['h1','h2','h3','h4','.titulo','.title','[class*="titulo"]','[class*="title"]']:
                    el = card.select_one(t_sel)
                    if el:
                        titulo = _texto(el)[:300]
                        if titulo:
                            break
                if not titulo:
                    titulo = (texto[:150].split('\n')[0]).strip() or 'Imóvel sem título'

                # Preços
                precos = RE_PRECO.findall(texto)
                valor_min = _parse_preco(precos[0]) if precos else None
                valor_aval = _parse_preco(precos[1]) if len(precos) > 1 else None

                # Área
                area_m = RE_AREA.search(texto)
                area = Decimal(area_m.group(1).replace(',', '.')) if area_m else None

                # Características
                quartos_m = RE_QUARTOS.search(texto)
                banheiros_m = RE_BANHEI.search(texto)
                vagas_m = RE_VAGAS.search(texto)

                # Localização
                uf_m = RE_UF.search(texto)
                estado = uf_m.group(1) if uf_m else 'DF'
                cep_m = RE_CEP.search(texto)
                cep = cep_m.group(0) if cep_m else None

                # Datas
                datas = _extrair_datas(texto)

                # Tipo
                tipo_str = _detectar_tipo(titulo + ' ' + texto[:500])

                # Imagem
                img = card.find('img')
                img_url = None
                if img:
                    src = img.get('src') or img.get('data-src') or img.get('data-lazy-src', '')
                    if src and not src.startswith('data:') and not IMG_SKIP.search(src):
                        img_url = urljoin(page_url, src)

                # Processo
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


def _scrape_detalhe(url: str, dados: dict, leiloeiro_nome: str) -> dict:
    """Visita página de detalhe do imóvel e enriquece os dados."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code != 200:
            return dados
        soup = BeautifulSoup(r.content, 'html.parser')
        texto = soup.get_text(' ', strip=True)

        # Enriquecer título se fraco
        if len(dados.get('titulo', '')) < 15:
            h1 = soup.find('h1')
            if h1:
                dados['titulo'] = _texto(h1)[:300]

        # Descrição
        for sel in ['[class*="descricao"]','[class*="description"]','[class*="detalhes"]',
                    '[class*="obs"]','main p','article p']:
            el = soup.select_one(sel)
            if el:
                t = _texto(el)
                if len(t) > 80:
                    dados['descricao'] = t[:8000]
                    break

        # Preços mais precisos
        precos = RE_PRECO.findall(texto)
        if precos and not dados.get('valor_minimo'):
            dados['valor_minimo'] = _parse_preco(precos[0])
        if len(precos) > 1 and not dados.get('valor_avaliacao'):
            dados['valor_avaliacao'] = _parse_preco(precos[1])

        # Endereço completo
        cep_m = RE_CEP.search(texto)
        if cep_m and not dados.get('cep'):
            dados['cep'] = cep_m.group(0)

        # Datas
        datas = _extrair_datas(texto)
        for k, v in datas.items():
            if v and not dados.get(k):
                dados[k] = v

        # Imagens
        img_principal, imgs = _extrair_imagens(soup, url)
        if not dados.get('imagem_principal') and img_principal:
            dados['imagem_principal'] = img_principal
        dados['imagens'] = json.dumps(imgs[:20])

        # Arquivos (editais, matrículas, PDFs)
        arquivos = _extrair_arquivos(soup, url)
        if arquivos:
            dados['arquivos'] = json.dumps(arquivos)

        # Processo judicial
        proc_m = RE_PROCESSO.search(texto)
        if proc_m and not dados.get('numero_processo'):
            dados['numero_processo'] = proc_m.group(0)

        # Tipo leilão
        if 'judicial' in texto.lower() or 'processo' in texto.lower():
            dados['tipo_leilao'] = 'judicial'

    except Exception as e:
        pass  # falha silenciosa no detalhe — usa dados do card

    return dados


def scrape_site_leiloeiro(nome: str, site_url: str) -> list[dict]:
    """Visita site do leiloeiro e extrai imóveis disponíveis."""
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
    texto_home = soup_home.get_text(' ', strip=True)

    # Tenta extrair imóveis direto da home
    cards_home = _extrair_cards_da_pagina(soup_home, site_url, nome)

    # Encontra páginas de listagem internas
    paginas_listagem = _encontrar_paginas_listagem(soup_home, site_url)
    paginas_listagem = [p for p in paginas_listagem if p not in visitados]
    visitados.add(site_url)

    # Coleta das páginas de listagem
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

    # Combinar: listagem > home
    todos_cards = todas_listagem_cards if todas_listagem_cards else cards_home
    if not todos_cards and cards_home:
        todos_cards = cards_home

    if not todos_cards:
        print(f"  [INFO] Nenhum card de imóvel encontrado em {nome}")
        return []

    print(f"  [INFO] {len(todos_cards)} cards encontrados. Visitando detalhes...")

    # Enriquece com detalhe (máx 30 por leiloeiro para não sobrecarregar)
    for i, card in enumerate(todos_cards[:30]):
        url_det = card.get('url_original', '')
        if url_det and url_det != site_url and url_det not in visitados:
            visitados.add(url_det)
            time.sleep(DELAY)
            card = _scrape_detalhe(url_det, card, nome)

        # Filtro: só imóveis com data >= hoje (seção 8.1 do guia)
        data_1 = card.get('data_primeiro_leilao')
        data_enc = card.get('data_encerramento')
        data_ref = data_1 or data_enc
        if data_ref:
            if isinstance(data_ref, datetime):
                data_ref = data_ref.date()
            if data_ref < date.today():
                continue  # descarta leilões já encerrados

        imoveis.append(card)

    print(f"  [OK] {nome}: {len(imoveis)} imóveis válidos (data >= hoje)")
    return imoveis


# ─────────────────────────────────────────────────────────────────────────────
# 4. BANCO DE DADOS
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
        Leiloeiro.uf == dados.get('uf', 'DF')
    ).first()
    if existing:
        for k, v in dados.items():
            if v is not None:
                setattr(existing, k, v)
        return existing
    lei = Leiloeiro(**{k: v for k, v in dados.items() if k in Leiloeiro.__table__.columns.keys()})
    session.add(lei)
    session.flush()
    return lei


def _map_tipo_imovel(tipo_str: str) -> TipoImovel:
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


def _map_tipo_leilao(tipo_str: str) -> TipoLeilao:
    if 'judicial' in tipo_str:
        return TipoLeilao.JUDICIAL
    if 'bancario' in tipo_str or 'bancário' in tipo_str:
        return TipoLeilao.BANCARIO
    return TipoLeilao.EXTRAJUDICIAL


def inserir_imovel(session: Session, fonte_id: int, leiloeiro_id: Optional[int],
                   dados: dict) -> bool:
    """Insere ou atualiza um imóvel. Retorna True se inseriu novo."""
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
        'estado':               dados.get('estado', 'DF'),
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
    else:
        session.add(Imovel(**campos))
        return True


# ─────────────────────────────────────────────────────────────────────────────
# 5. PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Inicia thread de relatório
    t = threading.Thread(target=_thread_relatorio, daemon=True)
    t.start()

    print(f"\n{'='*60}")
    print(f"SCRAPER JUCIS-DF — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    # ── 1. Coleta leiloeiros ──────────────────────────────────────────────────
    leiloeiros = parsear_jucis()
    if not leiloeiros:
        print("[FATAL] Nenhum leiloeiro extraído. Encerrando.", file=sys.stderr)
        return

    # ── 2. Salva CSV ──────────────────────────────────────────────────────────
    csv_path = salvar_csv(leiloeiros)

    com_site = [l for l in leiloeiros if l.get('site')]
    sem_site = [l for l in leiloeiros if not l.get('site')]
    print(f"\n[INFO] {len(com_site)} leiloeiros com site, {len(sem_site)} sem site.")

    # ── 3. Banco de dados ─────────────────────────────────────────────────────
    session: Optional[Session] = None
    if DB_AVAILABLE:
        try:
            engine = create_engine(DB_URL_SYNC, echo=False, pool_pre_ping=True)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            print(f"[DB] Conectado: {DB_URL_SYNC.split('@')[-1]}")
        except Exception as e:
            print(f"[AVISO] Falha ao conectar DB: {e}. Rodando sem inserção.", file=sys.stderr)
            session = None

    # ── 4. Upsert leiloeiros no DB ────────────────────────────────────────────
    if session:
        print("\n[DB] Upserting leiloeiros...")
        for l in leiloeiros:
            try:
                _upsert_leiloeiro(session, {
                    'matricula': l.get('matricula'),
                    'nome': l['nome'],
                    'uf': 'DF',
                    'junta_comercial': 'JUCIS-DF',
                    'situacao': l.get('situacao'),
                    'cidade': 'Brasília',
                    'email': l.get('email'),
                    'telefone': l.get('telefone'),
                    'site': l.get('site'),
                    'fonte_url': JUCIS_URL,
                })
            except Exception as e:
                session.rollback()
                print(f"  [AVISO] Erro ao upsert {l['nome']}: {e}", file=sys.stderr)
        try:
            session.commit()
            print(f"[DB] {len(leiloeiros)} leiloeiros sincronizados.")
        except Exception as e:
            session.rollback()
            print(f"[AVISO] Commit leiloeiros: {e}", file=sys.stderr)

    # ── 5. Scrape dos sites ───────────────────────────────────────────────────
    print(f"\n[SCRAPE] Iniciando coleta de {len(com_site)} sites de leiloeiros...")
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

        # Insere no DB
        if session:
            inseridos_lote = 0
            try:
                fonte = _get_or_create_fonte(session, nome, site)
                lei_db = session.query(Leiloeiro).filter(
                    Leiloeiro.nome == nome, Leiloeiro.uf == 'DF'
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

        # Registra progresso
        _registrar(nome, len(imoveis))
        total_imoveis += len(imoveis)

        # Delay entre sites
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


if __name__ == "__main__":
    main()
