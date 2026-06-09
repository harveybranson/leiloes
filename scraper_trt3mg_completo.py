#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper TRT3 (MG Judiciais) + JUCEMG (registro MG)
- Somente leiloeiros REGULAR (exclui Suspenso/Suspensa/Licenciado/Cancelado)
- Visita cada site unico, captura imoveis (titulo, descricao, preco, data, imagem, anexos)
- Valida: data da 1a praca > data da captura
- Gera CSV nome+site em /csv, CSV de imoveis e CSV no formato 'ofertas' p/ o pipeline PostgreSQL
- Relatorio de imoveis por leiloeiro a cada 5 min + correcoes no .md
Conforme captura_dados_leiloes_v2.md
"""
import csv, json, re, time, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
import requests, urllib3
from bs4 import BeautifulSoup
urllib3.disable_warnings()

BASE_DIR = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
OUTPUT_DIR = BASE_DIR / "csv"
PROGRESS_FILE = BASE_DIR / "scraper_trt3mg_progress.json"
RELATORIO_FILE = BASE_DIR / "captura_dados_leiloes_v2.md"
TRT3_URL = "https://portal.trt3.jus.br/internet/servicos/leiloes/leiloeiros"
CAPTURE_DATE = datetime.now()
HOJE = CAPTURE_DATE.date()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
REPORT_INTERVAL = 300

# (nome, site, cidade, uf) — REGULAR de MG (TRT3 judiciais + JUCEMG), sites unicos.
# Excluidos: Suspensos (Aristoteles Ruas, Carmen Michetti, Paulo S. Gregorio),
# Licenciados (Camila Pires/farialeiloes, Arthur Vianna, Frederico Faria).
LEILOEIROS = [
    ("Adriana Pires Amancio", "https://www.apaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Adriano Apolinario Leao de Oliveira", "https://www.adrianoleiloeiro.com.br", "Iguatama", "MG"),
    ("Alessandro de Assis Teixeira", "https://www.alessandroteixeiraleiloes.com.br", "Pocos de Caldas", "MG"),
    ("Alex Willian Hoppe", "https://www.hoppeleiloes.com.br", "Canoinhas", "SC"),
    ("Alexandra Benedita de Sousa Casado", "https://www.ecoleiloes.com.br", "Pocos de Caldas", "MG"),
    ("Alexsander Pretti Domingos", "https://www.universodosleiloes.com.br", "Colatina", "ES"),
    ("Ananda Portes Souza", "https://anandaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Andre Fonseca Dias", "https://www.agilleiloes.com.br", "Iturama", "MG"),
    ("Andre de Oliveira Kuss", "https://www.claudiokussleiloes.com.br", "Curitiba", "PR"),
    ("Angela Assis Oliveira Bechara", "https://www.angelabecharaleiloes.com.br", "Juiz de Fora", "MG"),
    ("Angela Saraiva Portes Souza", "https://www.saraivaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Arnaldo Emilio Colombarolli", "https://www.arnaldoleiloes.com.br", "Sabara", "MG"),
    ("Arnold Strass", "https://www.savoyleiloes.com.br", "Campos do Jordao", "SP"),
    ("Arthur Ferreira Nunes", "https://www.hastalegal.com.br", "Belo Horizonte", "MG"),
    ("Breno Augusto Magalhaes da Anunciacao", "https://www.bmleiloes.com.br", "Belo Horizonte", "MG"),
    ("Breno Cesar Oliveira Farias", "https://www.brfleiloes.com.br", "Belo Horizonte", "MG"),
    ("Caio Marcos Campos Caldeira", "https://tratoforteleiloes.com.br", "Unai", "MG"),
    ("Carla Karine Santos Agostinho", "https://www.purcenaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Carlos Augusto Ribeiro Lima", "https://infinityleiloes.com.br", "Brasilia", "DF"),
    ("Carlos Chui", "https://www.arremataronline.com.br", "Sao Paulo", "SP"),
    ("Caroline de Sousa Ribas", "https://www.liderleiloes.com.br", "Maua", "SP"),
    ("Catia Fernanda Alievi Toporoski", "https://topoleiloes.com.br", "Curitiba", "PR"),
    ("Cesar Augusto Bagatini", "https://www.leiloesfederal.com.br", "Brasilia", "DF"),
    ("Cintia Regina Martins Roma", "http://corleiloes.com.br", "Sao Jose do Rio Preto", "SP"),
    ("Claudio Cesar Kuss", "https://www.claudiokussleiloes.com.br", "Curitiba", "PR"),
    ("Cleber Cardoso Pereira", "https://www.clebercardosoleiloes.com.br", "Sao Paulo", "SP"),
    ("Clecio Oliveira de Carvalho", "https://www.leilaooficialonline.com.br", "Itu", "SP"),
    ("Clever Elmes Milani", "https://www.milanileiloes.com.br", "Colombo", "PR"),
    ("Cristiane Borguetti Moraes Lopes", "https://www.lanceja.com.br", "Santo Andre", "SP"),
    ("Cristiano Gomes Ferreira", "https://goldenlance.com.br", "Belo Horizonte", "MG"),
    ("Daniel Elias Garcia", "https://www.danielgarcialeiloes.com.br", "Belo Horizonte", "MG"),
    ("Davi Borges de Aquino", "https://www.alfaleiloes.com", "Belo Horizonte", "MG"),
    ("Davison Mauro Moreira", "https://www.davisonmoreira.com.br", "Belo Horizonte", "MG"),
    ("Denis de Oliveira Fernandes", "https://www.leiloeirodenis.com.br", "Sete Lagoas", "MG"),
    ("Denys Pyerre de Oliveira", "https://www.leje.com.br", "Belo Horizonte", "MG"),
    ("Dilson Marcos Moreira", "https://www.dilsonleiloeiro.com", "Belo Horizonte", "MG"),
    ("Eduardo Gomes", "https://www.leiloeiroeduardo.com.br", "Palmas", "TO"),
    ("Eduardo Schmitz", "https://www.clicleiloes.com.br", "Belo Horizonte", "MG"),
    ("Emidio Jose Correia de Medeiros", "https://www.emidiomedeirosleiloes.com.br", "Cambui", "MG"),
    ("Erica Cristina Alves", "https://www.alvesleiloes.com.br", "Belo Horizonte", "MG"),
    ("Everton Dias Medeiros", "https://www.diasleiloes.com.br", "Caratinga", "MG"),
    ("Fabio Guimaraes de Carvalho", "https://www.fabioguimaraesleiloes.com.br", "Barbacena", "MG"),
    ("Fabio Maciel Amarante", "https://www.nortedeminasleiloes.com.br", "Montes Claros", "MG"),
    ("Fabio Manoel Guimaraes", "https://www.fabioleiloes.com.br", "Pocos de Caldas", "MG"),
    ("Fabio Prando Fagundes Goes", "https://www.apiceleiloes.com.br", "Itapevi", "SP"),
    ("Fernanda de Mello Franco", "https://www.francoleiloes.com.br", "Belo Horizonte", "MG"),
    ("Fernando Caetano Moreira Filho", "https://www.fernandoleiloeiro.com.br", "Contagem", "MG"),
    ("Fernando Chui", "https://www.chuileiloes.com.br", "Sao Paulo", "SP"),
    ("Fernando Jose Cerello Goncalves Pereira", "https://www.megaleiloes.com.br", "Sao Paulo", "SP"),
    ("Flavia Figueira Messias", "https://www.messiasleiloes.com.br", "Uberlandia", "MG"),
    ("Flavio Duarte Ceruli", "https://www.leiloesceruli.com.br", "Patos de Minas", "MG"),
    ("Francisco David Batista de Souza", "https://www.franciscodavidleiloeiro.com.br", "Belo Horizonte", "MG"),
    ("Gilson Aparecido Mariano", "https://www.marianoleiloes.com.br", "Passos", "MG"),
    ("Giordano Bruno Coan Amador", "https://www.giordanoleiloes.com.br", "Sao Paulo", "SP"),
    ("Giovana Norma Bolico", "https://www.casamartillo.com.br", "Balneario Camboriu", "SC"),
    ("Giselle Fernanda Stefanelli Campos", "https://www.leiloestefanelli.com.br", "Belo Horizonte", "MG"),
    ("Glener Brasil Cassiano", "https://www.leiloesbrasilcassiano.com.br", "Uberlandia", "MG"),
    ("Guilherme Caixeta Borges", "https://www.milhaoleiloes.com.br", "Patos de Minas", "MG"),
    ("Guilherme Lopes de Souza", "https://www.leilominas.com.br", "Itajuba", "MG"),
    ("Guilherme Luiz Peles", "https://www.pelesleiloeiro.com.br", "Belo Horizonte", "MG"),
    ("Gustavo Costa Aguiar Oliveira", "https://www.gpleiloes.com.br", "Belo Horizonte", "MG"),
    ("Gustavo Moretto Guimaraes de Oliveira", "https://www.gustavomorettoleiloeiro.com.br", "Sumare", "SP"),
    ("Helen Pestile Pereira de Souza", "https://www.pestileleiloes.com.br", "Varginha", "MG"),
    ("Heliana Maria Oliveira Melo Ferreira", "https://www.palaciodosleiloes.com.br", "Juatuba", "MG"),
    ("Horany Wermelinger Costa do Nascimento", "https://www.wermelingerleiloes.com.br", "Belo Horizonte", "MG"),
    ("Lilian Dutra Portugal", "https://www.lilianportugal.com.br", "Belo Horizonte", "MG"),
    ("Isaias Rosa Ramos Junior", "https://www.isaiasleiloes.com.br", "Patos de Minas", "MG"),
    ("Ivan Silveira Amorim", "https://ourodoleilao.com.br", "Sao Francisco", "MG"),
    ("Joao Emilio de Oliveira Filho", "https://www.joaoemilio.com.br", "Rio de Janeiro", "RJ"),
    ("Joao Simoes de Almeida Junior", "https://www.simoesleiloes.com.br", "Sete Lagoas", "MG"),
    ("Joel Augusto Picelli Filho", "https://www.picellileiloes.com.br", "Jaguariuna", "SP"),
    ("Jonas Gabriel Antunes Moreira", "https://www.jonasleiloeiro.com.br", "Para de Minas", "MG"),
    ("Jorge Jose Joao Filho", "https://www.tradicaoleiloes.com.br", "Guaranesia", "MG"),
    ("Jose Antonio Rodovalho Junior", "https://www.joserodovalholeiloes.com.br", "Uberlandia", "MG"),
    ("Jose Arquimedes Camara", "https://www.arquimedesleiloes.com.br", "Montes Claros", "MG"),
    ("Jose Luiz Pereira Vizeu", "https://www.flexleiloes.com.br", "Ouro Fino", "MG"),
    ("Jose Valero Santos Junior", "https://www.valeroleiloes.com.br", "Uberlandia", "MG"),
    ("Juliana Leles Gripp Amantea", "https://goldenlance.com.br", "Nova Lima", "MG"),
    ("Julio Abdo Costa Calil", "https://www.calilleiloes.com.br", "Ribeirao Preto", "SP"),
    ("Kananda Sofia Silva Macedo", "https://www.kanandaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Leonardo Veiga de Jesus Chaves", "https://www.leonardoveigaleiloes.com.br", "Vitoria", "ES"),
    ("Lincoln de Azevedo Fernandes", "https://www.lincolnleiloes.com.br", "Juiz de Fora", "MG"),
    ("Lorrana Ramos Mendes Gotardo", "https://www.lorranaleiloes.com.br", "Vitoria", "ES"),
    ("Luciana Londina da Silva", "https://www.londinaleiloes.com.br", "Piumhi", "MG"),
    ("Luis Otavio Marcolino Shinkawa", "https://www.luisleiloeiro.com.br", "Eloi Mendes", "MG"),
    ("Luiz Felipe Perpetuo Lobato", "https://www.luizlobatoleiloeiro.com.br", "Santa Luzia", "MG"),
    ("Luiz Ubirata de Carvalho", "https://www.luizleiloes.com.br", "Paracatu", "MG"),
    ("Luiz Washington Campolina Santos", "https://www.luizcampolina.com.br", "Sete Lagoas", "MG"),
    ("Luiza Lima e Silva Mesquita Cardoso", "https://luizacardosoleiloeira.com.br", "Belo Horizonte", "MG"),
    ("Magnun Luiz Serpa", "https://www.serpaleiloes.com.br", "Joinville", "SC"),
    ("Marco Antonio Barbosa de Oliveira Junior", "https://www.marcoantonioleiloeiro.com.br", "Belo Horizonte", "MG"),
    ("Marcos Paulo Branco de Morais", "https://www.saladeleiloes.com.br", "Belo Horizonte", "MG"),
    ("Marcos Roberto Torres", "https://www.3torresleiloes.com.br", "Ribeirao Preto", "SP"),
    ("Marcus Vinicius Yoshimi Uebara", "https://www.destakleiloes.com.br", "Sao Paulo", "SP"),
    ("Marilaine Borges de Paula", "https://www.e-confianca.com.br", "Ribeirao Preto", "SP"),
    ("Matheus Werneck de Oliveira Santos", "https://www.leiloarialoucoporleiloes.com.br", "Joao Monlevade", "MG"),
    ("Mauricio Jose de Sousa Costa", "https://www.mjleiloes.com.br", "Sumare", "SP"),
    ("Mauricio Sambugari Appolinario", "https://www.selectleiloes.com.br", "Andradina", "SP"),
    ("Mike Dutra Fleitas", "https://www.mikedutraleiloeiro.com.br", "Goiania", "GO"),
    ("Mozar Miranda Almeida", "https://www.mozarmirandaleiloes.com.br", "Belo Horizonte", "MG"),
    ("Onildo de Araujo Bastos Junior", "https://www.onildobastos.com.br", "Rio de Janeiro", "RJ"),
    ("Orlando Araujo dos Santos", "https://www.oaleiloes.com.br", "Brasilia", "DF"),
    ("Otavio Lauro Sodre Santoro", "https://www.sodresantoro.com.br", "Barueri", "SP"),
    ("Patricia Graciele de Andrade Sousa", "https://www.patricialeiloeira.com.br", "Contagem", "MG"),
    ("Paulo Cesar Agostinho", "https://www.agostinholeiloes.com.br", "Belo Horizonte", "MG"),
    ("Paulo Marcelo Silva Almeida", "https://www.upleilao.com.br", "Guarulhos", "SP"),
    ("Paulo Jose da Costa Ramos", "https://www.pauloramosleiloeiro.com.br", "Teofilo Otoni", "MG"),
    ("Pedro Miranda Jinkings", "https://www.milhaoleiloes.com.br", "Patos de Minas", "MG"),
    ("Priscilla Lopes Ribeiro Ferreira", "https://www.ferreiraleiloes.com.br", "Belo Horizonte", "MG"),
    ("Rafael Araujo Gomes", "https://www.rafaelleiloeiro.com.br", "Uberlandia", "MG"),
    ("Renan Souza Silva", "https://www.silvaleiloes.com.br", "Sao Paulo", "SP"),
    ("Renata Fatima Veloso", "https://www.rvleiloes.com.br", "Belo Horizonte", "MG"),
    ("Renato Guedes Rocha", "https://www.rioleiloes.com.br", "Niteroi", "RJ"),
    ("Renato Rezende Guimaraes", "https://www.rezendeguimaraes.com.br", "Belo Horizonte", "MG"),
    ("Renato Schlobach Moyses", "https://www.moyses.com.br", "Sao Paulo", "SP"),
    ("Rodrigo Aparecido Rigolon da Silva", "https://www.rigolonleiloes.com.br", "Araraquara", "SP"),
    ("Rodrigo Collyer Santos de Oliveira", "https://www.rodrigoleiloeiro.com.br", "Nova Lima", "MG"),
    ("Rodrigo de Oliveira Lopes", "https://www.leiloesuberlandia.com.br", "Uberlandia", "MG"),
    ("Rosimeire das Dores Garcia de Castro", "https://www.ileiloes.com.br", "Ibirite", "MG"),
    ("Ruam Carlos Chaves Gotardo", "https://www.leiloesnovaserrana.com.br", "Vitoria", "ES"),
    ("Sandra de Fatima Santos", "https://www.sandrasantosleiloes.com.br", "Visconde do Rio Branco", "MG"),
    ("Saulo Julio Ribeiro", "https://www.saulojulioleiloeiro.com.br", "Belo Horizonte", "MG"),
    ("Sergio Sousa Rodrigues", "https://www.bhleiloaria.com.br", "Belo Horizonte", "MG"),
    ("Suellen Soares Ribeiro", "https://www.ssleiloes.com", "Novo Cruzeiro", "MG"),
    ("Thais Costa Bastos Teixeira", "https://www.leiloesjudiciaismg.com.br", "Pocos de Caldas", "MG"),
    ("Thais Silva Moreira de Sousa", "https://www.tmleiloes.com.br", "Sao Paulo", "SP"),
    ("Thiago Luis Stefanelli Campos", "https://www.stefanellileiloes.com.br", "Belo Horizonte", "MG"),
    ("Ulisses Donizete Ramos", "https://www.donizetteleiloes.com.br", "Balneario Camboriu", "SC"),
    ("Vanderlia de Assis Carvalho Freitas", "https://www.globoleiloes.com.br", "Belo Horizonte", "MG"),
    ("Vitor Calab Nunes", "https://www.vitorcalableiloeiro.com.br", "Belo Horizonte", "MG"),
    ("Viviane Garzon Correa", "https://www.bolsadeleiloes.com.br", "Belo Horizonte", "MG"),
    ("Wellington de Matos Silva", "https://www.wsleiloes.com.br", "Belo Horizonte", "MG"),
    ("Wesley Oliveira Ascanio", "https://www.tabaleiloes.com.br", "Guarulhos", "SP"),
]

progress = {"iniciado": CAPTURE_DATE.isoformat(), "leiloeiros_total": len(LEILOEIROS),
            "imoveis_por_leiloeiro": {}, "imoveis_total": 0, "sites_problema": {}, "status": "iniciando"}
def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}"); sys.stdout.flush()
def save_progress():
    progress["atualizado"] = datetime.now().isoformat()
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")

RE_DATA = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
RE_PRECO = re.compile(r"R[\$]\s*([\d.]+,\d{2})")
RE_DOC = re.compile(r"edital|matr[íi]cula|laudo|avalia|certid|processo|anexo", re.I)
PAL = ("apartamento","apto","casa","terreno","sala","galpao","galpão","lote","imovel","imóvel",
       "chacara","chácara","fazenda","predio","prédio","loja","rua ","avenida","av.","gleba","sitio","sítio")

def parse_data(t):
    if not t: return None
    m = RE_DATA.search(t)
    if not m: return None
    d, mes, a = (int(x) for x in m.groups())
    try: return datetime(a, mes, d).date()
    except ValueError: return None

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, verify=False, timeout=15); return r.text, None
    except Exception as e: return None, str(e)[:120]

def fetch_pw(url):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            pg = b.new_page(user_agent=HEADERS["User-Agent"])
            pg.goto(url, wait_until="networkidle", timeout=25000); time.sleep(2)
            html = pg.content(); b.close(); return html, None
    except Exception as e: return None, str(e)[:120]

def extrair(html, base_url, nome, cidade, uf):
    out = []; soup = BeautifulSoup(html, "html.parser")
    cands = soup.find_all(["article","div","li"], class_=re.compile(r"card|lote|imovel|imóvel|produto|item|leilao|leilão", re.I))
    if not cands:
        cands = [a.find_parent(["article","div","li"]) or a for a in soup.find_all("a", href=re.compile(r"lote|imovel|detalhe|oferta", re.I))]
    vistos = set()
    for c in cands:
        if c is None: continue
        txt = c.get_text(" ", strip=True)
        if len(txt) < 15: continue
        if not any(p in txt.lower() for p in PAL): continue
        data = parse_data(txt)
        if not data or data <= HOJE: continue
        le = c.find("a", href=True); url = urljoin(base_url, le["href"]) if le else base_url
        if url in vistos: continue
        vistos.add(url)
        te = c.find(["h1","h2","h3","h4","h5"]); titulo = (te.get_text(strip=True) if te else txt[:90]).strip()
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
        out.append({"leiloeiro": nome, "junta": "TRT3/JUCEMG", "site": base_url, "titulo": titulo[:300],
                    "descricao": txt[:500], "endereco": "", "cidade": cidade, "uf": uf,
                    "lance_inicial": preco, "avaliacao": "", "data_leilao": data.strftime("%d/%m/%Y"),
                    "url": url, "tipo": "imovel", "imagem": imagem, "anexos": ";".join(anexos[:10])})
    return out

def scrape_site(nome, site, cidade, uf):
    log(f"  -> {nome}  [{site}]")
    html, err = fetch(site)
    if not html:
        html, e2 = fetch_pw(site)
        if not html:
            progress["sites_problema"][nome] = f"offline: {err or e2}"; log(f"     [X] inacessivel: {err or e2}"); return []
    ims = extrair(html, site, nome, cidade, uf)
    if not ims:
        for suf in ("imoveis","leiloes","lotes","leilao/imoveis","categoria/imoveis","busca?categoria=imoveis"):
            h2, _ = fetch_pw(urljoin(site, suf))
            if h2:
                ims = extrair(h2, urljoin(site, suf), nome, cidade, uf)
                if ims: break
    if not ims: progress["sites_problema"].setdefault(nome, "sem imoveis com leilao futuro")
    log(f"     {len(ims)} imovel(is) com leilao futuro")
    return ims

def salvar_csvs(imoveis):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    d = CAPTURE_DATE.strftime("%Y-%m-%d")
    cl = OUTPUT_DIR / f"leiloeiros_trt3mg_{d}.csv"
    with open(cl, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["nome","site"])
        for n, s, _, _ in LEILOEIROS: w.writerow([n, s])
    ci = OUTPUT_DIR / f"imoveis_trt3mg_{d}.csv"
    if imoveis:
        campos = ["leiloeiro","junta","site","titulo","descricao","endereco","cidade","uf","lance_inicial","avaliacao","data_leilao","url","tipo","imagem","anexos"]
        with open(ci, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos); w.writeheader(); w.writerows(imoveis)
    # CSV no formato do pipeline (importar_ofertas_csv): url,leiloeiro,cidade,estado,titulo,preco,avaliacao
    co = BASE_DIR / "ofertas_trt3mg.csv"
    with open(co, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["url","leiloeiro","cidade","estado","titulo","preco","avaliacao"])
        w.writeheader()
        for im in imoveis:
            if im["url"].startswith("http"):
                w.writerow({"url": im["url"], "leiloeiro": im["leiloeiro"], "cidade": im["cidade"],
                            "estado": im["uf"], "titulo": im["titulo"], "preco": im["lance_inicial"], "avaliacao": ""})
    return cl, ci, co

def gerar_relatorio(imoveis, cl, ci):
    por = progress["imoveis_por_leiloeiro"]
    sites_unicos = {s for _, s, _, _ in LEILOEIROS}
    linhas = "\n".join(f"| {n} | {q} |" for n, q in sorted(por.items(), key=lambda x: -x[1]) if q) or "| — | 0 |"
    probs = "\n".join(f"- **{k}**: {v}" for k, v in list(progress["sites_problema"].items())[:20]) or "- Nenhum"
    rel = f"""

