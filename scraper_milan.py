"""
Scraper para Milan Leilões (milanleiloes.com.br) via FlareSolverr.

Requer:
  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest

Uso:
  python scraper_milan.py                 # roda e salva em ofertas_milan.csv
  python scraper_milan.py --merge         # também mescla em ofertas_leiloeiros.csv
"""
import argparse, csv, re, sys, time
from pathlib import Path
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS_URL      = "http://localhost:8191/v1"
BASE        = "https://www.milanleiloes.com.br"
OUTPUT_FILE = "ofertas_milan.csv"
MERGE_FILE  = "ofertas_leiloeiros.csv"
LEILOEIRO   = "Milan Leilões"

FIELDNAMES  = ["leiloeiro", "url", "titulo", "cidade", "estado",
               "preco", "avaliacao", "desconto_pct", "duplicado"]


# ─────────────────────────────────────────────────────────────���────
# FlareSolverr helpers
# ──────────────────────────────────────────────────────────────────

def _fs(cmd, **kw):
    r = requests.post(FS_URL, json={"cmd": cmd, **kw}, timeout=120)
    r.raise_for_status()
    return r.json()

def _get(url, sid, max_timeout=60000):
    return _fs("request.get", url=url, session=sid,
               maxTimeout=max_timeout).get("solution", {})

def is_cf(html):
    l = html.lower()
    return "just a moment" in l or "cf_chl_f_tk" in l

