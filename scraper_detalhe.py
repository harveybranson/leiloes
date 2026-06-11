"""
scraper_detalhe.py  —  Enriquecimento de imóveis com dados de página de detalhe
Referência: captura_dados_leiloes_v2.md  (seções 3, 5, 14, 16)

Lê ofertas_completo.csv (1505 URLs), visita cada página individualmente e
extrai: tipo_imovel, tipo_leilao, area_m2, quartos, banheiros, vagas,
        data_leilao_1, data_leilao_2, descricao, bairro, imagem_url.

Central Sul: dados já vêm ricos da API → apenas classifica, sem visita.
Milan:       usa FlareSolverr (seção 14) em vez de Playwright.
Demais:      Playwright async, CONCURRENCY páginas simultâneas.

Uso:
  python scraper_detalhe.py               # retoma de onde parou
  python scraper_detalhe.py --reset       # reinicia do zero
  python scraper_detalhe.py --limite 200  # processa só N URLs
"""

import argparse
import asyncio
import csv
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright

import scraper_commons as sc

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = "imoveis_leiloeiros.db"

INPUT_CSV    = "ofertas_completo.csv"
OUTPUT_CSV   = "ofertas_detalhadas.csv"
PROGRESS_FILE= "scraper_detalhe_progress.json"
FLARESOLVERR = "http://localhost:8191/v1"
CONCURRENCY  = 5   # páginas Playwright simultâneas

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

FIELDNAMES = [
    "fonte", "url", "titulo", "descricao",
    "cidade", "estado", "bairro",
    "preco", "avaliacao", "desconto_pct",
    "tipo_imovel", "tipo_leilao",
    "area_m2", "quartos", "banheiros", "vagas",
    "data_leilao_1", "data_leilao_2",
    "imagem_url",
]


# ── Helpers numéricos ─────────────────────────────────────────────────────────

