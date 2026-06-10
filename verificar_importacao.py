"""
verificar_importacao.py
=======================
Checagem pós-scraping: compara CSV com banco e reimporta faltantes.
Executar após qualquer scraping de leiloeiros.

Uso:
    python verificar_importacao.py                          # CSV mais recente, auto-detecta fonte
    python verificar_importacao.py --csv csv/imoveis_jucisrs_2026-06-08.csv
    python verificar_importacao.py --fonte JUCISRS
    python verificar_importacao.py --so-verificar           # só mostra divergência
    python verificar_importacao.py --forcar                 # reimporta tudo (banco recriado)
"""
import csv, os, sys, subprocess, argparse
from pathlib import Path
from decimal import Decimal

BASE    = Path(__file__).resolve().parent
CSV_DIR = BASE / "csv"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _d(v) -> float | None:
    try:
        f = float(Decimal(str(v).replace(',', '.'))) if v else None
        return f if f is None or abs(f) <= 9_999_999_999_999.99 else None
    except Exception:
        return None


def clean(v, max_len: int | None = None) -> str:
    s = str(v or '').replace('\x00', '')
    return s[:max_len] if max_len else s


def detectar_fonte(csv_path: Path) -> str:
    nome = csv_path.stem.lower()
    for junta in ['jucisrs', 'jucems', 'jucesc', 'jucerja', 'jucemat', 'jucesp', 'jucisdf']:
        if junta in nome:
            return junta.upper()
    if 'caixa' in nome:            return 'Caixa'
    if 'leiloesjudiciais' in nome: return 'LeilõesJudiciais'
    if 'bomvalor' in nome:         return 'BomValor'
    return csv_path.stem.replace('imoveis_', '').replace('_', ' ').title()


def psql_query(sql: str) -> list[str]:
    proc = subprocess.run(
        ['docker', 'exec', 'leilao_postgres', 'psql', '-U', 'leilao', '-d', 'leilao_db',
         '--no-align', '--tuples-only', '-c', sql],
        capture_output=True, text=True, encoding='utf-8', timeout=30
    )
    return [l.strip() for l in proc.stdout.splitlines() if l.strip()]


# ── Verificação ───────────────────────────────────────────────────────────────

def verificar(csv_path: Path, fonte_nome: str) -> dict:
    rows     = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8-sig')))
    ids_csv  = {clean(r.get('id_externo', ''), 200) for r in rows if r.get('id_externo')}

    ids_banco_raw = psql_query(
        f"SELECT id_externo FROM imoveis "
        f"WHERE fonte_id=(SELECT id FROM fontes WHERE nome='{fonte_nome}' LIMIT 1)"
    )
    ids_banco = set(ids_banco_raw)

    return {
        'rows':             rows,
        'total_csv':        len(rows),
        'unicos_csv':       len(ids_csv),
        'ids_csv':          ids_csv,
        'ids_banco':        ids_banco,
        'faltantes':        ids_csv - ids_banco,
        'extras':           ids_banco - ids_csv,
    }


# ── Script executado dentro do container ─────────────────────────────────────

_INNER = '''\
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
    s = str(v or '').replace('\\x00','')
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
'''


# ── Reimportação ──────────────────────────────────────────────────────────────