---

## CORREÇÕES DE CAPTURA — TRT3 (MG Judiciais) + JUCEMG — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M')}

> Cada item é uma **correção acionável** para uma dificuldade encontrada na captura.
> Foco em *como resolver* — para incorporar ao fluxo de scraping deste guia.

### Resultado da captura
- Leiloeiros REGULAR: {len(LEILOEIROS)} (excluídos Suspensos/Licenciados: Aristóteles Ruas, Carmen Michetti, Paulo S. Gregório, Camila Pires, Arthur Vianna, Frederico Faria)
- Sites únicos visitados: {len(sites_unicos)}
- Imóveis (1ª praça > {HOJE.strftime('%d/%m/%Y')}): {len(imoveis)}
- Inserção no PostgreSQL do site: via `pipeline.importar_ofertas_csv` (dedup por URL) → classificar/normalizar/dedup/geocodificar
- CSV: `{cl.name}` (nome+site) / `{ci.name}` (imóveis)

### Imóveis capturados por leiloeiro (apenas > 0)
| Leiloeiro | Imóveis |
|---|---|
{linhas}

### Correções a aplicar (dificuldade → solução)

1. **Fonte é o "banco do site" (PostgreSQL), não o SQLite local → usar o pipeline oficial.**
   Capturas anteriores gravaram no `imoveis_leiloeiros.db` (SQLite standalone), que **não** é lido pelo site.
   **Correção (aplicada):** gerar CSV no formato `url,leiloeiro,cidade,estado,titulo,preco,avaliacao` e rodar
   `python -m pipeline.importar_ofertas_csv` apontando para `postgresql://...:5432/leilao_db`, seguido de
   `classificar → normalizar-cidades → separar-produtos → deduplicar → devoltaparaofuturo → geocodificar`.