def parse_price(text: str) -> float:
    t = re.sub(r"[^\d,]", "", text or "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def parse_int(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


# ── Classificadores ───────────────────────────────────────────────────────────

def classify_tipo(text: str) -> str:
    t = (text or "").lower()
    for kw, tipo in [
        (["apartamento","apto","ap."],           "apartamento"),
        (["casa","sobrado","chácara","chacara"],  "casa"),
        (["terreno","lote ","gleba"],             "terreno"),
        (["galpão","galpao","industrial","barracão"],"galpao"),
        (["sala","loja","escritório","comercial"],"comercial"),
        (["rural","fazenda","sítio","sitio"],     "rural"),
        (["vaga","garagem"],                      "vaga"),
    ]:
        if any(k in t for k in kw):
            return tipo
    return "outro"


def classify_leilao(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["judicial","execução","execucao","penhora"]):
        return "judicial"
    if any(k in t for k in ["banco","caixa","itaú","itau","bradesco","santander","financ"]):
        return "bancario"
    return "extrajudicial"


# ── Extrator genérico ─────────────────────────────────────────────────────────

def _extract(html: str, base: dict) -> dict:
    r = dict(base)
    txt = html  # HTML completo para busca

    # ── Preços  (R$ X.XXX,XX) — usando R[\$] conforme seção 14.3 ─────────────
    prices_raw = re.findall(r'R[\$]\s*([\d.,]{4,})', txt)
    prices = sorted({parse_price(p) for p in prices_raw if parse_price(p) > 500})
    if prices and not r.get("preco"):
        r["preco"] = prices[0]          # menor = lance mínimo
    if len(prices) > 1 and not r.get("avaliacao"):
        r["avaliacao"] = prices[-1]     # maior = avaliação

    # desconto
    dm = re.search(r'(\d+)[,.]?\d*\s*%\s*(?:de\s*)?desc', txt, re.I)
    if dm and not r.get("desconto_pct"):
        r["desconto_pct"] = float(dm.group(1))

    # ── Área m² ───────────────────────────────────────────────────────────────
    am = re.search(r'([\d.,]+)\s*m[²2]', txt, re.I)
    if am and not r.get("area_m2"):
        r["area_m2"] = parse_price(am.group(1))

    # ── Cômodos ───────────────────────────────────────────────────────────────
    qm = re.search(r'(\d+)\s*(?:quart|dorm)', txt, re.I)
    if qm and not r.get("quartos"):
        r["quartos"] = int(qm.group(1))

    bm = re.search(r'(\d+)\s*banh', txt, re.I)
    if bm and not r.get("banheiros"):
        r["banheiros"] = int(bm.group(1))

    vm = re.search(r'(\d+)\s*vaga', txt, re.I)
    if vm and not r.get("vagas"):
        r["vagas"] = int(vm.group(1))

    # ── Datas (dd/mm/yyyy) ────────────────────────────────────────────────────
    dates = list(dict.fromkeys(re.findall(r'\d{2}/\d{2}/\d{4}', txt)))
    # filtra datas de criação do HTML (anos <2024)
    dates = [d for d in dates if int(d[6:]) >= 2024]
    if dates and not r.get("data_leilao_1"):
        r["data_leilao_1"] = dates[0]
    if len(dates) > 1 and not r.get("data_leilao_2"):
        r["data_leilao_2"] = dates[1]

    # ── Tipo / modalidade ─────────────────────────────────────────────────────
    snippet = (r.get("titulo","") + " " + txt[:3000]).lower()
    if not r.get("tipo_imovel"):
        r["tipo_imovel"] = classify_tipo(snippet)
    if not r.get("tipo_leilao"):
        r["tipo_leilao"] = classify_leilao(snippet)

    # ── Bairro ────────────────────────────────────────────────────────────────
    bairro_m = re.search(
        r'(?:bairro|district)[:\s]+([A-ZÀ-Ú][a-zà-ú\s]+?)(?=[,<\n])',
        txt, re.I
    )
    if bairro_m and not r.get("bairro"):
        r["bairro"] = bairro_m.group(1).strip()[:100]

    # ── Descrição curta (primeiros 300 chars da descrição) ───────────────────
    desc_m = re.search(
        r'(?:descri[çc][aã]o|sobre\s+o\s+im[oó]vel)[^>]*>([^<]{30,})',
        txt, re.I | re.S
    )
    if desc_m and not r.get("descricao"):
        r["descricao"] = re.sub(r'\s+', ' ', desc_m.group(1)).strip()[:300]

    # ── Imagem principal ──────────────────────────────────────────────────────
    img_m = re.search(
        r'<(?:img|meta[^>]+?og:image)[^>]+?(?:src|content)=["\']'
        r'(https?://[^"\']+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']',
        txt, re.I
    )
    if img_m and not r.get("imagem_url"):
        r["imagem_url"] = img_m.group(1)

    # ── Galeria completa + anexos (Parte VII do master) ───────────────────────
    base_url = r.get("url", "")
    galeria = sc.extrair_galeria(html, base_url)
    if galeria:
        r["imagens"] = galeria                       # lista completa (persistida em 1→N)
        if not r.get("imagem_url"):
            r["imagem_url"] = galeria[0]             # capa = 1ª da galeria
    anexos = sc.extrair_anexos(html, base_url)
    if anexos:
        r["anexos"] = anexos

    # ── UF: deduz do texto quando ausente, validando contra IBGE ──────────────
    if not (r.get("estado") or "").strip():
        uf = sc.inferir_uf(r.get("titulo"), r.get("bairro"), r.get("cidade"), txt[:4000])
        if uf:
            r["estado"] = uf

    return r


# ── Extratores específicos por fonte ──────────────────────────────────────────

def _extract_megaleiloes(html: str, base: dict) -> dict:
    r = dict(base)
    # Padrões específicos do Mega Leilões
    pm = re.search(r'Lance\s+M[íi]nimo.*?R[\$]\s*([\d.,]+)', html, re.I|re.S)
    if pm: r["preco"] = parse_price(pm.group(1))
    av = re.search(r'Avalia[çc][aã]o.*?R[\$]\s*([\d.,]+)', html, re.I|re.S)
    if av: r["avaliacao"] = parse_price(av.group(1))
    return _extract(html, r)


