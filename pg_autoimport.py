# -*- coding: utf-8 -*-
"""
pg_autoimport.py — helper de importacao automatica para o PostgreSQL do site.
==============================================================================
Chamado ao FINAL de cada scraper standalone deste diretorio (apos gerar o CSV),
para garantir que os imoveis cheguem ao banco que a LISTAGEM DO SITE le — e nao
fiquem apenas no SQLite local (ver secoes 46 e 48 do guia captura_dados_leiloes_v2.md).

Reusa o importador canonico `importar_site.py` (grava em AMBOS os bancos, dedup
global por url_original, e usa `docker exec leilao_postgres` — NUNCA localhost:5432).

Regra de ouro: uma falha de Docker/container NAO pode abortar o scraping. Se o
import automatico falhar, os imoveis continuam no SQLite/CSV e a funcao apenas
avisa com o comando manual a rodar depois.
"""
import sys
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent


def importar_para_site(csv_path, junta, url_base=""):
    """Empurra o CSV gerado para o PostgreSQL do site via importar_site.py.

    `junta` vem no formato 'FONTE/UF' (ex.: 'JUCEP/PB') — dele derivamos
    --fonte (JUCEP) e --estado-padrao (PB). Idempotente: re-rodar nao duplica.
    Retorna True se o import rodou com sucesso (rc=0), False caso contrario.
    """
    csv_path = Path(csv_path)
    fonte = junta.split("/")[0].strip() or junta
    estado = junta.split("/")[-1].strip()[:2]
    cmd = [sys.executable, str(BASE / "importar_site.py"), "--csv", str(csv_path),
           "--fonte", fonte, "--junta", junta, "--estado-padrao", estado]
    if url_base:
        cmd += ["--url-base", url_base]
    manual = (f"python importar_site.py --csv {csv_path} --fonte {fonte} "
              f"--junta \"{junta}\" --estado-padrao {estado}")
    print(f"[PG] importando {csv_path.name} para o site (fonte={fonte})...", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except Exception as e:
        print(f"[PG] import automatico indisponivel: {type(e).__name__}: {e}\n"
              f"     Imoveis no SQLite/CSV. Rode manualmente: {manual}", flush=True)
        return False
    if r.stdout:
        print(r.stdout.strip(), flush=True)
    if r.returncode != 0:
        print(f"[PG] import automatico FALHOU (rc={r.returncode}). "
              f"Imoveis no SQLite/CSV. Rode manualmente: {manual}", flush=True)
        if r.stderr:
            print("     " + r.stderr.strip()[:500], flush=True)
        return False
    return True
