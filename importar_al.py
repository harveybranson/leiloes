# -*- coding: utf-8 -*-
"""
Importa csv/imoveis_al_2026-06-09.csv para o PostgreSQL do site (leilao_db).
- Cria/garante a fonte 'JUCEAL'.
- Dedup GLOBAL por url_original (nao reinsere imovel ja listado sob outra fonte).
- Mapeia colunas do CSV simples -> schema PG (enums, datas, valores).
- Insercao linha-a-linha com SAVEPOINT (uma falha nao derruba o lote).
"""
import csv, sys, re, hashlib
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import psycopg2

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_FILE = BASE / "csv" / "imoveis_al_2026-06-09.csv"
DSN = "postgresql://leilao:leilao123@localhost:5432/leilao_db"

TIPOS = {"APARTAMENTO", "CASA", "TERRENO", "COMERCIAL", "RURAL", "GALPAO", "SALA", "VAGA", "OUTRO"}


def tipo_imovel(titulo):
    t = (titulo or "").lower()
    if "apartament" in t or "apto" in t or "flat" in t or "kitnet" in t or "quitinete" in t or "cobertura" in t:
        return "APARTAMENTO"
    if "casa" in t or "sobrado" in t or "residenc" in t:
        return "CASA"
    if "fazenda" in t or "sitio" in t or "sítio" in t or "chacara" in t or "chácara" in t or "rural" in t or "gleba" in t:
        return "RURAL"
    if "galp" in t or "barrac" in t:
        return "GALPAO"
    if "sala" in t or "loja" in t or "comercial" in t or "predio" in t or "prédio" in t or "edific" in t:
        return "COMERCIAL"
    if "terreno" in t or "lote" in t or "area" in t or "área" in t:
        return "TERRENO"
    if "vaga" in t or "garagem" in t or "box" in t:
        return "VAGA"
    return "OUTRO"


def to_dec(v):
    try:
        return float(Decimal(str(v).replace(".", "").replace(",", "."))) if v else None
    except Exception:
        return None


def to_date(v):
    if not v:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v))
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None


def clean(s, n=None):
    s = (s or "").replace("\x00", "").strip()
    return s[:n] if n else s


def main():
    rows = list(csv.DictReader(open(CSV_FILE, encoding="utf-8")))
    print(f"[INFO] {len(rows)} linhas no CSV {CSV_FILE.name}")

    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor()

    # garante fonte JUCEAL
    cur.execute("""INSERT INTO fontes (nome,url_base,ativo,criado_em)
                   VALUES ('JUCEAL','https://www.juceal.al.gov.br/',true,NOW())
                   ON CONFLICT (nome) DO NOTHING""")
    conn.commit()
    cur.execute("SELECT id FROM fontes WHERE nome='JUCEAL'")
    fonte_id = cur.fetchone()[0]
    print(f"[INFO] fonte_id JUCEAL = {fonte_id}")

    # dedup global por url_original
    urls = [r["url"] for r in rows if r.get("url")]
    cur.execute("SELECT url_original FROM imoveis WHERE url_original = ANY(%s)", (urls,))
    ja = set(x[0] for x in cur.fetchall())

    # dedup dentro do CSV (sites compartilhados geram a mesma url p/ varios leiloeiros)
    vistos = set()
    ins = pulados = err = 0
    for r in rows:
        url = clean(r.get("url"))
        if not url or url in ja or url in vistos:
            pulados += 1
            continue
        vistos.add(url)
        idext = hashlib.md5(url.encode("utf-8")).hexdigest()[:24]
        d1 = to_date(r.get("data_leilao"))
        try:
            cur.execute("SAVEPOINT sp")
            cur.execute("""INSERT INTO imoveis
                (fonte_id,id_externo,titulo,descricao,url_original,
                 tipo_imovel,tipo_leilao,status,categoria,
                 cidade,estado,endereco_completo,
                 valor_minimo,data_primeiro_leilao,
                 imagem_principal,arquivos,leiloeiro,
                 ativo,classificado,geocodificado,criado_em,atualizado_em)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,false,false,NOW(),NOW())
                ON CONFLICT (fonte_id,id_externo) DO NOTHING""",
                (fonte_id, idext, clean(r.get("titulo"), 500), clean(r.get("descricao"), 500), url,
                 tipo_imovel(r.get("titulo")), "EXTRAJUDICIAL", "ABERTO", "IMOVEL",
                 clean(r.get("cidade"), 200), clean(r.get("uf"), 2), clean(r.get("descricao"), 500),
                 to_dec(r.get("preco")), d1,
                 clean(r.get("imagem"), 1000), clean(r.get("anexos"), 4000), clean(r.get("leiloeiro"), 300)))
            cur.execute("RELEASE SAVEPOINT sp")
            ins += cur.rowcount
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp")
            err += 1
            print(f"  [ERR] {clean(r.get('titulo'),50)} -> {str(e)[:100]}")
    conn.commit()

    cur.execute("SELECT count(*) FROM imoveis WHERE fonte_id=%s", (fonte_id,))
    total_fonte = cur.fetchone()[0]
    conn.close()
    print(f"\n[OK] inseridos={ins} | pulados(dedup)={pulados} | erros={err}")
    print(f"[OK] total na fonte JUCEAL agora: {total_fonte}")


if __name__ == "__main__":
    main()
