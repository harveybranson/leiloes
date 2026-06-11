# -*- coding: utf-8 -*-
"""
finalizar_coleta.py — etapa de pós-coleta (gate de qualidade + observabilidade).

Referência: captura_dados_leiloes_master.md (Parte XI "no fim de cada coleta..."). Encadeia,
nesta ordem, o que antes era manual:

  1. snapshot_cobertura  — grava o snapshot do dia e ALERTA sobre regressões (redesign).
  2. check_cobertura     — GATE DURO: se um campo crítico cair abaixo do limite, falha.
  3. gerar_dashboard     — regenera dashboard_frescor.html.

Se o gate falhar, retorna código ≠ 0 — assim o agendador/CI **aborta o commit do banco**
em vez de publicar dados degradados. As regressões de frescor são aviso (não travam por
padrão; use --strict-regressao para também travar nelas).

Uso:
  python finalizar_coleta.py
  python finalizar_coleta.py --desde 2026-06-11        # avalia só o que entrou hoje
  python finalizar_coleta.py --strict-regressao        # regressão também falha o gate
  python run_scraper.py --finalize                     # roda a coleta e depois isto
"""
import argparse
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import check_cobertura
import gerar_dashboard_frescor
import snapshot_cobertura


def finalizar(db="imoveis_leiloeiros.db", desde=None, out_html="dashboard_frescor.html",
              strict_regressao=False, limites=None):
    """Roda snapshot + gate de cobertura + dashboard. Retorna 0 se OK, 1 se reprovar."""
    print("=" * 60)
    print("  FINALIZAÇÃO DE COLETA — gate de qualidade")
    print("=" * 60)

    # 1. Snapshot + regressão (aviso) ----------------------------------------
    atual = snapshot_cobertura.snapshot(db)
    anterior = snapshot_cobertura.ultimo_snapshot(snapshot_cobertura.HIST_PADRAO)
    regs = snapshot_cobertura.regressoes(anterior, atual, limite=15.0, min_volume=20)
    with open(snapshot_cobertura.HIST_PADRAO, "a", encoding="utf-8") as f:
        import json
        f.write(json.dumps(atual, ensure_ascii=False) + "\n")
    if regs:
        print(f"\n  ⚠ {len(regs)} regressão(ões) de cobertura (provável redesign):")
        for r in regs[:10]:
            print(f"    {r['leiloeiro'][:32]:<32} {r['campo']:<13} "
                  f"{r['antes']:.0f}%→{r['agora']:.0f}% (−{r['queda']:.0f}pp)")
    else:
        print("\n  ✓ sem regressões de cobertura vs. snapshot anterior.")

    # 2. Gate de cobertura ----------------------------------------------------
    linhas = check_cobertura._carregar_db(db, desde)
    lim = limites or check_cobertura.LIMITES_PADRAO
    cob, total = check_cobertura.cobertura(linhas, list(lim))
    falhas = {c: cob[c] for c in lim if cob[c] < lim[c]}

    if total == 0 and desde:
        # Nada entrou no período: não há o que validar — não reprova por 0% espúrio.
        print(f"\n  • Nenhum imóvel importado desde {desde} — gate de cobertura pulado.")
        falhas = {}
    else:
        print(f"\n  Cobertura ({total} imóveis"
              + (f", desde {desde}" if desde else "") + "):")
        for c in lim:
            marca = "✗" if c in falhas else "✓"
            print(f"    {marca} {c:<15} {cob[c]:6.1f}%  (mín {lim[c]:.0f}%)")

    # 3. Dashboard ------------------------------------------------------------
    dados = gerar_dashboard_frescor.coletar(db)
    html = gerar_dashboard_frescor.HTML.replace(
        "__DADOS__", __import__("json").dumps(dados, ensure_ascii=False))
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  → {out_html} regenerado ({dados['total']} imóveis).")

    # Veredito ----------------------------------------------------------------
    reprovou = bool(falhas) or (strict_regressao and bool(regs))
    print("\n" + "-" * 60)
    if reprovou:
        motivos = []
        if falhas:
            motivos.append("campos abaixo do limite: "
                           + ", ".join(f"{c} ({v:.1f}%)" for c, v in falhas.items()))
        if strict_regressao and regs:
            motivos.append(f"{len(regs)} regressão(ões)")
        print("  ✗ GATE REPROVADO — " + "; ".join(motivos))
        print("    NÃO commitar/publicar o banco até investigar.")
    else:
        print("  ✓ GATE APROVADO — coleta dentro dos limites de qualidade.")
    print("-" * 60)
    return 1 if reprovou else 0


def main():
    ap = argparse.ArgumentParser(description="Pós-coleta: gate de qualidade + dashboard.")
    ap.add_argument("--db", default="imoveis_leiloeiros.db")
    ap.add_argument("--desde", default=None,
                    help="avalia só importado_em >= AAAA-MM-DD (use 'hoje' p/ a data atual)")
    ap.add_argument("--out", default="dashboard_frescor.html")
    ap.add_argument("--strict-regressao", action="store_true")
    args = ap.parse_args()
    desde = date.today().isoformat() if args.desde == "hoje" else args.desde
    sys.exit(finalizar(args.db, desde, args.out, args.strict_regressao))


if __name__ == "__main__":
    main()
