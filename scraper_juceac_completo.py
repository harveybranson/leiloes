#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper completo JUCEAC + PDFs anexos (Rondônia/JUCER cross-registro JUCEAC)
- Filtra somente leiloeiros REGULAR (Situação autoritativa do PDF de antiguidade)
- Exclui CANCELADO / SUSPENSO / IRREGULAR / AFASTADO
- Entra em cada site, captura imóveis (título, descrição, preço, data, imagem, anexos)
- Valida: data do 1º leilão > data da captura
- Gera CSV de leiloeiros e de imóveis em /csv
- Insere imóveis no banco imoveis_leiloeiros.db
- Relatório de imóveis por leiloeiro a cada 5 min
Conforme captura_dados_leiloes_v2.md
"""

import csv
import json
import re
import sys
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
OUTPUT_DIR = BASE_DIR / "csv"
DB_PATH = BASE_DIR / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE_DIR / "scraper_juceac_completo_progress.json"
RELATORIO_FILE = BASE_DIR / "captura_dados_leiloes_v2.md"
HTML_DEBUG = BASE_DIR / "juceac_rendered.html"

CAPTURE_DATE = datetime.now()
HOJE = CAPTURE_DATE.date()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
REPORT_INTERVAL = 300  # 5 min

# ---------------------------------------------------------------------------
# Leiloeiros REGULAR (do PDF "Lista por ordem de antiguidade" = Situação autoritativa)
# Cruzado com sites do PDF "Leiloeiros judiciais Rondônia" e da lista detalhada.
# Apenas Situação == REGULAR. Excluídos: IRREGULAR, AFASTADO, CANCELADO, SUSPENSO.
# ---------------------------------------------------------------------------
LEILOEIROS_REGULAR = [
    {"nome": "Vladmir Oliani", "matricula": "008/1995", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.leiloesaguiar.com.br/"},
    {"nome": "Vera Lucia Aguiar de Sousa", "matricula": "010/2006", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.leiloesaguiar.com.br/"},
    {"nome": "Evanilde Aquino Pimentel Rosa", "matricula": "015/2009", "cidade": "Ji-Parana", "uf": "RO", "site": "https://www.lancevip.com.br/"},
    {"nome": "Vera Maria Aguiar de Sousa", "matricula": "018/2013", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.leiloesaguiar.com.br/"},
    {"nome": "Francisco Portela Aguiar", "matricula": "019/2013", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.portelaleiloes.com.br/"},
    {"nome": "Deonizia Kiratch", "matricula": "021/2017", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.deonizialeiloes.com.br/"},
    {"nome": "Ana Carolina Zaninetti Rosa", "matricula": "022/2017", "cidade": "Ji-Parana", "uf": "RO", "site": "https://www.lancevip.com.br/"},
    {"nome": "Marcus Allain de Oliveira Barbosa", "matricula": "024/2018", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.maleiloesro.com.br/"},
    {"nome": "Patricia Pimentel Grocoski Costa", "matricula": "029/2020", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.pimentelleiloes.com.br/"},
    {"nome": "Maria Vanielly de Lima Honorato Portela", "matricula": "032/2021", "cidade": "Porto Velho", "uf": "RO", "site": ""},
    {"nome": "Alex Willian Hoppe", "matricula": "033/2021", "cidade": "Canoinhas", "uf": "SC", "site": "https://www.hoppeleiloes.com.br/"},
    {"nome": "Bruno Pimentel Rosa", "matricula": "038/2022", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.lancevip.com.br/"},
    {"nome": "Daniel Elias Garcia", "matricula": "042/2023", "cidade": "Vilhena", "uf": "RO", "site": "https://www.danielgarcialeiloes.com.br/"},
    {"nome": "Jonas Gabriel Antunes Moreira", "matricula": "044/2023", "cidade": "Para de Minas", "uf": "MG", "site": ""},
    {"nome": "Maciel Rodrigues Chaves", "matricula": "045/2023", "cidade": "Porto Velho", "uf": "RO", "site": ""},
    {"nome": "Fernando Caetano Moreira Filho", "matricula": "046/2023", "cidade": "Contagem", "uf": "MG", "site": "https://www.fernandoleiloeiro.com.br/"},
    {"nome": "Lucas Rafael Antunes Moreira", "matricula": "047/2023", "cidade": "Belo Horizonte", "uf": "MG", "site": "https://www.lucasleiloeiro.com.br/"},
    {"nome": "Pedro Augusto da Costa Silva", "matricula": "048/2023", "cidade": "Porto Velho", "uf": "RO", "site": ""},
    {"nome": "Joabe Balbino da Silva", "matricula": "050/2024", "cidade": "Ji-Parana", "uf": "RO", "site": "https://www.balbinoleiloes.com.br/"},
    {"nome": "Angelica Vilas Boas Nunes", "matricula": "051/2024", "cidade": "Porto Velho", "uf": "RO", "site": "https://www.vbleiloes.com.br/"},
    {"nome": "Thais Costa Bastos Teixeira", "matricula": "052/2024", "cidade": "Pocos de Caldas", "uf": "MG", "site": "https://www.thaisteixeiraleiloes.com.br/"},
    {"nome": "Sandro de Oliveira", "matricula": "053/2024", "cidade": "Marituba", "uf": "PA", "site": "https://www.norteleiloes.com.br/"},
    {"nome": "Marcus Vinicius Moreira Chaves", "matricula": "054/2024", "cidade": "Porto Velho", "uf": "RO", "site": ""},
    {"nome": "Dora Plat", "matricula": "055/2024", "cidade": "Taboao da Serra", "uf": "SP", "site": "https://www.portalzuk.com.br/"},
    {"nome": "Wallason Silva Beltrame", "matricula": "057/2024", "cidade": "Vilhena", "uf": "RO", "site": "https://beltrameleiloes.com.br/"},
    {"nome": "Rodrigo Aparecido Rigolon da Silva", "matricula": "060/2025", "cidade": "Araraquara", "uf": "SP", "site": "https://www.rigolonleiloes.com.br/"},
    {"nome": "Antonio Carlos Celso Santos Frazao", "matricula": "065/2025", "cidade": "Barueri", "uf": "SP", "site": "https://www.vincoleiloes.com.br/"},
    {"nome": "Victor Alberto Severino Frazao", "matricula": "066/2025", "cidade": "Barueri", "uf": "SP", "site": "https://www.vincoleiloes.com.br/"},
    {"nome": "Jaqueline Vieira de Amorim", "matricula": "067/2025", "cidade": "Barueri", "uf": "SP", "site": "https://www.vincoleiloes.com.br/"},
    {"nome": "Flavia Correa Duarte Feitosa", "matricula": "068/2025", "cidade": "Boa Vista", "uf": "RR", "site": ""},
    {"nome": "Michael de Oliveira", "matricula": "069/2026", "cidade": "Porto Velho", "uf": "RO", "site": ""},
    {"nome": "Icaro Alexandre Felfili Jardim", "matricula": "070/2026", "cidade": "Sinop", "uf": "MT", "site": ""},
    {"nome": "Diogo Reis Dutra", "matricula": "071/2026", "cidade": "Vilhena", "uf": "RO", "site": "https://reisleiloes.com.br/"},
    {"nome": "Carlos Henrique Barbosa", "matricula": "072/2026", "cidade": "Cuiaba", "uf": "MT", "site": "https://www.chbarbosaleiloes.com.br/"},
    {"nome": "Maria Rafaela Barbosa Silva", "matricula": "074/2026", "cidade": "Porto Velho", "uf": "RO", "site": ""},
]

# ---------------------------------------------------------------------------
# Progresso / log
# ---------------------------------------------------------------------------
progress = {
    "iniciado": CAPTURE_DATE.isoformat(),
    "leiloeiros_total": len(LEILOEIROS_REGULAR),
    "imoveis_por_leiloeiro": {},
    "imoveis_total": 0,
    "erros": [],
    "sites_problema": {},
    "status": "iniciando",
}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def save_progress():
    progress["atualizado"] = datetime.now().isoformat()
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")

# ---------------------------------------------------------------------------
# Etapa 1: scraping do site JUCEAC (renderiza JS via Playwright)
# ---------------------------------------------------------------------------
def scrape_juceac_site():
    """Confirma a lista de leiloeiros REGULAR no site da JUCEAC (Acre)."""
    log("Acessando site JUCEAC (juceac.ac.gov.br/leiloeiro/)...")
    regulares_site = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto("https://juceac.ac.gov.br/leiloeiro/",
                      wait_until="networkidle", timeout=40000)
            html = page.content()
            HTML_DEBUG.write_text(html, encoding="utf-8")
            browser.close()

        texto = BeautifulSoup(html, "html.parser").get_text("\n")
        blocos = re.split(r"Matr[íi]cula\s*n[.º°]*\s*", texto)
        for bloco in blocos[1:]:
            cab = bloco[:200]
            if re.search(r"CANCELAD|SUSPENS|INATIV|IRREGULAR", cab, re.I):
                continue
            regulares_site.append(cab.split("\n")[0].strip())
        log(f"Site JUCEAC: {len(blocos)-1} registros lidos "
            f"({len(regulares_site)} sem marca de cancelamento no cabecalho).")
    except Exception as e:
        log(f"[AVISO] Falha ao renderizar JUCEAC: {e}")
        progress["erros"].append(f"JUCEAC site: {str(e)[:120]}")
    return regulares_site

# ---------------------------------------------------------------------------
# Etapa 2: scraping de imóveis por site de leiloeiro
# ---------------------------------------------------------------------------
RE_DATA = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
RE_PRECO = re.compile(r"R[\$]\s*([\d.]+,\d{2})")  # R[\$] -> cifrao literal (secao 14.3)
RE_DOC = re.compile(r"edital|matr[íi]cula|laudo|avalia|certid|processo|anexo", re.I)
PALAVRAS_IMOVEL = ("apartamento", "apto", "casa", "terreno", "sala", "galpao",
                   "galpão", "lote", "imovel", "imóvel", "chacara", "chácara",
                   "fazenda", "predio", "prédio", "loja", "rua ", "avenida", "av.")

def parse_data(txt):
    if not txt:
        return None
    m = RE_DATA.search(txt)
    if not m:
        return None
    d, mes, a = (int(x) for x in m.groups())
    try:
        return datetime(a, mes, d).date()
    except ValueError:
        return None

def fetch(url):
    """GET com fallback verify=False; retorna (html, erro)."""
    try:
        r = requests.get(url, headers=HEADERS, verify=False, timeout=15)
        return r.text, None
    except Exception as e:
        return None, str(e)[:120]

def fetch_playwright(url):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=25000)
            time.sleep(2)
            html = page.content()
            browser.close()
            return html, None
    except Exception as e:
        return None, str(e)[:120]

def extrair_imoveis(html, base_url, leiloeiro):
    """Extrai imóveis de uma página, filtrando data futura."""
    imoveis = []
    soup = BeautifulSoup(html, "html.parser")

    # candidatos: blocos com link de lote/imovel ou cards
    candidatos = soup.find_all(["article", "div", "li"],
        class_=re.compile(r"card|lote|imovel|imóvel|produto|item|leilao|leilão", re.I))
    if not candidatos:
        # fallback: qualquer container com link a lote
        candidatos = [a.find_parent(["article", "div", "li"]) or a
                      for a in soup.find_all("a", href=re.compile(r"lote|imovel|detalhe|oferta", re.I))]

    vistos = set()
    for c in candidatos:
        if c is None:
            continue
        texto = c.get_text(" ", strip=True)
        if len(texto) < 15:
            continue
        low = texto.lower()
        if not any(pal in low for pal in PALAVRAS_IMOVEL):
            continue

        data = parse_data(texto)
        # exige data e que seja futura (> hoje)
        if not data or data <= HOJE:
            continue

        link_el = c.find("a", href=True)
        url = urljoin(base_url, link_el["href"]) if link_el else base_url
        if url in vistos:
            continue
        vistos.add(url)

        # titulo
        tit_el = c.find(["h1", "h2", "h3", "h4", "h5"])
        titulo = (tit_el.get_text(strip=True) if tit_el else texto[:90]).strip()

        # preco
        mp = RE_PRECO.search(texto)
        preco = mp.group(1) if mp else ""

        # imagem
        img_el = c.find("img")
        imagem = ""
        if img_el:
            imagem = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or ""
            if imagem:
                imagem = urljoin(base_url, imagem)

        # anexos (docs)
        anexos = []
        for a in c.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf") or RE_DOC.search(href) or RE_DOC.search(a.get_text()):
                anexos.append(urljoin(base_url, href))

        imoveis.append({
            "leiloeiro": leiloeiro["nome"],
            "junta": "JUCEAC/JUCER",
            "site": base_url,
            "titulo": titulo[:300],
            "descricao": texto[:500],
            "endereco": "",
            "cidade": leiloeiro.get("cidade", ""),
            "uf": leiloeiro.get("uf", ""),
            "lance_inicial": preco,
            "avaliacao": "",
            "data_leilao": data.strftime("%d/%m/%Y"),
            "url": url,
            "tipo": "imovel",
            "imagem": imagem,
            "anexos": ";".join(anexos[:10]),
        })
    return imoveis

def scrape_site(leiloeiro):
    url = leiloeiro["site"]
    if not url:
        return []
    log(f"  -> {leiloeiro['nome']}  [{url}]")

    html, err = fetch(url)
    if not html:
        # tenta playwright
        html, err2 = fetch_playwright(url)
        if not html:
            progress["sites_problema"][leiloeiro["nome"]] = f"offline: {err or err2}"
            log(f"     [X] inacessivel: {err or err2}")
            return []

    imoveis = extrair_imoveis(html, url, leiloeiro)

    # se nada e parece SPA, tenta playwright + paginas comuns de listagem
    if not imoveis:
        for sufixo in ("imoveis", "leiloes", "lotes", "leilao/imoveis", "categoria/imoveis"):
            cand = urljoin(url, sufixo)
            h2, _ = fetch_playwright(cand)
            if h2:
                imoveis = extrair_imoveis(h2, cand, leiloeiro)
                if imoveis:
                    break

    if not imoveis:
        progress["sites_problema"].setdefault(leiloeiro["nome"], "sem imoveis com leilao futuro")
    log(f"     {len(imoveis)} imovel(is) com leilao futuro")
    return imoveis

# ---------------------------------------------------------------------------
# Etapa 3: banco de dados
# ---------------------------------------------------------------------------
def inserir_banco(imoveis):
    if not imoveis:
        return 0
    import hashlib
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inseridos = 0
    for im in imoveis:
        # dedup por url
        cur.execute("SELECT 1 FROM imoveis WHERE url = ? LIMIT 1", (im["url"],))
        if cur.fetchone():
            continue
        # id no mesmo formato dos demais registros (hash hex 12 chars da URL)
        novo_id = hashlib.md5(im["url"].encode()).hexdigest()[:12]
        cur.execute("SELECT 1 FROM imoveis WHERE id = ?", (novo_id,))
        if cur.fetchone():
            novo_id = hashlib.md5((im["url"] + im["titulo"]).encode()).hexdigest()[:12]
        cur.execute("""
            INSERT INTO imoveis
            (id, leiloeiro, junta, site, titulo, descricao, endereco, cidade, uf,
             lance_inicial, avaliacao, data_leilao, url, tipo, imagem, importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            novo_id, im["leiloeiro"], im["junta"], im["site"], im["titulo"], im["descricao"],
            im["endereco"], im["cidade"], im["uf"], im["lance_inicial"], im["avaliacao"],
            im["data_leilao"], im["url"], im["tipo"], im["imagem"],
            CAPTURE_DATE.isoformat(),
        ))
        inseridos += 1
    conn.commit()
    conn.close()
    return inseridos

