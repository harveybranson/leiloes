#!/usr/bin/env python3
"""
scraper_bomvalor.py
Captura imóveis de todos os leiloeiros oficiais cadastrados em
comunidades.bomvalor.com.br → mercado.bomvalor.com.br

Fluxo:
  1. Para cada slug, varre as páginas de listagem de imóveis.
  2. Aplica filtro de data: só inclui itens cuja data de encerramento (ou
     1ª praça) seja >= hoje E que sejam Praça Única ou 1ª Praça.
  3. Para cada item válido, raspa a página de detalhe para enriquecer
     os campos (valor avaliação, datas separadas, processo, comitente...).
  4. Salva CSV em ./csv/bomvalor_YYYYMMDD_HHMM.csv com o mesmo schema
     do banco de dados existente.

Uso:
  python scraper_bomvalor.py              # modo completo (listagem + detalhe)
  python scraper_bomvalor.py --fast       # só listagem (sem detalhe), mais rápido
  python scraper_bomvalor.py --resume     # retoma do progresso salvo
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import urllib3
import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────────────────────

BASE_URL    = "https://mercado.bomvalor.com.br"
TODAY       = date.today()
DELAY_LIST  = 1.2   # segundos entre páginas de listagem
DELAY_DET   = 1.5   # segundos entre páginas de detalhe

ts          = datetime.now().strftime("%Y%m%d_%H%M")
OUTPUT_CSV  = Path("csv") / f"bomvalor_{ts}.csv"
PROGRESS_FILE = Path("bomvalor_progress.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

# Mapeamento slug → nome do leiloeiro (coletado da página de comunidades)
LEILOEIRO_NAMES = {
    "leiloeirodenis":        "Dênis de Oliveira Fernandes",
    "lisboaleiloes":         "JOSÉ CLÁUDIO COSTA LISBÔA",
    "leiloei":               "Felipe Nunes Gomes Teixeira Bignardi",
    "idleiloes":             "José Ivanildo de Sousa Damasceno",
    "fernandopelloni":       "Fernando Pelloni Barros da Silveira",
    "gandelmanleiloes":      "Karina Gandelman",
    "infinityleiloes":       "Rafael Soares da Silva",
    "multleiloes":           "Fernando Gonçalves Costa",
    "ctmleiloes":            "Carina Tome Mattar",
    "bramoleiloes":          "Julio José da Silva Moreira",
    "americaleiloes":        "Guilherme Roberto Dorta Da Silva",
    "pamplonaleiloes":       "Jose Araken Pamplona Barros",
    "martinsleiloes":        "Evandro Viegas Martins",
    "franciscodavidleiloeiro": "Francisco David Batista De Souza",
    "celsocunhaleiloes":     "Celso Alves Cunha",
    "agroleileos":           "Ícaro Alexandre Felfili Jardim",
    "paulotolentino":        "Paulo Henrique De Almeida Tolentino",
    "corleiloes":            "Cíntia Regina Martins Roma",
    "emperadorleiloes":      "Dafne Bisachi Dias",
    "oreidosleiloes":        "Eder Jordan de Souza Paes",
    "emersonoliveira":       "Emerson Moreira de Oliveira",
    "emiliomatos":           "Emílio Matos Rocha",
    "leiloesbrasil":         "Antônio Brasil II",
    "costanetoleiloeiro":    "Sebastião Felix Da Costa Neto",
    "ednleiloes":            "EDILENE NAZARÉ SILVA",
    "vallandleiloes":        "Marcelo Valland",
    "macariosleiloes":       "Eduardo Macario de Melo",
    "bastosleiloes":         "BRENO RIBEIRO PENNA BASTOS",
    "kronbergleiloes":       "Helcio Kronberg",
    "hastalegal":            "Arthur Ferreira Nunes",
    "apiceleiloes":          "Fábio Prando Fagundes Góes",
    "kcleiloes":             "Kátia Cerqueira Da Silva Casaes",
    "braleiloes":            "André Amaral Barros",
    "anabrasilleiloes":      "Ana Carolina Brasil de Oliveira Maia",
    "ajleiloes":             "Antonio Jose Da Silva Filho",
    "luizbalbinoleiloes":    "Luiz Balbino Da Silva Júnior",
    "leiloesrionegro":       "Hugo Moreira Pimenta",
    "doulhetresleiloes":     "Raphael de Souza Menegon",
    "willianleiloes":        "Willian Augusto Ferreira De Araújo",
    "schererleiloes":        "Adalberto Scherer Filho",
    "federalleiloes":        "Cesar Augusto Bagatini",
    "colossoleiloes":        "Samara Barbosa Araújo",
    "jorgemarcoleiloes":     "Jorge Marco Aurelio Biavati",
    "malveiraleiloes":       "André de Almeida Malveira",
    "agsleiloes":            "Daniel Bizerra da Costa",
    "grupoarremateleiloes":  "Fernando Cabeças Barbosa",
    "meloleiloes":           "Agnaldo José De Melo",
    "eduardagodoy":          "Eduarda Godoy de Souza",
    "sperancaleiloes":       "Jaqueline Sperança",
    "atleiloes":             "Laerte Teixeira Martins Silva",
    "pedrolernerkronberg":   "Pedro Lerner Kronberg",
    "pietrangello":          "Pietrangello Rosalém",
    "faleiloes":             "Flares Aguiar da Silva",
    "portovelholeiloes":     "Adriano Apolinário Leão De Oliveira",
    "leiloesonlinesp":       "Gustavo Correa Pereira Da Silva",
    "portoleiloes":          "Brenno de Figueiredo Porto",
    "mzmleiloes":            "Mauro José Zecchin De Morais",
    "duxleiloes":            "Kaio Albuquerque Rosa Botelho",
    "universodosleiloes":    "Alexsander Pretti Domingos",
    "leiloessimoesfahel":    "Roberto Simoes Pereira",
    "arthurmichelonleiloes": "Arthur Michelon Sampaio",
    "bianchileiloes":        "Dionir Bianchi",
    "maestroleiloes":        "Leonardo Simon Tobelem",
    "srleiloes":             "Sostenes De Almeida Rabelo",
    "onebidleiloes":         "Fabiana Goldhar Raicher",
    "brancalliaoleiloes":    "Nilton Brancallião",
    "liraleiloes":           "Elenice Lira Sales de Sousa",
    "primeirapraca":         "Guido Santos do Nascimento",
    "cunhaleiloeiro":        "Hugo Leonardo Alvarenga Cunha",
    "renannerisleiloeiro":   "Renan Neris da Silva",
    "arnaldoleiloes":        "Arnaldo Emilio Colombarolli",
    "monzonleiloes":         "Joacir Monzon Pouey",
    "scheidtleiloes":        "Mirella Beatriz Scheidt",
    "apabrfleiloes":         "Breno César Oliveira Farias",
    "mozarmirandaleiloes":   "Mozar Miranda Almeida",
    "arremataronline":       "Carlos Chui",
    "nortedeminasleiloes":   "Fábio Maciel Amarante",
    "emleilao":              "Ayrton De Souza Porto Filho",
    "globoleiloes":          "Cassia Negrete Nunes Balbino",
    "starupleiloes":         "RENAN AUGUSTO FERNANDES GUIMARÃES",
    "nasarleiloes":          "Ives Harrisson Nasar dos Santos",
    "mrl4leiloes":           "Lidia Ribeiro de Andrade",
    "melhorleiloes":         "Rennan de Souza Menegon",
    "eneasnetoleiloes":      "Enéas Carrilho De Vasconcelos Neto",
    "josimarleiloeiro":      "JOSIMAR DE AZEVEDO SANTOS",
    "margaretegomesleiloes": "Margarete Sueli Comin Gomes",
    "patiorochaleiloes":     "Ivana Montenegro Castelo Branco Rocha",
    "luisleiloeiro":         "LUIS OTAVIO MARCOLINO SHINKAWA",
    "ialeiloes":             "Kesley Nunes de Souza",
    "whleiloes":             "Weider André Henojo",
    "hisaleiloes":           "Tatiana Hisa Sato",
    "jocaleiloes":           "João Luiz de França Neto",
    "leiloesaguiar":         "Vladmir Oliani",
    "abaleiloes":            "Adilson Bento De Araújo",
    "munizleiloes":          "Milton Santiago Sola Muniz",
    "pulseleiloes":          "Anderson Neves de Oliveira",
    "msoleiloes":            "Maurice da Silva Oliveira",
    "gbleiloes":             "Gustavo Bolzan",
    "icaroleiloes":          "Icaro Lacialamella",
    "montenegroleiloes":     "Georgia Castelo",
    "alexandreometto":       "Alexandre Ometto Furlan Silva",
    "loteleiloes":           "Valdyr Alves de Sá",
    "lubreleiloes":          "Luiz Roberto de Oliveira Brenneken",
    "jmfleiloes":            "João Vitor Martins Ferreira",
    "dialaleiloes":          "Diala Natalia Pinheiro do Nascimento",
    "jzleiloes":             "Juliano Zenzeluk",
    "cariocaleiloes":        "Rodrigo da Conceição Prata",
    "roraimaleiloes":        "Mayco Silva Dos Santos",
    "focoleiloes":           "Anna Karoline Santos do Amaral",
    "masaileiloes":          "Manuela Masai Vilar Vieira do Nascimento",
}

# Lista de todos os slugs (deduplicados)
ALL_SLUGS = list(dict.fromkeys(LEILOEIRO_NAMES.keys()))

# Colunas do CSV de saída (compatível com o schema existente)
CSV_FIELDS = [
    "leiloeiro", "titulo", "descricao", "cidade", "estado", "tipo_imovel",
    "area_total", "valor_minimo", "valor_avaliacao", "desconto_percentual",
    "data_primeiro_leilao", "data_segundo_leilao", "data_encerramento",
    "url_original", "imagem_principal", "numero_processo", "vara", "comarca",
    "comitente", "criado_em", "praca_tipo",
]


# ──────────────────────────────────────────────────────────────
# Helpers de parse
# ──────────────────────────────────────────────────────────────

def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_price(text):
    """'R$ 1.234.567,89' ou 'Entrada R$ 190.000,00' → 190000.0"""
    m = re.search(r"([\d.]+,\d{2})", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_date_br(text):
    """'01/06/2026' ou '09 de jun. de 2026' → date"""
    # DD/MM/YYYY
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    # "09 de jun. de 2026"
    MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
              "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
    m2 = re.search(r"(\d{1,2})\s+de\s+(\w{3})\.?\s+de\s+(\d{4})", text or "", re.I)
    if m2:
        mes = MESES.get(m2.group(2).lower()[:3])
        if mes:
            try:
                return date(int(m2.group(3)), mes, int(m2.group(1)))
            except ValueError:
                pass
    return None


def parse_city_state(text):
    """Extrai 'Cidade/UF' do título: '...em São Gonçalo/RJ' → ('São Gonçalo', 'RJ')"""
    # Padrão: "em Cidade/UF" ou ", Cidade/UF" no final
    m = re.search(
        r"(?:em\s+|,\s+|[-–]\s+)([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\.']+?)/([A-Z]{2})(?:\s|$|,|\.)",
        text or ""
    )
    if m:
        return m.group(1).strip(), m.group(2)
    # Fallback: Cidade/UF no final da string
    m2 = re.search(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\.]+)/([A-Z]{2})\s*$", text or "")
    if m2:
        return m2.group(1).strip(), m2.group(2)
    return "", ""


def parse_area(text):
    """'A.T. 47,00m²' → 47.0"""
    m = re.search(r"([\d.,]+)\s*m[²2]", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def tipo_from_url(url_path):
    """'/slug/imoveis/apartamento-misto/...' → 'APARTAMENTO MISTO'"""
    parts = url_path.strip("/").split("/")
    # esperado: slug / imoveis / tipo / slug-item
    if len(parts) >= 3 and parts[1] == "imoveis":
        return parts[2].upper().replace("-", " ")
    if "imoveis" in parts:
        idx = parts.index("imoveis")
        if idx + 1 < len(parts):
            return parts[idx + 1].upper().replace("-", " ")
    return ""


def desconto(valor_min, valor_aval):
    if valor_aval and valor_min and valor_aval > 0:
        return round((1 - valor_min / valor_aval) * 100, 2)
    return None


# ──────────────────────────────────────────────────────────────
# Scraping de listagem
# ──────────────────────────────────────────────────────────────

def fetch(session, url, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=25, allow_redirects=True, verify=False)
            return r
        except Exception as e:
            if attempt == retries - 1:
                print(f"    [ERRO] {url}: {e}")
                return None
            time.sleep(2)
    return None


def _parse_cards(soup, slug):
    """
    Extrai itens dos cards de imóveis de um BeautifulSoup já parseado.

    Estrutura real do HTML (bomvalor):
      <a class="link-leilao" href="/{slug}/imoveis/{tipo}/{titulo-id}">
        <div class="titulo-leilao">...</div>          ← título
        <div class="numero-praca-status">2ª Praça</div>
        <span class="data-encerramento-leilao">DD/MM/YYYY</span>
        <div class="valor-lance-inicial valor">R$190.000,00</div>
        <img class="d-block w-100" src="...">        ← imagem principal
    """
    pattern = re.compile(r"/imoveis/[^/]+/[^/]+-\d+")
    cards = soup.find_all("a", href=pattern)
    items = []
    seen = set()

    for a in cards:
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        full_url = BASE_URL + href if href.startswith("/") else href

        # ── Título ──────────────────────────────────────────────────────
        titulo_div = a.find(class_="titulo-leilao")
        if titulo_div:
            # Remove sub-elementos (links de cartório, PDF) e pega só o texto
            for sub in titulo_div.find_all(["object", "a", "label"]):
                sub.decompose()
            titulo = clean(titulo_div.get_text())
        else:
            titulo = ""

        # ── Praça ────────────────────────────────────────────────────────
        praca_div = a.find(class_="numero-praca-status")
        praca_tipo = clean(praca_div.get_text()) if praca_div else ""

        # ── Data de encerramento ─────────────────────────────────────────
        data_span = a.find(class_="data-encerramento-leilao")
        data_enc_str = clean(data_span.get_text()) if data_span else ""
        data_enc = parse_date_br(data_enc_str)

        # ── Valor ────────────────────────────────────────────────────────
        valor_div = a.find(class_="valor-lance-inicial valor")
        if not valor_div:
            valor_div = a.find(class_=re.compile(r"valor-lance-inicial"))
        preco_raw = clean(valor_div.get_text()) if valor_div else ""
        preco = parse_price(preco_raw)

        # ── Imagem principal ─────────────────────────────────────────────
        img = a.find("img", class_=re.compile(r"d-block"))
        if not img:
            img = a.find("img")
        img_url = (img.get("src") or img.get("data-src") or "") if img else ""

        # ── Parse campos do título ───────────────────────────────────────
        cidade, estado = parse_city_state(titulo)
        tipo = tipo_from_url(href)
        area = parse_area(titulo)

        is_1a = bool(re.search(r"1[ªa]\s*[Pp]ra[çc]a", praca_tipo, re.I))
        is_unica = bool(re.search(r"[Úú]nica", praca_tipo, re.I))
        is_2a = bool(re.search(r"2[ªa]\s*[Pp]ra[çc]a", praca_tipo, re.I))

        items.append({
            "slug":              slug,
            "leiloeiro":         LEILOEIRO_NAMES.get(slug, slug),
            "url_original":      full_url,
            "titulo":            titulo,
            "tipo_imovel":       tipo,
            "area_total":        area,
            "cidade":            cidade,
            "estado":            estado,
            "data_enc_raw":      data_enc_str,
            "data_encerramento": data_enc.isoformat() if data_enc else "",
            "data_enc_obj":      data_enc,
            "praca_tipo":        praca_tipo,
            "is_1a_praca":       is_1a,
            "is_unica":          is_unica,
            "is_2a_praca":       is_2a,
            "valor_minimo":      preco,
            "imagem_principal":  img_url,
        })
    return items


def scrape_listing_slug(session, slug):
    """
    Retorna todos os itens de imóveis de um slug usando:
    1. Primeira requisição HTML para saber o total
    2. Requisição AJAX com perPage=total para capturar tudo de uma vez
    """
    listing_url = f"{BASE_URL}/{slug}/busca/segmento/imoveis"
    ajax_headers = {**HEADERS,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": listing_url}

    # Passo 1: HTML inicial para descobrir total de itens
    r = fetch(session, listing_url)
    if not r or r.status_code != 200:
        print(f"  [{slug}] HTTP {r.status_code if r else 'ERR'} → pulando")
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Cloudflare check
    title_tag = soup.find("title")
    if title_tag and "just a moment" in (title_tag.get_text() or "").lower():
        print(f"  [{slug}] Cloudflare challenge → pulando")
        return []

    # Detecta o total de itens (JS embeds: '95' no script)
    total = 30  # default conservador
    for s in soup.find_all("script"):
        t = s.string or ""
        m = re.search(r"'(\d+)'\s*<=\s*perPage", t)
        if m:
            total = int(m.group(1))
            break

    # Passo 2: AJAX com perPage >= total para pegar tudo de uma vez
    per_page = max(total, 50)  # nunca menos que 50
    ajax_url = f"{listing_url}?perPage={per_page}&page=1"
    r2 = fetch(session, ajax_url)
    if r2 and r2.status_code == 200 and len(r2.text) > 1000:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        items = _parse_cards(soup2, slug)
        # Complementa com itens da página HTML caso o AJAX retorne menos
        if len(items) < 5:
            items = _parse_cards(soup, slug)
    else:
        items = _parse_cards(soup, slug)

    print(f"  [{slug}] total={total} → {len(items)} itens capturados")
    return items


# ──────────────────────────────────────────────────────────────
# Scraping de detalhe
# ──────────────────────────────────────────────────────────────

def scrape_detail(session, url):
    """Enriquece um item com dados da página de detalhe."""
    r = fetch(session, url)
    if not r or r.status_code != 200:
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    result = {}

    # Título (h1)
    h1 = soup.find("h1")
    if h1:
        result["titulo"] = clean(h1.get_text())

    # Descrição (primeiros ~500 chars do bloco de descrição)
    desc_el = (
        soup.find(class_=re.compile(r"descri[çc]ao|description|detail", re.I)) or
        soup.find(id=re.compile(r"descri[çc]ao|description", re.I))
    )
    if desc_el:
        result["descricao"] = clean(desc_el.get_text())[:500]

    # Valor de avaliação
    m = re.search(
        r"[Vv]alor\s+[Aa]valia[çc][aã]o[:\s]*R\$\s*([\d.,]+)",
        text
    )
    if m:
        result["valor_avaliacao"] = parse_price("R$ " + m.group(1))

    # Datas dos eventos (busca todas as datas DD/MM/YYYY na ordem que aparecem)
    dates_found = []
    seen_dates = set()
    for dm in re.finditer(r"(\d{2}/\d{2}/\d{4})", text):
        d = parse_date_br(dm.group(1))
        if d and d not in seen_dates:
            seen_dates.add(d)
            dates_found.append(d)

    if dates_found:
        result["data_primeiro_leilao"] = dates_found[0].isoformat()
    if len(dates_found) >= 2:
        result["data_segundo_leilao"] = dates_found[1].isoformat()

    # Número do processo
    pm = re.search(r"(?:Processo|N[ºo°]\s*Processo)[:\s]+([\d./-]{5,})", text)
    if pm:
        result["numero_processo"] = pm.group(1).strip()

    # Comarca
    cm = re.search(r"[Cc]omarca[:\s]+([A-Za-zÀ-ÿ\s]+?)(?:\s{2,}|/[A-Z]{2}|$)", text)
    if cm:
        result["comarca"] = clean(cm.group(1))

    # Vara
    vm = re.search(r"[Vv]ara[:\s]+([A-Za-zÀ-ÿ\s\d]+?)(?:\s{2,}|$)", text)
    if vm:
        result["vara"] = clean(vm.group(1))[:100]

    # Comitente / vendedor
    ktm = re.search(r"[Cc]omitente[:\s]+([^\n\r]+)", text)
    if not ktm:
        ktm = re.search(r"[Vv]endedor[:\s]+([^\n\r]+)", text)
    if ktm:
        result["comitente"] = clean(ktm.group(1))[:150]

    # Leiloeiro responsável
    lm = re.search(r"[Ll]eiloeiro\s+[Oo]ficial[:\s]+([^\n\r]+)", text)
    if lm:
        result["leiloeiro"] = clean(lm.group(1))[:150]

    # Cidade/estado do endereço (mais preciso que do título)
    am = re.search(
        r"(?:Cidade|City)[:\s]+([A-Za-zÀ-ÿ\s]+)/([A-Z]{2})",
        text
    )
    if am:
        result["cidade"] = clean(am.group(1))
        result["estado"] = am.group(2)

    return result


# ──────────────────────────────────────────────────────────────
# Filtro de data
# ──────────────────────────────────────────────────────────────

def deve_incluir(item):
    """
    Inclui se:
      - É 1ª Praça ou Praça Única E data de encerramento >= hoje
      - Ou se é 2ª Praça mas o data_primeiro_leilao ainda não passou (raro)
    Em caso de data ausente, inclui com aviso (preferível a descartar).
    """
    d = item.get("data_enc_obj")

    # Se é 2ª Praça e só temos a data de encerramento (que é a 2ª), descarta
    if item.get("is_2a_praca"):
        return False

    if d is None:
        # Sem data: inclui mas marca para revisão
        return True

    return d >= TODAY


# ──────────────────────────────────────────────────────────────
# Progresso
# ──────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_slugs": [], "rows": []}


def save_progress(prog):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2, default=str)


# ──────────────────────────────────────────────────────────────
# Saída CSV
# ──────────────────────────────────────────────────────────────

def save_csv(rows):
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n✓ Salvo: {OUTPUT_CSV} ({len(rows)} imóveis)")


def build_row(item):
    """Constrói a linha do CSV a partir do dict enriquecido."""
    vm = item.get("valor_minimo")
    va = item.get("valor_avaliacao")
    return {
        "leiloeiro":           item.get("leiloeiro", ""),
        "titulo":              item.get("titulo", ""),
        "descricao":           item.get("descricao", ""),
        "cidade":              item.get("cidade", ""),
        "estado":              item.get("estado", ""),
        "tipo_imovel":         item.get("tipo_imovel", ""),
        "area_total":          item.get("area_total", ""),
        "valor_minimo":        vm or "",
        "valor_avaliacao":     va or "",
        "desconto_percentual": desconto(vm, va) or "",
        "data_primeiro_leilao": item.get("data_primeiro_leilao", ""),
        "data_segundo_leilao":  item.get("data_segundo_leilao", ""),
        "data_encerramento":   item.get("data_encerramento", ""),
        "url_original":        item.get("url_original", ""),
        "imagem_principal":    item.get("imagem_principal", ""),
        "numero_processo":     item.get("numero_processo", ""),
        "vara":                item.get("vara", ""),
        "comarca":             item.get("comarca", ""),
        "comitente":           item.get("comitente", ""),
        "criado_em":           datetime.now().isoformat(timespec="seconds"),
        "praca_tipo":          item.get("praca_tipo", ""),
    }


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scraper Bom Valor - Leiloeiros Oficiais")
    parser.add_argument("--fast",   action="store_true", help="Apenas listagem, sem detalhe")
    parser.add_argument("--resume", action="store_true", help="Retoma do progresso salvo")
    parser.add_argument("--slug",   type=str, help="Testa apenas um slug específico")
    args = parser.parse_args()

    prog = load_progress() if args.resume else {"done_slugs": [], "rows": []}
    rows = prog["rows"]
    done_slugs = set(prog["done_slugs"])

    slugs = [args.slug] if args.slug else ALL_SLUGS

    session = requests.Session()
    session.headers.update(HEADERS)

    total_collected = 0

    for i, slug in enumerate(slugs, 1):
        if slug in done_slugs:
            print(f"[{i}/{len(slugs)}] {slug} → já processado, pulando")
            continue

        print(f"\n[{i}/{len(slugs)}] Processando: {slug}")

        # 1. Listagem
        listing_items = scrape_listing_slug(session, slug)
        print(f"  → {len(listing_items)} itens na listagem")

        # 2. Filtro de data
        valid_items = [it for it in listing_items if deve_incluir(it)]
        skip_count = len(listing_items) - len(valid_items)
        if skip_count:
            print(f"  → {skip_count} descartados (2ª praça ou data passada)")

        # 3. Detalhe (opcional)
        for j, item in enumerate(valid_items, 1):
            if not args.fast:
                detail = scrape_detail(session, item["url_original"])
                item.update({k: v for k, v in detail.items() if v})  # não sobrescreve com vazio
                time.sleep(DELAY_DET)

            rows.append(build_row(item))
            total_collected += 1

        print(f"  → {len(valid_items)} imóveis incluídos (total acumulado: {total_collected})")

        done_slugs.add(slug)
        prog["done_slugs"] = list(done_slugs)
        prog["rows"] = rows
        save_progress(prog)

        # Salva CSV parcial a cada 5 slugs
        if i % 5 == 0:
            save_csv(rows)

    # Salva final
    save_csv(rows)

    # Limpa arquivo de progresso
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    print(f"\nTotal final: {len(rows)} imóveis capturados de {len(done_slugs)} leiloeiros.")


if __name__ == "__main__":
    main()
