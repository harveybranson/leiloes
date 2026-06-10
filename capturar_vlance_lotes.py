# -*- coding: utf-8 -*-
"""
Captura por LOTE (granularidade real) das plataformas vlance e envia ao staging.
- get-leiloes (API) -> leilões de imóveis com data FUTURA (abertos).
- Renderiza cada página de leilão -> hrefs reais por lote
  (/leilao/index/leilao_id/<aid>/lote/<lid>) — URLs que NÃO estão no banco.
- Renderiza cada lote -> título, 1ª praça, lance, fotos, descrição, edital, matrícula, anexos.
- Compara com os 2 bancos (todas novas) e grava em staging.db; regenera a página.
"""
import sys, re, json, sqlite3, requests, urllib3
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import scraper_rr_ro as S
import staging_anuncios as ST
from playwright.sync_api import sync_playwright

H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
PRICE = re.compile(r"R\$\s*([\d.]+,\d{2})")
LOTE_HREF = re.compile(r"/leilao/index/leilao_id/\d+/lote/\d+")
TIPOS = ["apartamento", "apto", "casa", "terreno", "sítio", "sitio", "chácara", "chacara",
         "fazenda", "galpão", "galpao", "sala comercial", "loja", "imóvel", "imovel",
         "gleba", "lote urbano", "lote de terreno", "edifício", "edificio", "área", "fração"]
# leiloeiros vlance do conjunto RR/RO (detectáveis por /core/api/get-leiloes)
VLANCE = [("Deonizia Kiratch", "https://www.deonizialeiloes.com.br")]


def get_auctions(base):
    try:
        r = requests.get(base + "/core/api/get-leiloes", headers=H, verify=False, timeout=25)
        items = r.json().get("items", [])
    except Exception:
        return []
    out = []
    for it in items:
        df = it.get("dt_formatada", "")
        cat = (it.get("nm", "") + " " + str(it.get("categoria", ""))).lower()
        try:
            d = datetime.strptime(df, "%d/%m/%Y")
        except Exception:
            d = None
        if d and d > TODAY:  # estritamente futuro (aberto)
            out.append({"id": it["id"], "dt": df, "nm": it.get("nm", ""),
                        "dt2": it.get("dt_segundoleilao_data", "")})
    return out


def lot_links(base, aid):
    h = S.render(f"{base}/leilao/index/leilao_id/{aid}", wait_ms=5000)[0] or ""
    soup = BeautifulSoup(h, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if LOTE_HREF.search(href) and "undefined" not in href:
            out.append(urljoin(base, href.split("#")[0]))
    return list(dict.fromkeys(out))


def parse_lot(url, auction):
    h = S.render(url, wait_ms=4000)[0] or ""
    if not h:
        return None
    soup = BeautifulSoup(h, "html.parser")
    txt = soup.get_text(" ", strip=True)
    # título: 1ª frase com tipo de imóvel na descrição do lote
    titulo = ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "div"]):
        t = el.get_text(" ", strip=True)
        tl = t.lower()
        if 15 < len(t) < 160 and any(k in tl for k in TIPOS) and "leilão" not in tl[:8]:
            titulo = t
            break
    if not titulo:
        titulo = f"Lote {url.rsplit('/', 1)[-1]} - {auction['nm'][:50]}"
    # 1ª praça
    d1 = None
    m = re.search(r"1[ºoª][^0-9]{0,30}(\d{2}/\d{2}/\d{4})", txt)
    if not m:
        m = re.search(r"encerramento[^0-9]{0,20}(\d{2}/\d{2}/\d{4})", txt, re.I)
    if m:
        try:
            d1 = datetime.strptime(m.group(1), "%d/%m/%Y")
        except Exception:
            d1 = None
    if not d1:
        try:
            d1 = datetime.strptime(auction["dt"], "%d/%m/%Y")
        except Exception:
            d1 = None
    if not d1 or d1 < TODAY:
        return None
    # preço (lance) = menor valor >= 1000
    vals = sorted({ST.to_real(p) if False else _to_real(p) for p in PRICE.findall(txt)} - {None})
    vals = [v for v in vals if v and v >= 1000]
    preco = ""
    if vals:
        for p in PRICE.findall(txt):
            if _to_real(p) == vals[0]:
                preco = p
                break
    det = ST.enrich(url)  # fotos, descrição, edital, matrícula, anexos
    cm = re.search(r"\b([A-ZÀ-Ý][a-zà-ÿ]+(?:\s[A-ZÀ-Ý][a-zà-ÿ]+)*)\s*[/-]\s*(RO|RR|AC|AM|SP|MG)\b", titulo + " " + auction["nm"])
    return {
        "leiloeiro": auction["leiloeiro"], "site": auction["site"], "url": url,
        "titulo": re.sub(r"\s+", " ", titulo)[:200],
        "descricao_full": det["descricao"], "fotos": det["fotos"],
        "edital": det["edital"], "matricula": det["matricula"], "anexos_list": det["anexos"],
        "preco": preco, "lance_inicial": _to_real(preco),
        "cidade": cm.group(1) if cm else "", "uf": cm.group(2) if cm else "RO",
        "data_leilao": d1.strftime("%d/%m/%Y"),
    }


