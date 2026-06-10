# -*- coding: utf-8 -*-
"""
consolidar_danielgarcia_leiloeiro.py — unifica as 5 duplicatas de grafia do leiloeiro
Daniel Garcia na tabela `leiloeiros` do PG do site e liga `leiloeiro_id` dos imoveis.

Autorizado pelo usuario (sessao 2026-06-09): "so ligar leiloeiro_id" + "consolidar duplicatas".
NAO altera status/data/valor dos imoveis (apenas leiloeiro_id). Roda via docker exec
(NUNCA localhost:5432 — banco-host sombreia o Docker).

Canonico = id 1087 (Daniel Elias Garcia, SC, Regular — melhor metadado).
Duplicatas repontadas e removidas: 2004, 2254, 2333, 2394.
Tambem liga os 87 imoveis recem-raspados (leiloeiro_id IS NULL) por url_original.
"""
import csv, subprocess, tempfile, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CSV = BASE / "csv" / "imoveis_danielgarcia_2026-06-09.csv"
CONTAINER = "leilao_postgres"
CANON = 1087
DUPS = [2004, 2254, 2333, 2394]


def main():
    urls = sorted({r["url"].strip() for r in csv.DictReader(open(CSV, encoding="utf-8")) if r.get("url")})
    tmp = Path(tempfile.gettempdir()) / "dg_urls.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        f.write("url\n")
        for u in urls:
            f.write(u + "\n")
    subprocess.run(["docker", "cp", str(tmp), f"{CONTAINER}:/tmp/dg_urls.csv"], check=True)

    dups = ",".join(str(d) for d in DUPS)
    script = f"""
\\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE u(url text);
\\copy u FROM '/tmp/dg_urls.csv' WITH (FORMAT csv, HEADER true)

-- 1) repontar imoveis das 4 duplicatas para o canonico
UPDATE imoveis SET leiloeiro_id={CANON}, atualizado_em=NOW()
 WHERE leiloeiro_id IN ({dups});

-- 2) ligar os imoveis recem-raspados que estavam com leiloeiro_id NULL
UPDATE imoveis i SET leiloeiro_id={CANON}, atualizado_em=NOW()
 FROM u WHERE i.url_original=u.url AND i.leiloeiro_id IS NULL;

-- 3) preservar dominios alternativos no canonico
UPDATE leiloeiros SET sites_alternativos='https://www.dgleiloes.com.br; https://dgleiloes.leilao.br',
       atualizado_em=NOW()
 WHERE id={CANON};

-- 4) remover as duplicatas (ja sem imoveis apontando)
DELETE FROM leiloeiros WHERE id IN ({dups});

-- verificacao
SELECT 'imoveis no canonico' AS chk, count(*) FROM imoveis WHERE leiloeiro_id={CANON};
SELECT 'dos 87, ainda NULL' AS chk, count(*) FROM imoveis i JOIN u ON i.url_original=u.url WHERE i.leiloeiro_id IS NULL;
SELECT 'duplicatas restantes' AS chk, count(*) FROM leiloeiros WHERE id IN ({dups});
COMMIT;
"""
    r = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "leilao", "-d", "leilao_db", "-f", "-"],
        input=script, capture_output=True, text=True, encoding="utf-8", timeout=180)
    print(r.stdout)
    if r.returncode != 0:
        print("[PG ERRO]", r.stderr[:1500])
        sys.exit(1)
    print("[OK] consolidacao concluida.")


if __name__ == "__main__":
    main()
