# -*- coding: utf-8 -*-
"""
Adapter dedicado: NORTE LEILOES (Sandro de Oliveira - JUCAP 14/2021).
Site Next.js/RSC (norteleiloes.com.br) - os lotes ficam em /lotes com lazy-load e a
data da 1a praca esta na linha "1o LEILAO ... ID nnn - DD/MM/YYYY", NAO no cabecalho
global "PROXIMO LEILAO: HOJE" (armadilha que fez o scraper generico capturar a data errada).

Fluxo:
  1. Abre /lotes, faz scroll incremental ate parar de carregar -> coleta /lote/<id>.
  2. Em cada detalhe: titulo (CODIGO - CIDADE/UF - TIPO - desc), data da 1a praca (1o leilao),
     avaliacao/lance, imagem real (_534_380, ignora logo _250_50 e pixels), anexos (PDF/arquivo).
  3. Mantem so IMOVEL com 1a praca > hoje. Pula veiculos e lotes em 2a praca (1a ja passou).
  4. Insere em imoveis_leiloeiros.db (dedup por URL) + CSV datado em /csv.
"""
import csv, re, sys, sqlite3, hashlib, json
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
DB = BASE / "imoveis_leiloeiros.db"
OUT = BASE / "csv" / f"imoveis_norte_amapa_{datetime.now():%Y-%m-%d}.csv"
SITE = "https://www.norteleiloes.com.br"
LISTA = SITE + "/lotes"
LEILOEIRO = "Sandro de Oliveira"
JUNTA = "JUCAP/AP"
TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

IMOVEL = ["sala comercial", "imovel", "imóvel", "casa", "terreno", "apartamento", "apto",
          "galpao", "galpão", "loja", "predio", "prédio", "sobrado", "kitnet", "chacara",
          "chácara", "fazenda", "sitio", "sítio", "gleba", "lote de terreno", "fracao", "fração",
          "area de", "área de", "cobertura", "edificio", "edifício", "barracao", "barracão",
          "residencia", "residência", "imobili"]
VEICULO = ["motocicleta", "motocicletas", "veiculo", "veículo", "automovel", "automóvel",
           "caminhao", "caminhão", "reboque", "trator", "/ano", "placa ", "chassi",
           "toyota", "volkswagen", "fiat", "chevrolet", "ford", "honda", "yamaha", "hyundai",
           "renault", "jeep", "nissan", "scania", "volvo", "mercedes", "hilux", "corolla"]

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
PRICE_RE = re.compile(r"R\$\s*([\d\.]+,\d{2})")
# linha do leilao: "1o LEILAO ... ID 4153 • 15/06/2026 • 09h00" (separador e bullet U+2022)
PRACA_RE = re.compile(r"(1|2)[ºoªa]?\s*LEIL[ÃA]O.{0,160}?ID\s*\d+\D{1,6}(\d{1,2}/\d{1,2}/\d{4})", re.I)


def to_real(s):
    try:
        return float(s.replace(".", "").replace(",", ".")) if s else None
    except Exception:
        return None


def harvest_lote_links(pg):
    pg.goto(LISTA, wait_until="networkidle", timeout=45000)
    pg.wait_for_timeout(3500)
    seen = set()
    stale = 0
    for _ in range(40):
        links = set(re.findall(r"/lote/\d+", pg.content()))
        if len(links) <= len(seen):
            stale += 1
        else:
            stale = 0
        seen |= links
        if stale >= 4:
            break
        pg.mouse.wheel(0, 4000)
        pg.wait_for_timeout(1200)
    return sorted(seen, key=lambda u: int(u.split("/")[-1]))


def parse_detail(html, url):
    s = BeautifulSoup(html, "html.parser")
    txt = re.sub(r"\s+", " ", s.get_text(" ", strip=True))
    # titulo: heading com padrao CODIGO - CIDADE/UF - TIPO - desc
    titulo = ""
    for h in s.find_all(["h1", "h2", "h3"]):
        t = h.get_text(" ", strip=True)
        if re.search(r"\d{4,}\s*-\s*[A-Za-zÀ-ÿ ]+/[A-Z]{2}\s*-", t):
            titulo = t
            break
    if not titulo:
        m = re.search(r"\d{6,}\s*-\s*[A-Za-zÀ-ÿ .]+/[A-Z]{2}\s*-\s*[^.]{5,120}", txt)
        titulo = m.group(0) if m else ""
    titulo = re.sub(r"\s+", " ", titulo).strip()[:200]
    if not titulo:
        return None
    low = titulo.lower()
    # classificacao imovel x veiculo
    is_imovel = any(k in low for k in IMOVEL)
    is_veiculo = any(k in low for k in VEICULO)
    if not is_imovel or (is_veiculo and not is_imovel):
        return None
    # cidade/uf
    cm = re.search(r"-\s*([A-Za-zÀ-ÿ .]+?)\s*/\s*([A-Z]{2})\s*-", titulo)
    cidade = cm.group(1).strip() if cm else ""
    uf = cm.group(2) if cm else ""
    # 1a praca: linha do 1o leilao
    pracas = {}
    for n, d in PRACA_RE.findall(txt):
        try:
            pracas.setdefault(n, datetime(*map(int, reversed(d.split("/")))))
        except ValueError:
            pass
    primeira = pracas.get("1")
    if not primeira:  # sem rotulo de 1o leilao -> nao aceita (evita capturar data global)
        return None
    # preco: maior valor = avaliacao
    precos = [to_real(p) for p in PRICE_RE.findall(txt)]
    precos = [p for p in precos if p]
    avaliacao = max(precos) if precos else None
    lance = min(precos) if precos else None
    # imagem real (_534_380), ignora logo _250_50 e trackers
    img = ""
    for i in s.find_all("img"):
        src = i.get("src") or i.get("data-src") or ""
        if src.startswith("http") and "_250_50" not in src and "facebook" not in src and "/tr?" not in src:
            img = src
            break
    # anexos
    anexos = []
    for a in s.find_all("a", href=True):
        hf = a["href"]
        if hf.lower().endswith(".pdf") or "download-veiculo-arquivo" in hf or "download-arquivo" in hf \
                or any(k in a.get_text(" ", strip=True).lower() for k in ["edital", "matricula", "matrícula", "laudo"]):
            anexos.append(hf if hf.startswith("http") else SITE + hf)
    return {"titulo": titulo, "cidade": cidade, "uf": uf, "primeira": primeira,
            "avaliacao": avaliacao, "lance": lance, "imagem": img,
            "anexos": "; ".join(dict.fromkeys(anexos))[:500], "url": url}