def wait_flaresolverr():
    """Verifica se FlareSolverr está disponível; aguarda até 30s."""
    for _ in range(30):
        try:
            r = requests.get(f"{FS_URL.rsplit('/v1',1)[0]}/", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ──────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────

def extract_leilao_imoveis_ids(html):
    """Retorna IDs únicos de leilões de imóveis do Milan."""
    return list(dict.fromkeys(re.findall(r'/leilao/imoveis/(\d+)', html)))

def extract_lote_links(html, lid):
    """Extrai todos os links /leilao/{lid}/lote/{num} do HTML do leilão."""
    pattern = rf'/leilao/{lid}/lote/(\w+)'
    nums = list(dict.fromkeys(re.findall(pattern, html)))
    return [f"{BASE}/leilao/{lid}/lote/{n}" for n in nums]

def extract_lote_info(html, url):
    """Extrai título, cidade/estado e preço de um card de lote."""
    titulo, cidade, estado, preco = "", "", "", 0.0

    # Título — usa og:title ou o primeiro h1/h2
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if not m:
        m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        titulo = m.group(1).strip()
        # Remove prefixo padrão "Milan Leilões | " se presente
        titulo = re.sub(r'^Milan\s+Leil[oõ]es\s*[|–-]\s*', '', titulo, flags=re.I).strip()

    # Cidade/Estado — padrão "CIDADE - UF" no título
    cs = re.search(r'^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇa-záéíóúâêôãõç\s]+)\s*[-–]\s*([A-Z]{2})\s*[-–]', titulo)
    if cs:
        cidade = cs.group(1).strip().title()
        estado = cs.group(2).upper()

    # Preço — LANCE INICIAL ou lanceMinimo
    pm = re.search(
        r'(?:LANCE\s+INICIAL|lanceMinimo)[^R]*R[\$]\s*([\d.,]+)',
        html, re.I
    )
    if not pm:
        pm = re.search(r'R[\$]\s*([\d.,]+)', html)
    if pm:
        raw = pm.group(1).replace(".", "").replace(",", ".")
        try:
            preco = float(raw)
        except ValueError:
            pass

    return {"leiloeiro": LEILOEIRO, "url": url, "titulo": titulo,
            "cidade": cidade, "estado": estado, "preco": preco,
            "avaliacao": 0.0, "desconto_pct": 0.0, "duplicado": False}


# ──────────────────────────────────────────────────────────────────
# Scraper principal
# ──────────────────────────────────────────────────────────────────

def scrape_milan():
    if not wait_flaresolverr():
        print("ERRO: FlareSolverr não está disponível em http://localhost:8191")
        print("Inicie com: docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return []

    sid = _fs("sessions.create")["session"]
    print(f"Sessão FlareSolverr: {sid}")

    rows = []
    seen = set()

    def add(url, info=None):
        if url and url not in seen:
            seen.add(url)
            rows.append(info or {"leiloeiro": LEILOEIRO, "url": url,
                                  "titulo": "", "cidade": "", "estado": "",
                                  "preco": 0.0, "avaliacao": 0.0,
                                  "desconto_pct": 0.0, "duplicado": False})

    try:
        # ── 1. Coletar IDs de leilões da home ──────────────────────
        print(f"\n[1/3] Home page: {BASE}")
        sol_home = _get(BASE, sid)
        h_home   = sol_home.get("response", "")
        if is_cf(h_home):
            print("  ERRO: Home CF-blocked")
            return []

        leilao_ids = extract_leilao_imoveis_ids(h_home)
        print(f"  Leilões de imóveis na home: {leilao_ids}")

        # ── 2. Tentar /imoveis para pegar mais leilões ─────────────
        print(f"\n[2/3] Listagem: {BASE}/imoveis")
        sol_imoveis = _get(f"{BASE}/imoveis", sid, max_timeout=90000)
        h_imoveis   = sol_imoveis.get("response", "")
        extra_ids   = extract_leilao_imoveis_ids(h_imoveis)
        for eid in extra_ids:
            if eid not in leilao_ids:
                leilao_ids.append(eid)
        print(f"  IDs totais após /imoveis: {leilao_ids}")

        # ── 3. Para cada leilão, coletar lotes ─────────────────────
        print(f"\n[3/3] Scraping {len(leilao_ids)} leilões...")
        for lid in leilao_ids:
            lurl = f"{BASE}/leilao/imoveis/{lid}"
            print(f"\n  Leilão {lid}: {lurl}")
            sol_l = _get(lurl, sid)
            h_l   = sol_l.get("response", "")

            if is_cf(h_l):
                print(f"    AVISO: CF-blocked, pulando")
                continue

            lote_urls = extract_lote_links(h_l, lid)
            # Retry se a página renderizou sem lotes (SPA lazy render)
            if not lote_urls:
                import time; time.sleep(3)
                sol_l2 = _get(lurl, sid, max_timeout=90000)
                h_l    = sol_l2.get("response", "")
                lote_urls = extract_lote_links(h_l, lid)
                if lote_urls:
                    print(f"    (retry OK)")
            print(f"    {len(lote_urls)} lotes encontrados")

            for lote_url in lote_urls:
                # Extrair info básica do card (o HTML do leilão contém os cards)
                # Pega o bloco HTML do card específico
                lote_num = lote_url.split("/lote/")[-1]
                card_pattern = (
                    rf'href=["\']?{re.escape(f"/leilao/{lid}/lote/{lote_num}")}["\']?'
                    r'.{0,4000}'
                )
                card_match = re.search(card_pattern, h_l, re.S)
                card_html  = card_match.group(0) if card_match else h_l

                info = {
                    "leiloeiro": LEILOEIRO,
                    "url": lote_url,
                    "titulo": "",
                    "cidade": "",
                    "estado": "",
                    "preco": 0.0,
                    "avaliacao": 0.0,
                    "desconto_pct": 0.0,
                    "duplicado": False,
                }

                # Título do card
                t_m = re.search(r'card_lote_titulo[^>]*>([^<]+)', card_html, re.S)
                if t_m:
                    info["titulo"] = t_m.group(1).strip()
                    # "Serra Dourada – BA." ou "SÃO GONÇALO - RJ - ..."
                    cs = re.search(
                        r'^(.+?)\s*[–\-]\s*([A-Z]{2})[.\s–\-]',
                        info["titulo"]
                    )
                    if cs:
                        info["cidade"] = cs.group(1).strip().title()
                        info["estado"] = cs.group(2).upper()

                # Preço do card — <p id="card_lote_lanceMinimo..."><span>LANCE MÍNIMO/INICIAL:</span> R$ X
                p_m = re.search(r'lanceMinimo[^>]*>.*?R[\$]\s*([\d.,]+)', card_html, re.I | re.S)
                if p_m:
                    raw = p_m.group(1).replace(".", "").replace(",", ".")
                    try:
                        info["preco"] = float(raw)
                    except ValueError:
                        pass

                add(lote_url, info)
                print(f"    + lote/{lote_num}: {info['titulo'][:50]} | R${info['preco']:,.0f}")

    finally:
        _fs("sessions.destroy", session=sid)
        print(f"\nSessão {sid} destruída.")

    return rows


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Salvo: {path} ({len(rows)} ofertas)")


def merge_into(rows, merge_path):
    existing = []
    existing_urls = set()
    if Path(merge_path).exists():
        with open(merge_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.append(r)
                existing_urls.add(r.get("url", ""))

    added = 0
    for row in rows:
        if row["url"] not in existing_urls:
            row["duplicado"] = False
            existing.append(row)
            existing_urls.add(row["url"])
            added += 1
        else:
            # Atualiza duplicado flag para registros Milan já presentes
            for er in existing:
                if er.get("url") == row["url"]:
                    er["leiloeiro"] = LEILOEIRO
                    break

    seen, deduped = set(), []
    for r in existing:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u); deduped.append(r)

    # Garantir todos os fieldnames no merge
    all_fields = list(dict.fromkeys(FIELDNAMES + [k for k in (deduped[0].keys() if deduped else [])]))
    with open(merge_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)

    print(f"Mesclado em {merge_path}: +{added} novos | total={len(deduped)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", action="store_true",
                    help=f"Mesclar resultados em {MERGE_FILE}")
    args = ap.parse_args()

    rows = scrape_milan()
    if not rows:
        print("Nenhuma oferta coletada.")
        return

    save_csv(rows, OUTPUT_FILE)

    if args.merge:
        merge_into(rows, MERGE_FILE)

    print(f"\n=== RESUMO ===")
    print(f"Total Milan Leilões: {len(rows)} lotes")
    from collections import Counter
    estados = Counter(r["estado"] for r in rows if r["estado"])
    for uf, n in estados.most_common():
        print(f"  {n:3d}  {uf}")


if __name__ == "__main__":
    main()
