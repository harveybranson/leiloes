# -*- coding: utf-8 -*-
"""
Pipeline de STAGING (área de aprovação) de novos anúncios de imóveis.

Fluxo:
  1. Captura imóveis dos leiloeiros (reusa scraper_rr_ro) com crawl mais fundo.
  2. ENRIQUECE cada anúncio abrindo a página de detalhe: descrição, todas as fotos,
     edital, matrícula e demais anexos (PDFs).
  3. COMPARA a URL contra os DOIS bancos (SQLite imoveis_leiloeiros.db e Postgres
     leilao_db). NÃO deduplica anúncios — apenas classifica NOVO x JÁ-NO-BANCO.
  4. NOVOS -> tabela de staging (SQLite staging.db) + JSON, e gera a página de
     aprovação anuncios_novos.html (somente novos). JÁ-NO-BANCO -> marca p/ enriquecer.
  5. A inserção nos 2 bancos só acontece depois do OK, via aprovar_anuncios.py.
"""
import csv, json, re, sqlite3, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_LEI = BASE / "csv" / "leiloeiros_roraima_rondonia_2026-06-09.csv"
SQLITE_MAIN = BASE / "imoveis_leiloeiros.db"
STAGING_DB = BASE / "staging.db"
STAGING_JSON = BASE / "staging_anuncios.json"
HTML_OUT = BASE / "anuncios_novos.html"
PG_DSN = "host=localhost port=5432 dbname=leilao_db user=leilao password=leilao123"

import scraper_rr_ro as S  # reusa render(), scrape_leiloeiro(), extract_cards()
import scraper_commons as S_commons  # inferir_uf (backfill de UF no staging)

PDF_OK = re.compile(r"edital|matr[ií]cula|laudo|avalia[çc][ãa]o|certid|processo", re.I)
PDF_BAD = re.compile(r"cookie|termo|pol[ií]tica|aviso|lgpd|privacidade", re.I)
IMG_BAD = re.compile(r"logo|icon|sprite|avatar|placeholder|banner|whats|loading", re.I)


def enrich(url):
    """Abre a página do lote e extrai descrição, fotos[], edital, matrícula, anexos[]."""
    out = {"descricao": "", "fotos": [], "edital": "", "matricula": "", "anexos": []}
    if not url:
        return out
    html = S.render(url, wait_ms=2500)[0]
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    base = url
    # fotos
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-original") or ""
        if src.startswith("http") and not IMG_BAD.search(src) and not src.startswith("data:"):
            full = src.split("?")[0]
            if full not in out["fotos"]:
                out["fotos"].append(full)
    out["fotos"] = out["fotos"][:12]
    # anexos / edital / matrícula
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = a.get_text(" ", strip=True)
        low = (href + " " + txt).lower()
        is_pdf = href.lower().endswith(".pdf")
        if (is_pdf or PDF_OK.search(low)) and not PDF_BAD.search(low):
            full = urljoin(base, href)
            tipo = ("matricula" if "matr" in low else "edital" if "edital" in low
                    else "laudo" if "laudo" in low else "documento")
            out["anexos"].append({"tipo": tipo, "url": full, "nome": (txt or tipo)[:80]})
            if tipo == "edital" and not out["edital"]:
                out["edital"] = full
            if tipo == "matricula" and not out["matricula"]:
                out["matricula"] = full
    # dedup anexos por url
    seen = set(); uniq = []
    for d in out["anexos"]:
        if d["url"] not in seen:
            seen.add(d["url"]); uniq.append(d)
    out["anexos"] = uniq[:10]
    # descrição: maior bloco de texto plausível
    cand = ""
    for sel in [".descricao", ".description", "[class*=descri]", ".lote-descricao",
                ".conteudo", "#descricao", "article", ".detalhe"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if len(t) > len(cand):
                cand = t
    if len(cand) < 60:
        cand = soup.get_text(" ", strip=True)
    out["descricao"] = re.sub(r"\s+", " ", cand)[:1500]
    return out


def in_sqlite(cur, url):
    cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (url,))
    return cur.fetchone() is not None


def pg_existing(urls):
    try:
        import psycopg2
        cn = psycopg2.connect(PG_DSN); cur = cn.cursor()
        cur.execute("SELECT url_original FROM imoveis WHERE url_original = ANY(%s)", (urls,))
        got = {r[0] for r in cur.fetchall()}
        cn.close()
        return got
    except Exception as e:
        print(f"[WARN] Postgres indisponível ({e}); checando só SQLite.", flush=True)
        return set()


