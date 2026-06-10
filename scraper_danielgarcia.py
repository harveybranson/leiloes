# -*- coding: utf-8 -*-
"""
Scraper standalone do site Daniel Garcia Leiloes (leiloeiro nacional - JUCESC/varios).
Site    : https://www.danielgarcialeiloes.com.br/ (HTML server-rendered, sem JS/Cloudflare).
Fonte   : /calendario-leiloes -> /leilao/<id>/lotes?page=N (30 lotes/pagina) -> /item/<id>/detalhes.
Captura : titulo, url, imagem (CDN), preco, cidade/uf (campo "Cidade:" / "LOCALIZACAO:"),
          descricao, data 1a praca (cabecalho do leilao), edital (1 por leilao).
Filtro  : (a) apenas IMOVEIS (title/desc) - exclui veiculos/sucata/semoventes/maquinas;
          (b) mantem so leiloes cuja 1a praca (1o Leilao / Data do Leilao) > data da captura.
Saida   : CSV datado em /csv + insercao SQLite imoveis_leiloeiros.db (dedup por url).
Report  : scraper_danielgarcia_progress.json + stdout a cada 5 min.
"""
import csv, json, re, time, sys, sqlite3, hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
import requests, urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "csv"
DB = BASE / "imoveis_leiloeiros.db"
PROG = BASE / "scraper_danielgarcia_progress.json"
SITE = "https://www.danielgarcialeiloes.com.br"
CALENDARIO = SITE + "/calendario-leiloes"
LEILOEIRO = "Daniel Elias Garcia"
JUNTA = "NACIONAL"
CAPTURE = datetime.now()
TODAY = CAPTURE.replace(hour=0, minute=0, second=0, microsecond=0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --- classificacao imovel ---------------------------------------------------
IMOVEL_WORDS = [
    "imovel", "imóvel", "imoveis", "imóveis", "apartament", "apto ", "casa",
    "terreno", "sala comercial", "galpao", "galpão", "chacara", "chácara",
    "fazenda", "sitio", "sítio", "predio", "prédio", "loja", "kitnet", "sobrado",
    "gleba", "edificio", "edifício", "barracao", "barracão", "cobertura",
    "lote de terreno", "lote urbano", "lote rural", "area de terra", "área de terra",
    "parte ideal", "fração de imovel", "fração de imóvel", "fracao de imovel",
    "residencia", "residência", "flat", "quitinete", "vaga de garagem",
    "pavilhao", "pavilhão", "unidade autonoma", "unidade autônoma", "matricula",
    "matrícula", "m²", "hectare", "imovel rural", "imóvel rural", "lote n",
]
NEG_WORDS = [
    "veiculo", "veículo", "automovel", "automóvel", "motocicleta", "moto ",
    "caminhao", "caminhão", "sucata", "trator", "carro ", "reboque", "carreta",
    "semovente", "ovino", "bovino", "suino", "suíno", "equino", "gado", "boi ",
    "vaca", "cavalo", "maquina", "máquina", "impressora", "monitor", "notebook",
    "computador", "geladeira", "fogao", "fogão", "celular", "smartphone",
    "iphone", "tv ", "televisor", "bicicleta", "eletrodom", "ferramenta",
    "mobiliario", "mobiliário", "movel ", "móvel ", "moveis", "móveis",
    "joia", "joias", "relogio", "relógio", "quadro ", "obra de arte",
    "embarcacao", "embarcação", "lancha", "jet ski", "aeronave",
]
DEAD_RE = re.compile(r"cancelad|encerrad|finalizad|arrematad|vendido|suspens|deserto", re.I)
# datas rotuladas no cabecalho do leilao (1a/2a praca). Ignora datas soltas das descricoes.
LABEL_DATE_RE = re.compile(
    r"(?:Data do Leil[ãa]o|[12]\s*[ºªoa°]\s*(?:Leil[ãa]o|Pra[çc]a))\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})",
    re.I)
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
CIDADE_RE = re.compile(
    r"(?:Cidade|Localiza[çc][ãa]o)\s*:?\s*([A-Za-zÀ-ÿ'\.\s]+?)\s*/\s*([A-Z]{2})\b")
PRICE_RE = re.compile(r"R\$\s*([\d\.]+,\d{2})")
LANCE_RE = re.compile(r"Lance Inicial\s*:?\s*R\$\s*([\d\.]+,\d{2})", re.I)
VALMIN_RE = re.compile(r"VALOR M[IÍ]NIMO[^R]*R\$\s*([\d\.]+,\d{2})", re.I)
BGURL_RE = re.compile(r"url\(['\"]?(https?://[^'\")]+)")

progress = {"iniciado": CAPTURE.isoformat(), "capture_date": CAPTURE.strftime("%d/%m/%Y %H:%M"),
            "leiloes_processados": 0, "leiloes_total": 0, "imoveis_total": 0,
            "por_leilao": {}, "erros": [], "last_report": CAPTURE.isoformat()}

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
_retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
               allowed_methods=["GET"])
