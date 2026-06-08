
import csv, os, sys
from pathlib import Path
from decimal import Decimal
CSV_FILE = '/tmp/imoveis_jucisrs.csv'
rows = list(csv.DictReader(open(CSV_FILE, newline='', encoding='utf-8-sig')))
print(f'[INFO] {len(rows)} registros')

def _d(v):
    try: return float(Decimal(str(v).replace(',','.'))) if v else None
    except: return None

import psycopg2
db_url = os.environ.get('DATABASE_URL_SYNC','postgresql://leilao:leilao123@postgres:5432/leilao_db').replace('postgresql+asyncpg://','postgresql://')
conn = psycopg2.connect(db_url)
cur = conn.cursor()

cur.execute("INSERT INTO fontes (nome,url_base,ativo,criado_em) VALUES ('JUCISRS','https://sistemas.jucisrs.rs.gov.br/leiloeiros/',true,NOW()) ON CONFLICT (nome) DO NOTHING")
cur.execute("SELECT id FROM fontes WHERE nome='JUCISRS' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
print(f'  fonte_id={FONTE_ID}')

TIPOS_I = {'APARTAMENTO','CASA','TERRENO','COMERCIAL','RURAL','GALPAO','SALA','VAGA','OUTRO'}
TIPOS_L = {'JUDICIAL','EXTRAJUDICIAL','BANCARIO'}
ins=upd=err=0

for r in rows:
    ti = r.get('tipo_imovel','outro').upper()
    if ti not in TIPOS_I: ti='OUTRO'
    tl = r.get('tipo_leilao','extrajudicial').upper()
    if tl not in TIPOS_L: tl='EXTRAJUDICIAL'
    try:
        cur.execute("""INSERT INTO imoveis (
            fonte_id,id_externo,titulo,descricao,url_original,
            tipo_imovel,tipo_leilao,status,categoria,
            cidade,estado,cep,endereco_completo,
            valor_minimo,valor_avaliacao,area_total,quartos,
            data_primeiro_leilao,data_segundo_leilao,
            imagem_principal,arquivos,numero_processo,
            leiloeiro,ativo,classificado,geocodificado,criado_em,atualizado_em
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'ABERTO','IMOVEL',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,false,false,NOW(),NOW())
        ON CONFLICT (fonte_id,id_externo) DO UPDATE SET
            titulo=EXCLUDED.titulo,valor_minimo=EXCLUDED.valor_minimo,
            data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
            imagem_principal=EXCLUDED.imagem_principal,
            arquivos=EXCLUDED.arquivos,atualizado_em=NOW()
        """, (
            FONTE_ID, r.get('id_externo','')[:200], r.get('titulo','')[:500],
            r.get('descricao','')[:500], r.get('url_original','')[:1000],
            ti, tl,
            r.get('cidade','')[:200], r.get('estado','RS')[:2], r.get('cep','')[:10],
            r.get('endereco_completo','')[:500],
            _d(r.get('valor_minimo')), _d(r.get('valor_avaliacao')),
            _d(r.get('area_total')), int(r['quartos']) if r.get('quartos') else None,
            r.get('data_primeiro_leilao','') or None, r.get('data_segundo_leilao','') or None,
            r.get('imagem_principal','')[:1000], r.get('arquivos','[]')[:4000],
            r.get('numero_processo','')[:100], r.get('leiloeiro','')[:300],
        ))
        if cur.rowcount==1: ins+=1
        else: upd+=1
    except Exception as e:
        err+=1; conn.rollback()
        if err<=3: print(f'  ERR: {str(e)[:80]}')
    else:
        if (ins+upd)%100==0: conn.commit()

conn.commit(); cur.close(); conn.close()
print(f'[OK] {ins} inseridos, {upd} atualizados, {err} erros')