def reimportar(csv_path: Path, fonte_nome: str, ids_alvo: set) -> tuple[int, int]:
    rows_todas   = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8-sig')))
    rows_filtro  = [r for r in rows_todas
                    if clean(r.get('id_externo', ''), 200) in ids_alvo]
    if not rows_filtro:
        return 0, 0

    tmp_csv     = BASE / '_reimport_tmp.csv'
    script_path = BASE / '_reimport_inner.py'
    fonte_path  = BASE / '_reimport_fonte.txt'

    with open(tmp_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows_todas[0].keys()))
        w.writeheader()
        w.writerows(rows_filtro)

    script_path.write_text(_INNER, encoding='utf-8')
    fonte_path.write_text(fonte_nome, encoding='utf-8')

    subprocess.run(['docker', 'cp', str(tmp_csv),     'leilao_api:/tmp/_reimport.csv'],       check=True, timeout=60)
    subprocess.run(['docker', 'cp', str(script_path), 'leilao_api:/tmp/_reimport_inner.py'],  check=True, timeout=30)
    subprocess.run(['docker', 'cp', str(fonte_path),  'leilao_api:/tmp/_reimport_fonte.txt'], check=True, timeout=10)

    proc = subprocess.run(
        ['docker', 'exec', 'leilao_api', 'python', '/tmp/_reimport_inner.py'],
        capture_output=True, text=True, encoding='utf-8', timeout=600
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print('[ERR container]', proc.stderr[:300])

    for linha in proc.stdout.splitlines():
        if '[OK]' in linha:
            try:
                parts = linha.replace('[OK]', '').split(',')
                ins = int(parts[0].strip().split()[0])
                err = int(parts[1].strip().split()[0])
                return ins, err
            except Exception:
                pass
    return 0, 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Checagem pós-scraping: CSV vs banco PostgreSQL')
    ap.add_argument('--so-verificar', action='store_true', help='Só verifica, não reimporta')
    ap.add_argument('--forcar',       action='store_true', help='Reimporta todos os registros do CSV')
    ap.add_argument('--fonte',        type=str,            help='Nome da fonte (ex: JUCISRS)')
    ap.add_argument('--csv',          type=str,            help='Caminho do CSV a verificar')
    args = ap.parse_args()

    # ── Encontra CSV
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = BASE / csv_path
    else:
        csvs = sorted(CSV_DIR.glob('imoveis_*.csv'),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not csvs:
            print(f'[ERRO] Nenhum CSV imoveis_*.csv em {CSV_DIR}')
            sys.exit(1)
        csv_path = csvs[0]

    fonte_nome = args.fonte or detectar_fonte(csv_path)

    print(f'\n{"="*56}')
    print(f'  Checagem pós-scraping')
    print(f'  CSV:   {csv_path.name}')
    print(f'  Fonte: {fonte_nome}')
    print(f'{"="*56}')

    # ── Verificação inicial
    r = verificar(csv_path, fonte_nome)
    duplicatas   = r['total_csv'] - r['unicos_csv']
    n_faltantes  = len(r['faltantes'])
    n_extras     = len(r['extras'])

    print(f'\n  Total no CSV:          {r["total_csv"]}')
    print(f'  Únicos (id_externo):   {r["unicos_csv"]}  ({duplicatas} URLs duplicadas no CSV — esperado)')
    print(f'  No banco (fonte):      {len(r["ids_banco"])}')
    print(f'  Faltando no banco:     {n_faltantes}')
    if n_extras:
        print(f'  Extras no banco:       {n_extras}  (importações anteriores — OK)')

    if n_faltantes == 0 and not args.forcar:
        print(f'\n{"="*56}')
        print(f'  [OK] Banco sincronizado. Nenhum faltante.')
        print(f'{"="*56}\n')
        return

    if args.so_verificar:
        print(f'\n  [ATENÇÃO] {n_faltantes} faltantes. Rode sem --so-verificar para corrigir.')
        return

    # ── Reimportação
    ids_alvo = r['faltantes'] if not args.forcar else r['ids_csv']
    print(f'\n  Reimportando {len(ids_alvo)} linhas...')
    ins, err_ri = reimportar(csv_path, fonte_nome, ids_alvo)

    # ── Verificação final
    r2           = verificar(csv_path, fonte_nome)
    n_faltantes2 = len(r2['faltantes'])

    print(f'\n{"="*56}')
    print(f'  Verificação final')
    print(f'  No banco agora:        {len(r2["ids_banco"])}')
    print(f'  Ainda faltando:        {n_faltantes2}')

    if n_faltantes2 == 0:
        print(f'  [OK] Banco sincronizado com o CSV.')
    elif n_faltantes2 <= err_ri:
        print(f'  [AVISO] {n_faltantes2} linha(s) com dados inválidos (overflow ou NUL irrecuperável):')
        rows_inv = r2['rows']
        for row in rows_inv:
            if clean(row.get('id_externo', ''), 200) in r2['faltantes']:
                print(f'    → {row.get("leiloeiro","")[:30]}'
                      f' | preco={row.get("valor_minimo","")}'
                      f' | titulo={row.get("titulo","")[:40]}')
    else:
        print(f'  [FALHA] {n_faltantes2} linhas ainda faltando.')
        print(f'  Verifique: docker logs leilao_api --tail 50')

    # ── Pós-processamento se inseriu novos
    novos = len(r2['ids_banco']) - len(r['ids_banco'])
    if novos > 0:
        print(f'\n  Pós-processamento ({novos} novos registros)...')
        for cmd in [
            'cd /app && python run.py classificar --limite 2000',
            'cd /app && python run.py deduplicar',
        ]:
            subprocess.run(['docker', 'exec', 'leilao_api', 'bash', '-c', cmd],
                           capture_output=True, timeout=120)
        subprocess.run(['docker', 'restart', 'leilao_api'],
                       capture_output=True, timeout=60)
        print('  Classificação, deduplicação e restart concluídos.')

    print(f'{"="*56}\n')


if __name__ == '__main__':
    main()