S.mount("https://", HTTPAdapter(max_retries=_retry))
S.verify = False


def save_progress():
    progress["last_report"] = datetime.now().isoformat()
    PROG.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")


def get_html(url, tries=4):
    for k in range(tries):
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
        except Exception:
            pass
        time.sleep(2 + 2 * k)
    return ""


def is_imovel(title, desc):
    blob = (title + " " + desc).lower()
    pos = any(w in blob for w in IMOVEL_WORDS)
    neg = any(w in blob for w in NEG_WORDS)
    if pos and not neg:
        return True
    # titulo-categoria forte vence ruido na descricao
    tl = title.lower()
    if any(w in tl for w in IMOVEL_WORDS) and not any(w in tl for w in NEG_WORDS):
        return True
    return False


def parse_future_dates(text):
    out = []
    for m in LABEL_DATE_RE.finditer(text):
        mm = DATE_RE.search(m.group(1))
        if not mm:
            continue
        d, mo, y = mm.groups()
        try:
            dt = datetime(int(y), int(mo), int(d))
            if dt.year <= TODAY.year + 3:
                out.append(dt)
        except ValueError:
            continue
    return sorted(set(out))


def to_real(s):
    try:
        return float(s.replace(".", "").replace(",", ".")) if s else None
    except Exception:
        return None


def page_count(html):
    """Maior ?page=N nos links de paginacao da lista de lotes."""
    pages = [int(n) for n in re.findall(r"/lotes\?page=(\d+)", html)]
    return max(pages) if pages else 1


def auction_header_text(soup):
    """Texto do cabecalho (antes da lista de lotes) p/ extrair datas e edital."""
    body = soup.get_text(" ", strip=True)
    first_lote = body.find("Lote 0")
    return body[:first_lote] if first_lote > 0 else body[:1500]


def find_edital(soup):
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.lower().endswith(".pdf") and "edital" in h.lower():
            return urljoin(SITE, h)
    return ""


def parse_cards(html, edital, data_leilao):
    soup = BeautifulSoup(html, "html.parser")
    rows = {}
    for row in soup.find_all("div", class_="row"):
        a = row.find("a", href=re.compile(r"/item/\d+/detalhes"))
        if not a:
            continue
        url = urljoin(SITE, re.sub(r"\?.*$", "", a["href"]))
        if url in rows:
            continue
        col = row.find("div", class_=re.compile(r"col-lg-7"))
        if not col:
            continue
        h5 = col.find("h5")
        title = re.sub(r"\s+", " ", h5.get_text(" ", strip=True)).strip() if h5 else ""
        full = row.get_text(" ", strip=True)
        if DEAD_RE.search(full) and not re.search(r"aberto para lances", full, re.I):
            continue
        desc = col.get_text(" ", strip=True)
        if not is_imovel(title, desc):
            continue
        # cidade/uf
        cm = CIDADE_RE.search(full)
        cidade = re.sub(r"\s+", " ", cm.group(1)).strip()[:60] if cm else ""
        uf = cm.group(2) if cm else ""
        # imagem (background-url do <a> da miniatura)
        img = ""
        for at in row.find_all("a", style=True):
            bm = BGURL_RE.search(at["style"])
            if bm:
                img = bm.group(1)
                break
        # preco: Lance Inicial > VALOR MINIMO > primeiro R$
        lm = LANCE_RE.search(full) or VALMIN_RE.search(full) or PRICE_RE.search(full)
        preco = lm.group(1) if lm else ""
        rows[url] = {
            "leiloeiro": LEILOEIRO, "junta": JUNTA, "site": SITE,
            "titulo": title[:200], "descricao": desc[:500],
            "cidade": cidade, "uf": uf,
            "lance_inicial": to_real(preco), "preco": preco,
            "data_leilao": data_leilao, "url": url, "imagem": img,
            "anexos": edital,
        }
    return rows


