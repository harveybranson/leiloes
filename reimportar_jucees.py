"""
reimportar_jucees.py
====================
Reimporta imoveis_jucees_YYYY-MM-DD.csv para PostgreSQL via SAVEPOINT
(evita WinError 206 — SQL muito longo no Windows).
"""
import sys, io, subprocess, csv, json, re
from pathlib import Path
from datetime import datetime
from decimal import Decimal

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE    = Path(__file__).resolve().parent
CSV_DIR = BASE / "csv"
TODAY   = datetime.now().strftime("%Y-%m-%d")

def psql_run(sql: str, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        ["docker", "exec", "leilao_postgres",
         "psql", "-U", "leilao", "-d", "leilao_db",
         "--no-align", "--tuples-only", "-c", sql],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout
    )
    return proc.returncode, proc.stdout + proc.stderr


def psql_single(sql: str, timeout: int = 30) -> tuple[int, str]:
    """Executa SQL passando por arquivo dentro do container."""
    # Escreve SQL num arquivo temporário dentro do container
    # via stdin do docker exec
    proc = subprocess.run(
        ["docker", "exec", "-i", "leilao_postgres",
         "psql", "-U", "leilao", "-d", "leilao_db",
         "--no-align", "--tuples-only"],
        input=sql, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout
    )
    return proc.returncode, proc.stdout + proc.stderr


def esc(v, max_len=None) -> str:
    s = str(v or "").replace("'", "''").replace("\x00", "")
    if max_len:
        s = s[:max_len]
    return s


def to_float(v):
    try:
        return float(Decimal(str(v).replace(",", "."))) if v else None
    except Exception:
        return None


def to_int(v):
    try:
        return int(v) if v else None
    except Exception:
        return None


TIPOS_IMOVEL = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
TIPOS_LEILAO = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}


def main():
    # Encontra CSV mais recente
    csvs = sorted(CSV_DIR.glob("imoveis_jucees_*.csv"), reverse=True)
    if not csvs:
        print("[ERRO] Nenhum CSV imoveis_jucees_*.csv encontrado.")
        sys.exit(1)
    csv_path = csvs[0]
    print(f"[CSV] Usando: {csv_path}")

    # Garante fonte
    _, out = psql_single("""
INSERT INTO fontes (nome, url_base, ativo, criado_em)
VALUES ('JUCEES','https://leiloeiros.jucees.es.gov.br/',true,NOW())
ON CONFLICT (nome) DO NOTHING;
""")
    print(f"Fonte: {out.strip()[:100]}")

    _, out = psql_single("SELECT id FROM fontes WHERE nome='JUCEES' LIMIT 1;")
    fonte_id_raw = out.strip()
    if not fonte_id_raw.isdigit():
        print(f"[ERRO] fonte_id inválido: {repr(fonte_id_raw)}")
        sys.exit(1)
    FONTE_ID = int(fonte_id_raw)
    print(f"fonte_id JUCEES = {FONTE_ID}")

    # Lê CSV
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Linhas no CSV: {len(rows)}")

    ins = upd = err = 0

    for i, r in enumerate(rows):
        tipo_i = (r.get("tipo_imovel") or "outro").upper()
        if tipo_i not in TIPOS_IMOVEL:
            tipo_i = "OUTRO"
        tipo_l = (r.get("tipo_leilao") or "extrajudicial").upper()
        if tipo_l not in TIPOS_LEILAO:
            tipo_l = "EXTRAJUDICIAL"

        vmin  = to_float(r.get("valor_minimo"))
        vaval = to_float(r.get("valor_avaliacao"))
        area  = to_float(r.get("area_total"))
        qtos  = to_int(r.get("quartos"))
        d1    = r.get("data_primeiro_leilao","") or ""
        d2    = r.get("data_segundo_leilao","") or ""

        sql = f"""
SAVEPOINT sp_{i};
INSERT INTO imoveis (
    fonte_id, id_externo, titulo, descricao, url_original,
    tipo_imovel, tipo_leilao, status, categoria,
    cidade, estado, cep, endereco_completo,
    valor_minimo, valor_avaliacao, area_total, quartos,
    data_primeiro_leilao, data_segundo_leilao,
    imagem_principal, arquivos, numero_processo,
    leiloeiro, ativo, classificado, geocodificado,
    criado_em, atualizado_em
) VALUES (
    {FONTE_ID},
    '{esc(r.get("id_externo",""),200)}',
    '{esc(r.get("titulo",""),500)}',
    '{esc(r.get("descricao",""),500)}',
    '{esc(r.get("url_original",""),1000)}',
    '{tipo_i}', '{tipo_l}',
    'ABERTO', 'IMOVEL',
    '{esc(r.get("cidade",""),200)}',
    '{esc(r.get("estado","ES"),2)}',
    '{esc(r.get("cep",""),10)}',
    '{esc(r.get("endereco_completo",""),500)}',
    {vmin if vmin is not None else 'NULL'},
    {vaval if vaval is not None else 'NULL'},
    {area if area is not None else 'NULL'},
    {qtos if qtos is not None else 'NULL'},
    {f"'{d1}'" if d1 else 'NULL'},
    {f"'{d2}'" if d2 else 'NULL'},
    '{esc(r.get("imagem_principal",""),1000)}',
    '{esc(r.get("arquivos","[]"),4000)}',
    '{esc(r.get("numero_processo",""),100)}',
    '{esc(r.get("leiloeiro",""),300)}',
    true, false, false,
    NOW(), NOW()
)
ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
    titulo = EXCLUDED.titulo,
    valor_minimo = EXCLUDED.valor_minimo,
    data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
    imagem_principal = EXCLUDED.imagem_principal,
    arquivos = EXCLUDED.arquivos,
    atualizado_em = NOW();
RELEASE SAVEPOINT sp_{i};
"""

        rc, out = psql_single(sql)
        if rc == 0:
            if "INSERT 0 1" in out:
                ins += 1
            elif "UPDATE 1" in out:
                upd += 1
            else:
                upd += 1
        else:
            err += 1
            if err <= 5:
                print(f"  [ERR {i}] {out[:150]}")

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(rows)}] ins={ins} upd={upd} err={err}")

    print(f"\n[RESULTADO] {ins} inseridos, {upd} atualizados, {err} erros")
    print(f"Total processado: {ins+upd+err}/{len(rows)}")


if __name__ == "__main__":
    main()
