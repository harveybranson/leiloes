"""
Gera um arquivo SQL com INSERTs para importar o CSV JUCEPAR para o PostgreSQL do Docker.
O SQL é copiado para o container via docker cp e executado com psql -f (sem limite de linha).
"""
import csv, sys, re
from pathlib import Path
from decimal import Decimal
from datetime import datetime

BASE = Path(__file__).parent
CSV_DIR = BASE / "csv"

TIPOS_IMOVEL = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
TIPOS_LEILAO = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

def esc(v, max_len=None):
    s = str(v or "").replace("\x00","").replace("'","''")
    if max_len: s = s[:max_len]
    return s

def to_float_sql(v):
    if not v: return "NULL"
    try: return str(float(Decimal(str(v).replace(",","."))))
    except: return "NULL"

def to_int_sql(v):
    if not v: return "NULL"
    try: return str(int(v))
    except: return "NULL"

def to_date_sql(v):
    if not v: return "NULL"
    v = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(v, fmt)
            return f"'{dt.strftime('%Y-%m-%d')}'"
        except: pass
    return "NULL"

def main():
    csvs = sorted(CSV_DIR.glob("imoveis_jucepar_*.csv"), reverse=True)
    if not csvs:
        print("Nenhum CSV encontrado"); return
    csv_path = csvs[0]
    out_path = BASE / "import_jucepar.sql"

    print(f"Lendo: {csv_path.name}")
    linhas = 0

    with open(out_path, "w", encoding="utf-8") as out:
        out.write("BEGIN;\n\n")
        out.write("""INSERT INTO fontes (nome, url_base, ativo, criado_em)
VALUES ('JUCEPAR','https://www.jucepar.pr.gov.br/',true,NOW())
ON CONFLICT (nome) DO NOTHING;\n\n""")

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ti = (row.get("tipo_imovel") or "outro").upper()
                if ti not in TIPOS_IMOVEL: ti = "OUTRO"
                tl = (row.get("tipo_leilao") or "extrajudicial").upper()
                if tl not in TIPOS_LEILAO: tl = "EXTRAJUDICIAL"

                out.write(f"""INSERT INTO imoveis (
    fonte_id, id_externo, titulo, descricao, url_original,
    tipo_imovel, tipo_leilao, status, categoria,
    cidade, estado, cep, endereco_completo,
    valor_minimo, valor_avaliacao, area_total, quartos,
    data_primeiro_leilao, data_segundo_leilao,
    imagem_principal, arquivos, numero_processo,
    leiloeiro, ativo, classificado, geocodificado, criado_em, atualizado_em
) VALUES (
    (SELECT id FROM fontes WHERE nome='JUCEPAR' LIMIT 1),
    '{esc(row.get("id_externo"),200)}',
    '{esc(row.get("titulo"),500)}',
    '{esc(row.get("descricao"),2000)}',
    '{esc(row.get("url_original"),1000)}',
    '{ti}','{tl}','ABERTO','IMOVEL',
    '{esc(row.get("cidade"),200)}',
    '{esc(row.get("estado","PR"),2)}',
    '{esc(row.get("cep"),10)}',
    '{esc(row.get("endereco_completo"),500)}',
    {to_float_sql(row.get("valor_minimo"))},
    {to_float_sql(row.get("valor_avaliacao"))},
    {to_float_sql(row.get("area_total"))},
    {to_int_sql(row.get("quartos"))},
    {to_date_sql(row.get("data_primeiro_leilao"))},
    {to_date_sql(row.get("data_segundo_leilao"))},
    '{esc(row.get("imagem_principal"),1000)}',
    '{esc(row.get("arquivos","[]"),4000)}',
    '{esc(row.get("numero_processo"),100)}',
    '{esc(row.get("leiloeiro"),300)}',
    true,false,false,NOW(),NOW()
) ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
    titulo=EXCLUDED.titulo,
    valor_minimo=EXCLUDED.valor_minimo,
    data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
    imagem_principal=EXCLUDED.imagem_principal,
    arquivos=EXCLUDED.arquivos,
    atualizado_em=NOW();\n""")
                linhas += 1

        out.write("\nCOMMIT;\n")

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"SQL gerado: {out_path.name} ({linhas} registros, {size_mb:.1f} MB)")

if __name__ == "__main__":
    main()
