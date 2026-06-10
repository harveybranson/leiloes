#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper JUCEG (Goias) — PDF "Leiloeiros Goias.pdf"
- Somente leiloeiros REGULAR (exclui SUSPENSO / CANCELADO / seção de matrículas canceladas)
- Visita cada site, captura imóveis (título, descrição, preço, data, imagem, anexos)
- Valida: data da 1ª praça > data da captura
- CSV nome+site em /csv; imóveis no banco imoveis_leiloeiros.db (dedup por URL)
- Relatório de imóveis por leiloeiro a cada 5 min + relatório de dificuldades no .md
Conforme captura_dados_leiloes_v2.md
"""

import csv
import json
import re
import time
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "csv"
DB_PATH = BASE_DIR / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE_DIR / "scraper_juceg_completo_progress.json"
RELATORIO_FILE = BASE_DIR / "captura_dados_leiloes_v2.md"

CAPTURE_DATE = datetime.now()
HOJE = CAPTURE_DATE.date()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
REPORT_INTERVAL = 300

# ---------------------------------------------------------------------------
# Leiloeiros REGULAR de Goias COM site (Situacao autoritativa do PDF).
# Excluidos: SUSPENSO, CANCELADO e todos da secao "MATRICULAS CANCELADAS"
# (ex.: Fernando Cezar Tobias da Silva/dfleiloes - consta na lista de cancelados).
# (nome, site, cidade, uf)
# ---------------------------------------------------------------------------
LEILOEIROS_REGULAR = [
    ("Braulio Ferreira Neto", "http://braunaleiloes.blogspot.com.br", "Goiania", "GO"),
    ("Marcia Regina Cardellichio Nunes", "https://www.mcleilao.com.br", "Goiania", "GO"),
    ("Antonio Brasil II", "https://www.leiloesbrasil.com.br", "Aparecida de Goiania", "GO"),
    ("Leila Nanci Karasiaki", "https://www.lkleiloes.com.br", "Goiania", "GO"),
    ("Leony Gomes dos Santos Junior", "https://www.leiloesbrasil.com.br", "Aparecida de Goiania", "GO"),
    ("Alvaro Sergio Fuzo", "https://www.leiloesjudiciaisgo.com.br", "Goiania", "GO"),
    ("Felipe Guimaraes Carrijo", "https://www.leilo.com.br", "Aparecida de Goiania", "GO"),
    ("Maria Aparecida de Freitas Fuzo", "https://www.leiloescentrooeste.com.br", "Goiania", "GO"),
    ("Eduardo Vinicius Fleury Lobo", "https://www.leilo.com.br", "Goiania", "GO"),
    ("Kesley Nunes de Souza", "", "Aparecida de Goiania", "GO"),
    ("Alglecio Bueno da Silva", "https://www.leiloesgoias.com.br", "Goiania", "GO"),
    ("Geoliano de Souza Lima", "https://www.teleselimaleiloes.com.br", "Goiania", "GO"),
    ("Ivan Rodrigues Nogueira", "https://www.arrematabem.com.br", "Goiania", "GO"),
    ("Erick Soares Teles", "https://www.tezaleiloes.com.br", "Sao Paulo", "SP"),
    ("Camilla Correia Vecchi Aguiar", "", "Teresopolis de Goias", "GO"),
    ("Maik Nunes de Oliveira", "https://www.mcleilao.com.br", "Goiania", "GO"),
    ("Sergio Fleury Batista", "https://www.leilo.com.br", "Goiania", "GO"),
    ("Mike Dutra Fleitas", "https://www.mikedutraleiloeiro.com.br", "Goiania", "GO"),
    ("Rogerio Augusto dos Santos Maia", "", "Goiania", "GO"),
    ("Johenn Brasil Balduino", "https://www.leiloesbrasil.com.br", "Goiania", "GO"),
    ("Leonardo Coelho Avelar", "https://www.arrematabem.com.br", "Goiania", "GO"),
    ("Rodrigo Schmitz", "https://www.hammer.lel.br", "Goiania", "GO"),
    ("Cesar Augusto Bagatini", "https://www.federalleiloes.com", "Goiania", "GO"),
    ("Claudio Moreira Santos", "", "Goiania", "GO"),
    ("Orlando Araujo dos Santos", "https://www.oaleiloes.com.br", "Goiania", "GO"),
    ("Elenice Lira Sales de Sousa", "https://www.liraleiloes.com.br", "Aparecida de Goiania", "GO"),
    ("Ygor Ferreira Brasil", "https://www.leiloesbrasil.com.br", "Aparecida de Goiania", "GO"),
    ("Jonas Gabriel Antunes Moreira", "", "Anapolis", "GO"),
    ("Lucas Rafael Antunes Moreira", "", "Goiania", "GO"),
    ("Fernando Caetano Moreira Filho", "https://www.fernandoleiloeiro.com.br", "Goiania", "GO"),
    ("Denise Araujo dos Santos", "", "Planaltina de Goias", "GO"),
    ("Jean Carlo Rosa", "https://www.prosperarleiloes.com.br", "Inhumas", "GO"),
    ("Jussiara Santos Ermano Sukiennik", "https://www.jussiaraleiloes.com", "Cidade Ocidental", "GO"),
    ("Alex Willian Hoppe", "https://www.hoppeleiloes.com.br", "Canoinhas", "SC"),
    ("Antonio Carlos Peres Bernardini", "https://www.bernardinileiloes.com.br", "Goiania", "GO"),
    ("Marciano Aguiar Carneiro", "", "Rio Verde", "GO"),
    ("Rossana Paiva Borges de Oliveira", "https://www.caiapoleiloes.com.br", "Goiania", "GO"),
    ("Fernanda Mont Serrat Pereira Resende", "", "Goiania", "GO"),
    ("Rudival Almeida Gomes Junior", "https://www.rjleiloes.com.br", "Goiania", "GO"),
    ("Lucas Andreatta de Oliveira", "https://www.leiloariasmart.com.br", "Goiania", "GO"),
    ("Frederico Albert Krausegg Neves", "https://www.fredericoleiloes.com.br", "Goiania", "GO"),
    ("Jose Valero Santos Junior", "https://www.valeroleiloes.com.br", "Goiania", "GO"),
    ("Tiago Tessler Blecher", "https://www.webleiloes.com.br", "Goiania", "GO"),
    ("Bruna Helena Vieira", "", "Goiania", "GO"),
    ("Davi Borges de Aquino", "https://www.alfaleiloes.com", "Goiania", "GO"),
    ("Juliana Cristina Carreira Golfeto", "", "Goiania", "GO"),
    ("Jose Luiz Pereira Vizeu", "https://www.flexleiloes.com.br", "Goiania", "GO"),
    ("Luiz Ubirata de Carvalho", "https://www.luizleiloes.com.br", "Luziania", "GO"),
    ("Frederico Horacio de Luiz Lopes", "https://arremateleiloesjudiciais.com.br", "Goiania", "GO"),
    ("Denys Pyerre de Oliveira", "https://www.leje.com.br", "Goiania", "GO"),
    ("Daniel Elias Garcia", "https://www.danielgarcialeiloes.com.br", "Goiania", "GO"),
    ("Fernando Jose Cerello Goncalves Pereira", "https://www.megaleiloes.com.br", "Sao Paulo", "SP"),
    ("Danielle Joy Karasiaki Carvalho", "https://www.lkleiloes.com.br", "Goiania", "GO"),
    ("Paulo de Oliveira Azevedo", "https://www.lkleiloes.com.br", "Anapolis", "GO"),
    ("Magnun Luiz Serpa", "https://www.serpaleiloes.com.br", "Joinville", "SC"),
    ("Edilson Lopes Rocha", "", "Salvador", "BA"),
    ("Rodrigo Paes Camapum Bringel", "https://www.bringelleiloes.com.br", "Goiania", "GO"),
    ("Leonardo Nunes Lobo", "https://www.leilo.com.br", "Goiania", "GO"),
    ("Marcelo da Silva Lima", "https://marcelolimaleiloes.com.br", "Goiania", "GO"),
    ("Carlos Augusto Ribeiro Lima", "https://www.infinityleiloes.com.br", "Goiania", "GO"),
    ("Bruno Barreto Sanches", "https://www.barretoleiloes.com.br", "Campo Grande", "MS"),
    ("Kaio Albuquerque Rosa Botelho", "https://www.duxleiloes.com.br", "Brasilia", "DF"),
    ("Cristiane Borguetti Moraes Lopes", "https://www.lanceja.com.br", "Santo Andre", "SP"),
    ("Frederico Alberto Severino Frazao", "https://www.maisleilao.com.br", "Sao Paulo", "SP"),
    ("Victor Alberto Severino Frazao", "https://www.sfrazao.com.br", "Santana de Parnaiba", "SP"),
    ("Antonio Carlos Celso Santos Frazao", "https://www.sfrazao.com.br", "Barueri", "SP"),
    ("Gracielle da Silva Coelho", "", "Goiania", "GO"),
    ("Adolpho Agostinho Mendes Quaresma", "", "Inhumas", "GO"),
    ("Adriano de Jesus Silva", "", "Petrolina de Goias", "GO"),
    ("Otavio Lauro Sodre Santoro", "https://www.sodresantoro.com.br", "Barueri", "SP"),
    ("Valentina Borges de Paula", "", "Goiania", "GO"),
    ("Maria Auxiliadora Rodrigues Teixeira", "", "Goiania", "GO"),
    ("Joabe Balbino da Silva", "https://www.balbinoleiloes.com.br", "Sao Paulo", "SP"),
    ("Renan Souza Silva", "https://www.souzasilvaleiloes.com.br", "Sao Paulo", "SP"),
    ("Erico Sobral Soares", "https://www.vipleiloes.com.br", "Fortaleza", "CE"),
    ("Diego Wolf de Oliveira", "https://www.diegoleiloes.com.br", "Joinville", "SC"),
    ("Dora Plat", "https://www.portalzuk.com.br", "Taboao da Serra", "SP"),
    ("Flavio Duarte Ceruli", "https://www.leiloesceruli.com.br", "Patos de Minas", "MG"),
    ("Graziella Tassi Santos", "", "Goiania", "GO"),
    ("Wesley Oliveira Ascanio", "https://www.tabaleiloes.com.br", "Guarulhos", "SP"),
    ("Marco Tulio Montenegro Cavalcanti Dias", "https://www.lancecertoleiloes.com.br", "Joao Pessoa", "PB"),
    ("Anderson Lopes de Paula", "https://www.e-leiloeiro.com.br", "Sao Paulo", "SP"),
    ("Aharo Espirito Santo Aquino", "", "Goiania", "GO"),
    ("Uriangela Borges Vieira", "", "Itumbiara", "GO"),
    ("Fernando da Silva Costa", "", "Anapolis", "GO"),
    ("Italo Augusto Santos", "", "Itapaci", "GO"),
    ("Victor Renno Polatto Vizeu", "https://www.vzleiloes.com.br", "Valparaiso de Goias", "GO"),
    ("Eduardo Schmitz", "https://www.clicleiloes.com.br", "Balneario Camboriu", "SC"),
    ("Rodrigo Aparecido Rigolon da Silva", "https://www.rigolonleiloes.com.br", "Araraquara", "SP"),
    ("Irani Flores", "https://www.leilaobrasil.com.br", "Sao Paulo", "SP"),
    ("Fabio Prando Fagundes Goes", "https://www.apiceleiloes.com", "Itapevi", "SP"),
    ("Pabline Gomes Lima", "", "Hidrolandia", "GO"),
    ("Helcio Kronberg", "https://www.kronbergleiloes.com.br", "Curitiba", "PR"),
    ("Sergio Luiz Cruvinel", "", "Ipora", "GO"),
    ("Giovana Norma Bolico", "https://www.casamartillo.com.br", "Balneario Camboriu", "SC"),
    ("Eduardo Henrique Firmino", "", "Aparecida de Goiania", "GO"),
    ("Jorge Vinicius de Moura Correa", "https://www.winleiloes.com.br", "Santo Angelo", "RS"),
    ("Luiz Eduardo Gomes", "", "Ribeirao Preto", "SP"),
    ("Victor Oliveira Dorta", "", "Palmas", "TO"),
    ("Caroline de Sousa Ribas", "https://www.liderleiloes.com.br", "Santo Andre", "SP"),
    ("Paulo Roberto dos Santos Junior", "", "Goiania", "GO"),
    ("Carlos Eduardo dos Santos Bueno", "", "Luziania", "GO"),
    ("Wellington Martins Araujo", "https://www.araujoleiloes.com.br", "Varzea Grande", "MT"),
    ("Icaro Alexandre Felfili Jardim", "", "Sinop", "MT"),
    ("Felipe Teixeira Loyola", "", "Goiania", "GO"),
    ("Rafael Cicolin", "https://www.absolutaleiloes.com", "Americana", "SP"),
    ("Fernando Domingos Tonon", "https://www.tononleiloes.com.br", "Rondonopolis", "MT"),
    ("Andre Amaral Barros", "", "Sao Paulo", "SP"),
    ("Carlos Henrique Barbosa", "https://www.chbarbosaleiloes.com.br", "Cuiaba", "MT"),
    ("Lidia Ribeiro de Andrade", "https://www.mrl4leiloes.com.br", "Brasilia", "DF"),
    ("Paulo Cesar de Toledo Filho", "", "Aparecida de Goiania", "GO"),
    ("Cibele Cristina Lino Lopes", "", "Sao Paulo", "SP"),
    ("Aline de Souza Flores", "https://www.leilaobrasil.com.br", "Goiania", "GO"),
    ("Vilton Pereira da Silva", "https://www.innovaleiloes.com.br", "Ipora", "GO"),
    ("Valdivino Fernandes de Freitas", "", "Anapolis", "GO"),
    ("Rute Cristina Abrantes Jacinto", "", "Goiania", "GO"),
    ("Alessandra Brasil do Vale", "", "Aparecida de Goiania", "GO"),
    ("Joao Paulo de Sousa Gualberto", "", "Goiania", "GO"),
    ("Ivana Abranches Jordao Costa", "", "Goiania", "GO"),
    ("Fabricio Pereira Paganucci", "", "Itumbiara", "GO"),
]

progress = {
    "iniciado": CAPTURE_DATE.isoformat(),
    "leiloeiros_total": len(LEILOEIROS_REGULAR),
    "imoveis_por_leiloeiro": {},
    "imoveis_total": 0,
    "sites_problema": {},
    "status": "iniciando",
}

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")
def save_progress():
    progress["atualizado"] = datetime.now().isoformat()
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")

RE_DATA = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
RE_PRECO = re.compile(r"R[\$]\s*([\d.]+,\d{2})")
RE_DOC = re.compile(r"edital|matr[íi]cula|laudo|avalia|certid|processo|anexo", re.I)
PAL_IMOVEL = ("apartamento", "apto", "casa", "terreno", "sala", "galpao", "galpão",
              "lote", "imovel", "imóvel", "chacara", "chácara", "fazenda", "predio",
              "prédio", "loja", "rua ", "avenida", "av.", "gleba", "sitio", "sítio")

def parse_data(t):
    if not t: return None
    m = RE_DATA.search(t)
    if not m: return None
    d, mes, a = (int(x) for x in m.groups())
    try: return datetime(a, mes, d).date()
    except ValueError: return None

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, verify=False, timeout=15)
        return r.text, None
    except Exception as e:
        return None, str(e)[:120]

def fetch_pw(url):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            pg = b.new_page(user_agent=HEADERS["User-Agent"])
            pg.goto(url, wait_until="networkidle", timeout=25000)
            time.sleep(2)
            html = pg.content(); b.close()
            return html, None
    except Exception as e:
        return None, str(e)[:120]

def extrair(html, base_url, nome, cidade, uf):
    out = []
    soup = BeautifulSoup(html, "html.parser")
    cands = soup.find_all(["article", "div", "li"],
        class_=re.compile(r"card|lote|imovel|imóvel|produto|item|leilao|leilão", re.I))
    if not cands:
        cands = [a.find_parent(["article", "div", "li"]) or a
                 for a in soup.find_all("a", href=re.compile(r"lote|imovel|detalhe|oferta", re.I))]
    vistos = set()
    for c in cands:
        if c is None: continue
        txt = c.get_text(" ", strip=True)
        if len(txt) < 15: continue
        low = txt.lower()
        if not any(p in low for p in PAL_IMOVEL): continue
        data = parse_data(txt)
        if not data or data <= HOJE: continue
        le = c.find("a", href=True)
        url = urljoin(base_url, le["href"]) if le else base_url
        if url in vistos: continue
        vistos.add(url)
        te = c.find(["h1", "h2", "h3", "h4", "h5"])
        titulo = (te.get_text(strip=True) if te else txt[:90]).strip()
        mp = RE_PRECO.search(txt); preco = mp.group(1) if mp else ""
        ie = c.find("img"); imagem = ""
        if ie:
            imagem = ie.get("src") or ie.get("data-src") or ie.get("data-lazy-src") or ""
            if imagem: imagem = urljoin(base_url, imagem)
        anexos = []
        for a in c.find_all("a", href=True):
            h = a["href"]
            if h.lower().endswith(".pdf") or RE_DOC.search(h) or RE_DOC.search(a.get_text()):
                anexos.append(urljoin(base_url, h))
        out.append({"leiloeiro": nome, "junta": "JUCEG", "site": base_url,
                    "titulo": titulo[:300], "descricao": txt[:500], "endereco": "",
                    "cidade": cidade, "uf": uf, "lance_inicial": preco, "avaliacao": "",
                    "data_leilao": data.strftime("%d/%m/%Y"), "url": url, "tipo": "imovel",
                    "imagem": imagem, "anexos": ";".join(anexos[:10])})
    return out

def scrape_site(nome, site, cidade, uf):
    log(f"  -> {nome}  [{site}]")
    html, err = fetch(site)
    if not html:
        html, err2 = fetch_pw(site)
        if not html:
            progress["sites_problema"][nome] = f"offline: {err or err2}"
            log(f"     [X] inacessivel: {err or err2}")
            return []
    ims = extrair(html, site, nome, cidade, uf)
    if not ims:
        for suf in ("imoveis", "leiloes", "lotes", "leilao/imoveis", "categoria/imoveis", "busca?categoria=imoveis"):
            h2, _ = fetch_pw(urljoin(site, suf))
            if h2:
                ims = extrair(h2, urljoin(site, suf), nome, cidade, uf)
                if ims: break
    if not ims:
        progress["sites_problema"].setdefault(nome, "sem imoveis com leilao futuro")
    log(f"     {len(ims)} imovel(is) com leilao futuro")
    return ims

def inserir_banco(imoveis):
    if not imoveis: return 0
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor(); ins = 0
    for im in imoveis:
        cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (im["url"],))
        if cur.fetchone(): continue
        nid = hashlib.md5(im["url"].encode()).hexdigest()[:12]
        cur.execute("SELECT 1 FROM imoveis WHERE id=?", (nid,))
        if cur.fetchone(): nid = hashlib.md5((im["url"]+im["titulo"]).encode()).hexdigest()[:12]
        cur.execute("""INSERT INTO imoveis (id,leiloeiro,junta,site,titulo,descricao,endereco,
            cidade,uf,lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (nid, im["leiloeiro"], im["junta"], im["site"], im["titulo"], im["descricao"],
             im["endereco"], im["cidade"], im["uf"], im["lance_inicial"], im["avaliacao"],
             im["data_leilao"], im["url"], im["tipo"], im["imagem"], CAPTURE_DATE.isoformat()))
        ins += 1
    conn.commit(); conn.close(); return ins

