"""
Importa CSV imoveis_jucepar_*.csv para PostgreSQL via psycopg2 (sem subprocess/psql).
Corrige WinError 206 (linha de comando muito longa).
"""
import csv, sys, re
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Instalando psycopg2-binary...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "psycopg2-binary"], check=True)
    import psycopg2
    import psycopg2.extras

DSN = "postgresql://leilao:leilao123@localhost:5432/leilao_db"
BASE = Path(__file__).parent
CSV_DIR = BASE / "csv"

TIPOS_IMOVEL = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
TIPOS_LEILAO = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

def to_float(v):
    if not v: return None
    try: return float(Decimal(str(v).replace(",",".")))
    except: return None

def to_int(v):
    if not v: return None
    try: return int(v)
    except: return None

def to_date(v):
    if not v: return None
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try: return datetime.strptime(v, fmt).date()
        except: pass
    return None

def trunc(v, n):
    return str(v or "").replace("\x00", "")[:n] or None

def main():
    csvs = sorted(CSV_DIR.glob("imoveis_jucepar_*.csv"), reverse=True)
    if not csvs:
        print("Nenhum CSV imoveis_jucepar_*.csv encontrado."); return
    csv_path = csvs[0]
    print(f"Lendo: {csv_path.name}")

    conn = psycopg2.connect(DSN)
    cur = conn.cursor()

    # Garante fonte JUCEPAR
    cur.execute("""
        INSERT INTO fontes (nome, url_base, ativo, criado_em)
        VALUES ('JUCEPAR','https://www.jucepar.pr.gov.br/',true,NOW())
        ON CONFLICT (nome) DO NOTHING
    """)
    conn.commit()
    cur.execute("SELECT id FROM fontes WHERE nome='JUCEPAR' LIMIT 1")
    fonte_id = cur.fetchone()[0]
    print(f"fonte_id JUCEPAR = {fonte_id}")

    INSERT_SQL = """
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
            %(fonte_id)s, %(id_externo)s, %(titulo)s, %(descricao)s, %(url_original)s,
            %(tipo_imovel)s, %(tipo_leilao)s, 'ABERTO', 'IMOVEL',
            %(cidade)s, %(estado)s, %(cep)s, %(endereco_completo)s,
            %(valor_minimo)s, %(valor_avaliacao)s, %(area_total)s, %(quartos)s,
            %(data_primeiro_leilao)s, %(data_segundo_leilao)s,
            %(imagem_principal)s, %(arquivos)s, %(numero_processo)s,
            %(leiloeiro)s, true, false, false,
            NOW(), NOW()
        )
        ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
            titulo              = EXCLUDED.titulo,
            valor_minimo        = EXCLUDED.valor_minimo,
            data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
            imagem_principal    = EXCLUDED.imagem_principal,
            arquivos            = EXCLUDED.arquivos,
            atualizado_em       = NOW()
    """

    ins = upd = err = 0
    batch = []
    BATCH_SIZE = 200

    def flush(batch):
        nonlocal ins, upd, err
        for row in batch:
            try:
                cur.execute(INSERT_SQL, row)
                if cur.statusmessage.startswith("INSERT"):
                    ins += 1
                else:
                    upd += 1
            except Exception as e:
                err += 1
                conn.rollback()
                print(f"  [ERR] {e} — id={row.get('id_externo','?')[:40]}")
                return
        conn.commit()

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            ti = (row.get("tipo_imovel") or "outro").upper()
            if ti not in TIPOS_IMOVEL: ti = "OUTRO"
            tl = (row.get("tipo_leilao") or "extrajudicial").upper()
            if tl not in TIPOS_LEILAO: tl = "EXTRAJUDICIAL"

            record = {
                "fonte_id":             fonte_id,
                "id_externo":           trunc(row.get("id_externo"), 200),
                "titulo":               trunc(row.get("titulo"), 500),
                "descricao":            trunc(row.get("descricao"), 2000),
                "url_original":         trunc(row.get("url_original"), 1000),
                "tipo_imovel":          ti,
                "tipo_leilao":          tl,
                "cidade":               trunc(row.get("cidade"), 200),
                "estado":               trunc(row.get("estado","PR"), 2),
                "cep":                  trunc(row.get("cep"), 10),
                "endereco_completo":    trunc(row.get("endereco_completo"), 500),
                "valor_minimo":         to_float(row.get("valor_minimo")),
                "valor_avaliacao":      to_float(row.get("valor_avaliacao")),
                "area_total":           to_float(row.get("area_total")),
                "quartos":              to_int(row.get("quartos")),
                "data_primeiro_leilao": to_date(row.get("data_primeiro_leilao")),
                "data_segundo_leilao":  to_date(row.get("data_segundo_leilao")),
                "imagem_principal":     trunc(row.get("imagem_principal"), 1000),
                "arquivos":             trunc(row.get("arquivos"), 4000) or "[]",
                "numero_processo":      trunc(row.get("numero_processo"), 100),
                "leiloeiro":            trunc(row.get("leiloeiro"), 300),
            }
            batch.append(record)

            if len(batch) >= BATCH_SIZE:
                flush(batch)
                batch = []
                print(f"  {i} linhas processadas — ins={ins} upd={upd} err={err}")

    if batch:
        flush(batch)

    cur.close()
    conn.close()
    print(f"\nConcluído: {ins} inseridos, {upd} atualizados, {err} erros")
    print(f"Total processado: {ins+upd+err} de {i} linhas")

if __name__ == "__main__":
    main()
