# -*- coding: utf-8 -*-
"""
Importa csv/imoveis_al_2026-06-09.csv para o PostgreSQL do SITE (container leilao_postgres).
Canal correto: docker exec (o localhost:5432 cai no Postgres do host, banco errado).
- fonte 'JUCEAL'
- staging temp + COPY + dedup GLOBAL por url_original (nao duplica o que ja esta no site)
- dedup intra-CSV por url (sites compartilhados)
"""
import csv, sys, re, hashlib, subprocess, tempfile, os
from pathlib import Path
from decimal import Decimal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CSV_FILE = BASE / "csv" / "imoveis_al_2026-06-09.csv"
CONTAINER = "leilao_postgres"
TIPOS = {"APARTAMENTO", "CASA", "TERRENO", "COMERCIAL", "RURAL", "GALPAO", "SALA", "VAGA", "OUTRO"}


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
        return str(float(Decimal(str(v).replace(".", "").replace(",", ".")))) if v else ""
    except Exception:
        return ""


def to_date(v):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v or ""))
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def clean(s, n=None):
    s = (s or "").replace("\x00", "").strip()
    return s[:n] if n else s


def psql(sql, stdin=None):
    return subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "leilao", "-d", "leilao_db", "-v", "ON_ERROR_STOP=1"]
        + (["-c", sql] if sql else ["-f", "-"]),
        input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120)


def main():
    rows = list(csv.DictReader(open(CSV_FILE, encoding="utf-8")))
    print(f"[INFO] {len(rows)} linhas no CSV")

    # monta staging deduplicado por url (intra-CSV)
    seen, stage = set(), []
    for r in rows:
        url = clean(r.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        stage.append({
            "id_externo": hashlib.md5(url.encode()).hexdigest()[:24],
            "titulo": clean(r.get("titulo"), 500),
            "descricao": clean(r.get("descricao"), 500),
            "url_original": url,
            "tipo_imovel": tipo_imovel(r.get("titulo")),
            "cidade": clean(r.get("cidade"), 200),
            "estado": clean(r.get("uf"), 2),
            "valor_minimo": to_dec(r.get("preco")),
            "data_primeiro_leilao": to_date(r.get("data_leilao")),
            "imagem_principal": clean(r.get("imagem"), 1000),
            "arquivos": clean(r.get("anexos"), 4000),
            "leiloeiro": clean(r.get("leiloeiro"), 300),
        })
    cols = list(stage[0].keys())
    print(f"[INFO] {len(stage)} imoveis unicos (apos dedup intra-CSV)")

    tmp = Path(tempfile.gettempdir()) / "stage_al.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(stage)
    subprocess.run(["docker", "cp", str(tmp), f"{CONTAINER}:/tmp/stage_al.csv"], check=True)

    coldefs = ", ".join(f"{c} text" for c in cols)
    script = f"""
\\set ON_ERROR_STOP on
INSERT INTO fontes (nome,url_base,ativo,criado_em)
  VALUES ('JUCEAL','https://www.juceal.al.gov.br/',true,NOW())
  ON CONFLICT (nome) DO NOTHING;
SELECT id AS fonte_id FROM fontes WHERE nome='JUCEAL' \\gset
CREATE TEMP TABLE stage ({coldefs});
\\copy stage FROM '/tmp/stage_al.csv' WITH (FORMAT csv, HEADER true)
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
SELECT 'JUCEAL total' AS label, count(*) FROM imoveis WHERE fonte_id=:fonte_id;
"""
    r = psql(None, stdin=script)
    print(r.stdout)
    if r.returncode != 0:
        print("[STDERR]", r.stderr[:1500])
        sys.exit(1)


if __name__ == "__main__":
    main()