def _to_real(s):
    try:
        return float(str(s).replace(".", "").replace(",", ".")) if s else None
    except Exception:
        return None


def main(max_auctions=6, max_lots=24):
    S._PW = sync_playwright().start()
    S._BROWSER = S._PW.chromium.launch(headless=True, args=["--no-sandbox"])
    capturados = []
    for nome, site in VLANCE:
        aucs = get_auctions(site)
        print(f"{nome}: {len(aucs)} leilões futuros (abertos)", flush=True)
        for a in aucs[:max_auctions]:
            a["leiloeiro"] = nome; a["site"] = site
            links = lot_links(site, a["id"])
            print(f"  leilão {a['id']} ({a['dt']}): {len(links)} lotes", flush=True)
            for url in links:
                if len([c for c in capturados]) >= max_lots:
                    break
                try:
                    lot = parse_lot(url, a)
                except Exception as e:
                    lot = None
                if lot:
                    capturados.append(lot)
            if len(capturados) >= max_lots:
                break
    print(f"\nLotes capturados (1ª praça futura): {len(capturados)}", flush=True)

    # comparação com os 2 bancos
    urls = [c["url"] for c in capturados]
    pg = ST.pg_existing(urls)
    scon = sqlite3.connect(ST.SQLITE_MAIN); scur = scon.cursor()
    novos = []
    for c in capturados:
        in_sl = ST.in_sqlite(scur, c["url"])
        if not in_sl and c["url"] not in pg:
            c["status"] = "NOVO"; novos.append(c)
    scon.close()
    print(f"NOVOS (não estão em nenhum banco): {len(novos)}", flush=True)

    cn = ST.ensure_staging()
    for im in novos:
        cn.execute("""INSERT OR REPLACE INTO staging_imoveis
            (url,leiloeiro,site,titulo,descricao,cidade,uf,preco,lance_inicial,data_leilao,
             tipo,imagem,fotos,edital,matricula,anexos,status,aprovado,capturado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (im["url"], im["leiloeiro"], im["site"], im["titulo"], im.get("descricao_full", ""),
             im.get("cidade", ""), im.get("uf", ""), im.get("preco", ""), im.get("lance_inicial"),
             im["data_leilao"], "imovel", (im.get("fotos") or [""])[0],
             json.dumps(im.get("fotos", []), ensure_ascii=False), im.get("edital", ""),
             im.get("matricula", ""), json.dumps(im.get("anexos_list", []), ensure_ascii=False),
             "NOVO", datetime.now().isoformat()))
    cn.commit(); cn.close()
    # regenera página só com os novos
    ST.gerar_html([{**im, "anexos_list": im.get("anexos_list", []),
                    "descricao_full": im.get("descricao_full", "")} for im in novos])
    S._BROWSER.close(); S._PW.stop()
    print(f"\n[OK] {len(novos)} novos -> staging.db | página: {ST.HTML_OUT}")


if __name__ == "__main__":
    main()
