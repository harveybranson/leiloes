# -*- coding: utf-8 -*-
"""
rodar_semanal.py — agente semanal que roda todos os scrapers estaveis.
=======================================================================

Roda toda segunda-feira 09:00 (BRT) via Windows Task Scheduler (ver
agendar_semanal.ps1). Para cada scraper estavel deste diretorio:

  1. Le COUNT(*) em imoveis WHERE junta=? do SQLite local (antes).
  2. Executa o scraper (subprocess) com timeout de 45 min.
  3. Le COUNT(*) novamente (depois). delta = imoveis novos da rodada.
  4. Grava status: OK / TIMEOUT / FALHOU / SEM_NOVOS.

Cada scraper ja chama pg_autoimport.importar_para_site no fim, entao o
PostgreSQL do site (container leilao_postgres) e atualizado automaticamente.
Aqui so orquestramos, contamos delta no SQLite e geramos o relatorio.

Saidas:
  - relatorios/relatorio_semanal_YYYY-MM-DD.md  (relatorio completo)
  - captura_dados_leiloes_v2.md                 (sessao nova datada anexada)
  - logs/semanal_YYYY-MM-DD.log                 (stdout/stderr de cada scraper)
"""
import os
import sys
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
DB_SQLITE = BASE / "imoveis_leiloeiros.db"
GUIA = BASE / "captura_dados_leiloes_v2.md"
DIR_REL = BASE / "relatorios"
DIR_LOG = BASE / "logs"

TIMEOUT_SCRAPER = 45 * 60  # 45 min por scraper

# Scrapers estaveis ja consolidados no banco principal. Mantenha em ordem
# alfabetica de UF para o relatorio sair previsivel.
SCRAPERS = [
    {"uf": "AC",    "nome": "Acre",                 "script": "scraper_acre.py",  "junta": "JUCEAC/AC"},
    {"uf": "AL",    "nome": "Alagoas",              "script": "scraper_al.py",    "junta": "JUCEAL/AL"},
    {"uf": "BA",    "nome": "Bahia",                "script": "scraper_bahia.py", "junta": "JUCEB/BA"},
    {"uf": "CE",    "nome": "Ceara",                "script": "scraper_ce.py",    "junta": "JUCEC/CE"},
    {"uf": "MA",    "nome": "Maranhao",             "script": "scraper_ma.py",    "junta": "JUCEMA/MA"},
    {"uf": "PA",    "nome": "Para",                 "script": "scraper_para.py",  "junta": "JUCEPA/PA"},
    {"uf": "PB",    "nome": "Paraiba",              "script": "scraper_pb.py",    "junta": "JUCEP/PB"},
    {"uf": "PE",    "nome": "Pernambuco",           "script": "scraper_pe.py",    "junta": "JUCEPE/PE"},
    {"uf": "PI",    "nome": "Piaui",                "script": "scraper_pi.py",    "junta": "JUCEPI/PI"},
    {"uf": "RN",    "nome": "Rio Grande do Norte",  "script": "scraper_rn.py",    "junta": "JUCERN/RN"},
    {"uf": "RR-RO", "nome": "Roraima/Rondonia",     "script": "scraper_rr_ro.py", "junta": "JUCER/RR-RO"},
    {"uf": "SE",    "nome": "Sergipe",              "script": "scraper_se.py",    "junta": "JUCESE/SE"},
]


def contar_imoveis(junta):
    """Conta imoveis no SQLite local filtrando por junta. 0 se erro."""
    try:
        with sqlite3.connect(str(DB_SQLITE)) as c:
            row = c.execute("SELECT COUNT(*) FROM imoveis WHERE junta = ?", (junta,)).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def contar_total_imoveis():
    try:
        with sqlite3.connect(str(DB_SQLITE)) as c:
            return int(c.execute("SELECT COUNT(*) FROM imoveis").fetchone()[0])
    except Exception:
        return 0


