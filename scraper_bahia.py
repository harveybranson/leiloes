# -*- coding: utf-8 -*-
"""
Scraper de imoveis dos leiloeiros REGULARES da Bahia (JUCEB) - PDF "Leiloeiros Bahia".
Entrada : csv/leiloeiros_bahia_2026-06-09.csv (88 regulares; exclui IRREGULAR/cancelados/destituidos).
Render  : Playwright (JS) com fallback FlareSolverr (Cloudflare 401/403/challenge).
Captura : titulo, url, imagem, preco, cidade/uf, data 1a praca, anexos (edital/matricula).
Filtro  : mantem imovel cuja 1a data de leilao > data da captura.
Saida   : CSV datado em /csv + insercao SQLite imoveis_leiloeiros.db (dedup por url).
Report  : por leiloeiro a cada 5 min (scraper_bahia_progress.json + stdout).
"""
import csv, json, re, time, sys, sqlite3, hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests, urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from pg_autoimport import importar_para_site

urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CSV_IN = BASE / "csv" / "leiloeiros_bahia_2026-06-09.csv"
OUT_DIR = BASE / "csv"
DB = BASE / "imoveis_leiloeiros.db"
PROG = BASE / "scraper_bahia_progress.json"
JUNTA = "JUCEB/BA"
FLARE = "http://localhost:8191/v1"
CAPTURE = datetime.now()
TODAY = CAPTURE.replace(hour=0, minute=0, second=0, microsecond=0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}

TITLE_TYPES = ["apartamento", "apto ", "casa", "terreno", "sala comercial",
               "galpao", "galpão", "imovel", "imóvel", "chacara", "chácara", "fazenda",
               "sitio", "sítio", "predio", "prédio", "loja", "kitnet", "sobrado",
               "gleba", "edificio", "edifício", "barracao", "barracão", "cobertura",
               "lote de terreno", "lote urbano", "lote rural", "area de", "área de",
               "parte ideal", "fração", "fracao", "imoveis", "imóveis", "residencia", "residência"]
IMOVEL_WORDS = TITLE_TYPES + ["rua ", "avenida", "av. ", "bairro", "m²", "hectare", "matricula", "matrícula"]
NEG_WORDS = ["veiculo", "veículo", "automovel", "automóvel", "moto", "caminhao", "caminhão",
             "sucata", "trator", "carro", "tv ", "smartv", "smart tv", "iphone", "smartphone",
             "celular", "notebook", "computador", "geladeira", "fogao", "fogão",
             "ar condicionado", "condicionador", "musculacao", "musculação", "bicicleta",
             "semovente", "gado", "boi ", "maquina", "máquina", "impressora", "monitor",
             "eletrodom", "movel ", "móvel ", "moveis", "móveis"]
JUNK_RE = re.compile(r"^(prev|next|aberto para lances|detalhes do lote|ver lote|auditori|"
                     r"lance|leil[aã]o|lote\b|lotes\b|\W*\d)|prev next|aberto para lances|"
                     r"cancelad|encerrad|finalizad|suspens|arrematad", re.I)
DEAD_RE = re.compile(r"cancelad|encerrad|finalizad|arrematad|vendido|suspens", re.I)
CAT_HINT = ["imove", "imóve", "lote", "leila", "leilã", "categoria", "busca", "pesquisa", "detalhe"]
DATE_RE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
PRICE_RE = re.compile(r"R[\$]\s*([\d\.]+,\d{2})")

progress = {"iniciado": CAPTURE.isoformat(), "capture_date": CAPTURE.strftime("%d/%m/%Y %H:%M"),
            "leiloeiros_processados": 0, "imoveis_total": 0,
            "por_leiloeiro": {}, "status_sites": {}, "erros": [], "last_report": CAPTURE.isoformat()}

_PW = None
_BROWSER = None


def save_progress():
    progress["last_report"] = datetime.now().isoformat()
    PROG.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")