def _extract_grupolance(html: str, base: dict) -> dict:
    r = dict(base)
    pm = re.search(r'Lance\s+M[íi]nimo.*?R[\$]\s*([\d.,]+)', html, re.I|re.S)
    if pm: r["preco"] = parse_price(pm.group(1))
    av = re.search(r'Avalia[çc][aã]o.*?R[\$]\s*([\d.,]+)', html, re.I|re.S)
    if av: r["avaliacao"] = parse_price(av.group(1))
    return _extract(html, r)


def _extract_sold(html: str, base: dict) -> dict:
    r = dict(base)
    # Sold tem estrutura bem definida
    pm = re.search(r'Lance\s+m[íi]nimo.*?R[\$]\s*([\d.,]+)', html, re.I|re.S)
    if pm: r["preco"] = parse_price(pm.group(1))
    return _extract(html, r)


EXTRACTORS = {
    "Mega Leilões":   _extract_megaleiloes,
    "Grupo Lance":    _extract_grupolance,
    "Sold Leilões":   _extract_sold,
    "Portal Zuk":     _extract,
    "Franco Leilões": _extract,
    "Frazão Leilões": _extract,
    "Milan Leilões":  _extract,
}


# ── FlareSolverr (Milan) ──────────────────────────────────────────────────────

def _fs_post(cmd, **kw):
    return requests.post(FLARESOLVERR, json={"cmd": cmd, **kw}, timeout=120).json()


def scrape_milan_detail(url: str, sid: str, base: dict) -> dict:
    sol = _fs_post("request.get", url=url, session=sid, maxTimeout=60000)
    html = sol.get("solution", {}).get("response", "")
    return _extract(html, base)


# ── Progress / CSV ────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_urls": [], "rows": []}


def save_progress(prog: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False)