def rodar_scraper(scr, log_path):
    """Roda 1 scraper. Retorna dict com status, novos, duracao_s, erro."""
    script = BASE / scr["script"]
    if not script.exists():
        return {"status": "AUSENTE", "novos": 0, "duracao_s": 0, "erro": f"script nao encontrado: {scr['script']}"}

    antes = contar_imoveis(scr["junta"])
    t0 = time.time()
    erro = ""
    status = "OK"

    cmd = [sys.executable, "-u", str(script)]
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n{'='*72}\n== {scr['nome']} ({scr['junta']}) — inicio {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*72}\n")
            logf.flush()
            r = subprocess.run(
                cmd, cwd=str(BASE), stdout=logf, stderr=subprocess.STDOUT,
                timeout=TIMEOUT_SCRAPER,
            )
        if r.returncode != 0:
            status = "FALHOU"
            erro = f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        status = "TIMEOUT"
        erro = f">{TIMEOUT_SCRAPER//60}min"
    except Exception as e:
        status = "FALHOU"
        erro = f"{type(e).__name__}: {e}"

    duracao = int(time.time() - t0)
    depois = contar_imoveis(scr["junta"])
    novos = max(depois - antes, 0)
    if status == "OK" and novos == 0:
        status = "SEM_NOVOS"

    return {
        "status": status, "novos": novos, "duracao_s": duracao,
        "antes": antes, "depois": depois, "erro": erro,
    }


def gerar_sugestoes(resultados):
    """Heuristicas leves para apontar pontos de melhoria a partir do resultado."""
    sug = []
    timeouts = [r for r in resultados if r["status"] == "TIMEOUT"]
    falhas   = [r for r in resultados if r["status"] == "FALHOU"]
    ausentes = [r for r in resultados if r["status"] == "AUSENTE"]
    sem_novos = [r for r in resultados if r["status"] == "SEM_NOVOS"]
    longos   = [r for r in resultados if r["duracao_s"] >= 30 * 60 and r["status"] not in ("TIMEOUT", "FALHOU")]

    if timeouts:
        nomes = ", ".join(r["nome"] for r in timeouts)
        sug.append(f"- **Timeouts** em {nomes}. Acoes possiveis: (a) dividir o scraper em batches por leiloeiro, (b) reduzir profundidade de paginacao, (c) aumentar `TIMEOUT_SCRAPER` em `rodar_semanal.py` se a janela permitir.")
    if falhas:
        nomes = ", ".join(f"{r['nome']} ({r['erro']})" for r in falhas)
        sug.append(f"- **Falhas com codigo de saida nao-zero**: {nomes}. Conferir o log da rodada (`logs/semanal_*.log`) e o `*_progress.json` correspondente para retomar.")
    if ausentes:
        nomes = ", ".join(r["nome"] for r in ausentes)
        sug.append(f"- **Scripts ausentes**: {nomes}. Restaurar do git ou remover da lista `SCRAPERS` em `rodar_semanal.py`.")
    if sem_novos:
        nomes = ", ".join(r["nome"] for r in sem_novos)
        sug.append(f"- **Zero imoveis novos** em {nomes}. Validar se os portais subjacentes mudaram de layout (selectors quebrados), se a 1a praca esta no passado (filtro), ou se ja estamos saturados de dedup global.")
    if longos:
        nomes = ", ".join(f"{r['nome']} ({r['duracao_s']//60}min)" for r in longos)
        sug.append(f"- **Rodadas longas (>=30min)**: {nomes}. Considerar cache de paginas estaticas, paralelismo controlado por dominio ou reuso de sessao Playwright.")
    if not sug:
        sug.append("- Nenhuma anomalia detectada nesta rodada. Manter cadencia atual.")
    return sug


def escrever_relatorio(data_iso, resultados, total_antes, total_depois, duracao_total_s):
    DIR_REL.mkdir(exist_ok=True)
    rel_path = DIR_REL / f"relatorio_semanal_{data_iso}.md"
    novos_total = sum(r["novos"] for r in resultados)
    novos_db_total = max(total_depois - total_antes, 0)

    linhas = []
    linhas.append(f"# Relatorio semanal — {data_iso}\n")
    linhas.append(f"**Inicio:** {datetime.now():%Y-%m-%d %H:%M:%S}  ")
    linhas.append(f"**Duracao total:** {duracao_total_s//60} min {duracao_total_s%60}s  ")
    linhas.append(f"**Imoveis novos somando por junta:** {novos_total}  ")
    linhas.append(f"**Imoveis novos no SQLite (total absoluto):** {novos_db_total}  ")
    linhas.append(f"**Total de imoveis no SQLite apos rodada:** {total_depois}\n")

    linhas.append("## Por scraper\n")
    linhas.append("| UF | Scraper | Status | Novos | Antes | Depois | Duracao | Erro |")
    linhas.append("|---|---|---|---:|---:|---:|---|---|")
    for r in resultados:
        dur = f"{r['duracao_s']//60}min{r['duracao_s']%60:02d}s"
        linhas.append(f"| {r['uf']} | {r['nome']} | {r['status']} | {r['novos']} | {r['antes']} | {r['depois']} | {dur} | {r['erro'] or '-'} |")
    linhas.append("")

    linhas.append("## Sugestoes de correcoes e melhorias\n")
    linhas += gerar_sugestoes(resultados)
    linhas.append("")
    linhas.append("## Logs\n")
    linhas.append(f"- stdout/stderr de cada scraper: `logs/semanal_{data_iso}.log`")
    linhas.append("")

    rel_path.write_text("\n".join(linhas), encoding="utf-8")
    return rel_path, novos_total, novos_db_total