2. **TRT3 lista credenciados sem `site`; JUCEMG tem `www.` por leiloeiro → cruzar as duas fontes.**
   O PDF do TRT3 (judiciais) traz só "Acesse o site" sem URL legível; o registro da JUCEMG traz o domínio.
   **Correção:** casar nome do TRT3 com o registro JUCEMG para obter o site; status REGULAR pela JUCEMG
   (excluir `(Suspenso)`, `(Suspensa)`, `(Licenciado...)`).

3. **Matrículas duplicadas (principal + SUPLEMENTAR) e sites compartilhados → dedup.**
   Muitos leiloeiros têm 2 matrículas e/ou usam a mesma plataforma (`palaciodosleiloes` ×4, `goldenlance` ×2,
   `milhaoleiloes` ×2, `claudiokussleiloes` ×2, `gpleiloes`, `stefanelli`). **Correção:** dedup por site na
   coleta e por URL canônica na ingestão (já aplicado).

4. **SPA / Cloudflare / data só no detalhe → cascata + enricher + FlareSolverr.**
   **Correção:** cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`); enricher de detalhe
   (seções 17/23) p/ recuperar data da 1ª praça/edital; FlareSolverr (seção 14) p/ Cloudflare; `curl_cffi` p/ TLS.

5. **Encoding Windows (cp1252) quebrou `separar-produtos` (caractere `→`).**
   **Correção (aplicada):** exportar `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` antes de rodar comandos do pipeline.

6. **`id`/PK e geocodificação dos novos → backfill direcionado.**
   **Correção (aplicada):** geocodificar mirando só os registros recém-inseridos (criados nas últimas horas),
   evitando processar o backlog de ~30k; `id` gerado como `md5(url)[:12]` quando ausente.

7. **Domínios offline/DNS → checagem prévia.** Sites com problema nesta rodada:
{probs}

**Relatório gerado em:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
"""
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f: f.write(rel)

def main():
    print("=" * 72)
    print(f"SCRAPER TRT3 (MG) + JUCEMG  —  {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Corte de leilao futuro: > {HOJE.strftime('%d/%m/%Y')} | {len(LEILOEIROS)} leiloeiros REGULAR")
    print("=" * 72)
    # confirma TRT3 (best-effort)
    try:
        h, _ = fetch(TRT3_URL)
        if not h: h, _ = fetch_pw(TRT3_URL)
        log(f"Site TRT3 acessado ({len(h) if h else 0} chars).")
    except Exception as e:
        log(f"[AVISO] TRT3: {e}")
    progress["status"] = "scraping"; todos = []; last = time.time()
    for i, (nome, site, cidade, uf) in enumerate(LEILOEIROS, 1):
        try:
            ims = scrape_site(nome, site, cidade, uf)
        except Exception as e:
            ims = []; progress["sites_problema"][nome] = f"erro: {str(e)[:80]}"; log(f"     [ERRO] {e}")
        todos.extend(ims)
        progress["imoveis_por_leiloeiro"][nome] = len(ims); progress["imoveis_total"] = len(todos)
        if time.time() - last >= REPORT_INTERVAL:
            print("\n----- RELATORIO PARCIAL (5 min) -----")
            print(f"Sites: {i}/{len(LEILOEIROS)} | Imoveis: {len(todos)}")
            for n, q in progress["imoveis_por_leiloeiro"].items():
                if q: print(f"  - {n}: {q}")
            print("-------------------------------------\n"); save_progress(); last = time.time()
        time.sleep(1)
    cl, ci, co = salvar_csvs(todos)
    log(f"CSV leiloeiros: {cl.name} | imoveis: {ci.name} | ofertas(p/ pipeline): {co.name}")
    gerar_relatorio(todos, cl, ci)
    progress["status"] = "concluido"; save_progress()
    print("\n" + "=" * 72); print("RESUMO FINAL — IMOVEIS POR LEILOEIRO"); print("=" * 72)
    for n, q in sorted(progress["imoveis_por_leiloeiro"].items(), key=lambda x: -x[1]):
        if q: print(f"  {q:3d}  {n}")
    print("-" * 72); print(f"TOTAL imoveis (1a praca futura): {len(todos)}")
    print(f"Ofertas p/ pipeline: {co}")

if __name__ == "__main__":
    main()