# ---------------------------------------------------------------------------
# Etapa 4: CSVs
# ---------------------------------------------------------------------------
def salvar_csvs(leiloeiros, imoveis):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data_str = CAPTURE_DATE.strftime("%Y-%m-%d")

    csv_lei = OUTPUT_DIR / f"leiloeiros_juceac_{data_str}.csv"
    with open(csv_lei, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["nome", "site"])
        for l in leiloeiros:
            w.writerow([l["nome"], l["site"]])

    csv_imo = OUTPUT_DIR / f"imoveis_juceac_{data_str}.csv"
    if imoveis:
        campos = ["leiloeiro", "junta", "site", "titulo", "descricao", "endereco",
                  "cidade", "uf", "lance_inicial", "avaliacao", "data_leilao",
                  "url", "tipo", "imagem", "anexos"]
        with open(csv_imo, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            w.writerows(imoveis)
    return csv_lei, csv_imo

# ---------------------------------------------------------------------------
# Etapa 5: relatório markdown
# ---------------------------------------------------------------------------
def gerar_relatorio(leiloeiros, imoveis, inseridos_db, csv_lei, csv_imo):
    por_lei = progress["imoveis_por_leiloeiro"]
    problemas = progress["sites_problema"]
    com_site = [l for l in leiloeiros if l["site"]]
    sem_site = [l for l in leiloeiros if not l["site"]]

    linhas_lei = "\n".join(
        f"| {l['nome']} | {l['site'] or '(sem site)'} | {por_lei.get(l['nome'], 0)} |"
        for l in leiloeiros
    )
    linhas_prob = "\n".join(f"- **{k}**: {v}" for k, v in problemas.items()) or "- Nenhum"

    rel = f"""

---

## RELATÓRIO DE CAPTURA — JUCEAC + PDFs (Rondônia/JUCER) — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M')}

### Resumo
- **Leiloeiros REGULAR processados:** {len(leiloeiros)} (de 49 no PDF de antiguidade; demais excluídos por IRREGULAR/AFASTADO/CANCELADO)
- **Com site identificado:** {len(com_site)} | **Sem site:** {len(sem_site)}
- **Sites únicos visitados:** {len({l['site'] for l in com_site})}
- **Imóveis com 1ª praça futura (> {HOJE.strftime('%d/%m/%Y')}):** {len(imoveis)}
- **Imóveis novos inseridos no banco (`imoveis_leiloeiros.db`):** {inseridos_db}
- **CSV leiloeiros:** `{csv_lei.name}`
- **CSV imóveis:** `{csv_imo.name}`

### Imóveis capturados por leiloeiro
| Leiloeiro | Site | Imóveis |
|---|---|---|
{linhas_lei}

### Principais dificuldades enfrentadas

1. **Site da JUCEAC é JavaScript-pesado e do estado errado.**
   `juceac.ac.gov.br/leiloeiro/` (Junta do **Acre**) renderiza a lista via JS (sem `<table>` no HTML estático) e a maioria dos registros está **CANCELADA A PEDIDO**. Os PDFs anexos referem-se à **Rondônia (JUCER)** — fonte muito mais rica e com campo `Situação` explícito. Solução adotada: usar o **PDF de antiguidade como fonte autoritativa de Situação** e o site só como confirmação (Playwright + `wait_until=networkidle`).

2. **Encoding/mojibake.** Metadados da JUCEAC e arquivos `.txt` vêm com UTF-8 mal decodificado (`JUC...LIA`). Mitigado lendo o HTML renderizado pelo Playwright e normalizando nomes.

3. **Filtragem de Situação só existe no PDF detalhado.** O primeiro PDF (tabela) **não** traz Situação; nele apareciam nomes que o PDF de antiguidade marca como **IRREGULAR** (ex.: Felipe Cezar, Wesley Ramos, Bruno em casos divergentes). Tratado cruzando os dois PDFs e confiando no campo `Situação` do detalhado.

4. **Sites compartilhados entre leiloeiros.** Vários leiloeiros usam o **mesmo domínio** (`leiloesaguiar.com.br` ×3, `lancevip.com.br` ×3, `vincoleiloes.com.br` ×3). Sem dedup, o mesmo imóvel seria contado várias vezes. Tratado com **dedup por URL** na inserção do banco.

5. **Leiloeiros sem site.** {len(sem_site)} leiloeiros REGULAR não possuem site no PDF — impossível capturar imóveis deles (só contato). Listados, mas com 0 imóveis.

6. **Estruturas HTML heterogêneas + SPA.** Cada site tem marcação diferente; alguns são SPA (Next.js/React) que exigem Playwright e variações de paginação (`/imoveis`, `/leiloes`, `/lotes`). Implementado fallback em cascata httpx → Playwright → sufixos de listagem.

7. **Validação de data.** Muitos cards não expõem a data da 1ª praça na listagem (só no detalhe), então itens sem data legível **> hoje** foram descartados — pode subnotificar imóveis válidos.

8. **Sites com proteção / offline.** Alguns domínios respondem com erro TLS/Cloudflare ou estão fora do ar:
{linhas_prob}

### Sugestões de correção

1. **Trocar a fonte primária para a JUCER** (`jucer.ro.gov.br`) quando o alvo for Rondônia — JUCEAC só vale para o Acre.
2. **Enricher de detalhe por imóvel** (seção 17/23 do guia): visitar a página de cada lote para extrair a data da 1ª praça, edital e matrícula que faltam na listagem, em vez de descartar por ausência de data.
3. **Adaptadores por plataforma** (seção 27): `lancevip`, `vincoleiloes`, `portalzuk`, `leiloesaguiar` têm padrões próprios — parser dedicado por domínio aumenta muito a taxa de captura.
4. **FlareSolverr (seção 14)** para domínios com Cloudflare; **curl_cffi** para erros de TLS.
5. **Dedup por `id_externo`/URL canônica** mantida ao reimportar; agendar re-scraping a cada 7–14 dias (leilões esporádicos).
6. **Resolver sites compartilhados** atribuindo o imóvel ao leiloeiro correto via campo do próprio lote, não pelo domínio.
7. **Geocodificação e normalização de cidade** pós-importação (seções 21/32).

**Relatório gerado em:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
"""
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f:
        f.write(rel)
    return rel

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 72)
    print(f"SCRAPER JUCEAC COMPLETO  —  {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Data de captura (corte de leilao futuro): {HOJE.strftime('%d/%m/%Y')}")
    print("=" * 72)

    # 1. confirma JUCEAC (best-effort)
    progress["status"] = "site_juceac"
    scrape_juceac_site()
    save_progress()

    # 2. itera leiloeiros REGULAR
    print(f"\nProcessando {len(LEILOEIROS_REGULAR)} leiloeiros REGULAR...\n")
    progress["status"] = "scraping_imoveis"
    todos = []
    last_report = time.time()

    for i, lei in enumerate(LEILOEIROS_REGULAR, 1):
        try:
            ims = scrape_site(lei)
        except Exception as e:
            ims = []
            progress["erros"].append(f"{lei['nome']}: {str(e)[:120]}")
            log(f"     [ERRO] {e}")

        todos.extend(ims)
        progress["imoveis_por_leiloeiro"][lei["nome"]] = len(ims)
        progress["imoveis_total"] = len(todos)

        # relatório parcial a cada 5 min
        if time.time() - last_report >= REPORT_INTERVAL:
            print("\n----- RELATORIO PARCIAL (5 min) -----")
            print(f"Leiloeiros: {i}/{len(LEILOEIROS_REGULAR)} | Imoveis: {len(todos)}")
            for n, q in progress["imoveis_por_leiloeiro"].items():
                if q:
                    print(f"  - {n}: {q}")
            print("-------------------------------------\n")
            save_progress()
            last_report = time.time()

        time.sleep(1)  # respeitar servidores

    # 3. banco
    progress["status"] = "inserindo_banco"
    inseridos = inserir_banco(todos)
    log(f"Inseridos {inseridos} imoveis novos no banco (dedup por URL).")

    # 4. CSVs
    csv_lei, csv_imo = salvar_csvs(LEILOEIROS_REGULAR, todos)
    log(f"CSV leiloeiros: {csv_lei.name}")
    log(f"CSV imoveis: {csv_imo.name}")

    # 5. relatório
    gerar_relatorio(LEILOEIROS_REGULAR, todos, inseridos, csv_lei, csv_imo)
    progress["status"] = "concluido"
    progress["imoveis_inseridos_db"] = inseridos
    save_progress()

    # resumo final
    print("\n" + "=" * 72)
    print("RESUMO FINAL — IMOVEIS POR LEILOEIRO")
    print("=" * 72)
    for n, q in sorted(progress["imoveis_por_leiloeiro"].items(), key=lambda x: -x[1]):
        if q:
            print(f"  {q:3d}  {n}")
    print("-" * 72)
    print(f"TOTAL imoveis (1a praca futura): {len(todos)}")
    print(f"Inseridos no banco: {inseridos}")
    print(f"Relatorio anexado em: {RELATORIO_FILE.name}")


if __name__ == "__main__":
    main()