def anexar_no_guia(data_iso, rel_path, resultados, novos_total, novos_db_total, duracao_total_s):
    """Anexa secao datada ao captura_dados_leiloes_v2.md com sumario + sugestoes."""
    if not GUIA.exists():
        return
    sucesso = sum(1 for r in resultados if r["status"] == "OK")
    sem_novos = sum(1 for r in resultados if r["status"] == "SEM_NOVOS")
    timeouts = sum(1 for r in resultados if r["status"] == "TIMEOUT")
    falhas = sum(1 for r in resultados if r["status"] == "FALHOU")
    ausentes = sum(1 for r in resultados if r["status"] == "AUSENTE")

    cabec = f"\n\n## RELATORIO SEMANAL AUTOMATICO — {data_iso}\n"
    corpo = [
        f"- Scrapers executados: **{len(resultados)}** (ok: {sucesso}, sem novos: {sem_novos}, timeouts: {timeouts}, falhas: {falhas}, ausentes: {ausentes})",
        f"- Imoveis novos (soma por junta): **{novos_total}**",
        f"- Imoveis novos no SQLite (total absoluto): **{novos_db_total}**",
        f"- Duracao total: **{duracao_total_s//60} min**",
        f"- Relatorio completo: [`{rel_path.relative_to(BASE).as_posix()}`]({rel_path.relative_to(BASE).as_posix()})",
        "",
        "### Sugestoes desta rodada",
        "",
    ] + gerar_sugestoes(resultados) + [""]

    with open(GUIA, "a", encoding="utf-8") as f:
        f.write(cabec + "\n".join(corpo))


def main():
    data_iso = datetime.now().strftime("%Y-%m-%d")
    DIR_REL.mkdir(exist_ok=True)
    DIR_LOG.mkdir(exist_ok=True)
    log_path = DIR_LOG / f"semanal_{data_iso}.log"

    print(f"[semanal] inicio em {datetime.now():%Y-%m-%d %H:%M:%S} — log: {log_path}", flush=True)
    print(f"[semanal] {len(SCRAPERS)} scrapers, timeout {TIMEOUT_SCRAPER//60}min cada", flush=True)

    total_antes = contar_total_imoveis()
    t0 = time.time()
    resultados = []
    for scr in SCRAPERS:
        print(f"[semanal] >> {scr['nome']} ({scr['junta']})", flush=True)
        res = rodar_scraper(scr, log_path)
        res.update({"uf": scr["uf"], "nome": scr["nome"], "junta": scr["junta"]})
        resultados.append(res)
        print(f"[semanal] << {scr['nome']}: {res['status']} novos={res['novos']} {res['duracao_s']//60}min{res['duracao_s']%60:02d}s {res['erro']}", flush=True)

    duracao_total = int(time.time() - t0)
    total_depois = contar_total_imoveis()
    rel_path, novos_total, novos_db_total = escrever_relatorio(
        data_iso, resultados, total_antes, total_depois, duracao_total
    )
    anexar_no_guia(data_iso, rel_path, resultados, novos_total, novos_db_total, duracao_total)

    print("\n" + "=" * 60, flush=True)
    print(f"[semanal] FIM. Novos por junta: {novos_total} | Novos no SQLite: {novos_db_total} | Duracao: {duracao_total//60}min", flush=True)
    print(f"[semanal] Relatorio: {rel_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
