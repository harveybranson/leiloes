import csv, os
from decimal import Decimal

CSV_FILE = '/tmp/imoveis_jucisrs.csv'
rows = list(csv.DictReader(open(CSV_FILE, newline='', encoding='utf-8-sig')))
print(f'[INFO] {len(rows)} registros')


def _d(v):
    try:
        return float(Decimal(str(v).replace(',', '.'))) if v else None
    except Exception:
        return None


def clean(v, max_len=None):
    s = str(v or '')
    # Remove NUL bytes (0x00) que causam erro no psycopg2
    s = s.replace('\x00', '')
    if max_len:
        s = s[:max_len]
    return s


import psycopg2
db_url = os.environ.get('DATABASE_URL_SYNC', 'postgresql://leilao:leilao123@postgres:5432/leilao_db')
db_url = db_url.replace('postgresql+asyncpg://', 'postgresql://')
conn = psycopg2.connect(db_url)
cur = conn.cursor()

cur.execute("INSERT INTO fontes (nome,url_base,ativo,criado_em) VALUES ('JUCISRS','https://sistemas.jucisrs.rs.gov.br/',true,NOW()) ON CONFLICT (nome) DO NOTHING")
cur.execute("SELECT id FROM fontes WHERE nome='JUCISRS' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
conn.commit()
print(f'  fonte_id={FONTE_ID}')

TIPOS_I = {'APARTAMENTO', 'CASA', 'TERRENO', 'COMERCIAL', 'RURAL', 'GALPAO', 'SALA', 'VAGA', 'OUTRO'}
TIPOS_L = {'JUDICIAL', 'EXTRAJUDICIAL', 'BANCARIO'}
ins = upd = err = skip = 0

SQL = """
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
    titulo=EXCLUDED.titulo,
    valor_minimo=EXCLUDED.valor_minimo,
    data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
    imagem_principal=EXCLUDED.imagem_principal,
    arquivos=EXCLUDED.arquivos,
    atualizado_em=NOW()
"""

for i, r in enumerate(rows):
    ti = clean(r.get('tipo_imovel', 'outro')).upper()
    if ti not in TIPOS_I:
        ti = 'OUTRO'
    tl = clean(r.get('tipo_leilao', 'extrajudicial')).upper()
    if tl not in TIPOS_L:
        tl = 'EXTRAJUDICIAL'

    id_ext = clean(r.get('id_externo', ''), 200)
    if not id_ext:
        skip += 1
        continue

    d1 = clean(r.get('data_primeiro_leilao', '')) or None
    d2 = clean(r.get('data_segundo_leilao', '')) or None

    # Usa SAVEPOINT para isolar falhas — só descarta a linha com erro
    cur.execute('SAVEPOINT sp')
    try:
        cur.execute(SQL, (
            FONTE_ID,
            id_ext,
            clean(r.get('titulo', ''), 500),
            clean(r.get('descricao', ''), 500),
            clean(r.get('url_original', ''), 1000),
            ti, tl,
            clean(r.get('cidade', ''), 200),
            clean(r.get('estado', 'RS'), 2),
            clean(r.get('cep', ''), 10),
            clean(r.get('endereco_completo', ''), 500),
            _d(r.get('valor_minimo')),
            _d(r.get('valor_avaliacao')),
            _d(r.get('area_total')),
            int(r['quartos']) if r.get('quartos') else None,
            d1, d2,
            clean(r.get('imagem_principal', ''), 1000),
            clean(r.get('arquivos', '[]'), 4000),
            clean(r.get('numero_processo', ''), 100),
            clean(r.get('leiloeiro', ''), 300),
        ))
        cur.execute('RELEASE SAVEPOINT sp')
        if cur.rowcount == 1:
            ins += 1
        else:
            upd += 1
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT sp')
        cur.execute('RELEASE SAVEPOINT sp')
        err += 1
        if err <= 5:
            print(f'  ERR [{i}] id={id_ext[:20]}: {str(e)[:120]}')

    if (i + 1) % 200 == 0:
        conn.commit()
        print(f'  Progresso {i+1}/{len(rows)}: {ins} ins, {upd} upd, {err} err')

conn.commit()
cur.close()
conn.close()
print(f'\n[OK] {ins} inseridos, {upd} atualizados, {err} erros, {skip} pulados')
