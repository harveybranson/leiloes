#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper JUCETINS (Tocantins) — PDF "Leiloeiros Tocantins.pdf"
- Somente leiloeiros REGULAR (exclui IRREGULAR / CANCELAMENTO DE MATRICULA)
- Visita cada site, captura imoveis (titulo, descricao, preco, data, imagem, anexos)
- Valida: data da 1a praca > data da captura
- CSV nome+site em /csv; imoveis no banco imoveis_leiloeiros.db (dedup por URL)
- Relatorio de imoveis por leiloeiro a cada 5 min + relatorio de dificuldades no .md
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

BASE_DIR = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
OUTPUT_DIR = BASE_DIR / "csv"
DB_PATH = BASE_DIR / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE_DIR / "scraper_jucetins_completo_progress.json"
RELATORIO_FILE = BASE_DIR / "captura_dados_leiloes_v2.md"

CAPTURE_DATE = datetime.now()
HOJE = CAPTURE_DATE.date()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
REPORT_INTERVAL = 300

# ---------------------------------------------------------------------------
# Leiloeiros REGULAR de Tocantins (situacao autoritativa do PDF DREI/JUCETINS).
# EXCLUIDOS (8): marcados (IRREGULAR) ou (CANCELAMENTO DE MATRICULA):
#   #4  Marcos Wladimir Dulnik        (IRREGULAR)
#   #11 Borges Guedes Neto            (IRREGULAR)
#   #15 Danilo Aparecido de Oliveira  (CANCELAMENTO)
#   #23 Carlos Chui                   (IRREGULAR)
#   #29 Mike Dutra Fleitas            (CANCELAMENTO)
#   #38 Renato Schlobach Moyses       (CANCELAMENTO A PEDIDO)
#   #40 Eduardo Schmitz               (CANCELAMENTO A PEDIDO)
#   #46 Lorrainny Fernandes R. Lopes  (IRREGULAR)
# (nome, site, cidade, uf)
# ---------------------------------------------------------------------------
LEILOEIROS_REGULAR = [
    ("Eduardo Gomes", "https://www.leiloeiroeduardo.com.br", "Palmas", "TO"),
    ("Rossana Paiva Borges de Oliveira", "https://www.caiapoleiloes.com.br", "Palmas", "TO"),
    ("Antonio Carlos Volpi Santana", "https://www.leiloesbrasilto.com.br", "Palmas", "TO"),
    ("Victor Oliveira Dorta", "https://www.victordortaleiloes.com.br", "Palmas", "TO"),
    ("Tatiana Dinelly e Silva Bonato", "https://www.rapidaovende.com.br", "Araguaina", "TO"),
    ("Cesar Augusto Bagatini", "https://www.leiloesfederal.com.br", "Brasilia", "DF"),
    ("Sandro de Oliveira", "https://www.norteleiloes.com.br", "Palmas", "TO"),
    ("Alvaro Sergio Fuzo", "https://www.alvaroleiloes.com.br", "Goiania", "GO"),
    ("Fernanda Lima Mascarenhas", "https://www.fernandalimaleiloes.com.br", "Palmas", "TO"),
    ("Rudival Almeida Gomes Junior", "https://www.rjleiloes.com.br", "Salvador", "BA"),
    ("Murilo Goncalves Ramos", "https://www.mgrleiloes.com.br", "Goiania", "GO"),
    ("Tiago Tessler Blecher", "https://www.webleiloes.com.br", "Brasilia", "DF"),
    ("Arnold Strass", "https://www.savoyleiloes.com.br", "Campos do Jordao", "SP"),
    ("Leonardo Coelho Avelar", "https://www.arrematabem.com.br", "Goiania", "GO"),
    ("Bruno Barreto Sanches", "https://www.barretoleiloes.com.br", "Campo Grande", "MS"),
    ("Nelci Dezan", "https://www.leiloesmwd.com.br", "Palmas", "TO"),
    ("Daniel Elias Garcia", "https://www.danielgarcialeiloes.com.br", "Palmas", "TO"),
    ("Alex Willian Hoppe", "https://www.hoppeleiloes.com.br", "Canoinhas", "SC"),
    ("Davi Borges de Aquino", "https://www.alfaleiloes.com", "Palmas", "TO"),
    ("Uesley da Silva Oliveira dos Santos", "https://www.carrollruralleiloes.com.br", "Luis Eduardo Magalhaes", "BA"),
    ("Rafael Galvani Ferreira", "https://www.galvanileiloes.com.br", "Tucuma", "PA"),
    ("Livia Leilane de Oliveira Azevedo", "https://www.livialeiloes.com.br", "Palmas", "TO"),
    ("Milena Rosa Di Giacomo Adri", "https://www.megaleiloes.com.br", "Campo Grande", "MS"),
    ("Joao Luiz de Franca Neto", "https://www.jocaleiloesagro.com.br", "Barreiras", "BA"),
    ("Mouzar Baston Filho", "https://www.bastonleiloes.com.br", "Franca", "SP"),
    ("Rodolfo da Rosa Schontag", "https://www.leiloeiropublico.com.br", "Florianopolis", "SC"),
    ("Lucas Fernandes Almeida", "https://www.leiloestocantins.com", "Palmas", "TO"),
    ("Aluisio Francisco de Assis Cardoso Bringel", "https://www.sancarleiloes.com.br", "Araguaina", "TO"),
    ("Elenice Lira Sales de Sousa", "https://www.leiloesbrasil.com.br", "Aparecida de Goiania", "GO"),
    ("Evando da Silva Lagares", "https://www.tonoleilao.com.br", "Palmas", "TO"),
    ("Mara Helena de Urzedo Fortunato", "https://www.maraurzedoleilao.com.br", "Palmas", "TO"),
    ("Joabe Balbino da Silva", "https://www.globoleiloes.com.br", "Sao Paulo", "SP"),
    ("Rosimeire Alves de Oliveira Maia", "https://rosioliveiraleiloes.com.br", "Palmas", "TO"),
    ("Lucas Rafael Antunes Moreira", "https://www.mgl.com.br", "Belo Horizonte", "MG"),
    ("Jonas Gabriel Antunes Moreira", "https://www.mgl.com.br", "Para de Minas", "MG"),
    ("Fernando Caetano Moreira Filho", "https://www.mgl.com.br", "Contagem", "MG"),
    ("Erico Sobral Soares", "https://www.ericosobral.com.br", "Fortaleza", "CE"),
    ("Luiz Barbosa de Lima Junior", "https://www.lbleiloes.com.br", "Londrina", "PR"),
    ("Joselma Moraes Martins", "https://jmleiloesto.com.br", "Palmas", "TO"),  # site resolvido via busca (JM Leilões; tel. confere com PDF)
    ("Lysia Moreira Silva", "", "Gurupi", "TO"),  # sem site próprio (advogada OAB TO002535, matrícula 12/12/2025, só Gmail)
    ("Camilla Correia Vecchi Aguiar", "https://www.vecchileiloes.com.br", "Goiania", "GO"),
    ("Marciano Aguiar Carneiro", "https://www.vecchileiloes.com.br", "Goiania", "GO"),
    ("Sergio Fleury Batista", "https://www.pmklog.com.br", "Goiania", "GO"),
    ("Paulo Marcelo Silva Almeida", "https://www.upleilao.com.br", "Sao Paulo", "SP"),
]

