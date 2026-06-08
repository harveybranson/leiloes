import csv, os, sys
from decimal import Decimal

CSV_FILE   = '/tmp/_reimport.csv'
FONTE_NOME = open('/tmp/_reimport_fonte.txt').read().strip()

rows = list(csv.DictReader(open(CSV_FILE, newline='', encoding='utf-8-sig')))
print(f'[INFO] {len(rows)} linhas a reimportar')

def _d(v):
    try:
        f = float(Decimal(str(v).replace(',','.'))) if v else None
        return f if f is None or abs(f) <= 9_999_999_999_999.99 else None
    except Exception:
        return None

def clean(v, n=None):
    s = str(v or '').replace('\x00','')
    return s[:n] if n else s

import psycopg2
db = os.environ.get('DATABASE_URL_SYNC','postgresql://leilao:leilao123@postgres:5432/leilao_db')
db = db.replace('postgresql+asyncpg://','postgresql://')
conn = psycopg2.connect(db)
cur  = conn.cursor()

cur.execute(f"INSERT INTO fontes(nome,url_base,ativo,criado_em) VALUES('{FONTE_NOME}','',true,NOW()) ON CONFLICT(nome) DO NOTHING")
cur.execute(f"SELECT id FROM fontes WHERE nome='{FONTE_NOME}' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
conn.commit()

TIPOS_I = {'APARTAMENTO','CASA','TERRENO','COMERCIAL','RURAL','GALPAO','SALA','VAGA','OUTRO'}
TIPOS_L = {'JUDICIAL','EXTRAJUDICIAL','BANCARIO'}
SQL = """
INSERT INTO imoveis(
    fonte_id,id_externo,titulo,descricao,url_original,
    tipo_imovel,tipo_leilao,status,categoria,
    cidade,estado,cep,endereco_completo,
    valor_minimo,valor_avaliacao,area_total,quartos,
    data_primeiro_leilao,data_segundo_leilao,
    imagem_principal,arquivos,numero_processo,
    leiloeiro,ativo,classificado,geocodificado,criado_em,atualizado_em
) VALUES(%s,%s,%s,%s,%s,%s,%s,'ABERTO','IMOVEL',
         %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
         true,false,false,NOW(),NOW())
ON CONFLICT(fonte_id,id_externo) DO UPDATE SET
    titulo=EXCLUDED.titulo, valor_minimo=EXCLUDED.valor_minimo,
    data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
    imagem_principal=EXCLUDED.imagem_principal,
    arquivos=EXCLUDED.arquivos, atualizado_em=NOW()
"""
ins=err=0
for i,r in enumerate(rows):
    ti = clean(r.get('tipo_imovel','outro')).upper()
    if ti not in TIPOS_I: ti='OUTRO'
    tl = clean(r.get('tipo_leilao','extrajudicial')).upper()
    if tl not in TIPOS_L: tl='EXTRAJUDICIAL'
    id_ext = clean(r.get('id_externo',''),200)
    if not id_ext: continue
    cur.execute('SAVEPOINT sp')
    try:
        cur.execute(SQL,(
            FONTE_ID, id_ext,
            clean(r.get('titulo',''),500), clean(r.get('descricao',''),500),
            clean(r.get('url_original',''),1000), ti, tl,
            clean(r.get('cidade',''),200), clean(r.get('estado','RS'),2),
            clean(r.get('cep',''),10), clean(r.get('endereco_completo',''),500),
            _d(r.get('valor_minimo')), _d(r.get('valor_avaliacao')),
            _d(r.get('area_total')),
            int(r['quartos']) if r.get('quartos') else None,
            clean(r.get('data_primeiro_leilao','')) or None,
            clean(r.get('data_segundo_leilao','')) or None,
            clean(r.get('imagem_principal',''),1000),
            clean(r.get('arquivos','[]'),4000),
            clean(r.get('numero_processo',''),100),
            clean(r.get('leiloeiro',''),300),
        ))
        cur.execute('RELEASE SAVEPOINT sp')
        ins+=1
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT sp')
        cur.execute('RELEASE SAVEPOINT sp')
        err+=1
        if err<=5: print(f'  ERR [{i}] {str(e)[:120]}')
    if (i+1)%200==0:
        conn.commit()
        print(f'  {i+1}/{len(rows)}: {ins} ok, {err} err')
conn.commit(); cur.close(); conn.close()
print(f'[OK] {ins} processados, {err} erros')
