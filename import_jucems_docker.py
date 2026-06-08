"""
Script para executar DENTRO do container Docker.
Copia este arquivo e o CSV para o container, depois executa.
"""
import csv, os, sys, re
from pathlib import Path
from datetime import datetime
from decimal import Decimal

CSV_FILE = "/tmp/imoveis_jucems.csv"

# Lê CSV
rows = []
with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
print(f"[INFO] {len(rows)} registros para importar")

def _dec(v):
    try: return float(Decimal(str(v).replace(",","."))) if v else None
    except: return None

# Conexão PostgreSQL — usa host da rede Docker
import psycopg2
db_url = os.environ.get("DATABASE_URL_SYNC", "postgresql://leilao:leilao123@postgres:5432/leilao_db")
# Remove prefixo asyncpg se presente
db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
print(f"  Conectando: {db_url[:50]}...")
conn = psycopg2.connect(db_url)

cur = conn.cursor()

# Garante fonte
cur.execute("""
    INSERT INTO fontes (nome, url_base, ativo, criado_em)
    VALUES ('JUCEMS','https://www.jucems.ms.gov.br/',true,NOW())
    ON CONFLICT (nome) DO NOTHING
""")
cur.execute("SELECT id FROM fontes WHERE nome='JUCEMS' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
print(f"  fonte_id = {FONTE_ID}")

TIPOS_IMOVEL_VALIDOS = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
TIPOS_LEILAO_VALIDOS = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

ins = upd = err = 0

for r in rows:
    tipo_i = r.get("tipo_imovel","outro").upper()
    if tipo_i not in TIPOS_IMOVEL_VALIDOS: tipo_i = "OUTRO"
    tipo_l = r.get("tipo_leilao","extrajudicial").upper()
    if tipo_l not in TIPOS_LEILAO_VALIDOS: tipo_l = "EXTRAJUDICIAL"

    vmin  = _dec(r.get("valor_minimo"))
    vaval = _dec(r.get("valor_avaliacao"))
    area  = _dec(r.get("area_total"))
    q = r.get("quartos","")
    try: q = int(q) if q else None
    except: q = None

    d1 = r.get("data_primeiro_leilao","") or None
    d2 = r.get("data_segundo_leilao","") or None

    try:
        cur.execute("""
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
                %s,%s,%s,%s,%s,%s,%s,'ABERTO','IMOVEL',
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                true,false,false,NOW(),NOW()
            )
            ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
                titulo = EXCLUDED.titulo,
                valor_minimo = EXCLUDED.valor_minimo,
                data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
                imagem_principal = EXCLUDED.imagem_principal,
                arquivos = EXCLUDED.arquivos,
                atualizado_em = NOW()
        """, (
            FONTE_ID,
            r.get("id_externo","")[:200],
            r.get("titulo","")[:500],
            r.get("descricao","")[:500],
            r.get("url_original","")[:1000],
            tipo_i, tipo_l,
            r.get("cidade","")[:200],
            r.get("estado","MS")[:2],
            r.get("cep","")[:10],
            r.get("endereco_completo","")[:500],
            vmin, vaval, area, q, d1, d2,
            r.get("imagem_principal","")[:1000],
            r.get("arquivos","[]")[:4000],
            r.get("numero_processo","")[:100],
            r.get("leiloeiro","")[:300],
        ))
        if cur.rowcount == 1:
            ins += 1
        else:
            upd += 1
    except Exception as e:
        err += 1
        conn.rollback()
        if err <= 3:
            print(f"  [ERR] {r.get('id_externo','?')}: {str(e)[:100]}")
    else:
        if (ins + upd) % 50 == 0:
            conn.commit()

conn.commit()
cur.close()
conn.close()
print(f"\n[OK] {ins} inseridos, {upd} atualizados, {err} erros")
