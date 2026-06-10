# -*- coding: utf-8 -*-
"""
importar_site.py — importador GENÉRICO para AMBOS os bancos.
=============================================================
Grava um CSV de imóveis (formato simples dos scrapers standalone deste diretório)
em:
  1) SQLite local  imoveis_leiloeiros.db  (fonte de verdade dos scrapers standalone)
  2) PostgreSQL do SITE, DENTRO do container leilao_postgres (o que a listagem lê)

⚠️ Por que docker exec e não localhost:5432:
   a máquina tem um PostgreSQL do HOST escutando em localhost:5432 que "sombreia"
   o mapeamento do Docker. Conectar via psycopg2 localhost cai no banco ERRADO.
   O único canal correto para o banco do site é:
       docker exec leilao_postgres psql -U leilao -d leilao_db

Dedup:
  - SQLite: por url (PK = id derivado da url).
  - PostgreSQL: GLOBAL por url_original (não duplica o que já está no site) +
    intra-CSV por url + ON CONFLICT (fonte_id, id_externo) DO NOTHING.

Uso:
    python importar_site.py --csv csv/imoveis_al_2026-06-09.csv \
        --fonte JUCEAL --url-base https://www.juceal.al.gov.br/ \
        --junta "JUCEAL/AL" --estado-padrao AL

Colunas esperadas no CSV (as que faltarem são ignoradas):
    leiloeiro, junta, site, titulo, descricao, cidade, uf, preco,
    data_leilao (dd/mm/aaaa), imagem, anexos, url
"""
import csv, sys, re, hashlib, subprocess, tempfile, sqlite3, argparse
from pathlib import Path
from datetime import datetime
from decimal import Decimal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
DB_SQLITE = BASE / "imoveis_leiloeiros.db"
CONTAINER = "leilao_postgres"

# enums REAIS do schema PG (nomes dos tipos != nomes das colunas)
TIPOS_IMOVEL = {"APARTAMENTO", "CASA", "TERRENO", "COMERCIAL", "RURAL", "GALPAO", "SALA", "VAGA", "OUTRO"}


def tipo_imovel(t):
    t = (t or "").lower()
    if any(k in t for k in ["apartament", "apto", "flat", "kitnet", "quitinete", "cobertura"]):
        return "APARTAMENTO"
    if any(k in t for k in ["casa", "sobrado", "residenc"]):
        return "CASA"
    if any(k in t for k in ["fazenda", "sitio", "sítio", "chacara", "chácara", "rural", "gleba"]):
        return "RURAL"
    if any(k in t for k in ["galp", "barrac"]):
        return "GALPAO"
    if any(k in t for k in ["sala", "loja", "comercial", "predio", "prédio", "edific"]):
        return "COMERCIAL"
    if any(k in t for k in ["terreno", "lote", "area", "área"]):
        return "TERRENO"
    if any(k in t for k in ["vaga", "garagem", "box"]):
        return "VAGA"
    return "OUTRO"


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


# ─────────────────────────────────────────────────────────── SQLite ──
def import_sqlite(rows, junta):
    conn = sqlite3.connect(DB_SQLITE)
    conn.execute("""CREATE TABLE IF NOT EXISTS imoveis (
        id TEXT PRIMARY KEY, leiloeiro TEXT, junta TEXT, site TEXT,
        titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
        lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
        url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT)""")
    cur = conn.cursor()
    novos = existe = 0
    agora = datetime.now().isoformat(timespec="seconds")
    for r in rows:
        url = clean(r.get("url"))
        if url and cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (url,)).fetchone():
            existe += 1
            continue
        rid = hashlib.md5((url or r.get("titulo", "")).encode("utf-8")).hexdigest()[:16]
        cur.execute("INSERT OR IGNORE INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, clean(r.get("leiloeiro")), r.get("junta") or junta, clean(r.get("site")),
                     clean(r.get("titulo"), 500), clean(r.get("descricao"), 300), "",
                     clean(r.get("cidade")), clean(r.get("uf")),
                     to_dec(r.get("preco")), None, clean(r.get("data_leilao")),
                     url, "imovel", clean(r.get("imagem")), agora))
        novos += cur.rowcount
    conn.commit()
    conn.close()
    return novos, existe


# ─────────────────────────────────────────────────── PostgreSQL site ──
def psql_stdin(script):
    return subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "leilao", "-d", "leilao_db", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=script, capture_output=True, text=True, encoding="utf-8", timeout=180)


