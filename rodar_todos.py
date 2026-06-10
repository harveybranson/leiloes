# -*- coding: utf-8 -*-
"""
rodar_todos.py — refaz o scraping de TODOS os leiloeiros (todos os scraper_*.py).
==================================================================================

Auto-descobre cada `scraper_*.py` deste diretorio (menos os modulos auxiliares),
roda um por vez em subprocess com timeout, e mede quantos imoveis novos cada um
trouxe contando COUNT(*) no SQLite (imoveis_leiloeiros.db) antes/depois.

Escreve um relatorio AO VIVO em `scraping_resultado.json` (atualizado apos cada
scraper) que a pagina do site le para mostrar o total encontrado e habilitar o
botao de importacao para o Postgres.

Ao final, chama `exportar_para_jsonl.py` para gerar o JSONL consolidado que o
endpoint de importacao do site consome (`run.py importar-scraping`).

Uso:
  python rodar_todos.py                 # roda todos
  python rodar_todos.py --timeout 900   # timeout por scraper (s), default 900
  python rodar_todos.py --only scraper_acre.py scraper_milan.py
  python rodar_todos.py --skip scraper_jucisrs.py
  python rodar_todos.py --list          # so lista os scrapers descobertos
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
DB_SQLITE = BASE / "imoveis_leiloeiros.db"
RESULTADO = BASE / "scraping_resultado.json"
DIR_LOG = BASE / "logs"

# Modulos auxiliares / nao-coletores: nunca rodar como scraper.
SKIP_SEMPRE = {
    "scraper_commons.py",   # biblioteca compartilhada (helpers)
    "scraper_detalhe.py",   # enriquecedor de detalhe, depende de listagem previa
    "scraper_enrich.py",    # enriquecedor de documentos
}

TIMEOUT_DEFAULT = 900  # 15 min por scraper


def descobrir_scrapers():
    nomes = sorted(p.name for p in BASE.glob("scraper_*.py"))
    return [n for n in nomes if n not in SKIP_SEMPRE]


def contar_total():
    try:
        with sqlite3.connect(str(DB_SQLITE)) as c:
            return int(c.execute("SELECT COUNT(*) FROM imoveis").fetchone()[0])
    except Exception:
        return 0


def escrever_resultado(estado):
    """Grava o relatorio ao vivo. Tolerante a falha: o arquivo de status nunca
    deve derrubar a rodada. Tenta escrita atomica (.tmp + replace); se o OneDrive/
    AV travar o arquivo (WinError 5), faz retry e por fim escreve direto."""
    conteudo = json.dumps(estado, ensure_ascii=False, indent=2)
    tmp = RESULTADO.with_suffix(".json.tmp")
    for tentativa in range(4):
        try:
            tmp.write_text(conteudo, encoding="utf-8")
            tmp.replace(RESULTADO)
            return
        except PermissionError:
            time.sleep(0.5 * (tentativa + 1))  # OneDrive solta o lock em ~1-2s
        except Exception:
            break
    # Fallback: escreve direto (nao-atomico, mas melhor que perder o status).
    try:
        RESULTADO.write_text(conteudo, encoding="utf-8")
    except Exception as e:
        print(f"[todos] aviso: nao consegui gravar {RESULTADO.name}: {e}", flush=True)


def rodar_scraper(script, timeout, log_path):
    caminho = BASE / script
    antes = contar_total()
    t0 = time.time()
    status, erro = "OK", ""
    cmd = [sys.executable, "-u", str(caminho)]
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n{'='*72}\n== {script} — inicio {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}\n")
            logf.flush()
            r = subprocess.run(cmd, cwd=str(BASE), stdout=logf,
                               stderr=subprocess.STDOUT, timeout=timeout)
        if r.returncode != 0:
            status, erro = "FALHOU", f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        status, erro = "TIMEOUT", f">{timeout//60}min"
    except Exception as e:
        status, erro = "FALHOU", f"{type(e).__name__}: {e}"

    depois = contar_total()
    novos = max(depois - antes, 0)
    if status == "OK" and novos == 0:
        status = "SEM_NOVOS"
    return {
        "script": script, "status": status, "novos": novos,
        "antes": antes, "depois": depois,
        "duracao_s": int(time.time() - t0), "erro": erro,
    }


def main():
    ap = argparse.ArgumentParser(description="Roda todos os scrapers de leiloeiros")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT, help="segundos por scraper")
    ap.add_argument("--only", nargs="*", help="rodar apenas estes scripts")
    ap.add_argument("--skip", nargs="*", default=[], help="pular estes scripts")
    ap.add_argument("--list", action="store_true", help="lista os scrapers e sai")
    ap.add_argument("--sem-export", action="store_true", help="nao gerar o JSONL ao final")
    args = ap.parse_args()

    scrapers = args.only if args.only else descobrir_scrapers()
    scrapers = [s for s in scrapers if s not in set(args.skip) and s not in SKIP_SEMPRE]

    if args.list:
        print(f"{len(scrapers)} scrapers descobertos:")
        for s in scrapers:
            print(" -", s)
        return 0

    DIR_LOG.mkdir(exist_ok=True)
    data_iso = datetime.now().strftime("%Y-%m-%d_%H%M")
    log_path = DIR_LOG / f"rodar_todos_{data_iso}.log"

    total_inicial = contar_total()
    estado = {
        "running": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "timeout_s": args.timeout,
        "total_scrapers": len(scrapers),
        "concluidos": 0,
        "total_sqlite_inicial": total_inicial,
        "total_sqlite": total_inicial,
        "novos_total": 0,
        "jsonl": None,
        "scrapers": [],
        "atual": None,
    }
    escrever_resultado(estado)

    print(f"[todos] {len(scrapers)} scrapers, timeout {args.timeout//60}min cada — log: {log_path}", flush=True)
    t0 = time.time()
    resultados = []
    for i, script in enumerate(scrapers, 1):
        print(f"[todos] ({i}/{len(scrapers)}) >> {script}", flush=True)
        estado["atual"] = script
        escrever_resultado(estado)

        res = rodar_scraper(script, args.timeout, log_path)
        resultados.append(res)

        estado["scrapers"] = resultados
        estado["concluidos"] = i
        estado["total_sqlite"] = res["depois"]
        estado["novos_total"] = max(res["depois"] - total_inicial, 0)
        estado["atual"] = None
        escrever_resultado(estado)

        print(f"[todos] ({i}/{len(scrapers)}) << {script}: {res['status']} "
              f"novos={res['novos']} {res['duracao_s']//60}min{res['duracao_s']%60:02d}s {res['erro']}", flush=True)

    # Exporta o JSONL consolidado para o site importar
    jsonl_rel = None
    if not args.sem_export:
        try:
            from exportar_para_jsonl import exportar
            jsonl_path, n = exportar()
            jsonl_rel = str(jsonl_path)
            print(f"[todos] JSONL consolidado: {jsonl_path} ({n} registros)", flush=True)
        except Exception as e:
            print(f"[todos] EXPORT falhou: {type(e).__name__}: {e}", flush=True)

    estado["running"] = False
    estado["finished_at"] = datetime.now().isoformat(timespec="seconds")
    estado["duracao_total_s"] = int(time.time() - t0)
    estado["jsonl"] = jsonl_rel
    escrever_resultado(estado)

    novos = estado["novos_total"]
    print("\n" + "=" * 60, flush=True)
    print(f"[todos] FIM. Total no SQLite: {estado['total_sqlite']} | Novos nesta rodada: {novos} "
          f"| Duracao: {estado['duracao_total_s']//60}min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