def flaresolverr(url):
    try:
        r = requests.post(FLARE, json={"cmd": "request.get", "url": url, "maxTimeout": 45000}, timeout=70)
        j = r.json()
        if j.get("status") == "ok":
            return j["solution"].get("response", ""), j["solution"].get("url", url)
    except Exception:
        pass
    return None, None


def render(url, wait_ms=3500, timeout=45000):
    """Playwright render -> (html, final_url). Fallback FlareSolverr se challenge/erro."""
    global _BROWSER
    html = ""
    final = url
    try:
        ctx = _BROWSER.new_context(user_agent=UA, locale="pt-BR", ignore_https_errors=True)
        pg = ctx.new_page()
        pg.set_default_timeout(timeout)
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                pg.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            pg.wait_for_timeout(wait_ms)
            html = pg.content()
            final = pg.url
        finally:
            ctx.close()
    except Exception:
        html = ""
    low = (html or "")[:9000].lower()
    blocked = (not html or len(html) < 800 or "just a moment" in low or
               "cf-chl" in low or "challenge-platform" in low or "attention required" in low)
    if blocked:
        fh, fu = flaresolverr(url)
        if fh:
            return fh, fu or url
    return html, final


def parse_future_dates(text):
    out = []
    if not text:
        return out
    for m in DATE_RE.finditer(text):
        d, mo, y = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d))
            if dt >= TODAY and dt.year <= TODAY.year + 3:
                out.append(dt)
        except ValueError:
            continue
    return sorted(set(out))


def same_host(a, b):
    return urlparse(a).netloc.replace("www.", "") == urlparse(b).netloc.replace("www.", "")


def collect_links(html, base, want):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = (href + " " + a.get_text(" ", strip=True)).lower()
        if any(k in low for k in want):
            full = urljoin(base, href).split("#")[0]
            if same_host(full, base):
                out.append(full)
    return list(dict.fromkeys(out))


def extract_cards(html, base):
    """Extrai cards de lote/imovel do HTML renderizado."""
    soup = BeautifulSoup(html, "html.parser")
    cards = {}
    for a in soup.find_all("a", href=True):
        block = a.find_parent(["article", "li", "div"]) or a
        btext = block.get_text(" ", strip=True)
        low = btext.lower()
        if DEAD_RE.search(low):
            continue
        href = urljoin(base, a["href"]).split("#")[0]
        if not same_host(href, base):
            continue
        if not re.search(r"lote|imove|imóve|detalhe|/bem/", href.lower()):
            continue
        title = ""
        h = a.find(["h1", "h2", "h3", "h4", "h5"]) or block.find(["h1", "h2", "h3", "h4", "h5"])
        if h:
            title = h.get_text(" ", strip=True)
        if len(title) < 10:
            t2 = a.get_text(" ", strip=True)
            if len(t2) >= 10:
                title = t2
        title = re.sub(r"\s+", " ", title).strip()
        seg = [s for s in urlparse(href).path.split("/") if s and not s.isdigit()]
        slug = seg[-1] if seg else ""
        slug_txt = re.sub(r"[-_]+", " ", slug).strip()
        if (len(title) < 12 or JUNK_RE.search(title) or not any(t in title.lower() for t in TITLE_TYPES)) \
                and len(slug_txt) >= 12 and any(t in slug_txt.lower() for t in TITLE_TYPES):
            title = slug_txt
        tl = title.lower()
        if (not title or len(title) < 12 or len(title) > 200 or JUNK_RE.search(title) or
                not any(t in tl for t in TITLE_TYPES)):
            continue
        if any(w in tl for w in NEG_WORDS) and not re.search(r"m²|m2|hectare|terreno|casa|apartament", tl):
            continue
        img = ""
        imgtag = block.find("img")
        if imgtag:
            img = imgtag.get("src") or imgtag.get("data-src") or imgtag.get("data-original") or ""
            if img and not img.startswith("data:"):
                img = urljoin(base, img)
            else:
                img = ""
        pm = PRICE_RE.search(btext)
        anexos = []
        for la in block.find_all("a", href=True):
            lh = la["href"].lower()
            if lh.endswith(".pdf") or any(k in la.get_text(" ", strip=True).lower()
                                          for k in ["edital", "matricula", "matrícula", "laudo"]):
                anexos.append(urljoin(base, la["href"]))
        cm = re.search(r"\b([A-ZÀ-Ý][a-zà-ÿ]+(?:\s[A-ZÀ-Ý][a-zà-ÿ]+)*)\s*[/-]\s*(AP|PA|AM|RR|RO|AC|MT|MS|MG|SP|RJ|ES|PR|SC|RS|BA|GO|TO|MA|PI|CE|RN|PB|PE|AL|SE|DF)\b", btext)
        cards[href] = {"titulo": title[:200], "url": href, "imagem": img,
                       "preco": pm.group(1) if pm else "", "ctx": btext[:600],
                       "anexos": "; ".join(anexos[:6]),
                       "cidade": cm.group(1) if cm else "", "uf": cm.group(2) if cm else "",
                       "datas": parse_future_dates(btext)}
    return cards


