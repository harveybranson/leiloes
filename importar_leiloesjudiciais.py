"""
Importa imoveis_leiloesjudiciais_YYYY-MM-DD.csv para o banco PostgreSQL
e também para o SQLite local (imoveis_leiloeiros.db) usado pelo viewer.

Uso:
    python importar_leiloesjudiciais.py
"""
import sys, io, csv, json, sqlite3, os
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE      = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR   = BASE / "csv"
DB_FILE   = BASE / "imoveis_leiloeiros.db"
SCRAPER_ROOT = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\leilao-scraper")

# Encontra o CSV mais recente de leiloesjudiciais
csvs = sorted(CSV_DIR.glob("imoveis_leiloesjudiciais_*.csv"), reverse=True)
if not csvs:
    print("[ERRO] Nenhum CSV imoveis_leiloesjudiciais_*.csv encontrado em", CSV_DIR)
    sys.exit(1)
CSV_FILE = csvs[0]
print(f"[INFO] CSV: {CSV_FILE}")

# ── Lê CSV ────────────────────────────────────────────────────────────────────
rows = []
with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        rows.append(r)
print(f"[INFO] {len(rows)} registros no CSV")

def _dec(v):
    try: return float(Decimal(str(v).replace(',','.'))) if v else None
    except: return None

def _int(v):
    try: return int(v) if v else None
    except: return None

