# -*- coding: utf-8 -*-
"""
corrigir_danielgarcia_pg.py — refresh dos imoveis de Daniel Garcia no PostgreSQL do site.

Problema (ver secao 49.5): os imoveis recem-raspados ja existiam no PG sob OUTRAS fontes
(notadamente a fonte antiga 'danielgarcialeiloes' com 38 registros ENCERRADO e datas de
2010-2019). Como url_original e unico globalmente, o importar_site.py (INSERT-only) NAO
atualizou esses registros stale -> sumiam da listagem (status ENCERRADO / data passada) e
todos os 87 estavam com leiloeiro_id=NULL (orfaos na visao por leiloeiro).

Correcao: UPDATE ... FROM stage casando por url_original, gravando os dados do scrape novo
(data 1a praca, status=ABERTO, valor, cidade, estado, titulo, imagem) e ligando leiloeiro_id.
Roda via 'docker exec leilao_postgres' (NUNCA localhost:5432 — banco-host sombreia o Docker).
"""
import csv, re, subprocess, tempfile, sys
from pathlib import Path
from decimal import Decimal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CSV = BASE / "csv" / "imoveis_danielgarcia_2026-06-09.csv"
CONTAINER = "leilao_postgres"
LEILOEIRO_ID = 1087  # Daniel Elias Garcia (SC, Regular) — canonico


def to_dec(v):
    try:
        return float(Decimal(str(v).replace(".", "").replace(",", "."))) if v not in (None, "") else None
    except Exception:
        return None


def to_date_iso(v):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v or ""))
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def clean(s, n=None):
    s = (s or "").replace("\x00", "").strip()
    return s[:n] if n else s


def main():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    seen, stage = set(), []
    for r in rows:
        url = clean(r.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        stage.append({
            "url_original": url,
            "titulo": clean(r.get("titulo"), 500),
            "cidade": clean(r.get("cidade"), 200),
            "estado": clean(r.get("uf"), 2),
            "valor_minimo": "" if to_dec(r.get("preco")) is None else str(to_dec(r.get("preco"))),
            "data_iso": to_date_iso(r.get("data_leilao")),
            "imagem": clean(r.get("imagem"), 1000),
        })
    print(f"[INFO] {len(stage)} URLs unicas para refresh")

    cols = list(stage[0].keys())
    tmp = Path(tempfile.gettempdir()) / "stage_dg.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(stage)
    subprocess.run(["docker", "cp", str(tmp), f"{CONTAINER}:/tmp/stage_dg.csv"], check=True)

    coldefs = ", ".join(f"{c} text" for c in cols)
    script = f"""
\\set ON_ERROR_STOP on
CREATE TEMP TABLE stage ({coldefs});
\\copy stage FROM '/tmp/stage_dg.csv' WITH (FORMAT csv, HEADER true)
UPDATE imoveis i SET
    leiloeiro_id = {LEILOEIRO_ID},
    titulo = COALESCE(NULLIF(s.titulo,''), i.titulo),
    cidade = COALESCE(NULLIF(s.cidade,''), i.cidade),
    estado = COALESCE(NULLIF(s.estado,''), i.estado),
    valor_minimo = COALESCE(NULLIF(s.valor_minimo,'')::numeric, i.valor_minimo),
    data_primeiro_leilao = COALESCE(NULLIF(s.data_iso,'')::timestamp, i.data_primeiro_leilao),
    imagem_principal = COALESCE(NULLIF(s.imagem,''), i.imagem_principal),
    status = 'ABERTO'::statusleilao,
    ativo = true,
    atualizado_em = NOW()
FROM stage s
WHERE i.url_original = s.url_original;
SELECT i.status, count(*), min(i.data_primeiro_leilao)::date, max(i.data_primeiro_leilao)::date,
       count(*) FILTER (WHERE i.leiloeiro_id IS NULL) AS sem_leiloeiro_id
FROM imoveis i JOIN stage s ON s.url_original=i.url_original
GROUP BY 1;
"""
    r = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "leilao", "-d", "leilao_db", "-f", "-"],
        input=script, capture_output=True, text=True, encoding="utf-8", timeout=180)
    print(r.stdout)
    if r.returncode != 0:
        print("[PG ERRO]", r.stderr[:1500])
        sys.exit(1)
    print("[OK] refresh concluido no PostgreSQL do site.")


if __name__ == "__main__":
    main()