progress = {
    "iniciado": CAPTURE_DATE.isoformat(),
    "leiloeiros_total": len(LEILOEIROS_REGULAR),
    "imoveis_por_leiloeiro": {},
    "imoveis_total": 0,
    "sites_problema": {},
    "status": "iniciando",
}

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
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
        out.append({"leiloeiro": nome, "junta": "JUCETINS", "site": base_url,
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
    cl = OUTPUT_DIR / f"leiloeiros_jucetins_{d}.csv"
    with open(cl, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["nome", "site"])
        for n, s, _, _ in LEILOEIROS_REGULAR: w.writerow([n, s])
    ci = OUTPUT_DIR / f"imoveis_jucetins_{d}.csv"
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
    probs = "\n".join(f"- **{k}**: {v}" for k, v in list(progress["sites_problema"].items())[:30]) or "- Nenhum"
    rel = f"""

---

## CORREÇÕES DE CAPTURA — JUCETINS (Tocantins) — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M')}

> Cada item abaixo é uma **correção acionável** para uma dificuldade encontrada na captura
> da JUCETINS (PDF DREI "Leiloeiros Tocantins"). Foco em *como resolver* — para incorporar ao guia.

### Resultado da captura
- Leiloeiros REGULAR: {len(LEILOEIROS_REGULAR)} ({len(com_site)} com site, {len(sem_site)} só e-mail) | Sites únicos: {len(sites_unicos)}
- Excluídos do PDF (IRREGULAR/CANCELAMENTO): 8 (Dulnik, Borges Guedes Neto, Danilo A. Oliveira, Carlos Chui, Mike Dutra Fleitas, Renato Moysés, Eduardo Schmitz, Lorrainny R. Lopes)
- Imóveis (1ª praça > {HOJE.strftime('%d/%m/%Y')}): {len(imoveis)} | Inseridos no banco: {inseridos}
- CSV: `{cl.name}` / `{ci.name}`

| Leiloeiro | Site | Imóveis |
|---|---|---|
{linhas}

### Correções a aplicar (dificuldade → solução)

1. **Numeração do PDF com saltos (1–48 e 52–55) e status no título → parsing por marcador, não por índice.**
   O PDF de Tocantins pula de 48 para 52 e marca a situação entre parênteses no nome (`(IRREGULAR)`,
   `(CANCELAMENTO DE MATRÍCULA…)`). **Correção:** detectar a situação por regex no título do leiloeiro,
   nunca pela posição na lista; tratar qualquer marcador ≠ vazio como exclusão.

2. **Sites compartilhados entre leiloeiros → dedup por URL na ingestão.**
   `mgl.com.br` ×3 (Lucas/Jonas/Fernando Moreira), `vecchileiloes.com.br` ×2 (Camilla/Marciano).
   **Correção (já aplicada):** inserir no banco com **dedup por URL canônica**; atribuir o imóvel ao
   leiloeiro pelo dado do próprio lote, não pelo domínio compartilhado.

3. **2 leiloeiros REGULAR sem site (só Gmail) → derivar/buscar domínio.**
   Joselma Moraes Martins e Lysia Moreira Silva têm apenas e-mail `@gmail.com`. **Correção:** resolver
   via busca "nome + leilões Tocantins" e gravar o site validado de volta no CSV; sem domínio próprio,
   marcar para captura manual.

4. **SPA / Cloudflare retornando 0 na listagem → cascata + extrator por plataforma.**
   **Correção:** manter a cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`,
   `/busca?categoria=imoveis`); para `megaleiloes`, `arrematabem`, `alfaleiloes`, `webleiloes`,
   `leiloesbrasil` escrever **parser dedicado por domínio** (seção 27) e acionar **FlareSolverr**
   (seção 14) onde houver Cloudflare; `curl_cffi` para erros de TLS.

5. **Data da 1ª praça só no detalhe → enricher de detalhe antes de descartar.**
   Itens sem data legível na listagem foram descartados (subnotifica). **Correção:** rodar o
   *enricher* (seções 17/23) que abre cada lote e extrai data da praça + edital + matrícula.

6. **Domínios offline/DNS/TLS → checagem prévia e fallback.**
   **Correção:** validar resolução DNS antes de raspar; tentar `www`/sem-`www` e `http`/`https`;
   marcar como inativo após 2 tentativas. Sites com problema nesta rodada:
{probs}

7. **Leiloeiros sediados fora de TO (GO, SP, MG, DF, BA, etc.) → captura nacional, filtro por leiloeiro.**
   Vários REGULAR de TO operam de outros estados. **Correção:** não filtrar imóvel por UF do leiloeiro;
   capturar tudo que o site publica e registrar a UF real do imóvel a partir do lote.

8. **Leilões esporádicos → re-scraping agendado.**
   **Correção:** agendar re-execução a cada 7–14 dias (cron/Celery beat, seção 21); o dedup por URL
   evita duplicar o que já está no banco.

**Relatório gerado em:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
"""
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f:
        f.write(rel)

def main():
    print("=" * 72, flush=True)
    print(f"SCRAPER JUCETINS (TOCANTINS) COMPLETO  —  {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}", flush=True)
    print(f"Corte de leilao futuro: > {HOJE.strftime('%d/%m/%Y')}", flush=True)
    print("=" * 72, flush=True)
    print(f"\n{len(LEILOEIROS_REGULAR)} leiloeiros REGULAR "
          f"({sum(1 for l in LEILOEIROS_REGULAR if l[1])} com site)\n", flush=True)
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
            print("\n----- RELATORIO PARCIAL (5 min) -----", flush=True)
            print(f"Sites: {i}/{len(com_site)} | Imoveis: {len(todos)}", flush=True)
            for n, q in progress["imoveis_por_leiloeiro"].items():
                if q: print(f"  - {n}: {q}", flush=True)
            print("-------------------------------------\n", flush=True)
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
    print("\n" + "=" * 72, flush=True)
    print("RESUMO FINAL — IMOVEIS POR LEILOEIRO", flush=True)
    print("=" * 72, flush=True)
    for n, q in sorted(progress["imoveis_por_leiloeiro"].items(), key=lambda x: -x[1]):
        if q: print(f"  {q:3d}  {n}", flush=True)
    print("-" * 72, flush=True)
    print(f"TOTAL imoveis (1a praca futura): {len(todos)} | Inseridos no banco: {inseridos}", flush=True)
    print(f"Relatorio anexado em: {RELATORIO_FILE.name}", flush=True)

if __name__ == "__main__":
    main()