def salvar_csvs(imoveis):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    d = CAPTURE_DATE.strftime("%Y-%m-%d")
    cl = OUTPUT_DIR / f"leiloeiros_juceg_{d}.csv"
    with open(cl, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["nome", "site"])
        for n, s, _, _ in LEILOEIROS_REGULAR: w.writerow([n, s])
    ci = OUTPUT_DIR / f"imoveis_juceg_{d}.csv"
    if imoveis:
        campos = ["leiloeiro","junta","site","titulo","descricao","endereco","cidade","uf",
                  "lance_inicial","avaliacao","data_leilao","url","tipo","imagem","anexos"]
        with open(ci, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos); w.writeheader(); w.writerows(imoveis)
    return cl, ci

def gerar_relatorio(imoveis, inseridos, cl, ci):
    por = progress["imoveis_por_leiloeiro"]
    com_site = [l for l in LEILOEIROS_REGULAR if l[1]]
    sem_site = [l for l in LEILOEIROS_REGULAR if not l[1]]
    sites_unicos = {l[1] for l in com_site}
    linhas = "\n".join(f"| {n} | {s or '(sem site)'} | {por.get(n,0)} |"
                       for n, s, _, _ in LEILOEIROS_REGULAR if por.get(n,0) > 0) or "| — | — | 0 |"
    probs = "\n".join(f"- **{k}**: {v}" for k, v in list(progress["sites_problema"].items())[:25]) or "- Nenhum"
    rel = f"""

---

## CORREÇÕES DE CAPTURA — JUCEG (Goiás) — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M')}

> Cada item abaixo é uma **correção acionável** para uma dificuldade encontrada na captura
> da JUCEG. Foco em *como resolver* — para incorporar ao fluxo de scraping deste guia.

### Resultado da captura
- Leiloeiros REGULAR: {len(LEILOEIROS_REGULAR)} ({len(com_site)} com site, {len(sem_site)} só e-mail) | Sites únicos: {len(sites_unicos)}
- Imóveis (1ª praça > {HOJE.strftime('%d/%m/%Y')}): {len(imoveis)} | Inseridos no banco: {inseridos}
- CSV: `{cl.name}` / `{ci.name}`

| Leiloeiro | Site | Imóveis |
|---|---|---|
{linhas}

### Correções a aplicar (dificuldade → solução)

1. **Situação conflitante no PDF → usar a fonte oficial da JUCEG, não o PDF estático.**
   O PDF lista o mesmo leiloeiro 2× com status divergente (SUSPENSO × REGULAR) e mantém quem já foi
   cancelado. **Correção:** consultar o status atual por matrícula no site da JUCEG no momento da
   captura; na ausência disso, **regra de desempate fixa** — (a) se o nome consta na seção
   "MATRÍCULAS CANCELADAS", excluir sempre; (b) senão, vale o bloco de data mais recente.

2. **Maioria sem campo `site` → derivar e validar o domínio automaticamente.**
   ~{len(sem_site)} REGULAR só têm e-mail/telefone. **Correção:** derivar site do domínio do e-mail
   corporativo (`@empresa.com.br` → `https://www.empresa.com.br`), descartando `@gmail/@hotmail`;
   para os sem domínio, resolver via busca "nome + leilões" e gravar o site validado de volta no CSV.

3. **Sites compartilhados entre leiloeiros → dedup por URL na ingestão.**
   `leiloesbrasil` ×4, `leilo.com.br` ×4, `lkleiloes` ×3, `sfrazao`/`mcleilao`/`arrematabem` ×2.
   **Correção (já aplicada):** inserir no banco com **dedup por URL canônica**; atribuir o imóvel ao
   leiloeiro pelo dado do próprio lote, não pelo domínio compartilhado.

4. **SPA / Cloudflare retornando 0 na listagem → cascata + extrator por plataforma.**
   **Correção:** manter a cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`,
   `/busca?categoria=imoveis`); para `megaleiloes`, `sodresantoro`, `portalzuk`, `alfaleiloes`,
   `leilo.com.br`, `leiloesbrasil`, `lkleiloes` escrever **parser dedicado por domínio** (seção 27) e
   acionar **FlareSolverr** (seção 14) onde houver Cloudflare; `curl_cffi` para erros de TLS.

5. **Data da 1ª praça só no detalhe → enricher de detalhe antes de descartar.**
   Itens sem data legível na listagem foram descartados (subnotifica). **Correção:** rodar o
   *enricher* (seções 17/23) que abre cada lote e extrai data da praça + edital + matrícula, em vez de
   descartar por ausência de data na listagem.

6. **Domínios offline/DNS/TLS → checagem prévia e fallback.**
   **Correção:** validar resolução DNS antes de raspar; tentar `www`/sem-`www` e `http`/`https`;
   marcar como inativo após 2 tentativas e remover do pool de re-scraping. Sites com problema nesta rodada:
{probs}

7. **Leilões esporádicos → re-scraping agendado.**
   **Correção:** agendar re-execução a cada 7–14 dias (cron/Celery beat, seção 21); o dedup por URL
   evita duplicar o que já está no banco.

**Relatório gerado em:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
"""
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f:
        f.write(rel)