def scrape_leilao(lid):
    url1 = f"{SITE}/leilao/{lid}/lotes"
    html = get_html(url1)
    if not html:
        progress["erros"].append(f"leilao {lid}: inacessivel")
        print(f"    [X] leilao {lid} inacessivel", flush=True)
        return []
    soup = BeautifulSoup(html, "html.parser")
    head = auction_header_text(soup)
    datas = parse_future_dates(head)
    futuras = [d for d in datas if d > TODAY]
    if not futuras:
        print(f"    [-] leilao {lid}: sem 1a praca futura (datas={[d.strftime('%d/%m/%Y') for d in datas]}) - pulado", flush=True)
        return []
    data_leilao = futuras[0].strftime("%d/%m/%Y")
    edital = find_edital(soup)
    npages = page_count(html)
    all_rows = {}
    all_rows.update(parse_cards(html, edital, data_leilao))
    for p in range(2, npages + 1):
        h = get_html(f"{url1}?page={p}")
        if not h:
            continue
        all_rows.update(parse_cards(h, edital, data_leilao))
    out = list(all_rows.values())
    print(f"    [OK] leilao {lid}: {npages} pag, {len(out)} imoveis (1a praca {data_leilao})", flush=True)
    return out


def db_insert(conn, rows):
    cur = conn.cursor()
    novos = existe = 0
    for r in rows:
        url = r["url"]
        cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (url,))
        if cur.fetchone():
            existe += 1
            continue
        rid = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        cur.execute("""INSERT INTO imoveis
            (id,leiloeiro,junta,site,titulo,descricao,endereco,cidade,uf,
             lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, r["leiloeiro"], r["junta"], r["site"], r["titulo"],
             r["descricao"], "", r["cidade"], r["uf"], r["lance_inicial"],
             None, r["data_leilao"], url, "imovel", r["imagem"],
             datetime.now().isoformat()))
        novos += 1
    conn.commit()
    return novos, existe


def main():
    print("=" * 72)
    print(f"SCRAPER DANIEL GARCIA LEILOES | captura {CAPTURE:%d/%m/%Y %H:%M}")
    print("=" * 72, flush=True)
    cal = get_html(CALENDARIO)
    ids = sorted(set(re.findall(r"/leilao/(\d+)/lotes", cal)), key=int)
    progress["leiloes_total"] = len(ids)
    print(f"Leiloes no calendario: {len(ids)}\n", flush=True)

    conn = sqlite3.connect(DB)
    all_im = []
    tot_novos = tot_exist = 0
    last = time.time()
    for i, lid in enumerate(ids, 1):
        print(f"[{datetime.now():%H:%M:%S}] >>> leilao {lid} ({i}/{len(ids)})", flush=True)
        try:
            ims = scrape_leilao(lid)
        except Exception as e:
            progress["erros"].append(f"leilao {lid}: {type(e).__name__}: {str(e)[:80]}")
            ims = []
            print(f"    [ERR] {e}", flush=True)
        if ims:
            n, ex = db_insert(conn, ims)
            tot_novos += n
            tot_exist += ex
        all_im.extend(ims)
        progress["por_leilao"][lid] = len(ims)
        progress["imoveis_total"] = len(all_im)
        progress["leiloes_processados"] = i
        progress["db_novos"] = tot_novos
        progress["db_ja_existiam"] = tot_exist
        save_progress()
        if time.time() - last > 300:
            print(f"\n----- REPORT PARCIAL ({datetime.now():%H:%M}) {i}/{len(ids)} leiloes -----")
            print(f"   imoveis={len(all_im)} | banco novos={tot_novos} ja={tot_exist}\n", flush=True)
            last = time.time()

    if all_im:
        out = OUT_DIR / f"imoveis_danielgarcia_{CAPTURE:%Y-%m-%d}.csv"
        cols = ["leiloeiro", "junta", "site", "titulo", "cidade", "uf", "preco",
                "data_leilao", "imagem", "anexos", "url", "descricao"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_im)
        print(f"\n[CSV] {out}")

    conn.close()
    progress["status"] = "concluido"
    save_progress()
    print("\n" + "=" * 72 + "\nRELATORIO FINAL\n" + "=" * 72)
    for lid, q in sorted(progress["por_leilao"].items(), key=lambda x: -x[1]):
        if q:
            print(f"   {q:4} | leilao {lid}")
    print(f"\nTotal imoveis (1a praca futura): {len(all_im)}")
    print(f"Banco: novos={tot_novos} ja_existiam={tot_exist} | Erros: {len(progress['erros'])}")


if __name__ == "__main__":
    main()
