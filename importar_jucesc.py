"""
importar_jucesc.py
==================
Importa imoveis_jucesc_YYYY-MM-DD.csv para:
  - SQLite  (imoveis_leiloeiros.db  — viewer local)
  - PostgreSQL Docker (leilao_db)

Uso:
    python importar_jucesc.py
"""
import sys, io, csv, json, sqlite3, subprocess
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE     = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE / "csv"
DB_FILE  = BASE / "imoveis_leiloeiros.db"

# CSV mais recente
csvs = sorted(CSV_DIR.glob("imoveis_jucesc_*.csv"), reverse=True)
if not csvs:
    print("[ERRO] Nenhum CSV imoveis_jucesc_*.csv encontrado em", CSV_DIR)
    sys.exit(1)
CSV_FILE = csvs[0]
print(f"[INFO] CSV: {CSV_FILE}")

rows = []
with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        rows.append(r)
print(f"[INFO] {len(rows)} registros")

def _dec(v):
    try: return float(Decimal(str(v).replace(",","."))) if v else None
    except: return None

def _dt(v):
    if not v: return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(str(v)[:19], fmt)
        except: pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
# 1. SQLite
# ══════════════════════════════════════════════════════════════════════════════
print("\n[SQLite] Importando para", DB_FILE)
conn = sqlite3.connect(DB_FILE)
conn.execute("""
    CREATE TABLE IF NOT EXISTS imoveis (
        id TEXT PRIMARY KEY,
        leiloeiro TEXT, junta TEXT, site TEXT,
        titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
        lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
        url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT
    )""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_leiloeiro ON imoveis(leiloeiro)")
conn.commit()

ins_sq = dup_sq = 0
agora = datetime.now().isoformat(timespec="seconds")
for r in rows:
    try:
        conn.execute(
            "INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id_externo",""), r.get("leiloeiro",""), "JUCESC",
                r.get("leiloeiro_site",""),
                r.get("titulo","")[:500], r.get("descricao","")[:300],
                r.get("endereco_completo","")[:200],
                r.get("cidade",""), r.get("estado","SC"),
                _dec(r.get("valor_minimo")), _dec(r.get("valor_avaliacao")),
                r.get("data_primeiro_leilao",""),
                r.get("url_original",""), r.get("tipo_imovel",""),
                r.get("imagem_principal",""), agora,
            )
        )
        ins_sq += 1
    except sqlite3.IntegrityError:
        dup_sq += 1
    except Exception as e:
        print(f"[SQLite ERR] {e}")

conn.commit()
conn.close()
print(f"  SQLite: {ins_sq} inseridos, {dup_sq} já existiam")

# ══════════════════════════════════════════════════════════════════════════════
# 2. PostgreSQL via Docker
# ══════════════════════════════════════════════════════════════════════════════
print("\n[PostgreSQL] Importando via Docker...")

def psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "leilao_postgres",
         "psql", "-U", "leilao", "-d", "leilao_db",
         "--no-align", "--tuples-only", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    return proc.stdout

# Garante fonte
psql("""
    INSERT INTO fontes (nome, url_base, ativo, criado_em)
    VALUES ('JUCESC','https://leiloeiros.jucesc.sc.gov.br/site/',true,NOW())
    ON CONFLICT (nome) DO NOTHING;
""")
fonte_id_raw = psql("SELECT id FROM fontes WHERE nome='JUCESC' LIMIT 1;").strip()
if not fonte_id_raw.isdigit():
    print(f"[ERRO] Não foi possível obter fonte_id JUCESC. Saída: {repr(fonte_id_raw)}")
    sys.exit(1)
FONTE_ID = int(fonte_id_raw)
print(f"  fonte_id JUCESC = {FONTE_ID}")

# Upsert em lotes de 50
TIPOS_IMOVEL_VALIDOS = {"apartamento","casa","terreno","comercial","rural","galpao","sala","vaga","outro"}
TIPOS_LEILAO_VALIDOS = {"judicial","extrajudicial","bancario"}

ins_pg = upd_pg = err_pg = 0

for i in range(0, len(rows), 50):
    batch = rows[i:i+50]
    values = []
    for r in batch:
        def esc(v, max_len=None):
            s = str(v or "").replace("'", "''")
            if max_len: s = s[:max_len]
            return s

        tipo_i = r.get("tipo_imovel","outro").lower()
        if tipo_i not in TIPOS_IMOVEL_VALIDOS: tipo_i = "outro"

        tipo_l = r.get("tipo_leilao","extrajudicial").lower()
        if tipo_l not in TIPOS_LEILAO_VALIDOS: tipo_l = "extrajudicial"

        vmin   = _dec(r.get("valor_minimo"))
        vaval  = _dec(r.get("valor_avaliacao"))
        area   = _dec(r.get("area_total"))
        quartos = r.get("quartos","")
        try: quartos = int(quartos) if quartos else None
        except: quartos = None

        d1 = r.get("data_primeiro_leilao","")
        d2 = r.get("data_segundo_leilao","")

        values.append(f"""(
            {FONTE_ID},
            '{esc(r.get("id_externo",""), 200)}',
            '{esc(r.get("titulo",""), 500)}',
            '{esc(r.get("descricao",""), 500)}',
            '{esc(r.get("url_original",""), 1000)}',
            '{tipo_i}',
            '{tipo_l}',
            'ABERTO',
            'IMOVEL',
            '{esc(r.get("cidade",""), 200)}',
            '{esc(r.get("estado","SC"), 2)}',
            '{esc(r.get("cep",""), 10)}',
            '{esc(r.get("endereco_completo",""), 500)}',
            {vmin if vmin is not None else 'NULL'},
            {vaval if vaval is not None else 'NULL'},
            {area if area is not None else 'NULL'},
            {quartos if quartos is not None else 'NULL'},
            {f"'{d1}'" if d1 else 'NULL'},
            {f"'{d2}'" if d2 else 'NULL'},
            '{esc(r.get("imagem_principal",""), 1000)}',
            '{esc(r.get("arquivos","[]"), 4000)}',
            '{esc(r.get("numero_processo",""), 100)}',
            '{esc(r.get("leiloeiro",""), 300)}',
            true, false, false,
            NOW(), NOW()
        )""")

    if not values:
        continue

    sql = f"""
    INSERT INTO imoveis (
        fonte_id, id_externo, titulo, descricao, url_original,
        tipo_imovel, tipo_leilao, status, categoria,
        cidade, estado, cep, endereco_completo,
        valor_minimo, valor_avaliacao, area_total, quartos,
        data_primeiro_leilao, data_segundo_leilao,
        imagem_principal, arquivos, numero_processo,
        leiloeiro, ativo, classificado, geocodificado,
        criado_em, atualizado_em
    ) VALUES {', '.join(values)}
    ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
        titulo = EXCLUDED.titulo,
        valor_minimo = EXCLUDED.valor_minimo,
        data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
        imagem_principal = EXCLUDED.imagem_principal,
        arquivos = EXCLUDED.arquivos,
        atualizado_em = NOW();
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", "leilao_postgres",
             "psql", "-U", "leilao", "-d", "leilao_db", "-c", sql],
            capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        out = proc.stdout + proc.stderr
        if "INSERT" in out:
            m = __import__("re").search(r"INSERT \d+ (\d+)", out)
            n = int(m.group(1)) if m else len(batch)
            ins_pg += n
        elif "UPDATE" in out or "conflict" in out.lower():
            upd_pg += len(batch)
        elif proc.returncode != 0:
            err_pg += len(batch)
            print(f"  [ERR lote {i}] {out[:200]}")
    except Exception as e:
        err_pg += len(batch)
        print(f"  [ERR lote {i}] {e}")

    if (i // 50) % 5 == 0:
        print(f"  Lote {i//50+1}: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")

print(f"\n[PostgreSQL] Total: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")

# Pós-processamento
print("\n[PG] Classificando imóveis...")
subprocess.run(
    ["docker", "exec", "leilao_api",
     "bash", "-c", "cd /app && python run.py classificar --limite 2000"],
    capture_output=True, text=True, timeout=120
)

print("[PG] Deduplicando...")
subprocess.run(
    ["docker", "exec", "leilao_api",
     "bash", "-c", "cd /app && python run.py deduplicar"],
    capture_output=True, text=True, timeout=120
)

# Verificação final
count_raw = psql("SELECT COUNT(*) FROM imoveis WHERE fonte_id = 943 AND ativo = true;").strip()
print(f"\n[PG] Imóveis JUCESC ativos no banco: {count_raw}")

top = psql("""
    SELECT leiloeiro, COUNT(*) as n FROM imoveis
    WHERE fonte_id = 943 AND ativo = true
    GROUP BY leiloeiro ORDER BY n DESC LIMIT 15;
""")
print("[PG] Top leiloeiros:")
print(top)