def ensure_staging():
    cn = sqlite3.connect(STAGING_DB)
    cn.execute("""CREATE TABLE IF NOT EXISTS staging_imoveis(
        url TEXT PRIMARY KEY, leiloeiro TEXT, site TEXT, titulo TEXT, descricao TEXT,
        cidade TEXT, uf TEXT, preco TEXT, lance_inicial REAL, data_leilao TEXT,
        tipo TEXT, imagem TEXT, fotos TEXT, edital TEXT, matricula TEXT, anexos TEXT,
        status TEXT, aprovado INTEGER DEFAULT 0, capturado_em TEXT)""")
    cn.commit()
    return cn


def main():
    leiloeiros = list(csv.DictReader(open(CSV_LEI, encoding="utf-8")))
    # foco em leiloeiros produtivos (têm imóveis) para a demonstração
    alvo = {"Deonizia Kiratch", "Thais Costa Bastos Teixeira",
            "Rodrigo Aparecido Rigolon da Silva", "Dora Plat (Portal Zuk)"}
    leiloeiros = [l for l in leiloeiros if l["nome"] in alvo] or leiloeiros[:4]

    from playwright.sync_api import sync_playwright
    S._PW = sync_playwright().start()
    S._BROWSER = S._PW.chromium.launch(headless=True, args=["--no-sandbox"])

    capturados = []
    for lei in leiloeiros:
        try:
            ims = S.scrape_leiloeiro(lei)
        except Exception as e:
            print(f"  [ERR] {lei['nome']}: {e}", flush=True)
            ims = []
        capturados.extend(ims)
    print(f"\nTotal capturado: {len(capturados)}", flush=True)

    # enriquecimento (detalhe) — limita p/ runtime
    for im in capturados:
        try:
            det = enrich(im.get("url", ""))
        except Exception:
            det = {"descricao": "", "fotos": [], "edital": "", "matricula": "", "anexos": []}
        im["descricao_full"] = det["descricao"] or im.get("descricao", "")
        im["fotos"] = det["fotos"] or ([im.get("imagem")] if im.get("imagem") else [])
        im["edital"] = det["edital"]
        im["matricula"] = det["matricula"]
        anx = det["anexos"]
        if im.get("anexos"):  # anexos já vindos do card
            for u in str(im["anexos"]).split(";"):
                u = u.strip()
                if u and not any(d["url"] == u for d in anx):
                    anx.append({"tipo": "documento", "url": u, "nome": "anexo"})
        im["anexos_list"] = anx

    # comparação com os 2 bancos (NÃO deduplica; só classifica)
    urls = [im["url"] for im in capturados if im.get("url")]
    pg = pg_existing(urls)
    scon = sqlite3.connect(SQLITE_MAIN); scur = scon.cursor()
    novos, existentes = [], []
    for im in capturados:
        u = im.get("url", "")
        in_sl = in_sqlite(scur, u) if u else False
        in_pg = u in pg
        im["status"] = "NOVO" if not (in_sl or in_pg) else "JA_NO_BANCO"
        (novos if im["status"] == "NOVO" else existentes).append(im)
    scon.close()
    print(f"NOVOS: {len(novos)} | JÁ NO BANCO (enriquecer): {len(existentes)}", flush=True)

    # grava staging dos NOVOS
    cn = ensure_staging()
    for im in novos:
        cn.execute("""INSERT OR REPLACE INTO staging_imoveis
            (url,leiloeiro,site,titulo,descricao,cidade,uf,preco,lance_inicial,data_leilao,
             tipo,imagem,fotos,edital,matricula,anexos,status,aprovado,capturado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (im["url"], im["leiloeiro"], im.get("site", ""), im["titulo"], im.get("descricao_full", ""),
             im.get("cidade", ""),
             im.get("uf") or S_commons.inferir_uf(im.get("cidade"), im.get("titulo"),
                                                   im.get("descricao_full")) or "",
             im.get("preco", ""), im.get("lance_inicial"),
             im.get("data_leilao", ""), "imovel", (im.get("fotos") or [""])[0],
             json.dumps(im.get("fotos", []), ensure_ascii=False), im.get("edital", ""),
             im.get("matricula", ""), json.dumps(im.get("anexos_list", []), ensure_ascii=False),
             "NOVO", datetime.now().isoformat()))
    cn.commit(); cn.close()
    STAGING_JSON.write_text(json.dumps({"novos": novos, "existentes": [
        {"url": e["url"], "anexos": e.get("anexos_list", []), "fotos": e.get("fotos", []),
         "descricao": e.get("descricao_full", "")} for e in existentes]},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    gerar_html(novos)
    S._BROWSER.close(); S._PW.stop()
    print(f"\n[OK] Staging: {len(novos)} novos -> {STAGING_DB.name}")
    print(f"[OK] Página de aprovação: {HTML_OUT}")
    print(f"[OK] Enriquecimento p/ {len(existentes)} já existentes em {STAGING_JSON.name}")


def gerar_html(novos):
    cards = []
    for i, im in enumerate(novos):
        fotos = im.get("fotos", [])[:6]
        foto = fotos[0] if fotos else ""
        anexos = im.get("anexos_list", [])
        anx_html = " ".join(
            f'<a class="doc" href="{a["url"]}" target="_blank">📎 {a["tipo"]}</a>' for a in anexos)
        gal = "".join(f'<img src="{f}" loading="lazy">' for f in fotos[1:6])
        cards.append(f"""
        <div class="card">
          <label class="chk"><input type="checkbox" class="ap" data-url="{im['url']}" checked> Aprovar</label>
          <div class="foto">{f'<img src="{foto}" loading="lazy">' if foto else 'sem foto'}</div>
          <div class="info">
            <h3>{(im.get('titulo') or '')[:120]}</h3>
            <p class="meta">{im.get('cidade','')}/{im.get('uf','')} · 1ª praça: <b>{im.get('data_leilao','')}</b> · {im.get('preco') and ('R$ '+im['preco']) or ''}</p>
            <p class="leil">{im.get('leiloeiro','')}</p>
            <p class="desc">{(im.get('descricao_full') or '')[:300]}</p>
            <div class="docs">{anx_html or '<span class=nodoc>sem documentos</span>'}</div>
            <div class="gal">{gal}</div>
            <a class="link" href="{im['url']}" target="_blank">ver anúncio ↗</a>
          </div>
        </div>""")
    html = f"""<!doctype html><html lang=pt-BR><head><meta charset=utf-8>
<title>Novos anúncios para aprovação ({len(novos)})</title>
<style>
body{{font-family:system-ui,Arial;margin:0;background:#0f1115;color:#e6e6e6}}
header{{position:sticky;top:0;background:#161a22;padding:16px 24px;border-bottom:1px solid #2a2f3a;display:flex;justify-content:space-between;align-items:center;z-index:5}}
header h1{{font-size:18px;margin:0}} .muted{{color:#9aa4b2;font-size:13px}}
button{{background:#2f81f7;color:#fff;border:0;padding:10px 16px;border-radius:8px;font-size:14px;cursor:pointer}}
.wrap{{padding:20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}}
.card{{background:#161a22;border:1px solid #2a2f3a;border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
.foto img,.foto{{width:100%;height:200px;object-fit:cover;background:#222;display:block}}
.info{{padding:12px 14px}} .info h3{{font-size:15px;margin:.2em 0}}
.meta{{color:#cbd5e1;font-size:13px;margin:.2em 0}} .leil{{color:#7dd3fc;font-size:12px;margin:.2em 0}}
.desc{{color:#9aa4b2;font-size:12px;max-height:60px;overflow:hidden}}
.docs{{margin:.5em 0}} .doc{{display:inline-block;background:#22305a;color:#cfe0ff;padding:3px 8px;border-radius:6px;font-size:12px;margin:2px;text-decoration:none}}
.nodoc{{color:#6b7280;font-size:12px}} .gal img{{width:48px;height:48px;object-fit:cover;border-radius:6px;margin:2px}}
.chk{{display:block;padding:8px 12px;background:#10141c;font-size:13px;border-bottom:1px solid #2a2f3a}}
.link{{color:#2f81f7;font-size:12px}}
</style></head><body>
<header>
  <div><h1>🆕 Novos anúncios de imóveis — aguardando aprovação</h1>
  <div class=muted>{len(novos)} novos · capturado em {datetime.now():%d/%m/%Y %H:%M} · revise e dê OK para inserir nos 2 bancos</div></div>
  <button onclick="exportar()">Gerar lista de aprovados</button>
</header>
<div class=wrap>{''.join(cards) or '<p style=padding:24px>Nenhum anúncio novo (todos já estão nos bancos).</p>'}</div>
<script>
function exportar(){{
  const urls=[...document.querySelectorAll('.ap:checked')].map(c=>c.dataset.url);
  const blob=new Blob([urls.join('\\n')],{{type:'text/plain'}});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='aprovados.txt';a.click();
  alert(urls.length+' anúncios marcados. Salve aprovados.txt e rode: python aprovar_anuncios.py');
}}
</script></body></html>"""
    HTML_OUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