def db_insert(rows):
    conn = sqlite3.connect(DB); cur = conn.cursor()
    novos = existe = 0
    for r in rows:
        cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (r["url"],))
        if cur.fetchone():
            existe += 1
            continue
        rid = hashlib.md5(r["url"].encode("utf-8")).hexdigest()[:16]
        cur.execute("""INSERT INTO imoveis
            (id,leiloeiro,junta,site,titulo,descricao,endereco,cidade,uf,
             lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, LEILOEIRO, JUNTA, SITE, r["titulo"], "", "", r["cidade"], r["uf"],
             r["lance"], r["avaliacao"], r["primeira"].strftime("%d/%m/%Y"), r["url"],
             "imovel", r["imagem"], datetime.now().isoformat()))
        novos += 1
    conn.commit(); conn.close()
    return novos, existe


def main():
    print("=" * 70)
    print(f"ADAPTER NORTE LEILOES | captura {datetime.now():%d/%m/%Y %H:%M}")
    print("=" * 70, flush=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(ignore_https_errors=True, locale="pt-BR",
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36")
        pg = ctx.new_page()
        pg.set_default_timeout(45000)
        lotes = harvest_lote_links(pg)
        print(f"lotes encontrados em /lotes: {len(lotes)}", flush=True)
        imoveis = []
        for i, path in enumerate(lotes, 1):
            url = SITE + path
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    pg.wait_for_function("document.body.innerText.includes('LEIL')", timeout=12000)
                except Exception:
                    pass
                pg.wait_for_timeout(1500)
                d = parse_detail(pg.content(), url)
            except Exception as e:
                print(f"  [{i}/{len(lotes)}] {path} ERRO {type(e).__name__}", flush=True)
                continue
            if not d:
                continue
            fut = d["primeira"] > TODAY
            tag = "OK-FUTURO" if fut else "passou/hoje"
            print(f"  [{i}/{len(lotes)}] {d['primeira']:%d/%m/%Y} [{tag}] {d['titulo'][:60]}", flush=True)
            if fut:
                imoveis.append(d)
        b.close()

    print(f"\nIMOVEIS com 1a praca futura: {len(imoveis)}", flush=True)
    if imoveis:
        cols = ["leiloeiro", "junta", "site", "titulo", "cidade", "uf", "avaliacao",
                "lance", "data_leilao", "imagem", "anexos", "url"]
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in imoveis:
                w.writerow({"leiloeiro": LEILOEIRO, "junta": JUNTA, "site": SITE,
                            "titulo": r["titulo"], "cidade": r["cidade"], "uf": r["uf"],
                            "avaliacao": r["avaliacao"], "lance": r["lance"],
                            "data_leilao": r["primeira"].strftime("%d/%m/%Y"),
                            "imagem": r["imagem"], "anexos": r["anexos"], "url": r["url"]})
        print(f"[CSV] {OUT}")
        n, ex = db_insert(imoveis)
        print(f"[BANCO] novos={n} ja_existiam={ex}")
        # verificacao
        conn = sqlite3.connect(DB)
        grav = sum(1 for r in imoveis if conn.execute("SELECT 1 FROM imoveis WHERE url=?", (r["url"],)).fetchone())
        conn.close()
        print(f"[VERIFICACAO] coletados={len(imoveis)} gravados={grav} -> {'OK' if grav==len(imoveis) else 'FALTAM'}")
    print("\n=== RELATORIO NORTE ===")
    for r in imoveis:
        print(f"  {r['primeira']:%d/%m/%Y} | {r['cidade']}/{r['uf']} | {r['titulo'][:55]}")


if __name__ == "__main__":
    main()