def save_csv(rows: list):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_input() -> list[dict]:
    with open(INPUT_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def make_base(row: dict) -> dict:
    return {
        "fonte":        row.get("leiloeiro", ""),
        "url":          row.get("url", ""),
        "titulo":       row.get("titulo", ""),
        "descricao":    "",
        "cidade":       row.get("cidade", ""),
        "estado":       row.get("estado", ""),
        "bairro":       "",
        "preco":        float(row.get("preco", 0) or 0),
        "avaliacao":    float(row.get("avaliacao", 0) or 0),
        "desconto_pct": float(row.get("desconto_pct", 0) or 0),
        "tipo_imovel":  "",
        "tipo_leilao":  "",
        "area_m2":      0,
        "quartos":      0,
        "banheiros":    0,
        "vagas":        0,
        "data_leilao_1": "",
        "data_leilao_2": "",
        "imagem_url":   "",
    }


# ── Playwright worker ─────────────────────────────────────────────────────────

async def visit(sem, page, url: str, base: dict) -> dict:
    async with sem:
        html, status = "", None
        try:
            resp = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            status = resp.status if resp else None
            await asyncio.sleep(1.5)
            html = await page.content()
        except Exception as e:
            print(f"    ERRO {url[:60]}: {e}")
        # Fallback FlareSolverr quando o headless é bloqueado (403/challenge) — Parte VI.2.
        if sc.parece_bloqueio(html, status):
            fs = await asyncio.to_thread(sc.fetch_flaresolverr, url)
            if fs and not sc.parece_bloqueio(fs):
                print(f"    ↻ FlareSolverr resolveu bloqueio: {url[:55]}")
                html = fs
        if not html:
            return base
        fn = EXTRACTORS.get(base["fonte"], _extract)
        return fn(html, base)


def load_input_sem_foto(db: str = DB_PATH) -> list[dict]:
    """Monta a lista de trabalho a partir dos imóveis SEM foto (nem em imoveis.imagem,
    nem em imovel_imagens). Usado por --reprocessar-sem-foto para atacar direto a
    lacuna de imagem, revisitando só esses lotes para capturar a galeria 1→N."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    com_galeria = set()
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='imovel_imagens'").fetchone():
        com_galeria = {r[0] for r in con.execute("SELECT DISTINCT imovel_id FROM imovel_imagens")}
    rows = []
    for r in con.execute("SELECT id, leiloeiro, url, titulo, cidade, uf, lance_inicial, "
                         "avaliacao FROM imoveis WHERE url IS NOT NULL AND TRIM(url) <> '' "
                         "AND (imagem IS NULL OR TRIM(imagem) = '')"):
        if r["id"] in com_galeria:
            continue  # já tem galeria persistida
        rows.append({"leiloeiro": r["leiloeiro"] or "", "url": r["url"],
                     "titulo": r["titulo"] or "", "cidade": r["cidade"] or "",
                     "estado": r["uf"] or "", "preco": r["lance_inicial"] or 0,
                     "avaliacao": r["avaliacao"] or 0})
    con.close()
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(reset: bool, limite: int | None, sem_foto: bool = False):
    rows_in = load_input_sem_foto() if sem_foto else load_input()
    if sem_foto:
        print(f"Modo --reprocessar-sem-foto: {len(rows_in)} imóveis sem foto a revisitar.")
    if limite:
        rows_in = rows_in[:limite]

    if reset and Path(PROGRESS_FILE).exists():
        Path(PROGRESS_FILE).unlink()

    prog      = load_progress()
    done_urls = set(prog["done_urls"])
    rows_out  = prog["rows"]

    total = len(rows_in)
    print(f"Total URLs: {total} | Já processados: {len(done_urls)}")

    # ── Central Sul: sem visita, dados já ricos da API ────────────────────────
    central = [r for r in rows_in
               if r.get("leiloeiro") == "Central Sul de Leilões"
               and r.get("url") not in done_urls]
    for r in central:
        base = make_base(r)
        base["tipo_imovel"] = classify_tipo(base["titulo"])
        base["tipo_leilao"] = classify_leilao(base["titulo"])
        rows_out.append(base)
        done_urls.add(r["url"])
    if central:
        print(f"Central Sul: {len(central)} processados via API (sem visita)")
        save_progress({"done_urls": list(done_urls), "rows": rows_out})
        save_csv(rows_out)

    # ── Milan: FlareSolverr ───────────────────────────────────────────────────
    milan_rows = [r for r in rows_in
                  if r.get("leiloeiro") == "Milan Leilões"
                  and r.get("url") not in done_urls]

    if milan_rows:
        print(f"\nMilan Leilões: {len(milan_rows)} via FlareSolverr")
        try:
            sid = _fs_post("sessions.create")["session"]
            for r in milan_rows:
                base = make_base(r)
                result = scrape_milan_detail(r["url"], sid, base)
                rows_out.append(result)
                done_urls.add(r["url"])
                time.sleep(1)
            _fs_post("sessions.destroy", session=sid)
        except Exception as e:
            print(f"  FlareSolverr indisponível: {e} — Milan pulado")
        save_progress({"done_urls": list(done_urls), "rows": rows_out})
        save_csv(rows_out)

    # ── Playwright: demais fontes ─────────────────────────────────────────────
    playwright_rows = [r for r in rows_in
                       if r.get("leiloeiro") not in ("Central Sul de Leilões","Milan Leilões")
                       and r.get("url") not in done_urls]

    print(f"\nPlaywright: {len(playwright_rows)} páginas a visitar")
    if not playwright_rows:
        print("Nada pendente.")
        save_csv(rows_out)
        _print_summary(rows_out)
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    BATCH = 30   # salva a cada 30 páginas

    async with async_playwright() as p:
        # Resiliência de ambiente (ver scripts/session-setup.sh):
        #  PW_CHROMIUM_PATH → usa um Chromium já presente (CDN bloqueado);
        #  PW_IGNORE_HTTPS=1 → ignora cert inválido de proxy TLS de sandbox.
        import os
        launch_kw = {"headless": True, "args": ["--no-sandbox", "--disable-quic"]}
        if os.environ.get("PW_CHROMIUM_PATH"):
            launch_kw["executable_path"] = os.environ["PW_CHROMIUM_PATH"]
        browser = await p.chromium.launch(**launch_kw)
        ctx = await browser.new_context(
            user_agent=UA, locale="pt-BR",
            ignore_https_errors=os.environ.get("PW_IGNORE_HTTPS") == "1",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
        )

        # pool fixo de CONCURRENCY páginas
        pages = [await ctx.new_page() for _ in range(CONCURRENCY)]
        for pg in pages:
            pg.set_default_timeout(25000)

        processed = 0
        for i in range(0, len(playwright_rows), BATCH):
            batch = playwright_rows[i : i + BATCH]
            bases = [make_base(r) for r in batch]

            # distribui URLs pelas páginas do pool
            tasks = []
            for j, (r, base) in enumerate(zip(batch, bases)):
                pg = pages[j % CONCURRENCY]
                tasks.append(visit(sem, pg, r["url"], base))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, dict):
                    rows_out.append(res)
                    done_urls.add(res.get("url", ""))
                    processed += 1

            save_progress({"done_urls": list(done_urls), "rows": rows_out})
            save_csv(rows_out)

            pct = min(100, round((i + len(batch)) / len(playwright_rows) * 100))
            print(f"  Playwright {i+len(batch)}/{len(playwright_rows)} ({pct}%)"
                  f" | total={len(rows_out)}")

        for pg in pages:
            await pg.close()
        await ctx.close()
        await browser.close()

    _print_summary(rows_out)
    persistir_midia(rows_out)


def persistir_midia(rows: list, db: str = DB_PATH):
    """Grava galeria (imovel_imagens) e anexos (imovel_anexos) das linhas coletadas.

    Casa cada linha com imoveis.id pela url. Cria as tabelas 1→N se faltarem.
    Falhas não interrompem a coleta (a mídia é enriquecimento, não bloqueante).
    """
    com_midia = [r for r in rows if r.get("imagens") or r.get("anexos")]
    if not com_midia:
        return
    try:
        import migrar_imagens_anexos
        con = sqlite3.connect(db)
        migrar_imagens_anexos.migrar(db, backfill=False)  # garante as tabelas
        url2id = {u: i for i, u in con.execute(
            "SELECT id, url FROM imoveis WHERE url IS NOT NULL AND TRIM(url) <> ''")}
        n_img = n_anx = n_lig = 0
        for r in com_midia:
            iid = url2id.get(r.get("url"))
            if not iid:
                continue
            n_lig += 1
            n_img += sc.salvar_galeria(con, iid, r.get("imagens") or [])
            n_anx += sc.salvar_anexos(con, iid, r.get("anexos") or [])
        con.close()
        print(f"\nMídia persistida: {n_img} imagens + {n_anx} anexos "
              f"em {n_lig} imóveis (1→N).")
    except Exception as e:  # noqa: BLE001
        print(f"\n[aviso] persistência de mídia pulada: {e}")


def _print_summary(rows: list):
    from collections import Counter
    print(f"\n{'='*60}")
    print(f"CONCLUÍDO — {OUTPUT_CSV}: {len(rows)} imóveis")
    for k, v in Counter(r.get("fonte","?") for r in rows).most_common():
        has_area = sum(1 for r in rows if r.get("fonte")==k and float(r.get("area_m2",0))>0)
        print(f"  {v:4d}  {k}  (com área: {has_area})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset",  action="store_true", help="Reinicia do zero")
    ap.add_argument("--limite", type=int, default=None, help="Processa só N URLs")
    ap.add_argument("--reprocessar-sem-foto", action="store_true",
                    help="revisita só os imóveis sem foto (alimenta a galeria 1→N)")
    args = ap.parse_args()
    asyncio.run(main(reset=args.reset, limite=args.limite,
                     sem_foto=args.reprocessar_sem_foto))