def to_real(s):
    try:
        return float(s.replace(".", "").replace(",", ".")) if s else None
    except Exception:
        return None


def db_insert(conn, rows):
    cur = conn.cursor()
    novos = existe = 0
    for r in rows:
        url = r["url"]
        if url:
            cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (url,))
            if cur.fetchone():
                existe += 1
                continue
        rid = hashlib.md5((url or r["titulo"]).encode("utf-8")).hexdigest()[:16]
        cur.execute("""INSERT INTO imoveis
            (id,leiloeiro,junta,site,titulo,descricao,endereco,cidade,uf,
             lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, r["leiloeiro"], r.get("junta", JUNTA), r["site"], r["titulo"],
             r.get("descricao", ""), "", r.get("cidade", ""), r.get("uf", ""),
             r.get("lance_inicial"), None, r["data_leilao"], url, "imovel",
             r.get("imagem", ""), datetime.now().isoformat()))
        novos += 1
    conn.commit()
    return novos, existe


def scrape_leiloeiro(lei):
    nome, site = lei["nome"], (lei.get("site") or "").strip()
    print(f"[{datetime.now():%H:%M:%S}] >>> {nome} | {site or '(sem site)'}", flush=True)
    if not site:
        progress["status_sites"][nome] = "sem site (nao raspavel)"
        print("    [-] sem site cadastrado - pulado", flush=True)
        return []
    home, base = render(site)
    if not home:
        progress["status_sites"][nome] = "inacessivel"
        progress["erros"].append(f"{nome}: inacessivel")
        print("    [X] inacessivel", flush=True)
        return []
    base = base or site
    cat_links = [u for u in collect_links(home, base, CAT_HINT)]
    cat_links.sort(key=lambda u: (0 if re.search(r"imove|imóve", u.lower()) else 1))
    pages = list(dict.fromkeys([base] + cat_links))[:5]

    cards = {}
    auction_links = []
    for pg in pages:
        h = home if pg == base else render(pg)[0]
        if not h:
            continue
        cards.update(extract_cards(h, base))
        for al in collect_links(h, base, ["leilao_id", "/leilao/", "/lote"]):
            if re.search(r"leilao_id|/lote", al.lower()) and al not in pages:
                auction_links.append(al)
        if len(cards) >= 80:
            break
    auction_links = list(dict.fromkeys(auction_links))[:8]
    for al in auction_links:
        if len(cards) >= 120:
            break
        h = render(al)[0]
        if h:
            cards.update(extract_cards(h, base))

    cards = list(cards.values())[:150]
    print(f"    cards candidatos: {len(cards)}", flush=True)

    imoveis = []
    detail_budget = 12
    for c in cards:
        datas = c["datas"]
        if not datas and detail_budget > 0 and c["url"]:
            detail_budget -= 1
            dh = render(c["url"], wait_ms=2000)[0]
            if dh:
                soup = BeautifulSoup(dh, "html.parser")
                txt = soup.get_text(" ", strip=True)[:9000]
                datas = parse_future_dates(txt)
                if not c["imagem"]:
                    im = soup.find("img")
                    if im and (im.get("src") or "").startswith("http"):
                        c["imagem"] = im.get("src")
                if not c["anexos"]:
                    anx = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)
                           if a["href"].lower().endswith(".pdf") or "edital" in a.get_text().lower()
                           or "matr" in a.get_text().lower()]
                    c["anexos"] = "; ".join(anx[:6])
                if not c["preco"]:
                    pm = PRICE_RE.search(txt)
                    c["preco"] = pm.group(1) if pm else ""
        if not datas:
            continue
        primeira = datas[0]
        if primeira <= TODAY:
            continue
        imoveis.append({
            "leiloeiro": nome, "junta": lei.get("junta") or JUNTA, "site": site,
            "titulo": c["titulo"], "descricao": c["ctx"][:500],
            "cidade": c["cidade"] or lei.get("cidade", ""), "uf": c["uf"] or lei.get("uf", ""),
            "lance_inicial": to_real(c["preco"]), "preco": c["preco"],
            "data_leilao": primeira.strftime("%d/%m/%Y"),
            "url": c["url"], "imagem": c["imagem"], "anexos": c["anexos"],
        })
    progress["status_sites"][nome] = f"ok ({len(imoveis)})"
    print(f"    [OK] imoveis com 1a praca futura: {len(imoveis)}", flush=True)
    return imoveis


def main():
    global _PW, _BROWSER
    leiloeiros = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
    print("=" * 72)
    print(f"SCRAPER BAHIA/JUCEB | captura {CAPTURE:%d/%m/%Y %H:%M} | {len(leiloeiros)} leiloeiros")
    print("=" * 72, flush=True)
    _PW = sync_playwright().start()
    _BROWSER = _PW.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    conn = sqlite3.connect(DB)
    all_im = []
    last = time.time()
    tot_novos = tot_exist = 0
    for i, lei in enumerate(leiloeiros, 1):
        try:
            ims = scrape_leiloeiro(lei)
        except Exception as e:
            progress["erros"].append(f"{lei['nome']}: {type(e).__name__}: {str(e)[:80]}")
            ims = []
            print(f"    [ERR] {e}", flush=True)
        if ims:
            n, ex = db_insert(conn, ims)
            tot_novos += n
            tot_exist += ex
        all_im.extend(ims)
        progress["por_leiloeiro"][lei["nome"]] = len(ims)
        progress["imoveis_total"] = len(all_im)
        progress["leiloeiros_processados"] = i
        progress["db_novos"] = tot_novos
        progress["db_ja_existiam"] = tot_exist
        save_progress()
        if time.time() - last > 300:
            print(f"\n----- REPORT PARCIAL ({datetime.now():%H:%M}) sites {i}/{len(leiloeiros)} -----")
            for nm, q in progress["por_leiloeiro"].items():
                print(f"   {q:4} | {nm}")
            print(f"   TOTAL: {len(all_im)} | banco novos={tot_novos}\n", flush=True)
            last = time.time()

    if all_im:
        out = OUT_DIR / f"imoveis_bahia_{CAPTURE:%Y-%m-%d}.csv"
        cols = ["leiloeiro", "junta", "site", "titulo", "cidade", "uf", "preco",
                "data_leilao", "imagem", "anexos", "url"]
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_im)
        print(f"\n[CSV] {out}")
        importar_para_site(out, JUNTA)

    conn.close()
    _BROWSER.close()
    _PW.stop()
    progress["status"] = "concluido"
    save_progress()
    print("\n" + "=" * 72 + "\nRELATORIO FINAL\n" + "=" * 72)
    for nm, q in sorted(progress["por_leiloeiro"].items(), key=lambda x: -x[1]):
        print(f"   {q:4} | {nm} -> {progress['status_sites'].get(nm,'')}")
    print(f"\nTotal imoveis (1a praca futura): {len(all_im)}")
    print(f"Banco: novos={tot_novos} ja_existiam={tot_exist} | Erros: {len(progress['erros'])}")


if __name__ == "__main__":
    main()