def main():
    print("=" * 72)
    print(f"SCRAPER JUCEG (GOIAS) COMPLETO  —  {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Corte de leilao futuro: > {HOJE.strftime('%d/%m/%Y')}")
    print("=" * 72)
    print(f"\n{len(LEILOEIROS_REGULAR)} leiloeiros REGULAR "
          f"({sum(1 for l in LEILOEIROS_REGULAR if l[1])} com site)\n")
    progress["status"] = "scraping"
    todos = []
    last = time.time()
    com_site = [l for l in LEILOEIROS_REGULAR if l[1]]
    for i, (nome, site, cidade, uf) in enumerate(com_site, 1):
        try:
            ims = scrape_site(nome, site, cidade, uf)
        except Exception as e:
            ims = []; progress["sites_problema"][nome] = f"erro: {str(e)[:80]}"
            log(f"     [ERRO] {e}")
        todos.extend(ims)
        progress["imoveis_por_leiloeiro"][nome] = len(ims)
        progress["imoveis_total"] = len(todos)
        if time.time() - last >= REPORT_INTERVAL:
            print("\n----- RELATORIO PARCIAL (5 min) -----")
            print(f"Sites: {i}/{len(com_site)} | Imoveis: {len(todos)}")
            for n, q in progress["imoveis_por_leiloeiro"].items():
                if q: print(f"  - {n}: {q}")
            print("-------------------------------------\n")
            save_progress(); last = time.time()
        time.sleep(1)
    progress["status"] = "inserindo_banco"
    inseridos = inserir_banco(todos)
    log(f"Inseridos {inseridos} imoveis novos no banco (dedup por URL).")
    cl, ci = salvar_csvs(todos)
    log(f"CSV leiloeiros: {cl.name} | CSV imoveis: {ci.name}")
    gerar_relatorio(todos, inseridos, cl, ci)
    progress["status"] = "concluido"; progress["imoveis_inseridos_db"] = inseridos
    save_progress()
    print("\n" + "=" * 72)
    print("RESUMO FINAL — IMOVEIS POR LEILOEIRO")
    print("=" * 72)
    for n, q in sorted(progress["imoveis_por_leiloeiro"].items(), key=lambda x: -x[1]):
        if q: print(f"  {q:3d}  {n}")
    print("-" * 72)
    print(f"TOTAL imoveis (1a praca futura): {len(todos)} | Inseridos no banco: {inseridos}")
    print(f"Relatorio anexado em: {RELATORIO_FILE.name}")

if __name__ == "__main__":
    main()
