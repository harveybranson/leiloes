# -*- coding: utf-8 -*-
"""
migrar_imagens_anexos.py — cria as tabelas 1→N de fotos e anexos por imóvel.

Referência: captura_dados_leiloes_master.md (Parte VII — "capture TODAS as fotos da
galeria" e "anexos: edital/matrícula/laudo"). Hoje `imoveis.imagem` guarda 1 só URL e
não há onde gravar os PDFs; estas tabelas destravam a captura completa sem perder dados.

Cria (idempotente):
  - imovel_imagens(imovel_id, url, ordem, principal, largura, altura, capturado_em)
  - imovel_anexos (imovel_id, tipo, url, caminho_local, descricao, capturado_em)

E faz backfill de imovel_imagens a partir do que já existe em imoveis.imagem
(marcando-a como principal, ordem 0).

Uso:
  python migrar_imagens_anexos.py                 # cria + backfill no banco padrão
  python migrar_imagens_anexos.py --db outro.db
  python migrar_imagens_anexos.py --sem-backfill   # só cria as tabelas
"""
import argparse
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PADRAO = "imoveis_leiloeiros.db"

DDL_IMAGENS = """
CREATE TABLE IF NOT EXISTS imovel_imagens (
    imovel_id    TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    ordem        INTEGER DEFAULT 0,      -- posição na galeria (0 = primeira)
    principal    INTEGER DEFAULT 0,      -- 1 = foto de capa
    largura      INTEGER,                -- px, quando conhecida (maior resolução)
    altura       INTEGER,
    capturado_em TEXT,
    PRIMARY KEY (imovel_id, url),        -- dedup natural: mesma foto não repete
    FOREIGN KEY (imovel_id) REFERENCES imoveis(id)
);
"""

DDL_ANEXOS = """
CREATE TABLE IF NOT EXISTS imovel_anexos (
    imovel_id     TEXT NOT NULL,
    tipo          TEXT,                  -- edital | matricula | laudo | outro
    url           TEXT NOT NULL,
    caminho_local TEXT,                  -- onde o PDF foi salvo (se baixado)
    descricao     TEXT,
    capturado_em  TEXT,
    PRIMARY KEY (imovel_id, url),
    FOREIGN KEY (imovel_id) REFERENCES imoveis(id)
);
"""

INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_img_imovel ON imovel_imagens(imovel_id)",
    "CREATE INDEX IF NOT EXISTS idx_anx_imovel ON imovel_anexos(imovel_id)",
    "CREATE INDEX IF NOT EXISTS idx_anx_tipo   ON imovel_anexos(tipo)",
]


def migrar(db_path: str, backfill: bool = True) -> None:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    cur.execute(DDL_IMAGENS)
    cur.execute(DDL_ANEXOS)
    for ix in INDICES:
        cur.execute(ix)
    con.commit()
    print(f"[ok] tabelas imovel_imagens / imovel_anexos garantidas em {db_path}")

    if backfill:
        agora = datetime.now().isoformat(timespec="seconds")
        # Só imóveis com imagem não-vazia e que ainda não estão na nova tabela.
        rows = cur.execute(
            """
            SELECT i.id, i.imagem
              FROM imoveis i
             WHERE i.imagem IS NOT NULL AND TRIM(i.imagem) <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM imovel_imagens g WHERE g.imovel_id = i.id
               )
            """
        ).fetchall()
        cur.executemany(
            "INSERT OR IGNORE INTO imovel_imagens "
            "(imovel_id, url, ordem, principal, capturado_em) VALUES (?,?,0,1,?)",
            [(rid, url.strip(), agora) for rid, url in rows],
        )
        con.commit()
        print(f"[ok] backfill: {len(rows)} imagens principais migradas de imoveis.imagem")

    tot_img = cur.execute("SELECT COUNT(*) FROM imovel_imagens").fetchone()[0]
    tot_anx = cur.execute("SELECT COUNT(*) FROM imovel_anexos").fetchone()[0]
    print(f"[info] imovel_imagens={tot_img}  imovel_anexos={tot_anx}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Cria tabelas 1→N de imagens e anexos.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--sem-backfill", action="store_true",
                    help="não migra imoveis.imagem para imovel_imagens")
    args = ap.parse_args()
    migrar(args.db, backfill=not args.sem_backfill)