def import_postgres(rows, fonte, url_base, estado_padrao):
    seen, stage = set(), []
    for r in rows:
        url = clean(r.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        vmin = to_dec(r.get("preco"))
        stage.append({
            "id_externo": hashlib.md5(url.encode()).hexdigest()[:24],
            "titulo": clean(r.get("titulo"), 500),
            "descricao": clean(r.get("descricao"), 500),
            "url_original": url,
            "tipo_imovel": tipo_imovel(r.get("titulo")),
            "cidade": clean(r.get("cidade"), 200),
            "estado": clean(r.get("uf"), 2) or estado_padrao,
            "valor_minimo": "" if vmin is None else str(vmin),
            "data_primeiro_leilao": to_date_iso(r.get("data_leilao")),
            "imagem_principal": clean(r.get("imagem"), 1000),
            "arquivos": clean(r.get("anexos"), 4000),
            "leiloeiro": clean(r.get("leiloeiro"), 300),
        })
    if not stage:
        return 0, 0
    cols = list(stage[0].keys())
    tmp = Path(tempfile.gettempdir()) / "stage_site.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(stage)
    subprocess.run(["docker", "cp", str(tmp), f"{CONTAINER}:/tmp/stage_site.csv"], check=True)

    coldefs = ", ".join(f"{c} text" for c in cols)
    fonte_esc = fonte.replace("'", "''")
    url_esc = (url_base or "").replace("'", "''")
    script = f"""
\\set ON_ERROR_STOP on
INSERT INTO fontes (nome,url_base,ativo,criado_em)
  VALUES ('{fonte_esc}','{url_esc}',true,NOW()) ON CONFLICT (nome) DO NOTHING;
SELECT id AS fonte_id FROM fontes WHERE nome='{fonte_esc}' \\gset
CREATE TEMP TABLE stage ({coldefs});
\\copy stage FROM '/tmp/stage_site.csv' WITH (FORMAT csv, HEADER true)
WITH dedup AS (SELECT DISTINCT ON (url_original) * FROM stage ORDER BY url_original)
INSERT INTO imoveis (
    fonte_id,id_externo,titulo,descricao,url_original,
    tipo_imovel,tipo_leilao,status,categoria,
    cidade,estado,endereco_completo,
    valor_minimo,data_primeiro_leilao,imagem_principal,arquivos,leiloeiro,
    ativo,classificado,geocodificado,criado_em,atualizado_em)
SELECT :fonte_id, d.id_externo, d.titulo, d.descricao, d.url_original,
    d.tipo_imovel::tipoimovel, 'EXTRAJUDICIAL'::tipoleilao, 'ABERTO'::statusleilao, 'IMOVEL'::categoriaitem,
    d.cidade, d.estado, d.descricao,
    NULLIF(d.valor_minimo,'')::numeric, NULLIF(d.data_primeiro_leilao,'')::timestamp,
    d.imagem_principal, d.arquivos, d.leiloeiro,
    true,false,false,NOW(),NOW()
FROM dedup d
WHERE NOT EXISTS (SELECT 1 FROM imoveis i WHERE i.url_original = d.url_original)
ON CONFLICT (fonte_id,id_externo) DO NOTHING;
SELECT count(*) AS total_fonte FROM imoveis WHERE fonte_id=:fonte_id;
"""
    r = psql_stdin(script)
    if r.returncode != 0:
        print("[PG ERRO]", r.stderr[:1500])
        sys.exit(1)
    # extrai INSERT n e total
    ins = 0
    m = re.search(r"INSERT \d+ (\d+)", r.stdout)
    if m:
        ins = int(m.group(1))
    mt = re.search(r"total_fonte\s*-+\s*(\d+)", r.stdout) or re.search(r"\n\s*(\d+)\s*\n\(1 row\)", r.stdout)
    total = int(mt.group(1)) if mt else None
    return ins, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--fonte", required=True, help="nome da fonte no PG, ex.: JUCEAL")
    ap.add_argument("--url-base", default="")
    ap.add_argument("--junta", default="", help="rótulo junta no SQLite, ex.: JUCEAL/AL")
    ap.add_argument("--estado-padrao", default="")
    a = ap.parse_args()

    csv_path = Path(a.csv)
    if not csv_path.is_absolute():
        csv_path = BASE / a.csv
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"[INFO] {len(rows)} linhas | {csv_path.name}")

    n_sq, e_sq = import_sqlite(rows, a.junta)
    print(f"[SQLite]   novos={n_sq} | ja_existiam={e_sq}")

    ins_pg, tot_pg = import_postgres(rows, a.fonte, a.url_base, a.estado_padrao)
    print(f"[Postgres] inseridos={ins_pg} | total na fonte {a.fonte}={tot_pg}")
    print("[OK] gravado em AMBOS os bancos.")


if __name__ == "__main__":
    main()
