# -*- coding: utf-8 -*-
"""
Aprovação -> inserção dos anúncios em STAGING nos DOIS bancos.

Uso:
  python aprovar_anuncios.py --listar              # mostra os pendentes
  python aprovar_anuncios.py --aprovar-todos       # aprova todos os pendentes
  python aprovar_anuncios.py --aprovar aprovados.txt   # aprova só as URLs do arquivo
  python aprovar_anuncios.py --inserir             # insere os aprovados nos 2 bancos

Bancos:
  - SQLite  : imoveis_leiloeiros.db (tabela imoveis)
  - Postgres: leilao_db via JSONL + `run.py importar-scraping` (no container leilao_worker)
NÃO deduplica anúncios: insere todos os aprovados (a checagem de URL é só p/ evitar repetir a
mesma inserção, não para descartar anúncios distintos).
"""
import argparse, csv, hashlib, json, sqlite3, subprocess, sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
STAGING_DB = BASE / "staging.db"
SQLITE_MAIN = BASE / "imoveis_leiloeiros.db"
MOUNT_SCRAPING = Path(r"C:\Users\arthur\leilao-scraper\scraping")


def _con():
    return sqlite3.connect(STAGING_DB)


def listar():
    cn = _con()
    rows = cn.execute("SELECT status,aprovado,count(*) FROM staging_imoveis GROUP BY status,aprovado").fetchall()
    print("Staging:")
    for st, ap, n in rows:
        print(f"  {st} aprovado={ap}: {n}")
    print("\nPendentes (amostra):")
    for r in cn.execute("SELECT titulo,cidade,uf,preco,data_leilao,url FROM staging_imoveis WHERE aprovado=0 LIMIT 15"):
        print(f"  - {r[4]} | {(r[0] or '')[:42]} | {r[1]}/{r[2]} | R$ {r[3]}")
    cn.close()


def aprovar(urls=None, todos=False):
    cn = _con()
    if todos:
        n = cn.execute("UPDATE staging_imoveis SET aprovado=1 WHERE status='NOVO'").rowcount
    else:
        n = 0
        for u in urls:
            n += cn.execute("UPDATE staging_imoveis SET aprovado=1 WHERE url=?", (u.strip(),)).rowcount
    cn.commit(); cn.close()
    print(f"{n} anúncios marcados como aprovados.")


def to_real(s):
    try:
        return float(str(s).replace(".", "").replace(",", ".")) if s else None
    except Exception:
        return None


def inserir():
    cn = _con()
    aprov = cn.execute("SELECT * FROM staging_imoveis WHERE aprovado=1").fetchall()
    cols = [c[1] for c in cn.execute("PRAGMA table_info(staging_imoveis)")]
    items = [dict(zip(cols, r)) for r in aprov]
    cn.close()
    if not items:
        print("Nenhum anúncio aprovado para inserir. Rode --aprovar-todos ou --aprovar <arquivo>.")
        return

    # 1) SQLite imoveis_leiloeiros.db
    sc = sqlite3.connect(SQLITE_MAIN); cur = sc.cursor()
    ins_sql = 0
    for it in items:
        rid = hashlib.md5((it["url"] or it["titulo"]).encode("utf-8")).hexdigest()[:16]
        cur.execute("SELECT 1 FROM imoveis WHERE url=? LIMIT 1", (it["url"],))
        if cur.fetchone():
            continue
        cur.execute("""INSERT INTO imoveis(id,leiloeiro,junta,site,titulo,descricao,endereco,
            cidade,uf,lance_inicial,avaliacao,data_leilao,url,tipo,imagem,importado_em)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, it["leiloeiro"], "STAGING", it["site"], it["titulo"], it.get("descricao", ""),
             "", it["cidade"], it["uf"], it.get("lance_inicial"), None, it["data_leilao"],
             it["url"], "imovel", it["imagem"], datetime.now().isoformat()))
        ins_sql += 1
    sc.commit(); sc.close()
    print(f"[SQLite] {ins_sql} inseridos.")

    # 2) Postgres via JSONL + importar-scraping (com docs em arquivos)
    jsonl = MOUNT_SCRAPING / f"staging_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for it in items:
            fotos = json.loads(it.get("fotos") or "[]")
            anexos = json.loads(it.get("anexos") or "[]")
            rec = {
                "titulo": it["titulo"], "url": it["url"],
                "tipo_imovel": (it.get("tipo") or "outro"),
                "fotos": fotos, "cidade": it["cidade"], "estado": it["uf"],
                "preco": to_real(it.get("preco")), "nome_anunciante": it["leiloeiro"],
                "descricao_completa": (it.get("descricao") or "")[:4000],
                "arquivos": anexos, "edital_url": it.get("edital") or None,
                "matricula_url": it.get("matricula") or None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    nome = jsonl.name
    print(f"[Postgres] JSONL gerado: {nome}. Importando no container...")
    # docker cp (bind-mount do Docker Desktop costuma ficar obsoleto) + import com upsert
    subprocess.run(["docker", "cp", str(jsonl), f"leilao_worker:/app/scraping/{nome}"],
                   check=False)
    r = subprocess.run(["docker", "exec", "leilao_worker", "python", "run.py",
                        "importar-scraping", "--arquivo", f"scraping/{nome}", "--upsert"],
                       capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        if any(k in line for k in ("Total lidos", "Inseridos", "Atualizados", "Duplicados")):
            print("   " + line.strip())
    print("[OK] Inserção concluída nos 2 bancos.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--aprovar-todos", action="store_true")
    ap.add_argument("--aprovar", metavar="ARQUIVO")
    ap.add_argument("--inserir", action="store_true")
    a = ap.parse_args()
    if a.listar:
        listar()
    elif a.aprovar_todos:
        aprovar(todos=True)
    elif a.aprovar:
        aprovar(urls=open(a.aprovar, encoding="utf-8").read().splitlines())
    elif a.inserir:
        inserir()
    else:
        listar()
        print("\nComandos: --aprovar-todos | --aprovar aprovados.txt | --inserir")