def _dt(v):
    if not v: return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(str(v)[:19], fmt)
        except: pass
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# 1. SQLite (viewer local)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n[SQLite] Importando para", DB_FILE)
conn = sqlite3.connect(DB_FILE)
conn.execute("""
    CREATE TABLE IF NOT EXISTS imoveis (
        id TEXT PRIMARY KEY,
        leiloeiro TEXT, junta TEXT, site TEXT,
        titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
        lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
        url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT
    )
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_leiloeiro ON imoveis(leiloeiro)")
conn.commit()

ins_sq = dup_sq = err_sq = 0
agora = datetime.now().isoformat(timespec="seconds")
for r in rows:
    try:
        conn.execute(
            "INSERT INTO imoveis (id,leiloeiro,junta,site,titulo,descricao,endereco,"
            "cidade,uf,lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id_externo","").strip(),
                r.get("leiloeiro","").strip(),
                "Leilões Judiciais",
                r.get("leiloeiro_site","").strip(),
                r.get("titulo","").strip(),
                "",
                r.get("endereco_completo","").strip(),
                r.get("cidade","").strip(),
                r.get("estado","").strip(),
                _dec(r.get("valor_minimo")),
                _dec(r.get("valor_avaliacao")),
                r.get("data_primeiro_leilao","") or r.get("data_encerramento",""),
                r.get("url_original","").strip(),
                r.get("tipo_imovel","").strip().upper(),
                r.get("imagem_principal","").strip() or None,
                agora,
            )
        )
        ins_sq += 1
    except sqlite3.IntegrityError:
        dup_sq += 1
    except Exception as e:
        err_sq += 1
        print(f"  [AVISO SQLite] {e}")
conn.commit()
conn.close()
print(f"[SQLite] Inseridos: {ins_sq} | Duplicados: {dup_sq} | Erros: {err_sq}")

# ═══════════════════════════════════════════════════════════════════════════════
# 2. PostgreSQL (sistema Docker)
# ═══════════════════════════════════════════════════════════════════════════════
if str(SCRAPER_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRAPER_ROOT))

try:
    from database.models import Leiloeiro, Imovel, Fonte, TipoImovel, TipoLeilao, StatusLeilao, CategoriaItem
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    PG_OK = True
except ImportError as e:
    print(f"\n[AVISO] PostgreSQL indisponível: {e}")
    PG_OK = False

if PG_OK:
    DB_URL = os.getenv("DATABASE_URL_SYNC", "postgresql://leilao:leilao123@localhost:5432/leilao_db")
    print(f"\n[PostgreSQL] Conectando: {DB_URL.split('@')[-1]}")
    try:
        engine = create_engine(DB_URL, echo=False, pool_pre_ping=True)
        Session = sessionmaker(bind=engine)
        session = Session()
    except Exception as e:
        print(f"[ERRO PG] {e}")
        PG_OK = False

if PG_OK:
    TIPO_MAP = {
        'rural':'rural','apartamento':'apartamento','casa':'casa',
        'terreno':'terreno','comercial':'comercial','galpao':'galpao','sala':'sala',
    }
    def _tipo(v):
        m = {'apartamento':TipoImovel.APARTAMENTO,'casa':TipoImovel.CASA,
             'terreno':TipoImovel.TERRENO,'comercial':TipoImovel.COMERCIAL,
             'rural':TipoImovel.RURAL,'galpao':TipoImovel.GALPAO,'sala':TipoImovel.SALA}
        return m.get(v, TipoImovel.OUTRO)

    # Fonte
    fonte = session.query(Fonte).filter(Fonte.nome == 'Leilões Judiciais').first()
    if not fonte:
        fonte = Fonte(nome='Leilões Judiciais', url_base='https://www.leiloesjudiciais.com.br', ativo=True)
        session.add(fonte)
        session.flush()

    # Leiloeiros únicos
    leiloeiros_db = {}
    nomes_unicos = {r.get('leiloeiro','').strip() for r in rows if r.get('leiloeiro')}
    col_names = set(Leiloeiro.__table__.columns.keys())
    for nome in nomes_unicos:
        lei = session.query(Leiloeiro).filter(Leiloeiro.nome == nome).first()
        if not lei:
            dados = {'nome': nome, 'site': 'https://www.leiloesjudiciais.com.br',
                     'situacao': 'regular', 'junta_comercial': 'Leilões Judiciais'}
            lei = Leiloeiro(**{k:v for k,v in dados.items() if k in col_names})
            session.add(lei)
            session.flush()
        leiloeiros_db[nome] = lei.id
    session.commit()
    print(f"[PostgreSQL] {len(leiloeiros_db)} leiloeiros sincronizados")

    ins_pg = dup_pg = err_pg = 0
    col_im = set(Imovel.__table__.columns.keys())
    BATCH = 100
    for i, r in enumerate(rows):
        lei_nome = r.get('leiloeiro','').strip()
        lei_id   = leiloeiros_db.get(lei_nome)
        id_ext   = r.get('id_externo','').strip()

        existing = session.query(Imovel).filter(
            Imovel.fonte_id == fonte.id, Imovel.id_externo == id_ext
        ).first()

        campos = {
            'id_externo':        id_ext,
            'fonte_id':          fonte.id,
            'titulo':            r.get('titulo','')[:500],
            'url_original':      r.get('url_original','')[:1000],
            'tipo_imovel':       _tipo(r.get('tipo_imovel','')),
            'tipo_leilao':       TipoLeilao.JUDICIAL if 'judicial' in r.get('tipo_leilao','') else TipoLeilao.EXTRAJUDICIAL,
            'status':            StatusLeilao.ABERTO,
            'categoria':         CategoriaItem.IMOVEL,
            'valor_minimo':      _dec(r.get('valor_minimo')),
            'valor_avaliacao':   _dec(r.get('valor_avaliacao')),
            'estado':            r.get('estado','')[:2],
            'cidade':            r.get('cidade','')[:200],
            'cep':               r.get('cep','')[:9] or None,
            'endereco_completo': r.get('endereco_completo','')[:500] or None,
            'area_total':        _dec(r.get('area_total')),
            'quartos':           _int(r.get('quartos')),
            'banheiros':         _int(r.get('banheiros')),
            'vagas':             _int(r.get('vagas')),
            'data_primeiro_leilao': _dt(r.get('data_primeiro_leilao')),
            'data_segundo_leilao':  _dt(r.get('data_segundo_leilao')),
            'data_encerramento':    _dt(r.get('data_encerramento')),
            'imagem_principal':  r.get('imagem_principal') or None,
            'numero_processo':   r.get('numero_processo') or None,
            'leiloeiro':         lei_nome,
            'leiloeiro_id':      lei_id,
        }

        try:
            if existing:
                for k, v in campos.items():
                    if v is not None and k in col_im:
                        setattr(existing, k, v)
                dup_pg += 1
            else:
                session.add(Imovel(**{k:v for k,v in campos.items() if k in col_im}))
                ins_pg += 1
        except Exception as e:
            err_pg += 1
            session.rollback()

        if (i+1) % BATCH == 0:
            try:
                session.commit()
                print(f"  [{i+1}/{len(rows)}] inseridos:{ins_pg} dup:{dup_pg} err:{err_pg}")
            except Exception as e:
                session.rollback()
                print(f"  [AVISO COMMIT] {e}")

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"[AVISO COMMIT FINAL] {e}")

    session.close()
    print(f"[PostgreSQL] Inseridos: {ins_pg} | Atualizados: {dup_pg} | Erros: {err_pg}")

print("\n[OK] Importação concluída.")
print(f"     SQLite : {ins_sq} inseridos → {DB_FILE.name}")
if PG_OK:
    print(f"     Banco  : {ins_pg} inseridos, {dup_pg} atualizados")
