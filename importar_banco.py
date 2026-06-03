"""
Importa imoveis do CSV para SQLite e gera relatorio por estado.
Extrai UF do campo descricao quando nao disponivel diretamente.
"""
import csv
import re
import sqlite3
import sys
import os
from pathlib import Path
from datetime import datetime

# Forcar UTF-8 no stdout (Python 3.7+)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"C:/Users/arthur/OneDrive/Documentos/Cursor/leiloes")
CSV_FILE = BASE / "csv" / "imoveis_leiloeiros_2026-06-03.csv"
DB_FILE = BASE / "imoveis_leiloeiros.db"

ESTADOS_BR = [
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO"
]
ESTADOS_NOME = {
    "AC": "Acre",              "AL": "Alagoas",           "AP": "Amapa",
    "AM": "Amazonas",          "BA": "Bahia",             "CE": "Ceara",
    "DF": "Distrito Federal",  "ES": "Espirito Santo",    "GO": "Goias",
    "MA": "Maranhao",          "MT": "Mato Grosso",       "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais",      "PA": "Para",              "PB": "Paraiba",
    "PR": "Parana",            "PE": "Pernambuco",        "PI": "Piaui",
    "RJ": "Rio de Janeiro",    "RN": "Rio Grande do Norte","RS": "Rio Grande do Sul",
    "RO": "Rondonia",          "RR": "Roraima",           "SC": "Santa Catarina",
    "SP": "Sao Paulo",         "SE": "Sergipe",           "TO": "Tocantins"
}
UF_PATTERN = re.compile(r'/(' + '|'.join(ESTADOS_BR) + r')\b')


def extrair_uf(row: dict) -> str:
    for campo in ("uf", "cidade", "endereco", "descricao", "titulo"):
        texto = (row.get(campo) or "").strip()
        if not texto:
            continue
        uf = texto.upper()
        if len(uf) == 2 and uf in ESTADOS_BR:
            return uf
        m = UF_PATTERN.search(texto)
        if m:
            return m.group(1)
    return ""


def criar_banco(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imoveis (
            id TEXT PRIMARY KEY,
            leiloeiro TEXT,
            junta TEXT,
            site TEXT,
            titulo TEXT,
            descricao TEXT,
            endereco TEXT,
            cidade TEXT,
            uf TEXT,
            lance_inicial REAL,
            avaliacao REAL,
            data_leilao TEXT,
            url TEXT,
            tipo TEXT,
            imagem TEXT,
            importado_em TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_junta ON imoveis(junta)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leiloeiro ON imoveis(leiloeiro)")
    conn.commit()


def importar_csv(conn: sqlite3.Connection, csv_path: Path) -> dict:
    stats = {"inseridos": 0, "duplicados": 0, "erro": 0, "total": 0}
    agora = datetime.now().isoformat(timespec="seconds")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total"] += 1
            uf = extrair_uf(row)

            try:
                lance = float(row.get("lance_inicial") or 0) or None
            except (ValueError, TypeError):
                lance = None
            try:
                aval = float(row.get("avaliacao") or 0) or None
            except (ValueError, TypeError):
                aval = None

            try:
                conn.execute(
                    """INSERT INTO imoveis
                       (id,leiloeiro,junta,site,titulo,descricao,endereco,cidade,uf,
                        lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (row.get("id") or "").strip(),
                        (row.get("leiloeiro") or "").strip(),
                        (row.get("junta") or "").strip(),
                        (row.get("site") or "").strip(),
                        (row.get("titulo") or "").strip(),
                        (row.get("descricao") or "").strip(),
                        (row.get("endereco") or "").strip(),
                        (row.get("cidade") or "").strip(),
                        uf,
                        lance,
                        aval,
                        (row.get("data_leilao") or "").strip() or None,
                        (row.get("url") or "").strip(),
                        (row.get("tipo") or "").strip().upper(),
                        (row.get("imagem") or "").strip() or None,
                        agora,
                    ),
                )
                stats["inseridos"] += 1
            except sqlite3.IntegrityError:
                stats["duplicados"] += 1
            except Exception as e:
                stats["erro"] += 1
                print(f"  [ERRO] row {stats['total']}: {e}", flush=True)

    conn.commit()
    return stats


def relatorio_por_estado(conn: sqlite3.Connection) -> list:
    cur = conn.execute("""
        SELECT
            CASE WHEN uf = '' OR uf IS NULL THEN '(sem UF)' ELSE uf END AS estado,
            COUNT(*) AS total,
            ROUND(AVG(lance_inicial), 2) AS lance_medio,
            MIN(lance_inicial) AS lance_min,
            MAX(lance_inicial) AS lance_max
        FROM imoveis
        GROUP BY estado
        ORDER BY total DESC
    """)
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


def fmt_brl(v):
    if v is None:
        return "          -"
    return f"R$ {v:>12,.0f}"


def imprimir_relatorio(rows: list):
    total_geral = sum(r["total"] for r in rows)
    sep = "-" * 85

    print()
    print("=" * 85)
    print("  RELATORIO - IMOVEIS CAPTADOS - LEILOEIROS REGULARES - 03/06/2026")
    print("=" * 85)
    print(f"  {'UF':<6} {'Estado':<25} {'Qtd':>6}  {'%':>6}  {'Lance Medio':>15}  {'Lance Min':>15}  {'Lance Max':>15}")
    print(sep)

    for r in rows:
        uf = r["estado"]
        nome = ESTADOS_NOME.get(uf, uf)
        pct = r["total"] / total_geral * 100
        print(
            f"  {uf:<6} {nome:<25} {r['total']:>6}  {pct:>5.1f}%  "
            f"{fmt_brl(r['lance_medio'])}  {fmt_brl(r['lance_min'])}  {fmt_brl(r['lance_max'])}"
        )

    print(sep)
    print(f"  {'TOTAL':<6} {'':25} {total_geral:>6}  100.0%")
    print("=" * 85)
    print()

    com_uf = sum(r["total"] for r in rows if r["estado"] != "(sem UF)")
    sem_uf = sum(r["total"] for r in rows if r["estado"] == "(sem UF)")
    print(f"  Imoveis com estado identificado : {com_uf:>5} ({com_uf/total_geral*100:.1f}%)")
    print(f"  Imoveis sem estado identificado : {sem_uf:>5} ({sem_uf/total_geral*100:.1f}%)")
    print()


def main():
    print(f"\n[INFO] Banco : {DB_FILE}", flush=True)
    print(f"[INFO] CSV   : {CSV_FILE}", flush=True)

    conn = sqlite3.connect(DB_FILE)

    criar_banco(conn)

    print("[INFO] Importando CSV...", flush=True)
    stats = importar_csv(conn, CSV_FILE)
    print(f"[OK]   Total no CSV  : {stats['total']}", flush=True)
    print(f"[OK]   Inseridos     : {stats['inseridos']}", flush=True)
    print(f"[OK]   Duplicados    : {stats['duplicados']}", flush=True)
    if stats["erro"]:
        print(f"[WARN] Erros         : {stats['erro']}", flush=True)

    rows = relatorio_por_estado(conn)
    imprimir_relatorio(rows)

    conn.close()
    print(f"[OK]   Banco salvo em: {DB_FILE}", flush=True)
    print()


if __name__ == "__main__":
    main()
