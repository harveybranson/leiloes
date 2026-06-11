# -*- coding: utf-8 -*-
"""
check_cobertura.py — validador de cobertura de campos (gate de qualidade do pipeline).

Referência: captura_dados_leiloes_master.md (Parte X — Definition of Done e Parte IX.4
"% de campos None subiu → sinal de redesign"). Transforma a DoD em teste executável:
roda ao fim de uma coleta e SAI COM CÓDIGO ≠ 0 se algum campo crítico ficar abaixo do
limite mínimo de preenchimento — assim trava CI/agendador antes de gravar lixo em massa.

Lê do banco SQLite (tabela imoveis) e/ou de um CSV de ofertas. Considera vazio tanto
NULL quanto string vazia/espaços. Imagens podem vir de imovel_imagens (1→N).

Uso:
  python check_cobertura.py                          # banco padrão, limites padrão
  python check_cobertura.py --db imoveis_leiloeiros.db --por-leiloeiro
  python check_cobertura.py --csv csv/ofertas.csv
  python check_cobertura.py --min-global 80 --min titulo=95 --min imagem=70
  python check_cobertura.py --desde 2026-06-08      # só o que foi importado a partir de
  python check_cobertura.py --json                  # saída JSON (para CI)

Código de saída: 0 = tudo acima do limite; 1 = algum campo crítico abaixo; 2 = erro.
"""
import argparse
import csv as csvmod
import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PADRAO = "imoveis_leiloeiros.db"

# Campos críticos e limite mínimo de preenchimento (% ) — alinhado à DoD da Parte X.
LIMITES_PADRAO = {
    "titulo": 95.0,
    "descricao": 70.0,
    "cidade": 60.0,
    "uf": 90.0,
    "lance_inicial": 80.0,
    "data_leilao": 60.0,
    "imagem": 70.0,
    "url": 99.0,
}
# Demais colunas medidas mas sem gate (informativas).
INFORMATIVOS = ["leiloeiro", "site", "endereco", "avaliacao", "tipo"]


def _vazio(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _carregar_db(db_path, desde=None):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    where, params = "", []
    if desde:
        where = "WHERE importado_em >= ?"
        params.append(desde)
    linhas = [dict(r) for r in con.execute(f"SELECT * FROM imoveis {where}", params)]

    # Imagem efetiva: a coluna imoveis.imagem OU qualquer linha em imovel_imagens.
    tem_galeria = {
        r[0] for r in con.execute(
            "SELECT DISTINCT imovel_id FROM imovel_imagens"
        )
    } if _tabela_existe(con, "imovel_imagens") else set()
    for ln in linhas:
        if _vazio(ln.get("imagem")) and ln.get("id") in tem_galeria:
            ln["imagem"] = "<galeria>"
    con.close()
    return linhas


def _tabela_existe(con, nome):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)
    ).fetchone() is not None


def _carregar_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csvmod.DictReader(f))


def cobertura(linhas, campos):
    """% de linhas com cada campo preenchido."""
    total = len(linhas)
    if total == 0:
        return {c: 0.0 for c in campos}, 0
    out = {}
    for c in campos:
        preenchidos = sum(1 for ln in linhas if not _vazio(ln.get(c)))
        out[c] = round(100.0 * preenchidos / total, 1)
    return out, total


def main():
    ap = argparse.ArgumentParser(description="Valida cobertura de campos.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--csv", help="valida um CSV em vez do banco")
    ap.add_argument("--desde", help="filtra importado_em >= AAAA-MM-DD (só banco)")
    ap.add_argument("--por-leiloeiro", action="store_true",
                    help="também quebra a cobertura por leiloeiro")
    ap.add_argument("--min-global", type=float, default=None,
                    help="limite mínimo único para todos os campos críticos")
    ap.add_argument("--min", action="append", default=[],
                    help="override por campo, ex.: --min titulo=95")
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    ap.add_argument("--gate-leiloeiro", action="store_true",
                    help="também falha se um leiloeiro despencar vs. a média (pega redesign)")
    ap.add_argument("--margem", type=float, default=40.0,
                    help="com --gate-leiloeiro: pp abaixo do global p/ flagar (padrão 40)")
    ap.add_argument("--min-volume", type=int, default=20,
                    help="com --gate-leiloeiro: ignora leiloeiros com menos de N imóveis")
    args = ap.parse_args()

    limites = dict(LIMITES_PADRAO)
    if args.min_global is not None:
        limites = {k: args.min_global for k in limites}
    for kv in args.min:
        k, _, v = kv.partition("=")
        limites[k.strip()] = float(v)

    try:
        linhas = _carregar_csv(args.csv) if args.csv else _carregar_db(args.db, args.desde)
    except Exception as e:  # noqa: BLE001
        print(f"[erro] não foi possível carregar dados: {e}", file=sys.stderr)
        sys.exit(2)

    campos = list(limites) + [c for c in INFORMATIVOS if c not in limites]
    cob, total = cobertura(linhas, campos)

    falhas = {c: cob[c] for c in limites if cob[c] < limites[c]}

    por_leil = {}
    if args.por_leiloeiro or args.gate_leiloeiro:
        grupos = {}
        for ln in linhas:
            grupos.setdefault(ln.get("leiloeiro") or "(sem leiloeiro)", []).append(ln)
        for nome, ls in sorted(grupos.items(), key=lambda x: -len(x[1])):
            c, t = cobertura(ls, list(limites))
            por_leil[nome] = {"total": t, "cobertura": c}

    # Gate por leiloeiro: flagra quem despenca >margem pp abaixo do global num campo
    # crítico (sinal de redesign/extrator quebrado), com volume mínimo. Auto-calibrado.
    outliers = []
    if args.gate_leiloeiro:
        for nome, info in por_leil.items():
            if info["total"] < args.min_volume:
                continue
            for c in limites:
                queda = cob[c] - info["cobertura"][c]
                if queda >= args.margem and info["cobertura"][c] < limites[c]:
                    outliers.append({"leiloeiro": nome, "campo": c, "total": info["total"],
                                     "leiloeiro_pct": info["cobertura"][c], "global_pct": cob[c]})
        outliers.sort(key=lambda x: x["leiloeiro_pct"] - x["global_pct"])

    if args.json:
        print(json.dumps({
            "total": total, "cobertura": cob, "limites": limites,
            "falhas": falhas, "por_leiloeiro": por_leil, "outliers_leiloeiro": outliers,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"\n  COBERTURA DE CAMPOS  ({total} imóveis"
              + (f", desde {args.desde}" if args.desde else "") + ")")
        print("  " + "-" * 46)
        for c in campos:
            lim = limites.get(c)
            marca = ("✗" if c in falhas else "✓") if lim else " "
            alvo = f"(mín {lim:.0f}%)" if lim else "(informativo)"
            print(f"  {marca} {c:<16} {cob[c]:6.1f}%  {alvo}")
        if args.por_leiloeiro:
            print("\n  POR LEILOEIRO (piores cobertura de título primeiro):")
            piores = sorted(por_leil.items(),
                            key=lambda x: x[1]["cobertura"].get("titulo", 100))[:15]
            for nome, info in piores:
                tit = info["cobertura"].get("titulo", 0)
                img = info["cobertura"].get("imagem", 0)
                print(f"    {nome[:34]:<34} n={info['total']:<5} "
                      f"titulo={tit:5.1f}%  imagem={img:5.1f}%")
        if args.gate_leiloeiro:
            print(f"\n  GATE POR LEILOEIRO (≥{args.margem:.0f}pp abaixo do global, "
                  f"n≥{args.min_volume}):")
            if outliers:
                for o in outliers[:15]:
                    print(f"    ✗ {o['leiloeiro'][:32]:<32} {o['campo']:<13} "
                          f"{o['leiloeiro_pct']:5.1f}% (global {o['global_pct']:.0f}%) n={o['total']}")
                if len(outliers) > 15:
                    print(f"    ... (+{len(outliers) - 15})")
            else:
                print("    ✓ nenhum leiloeiro destoa da média.")
        print()
        motivos = []
        if falhas:
            motivos.append("campos globais abaixo do limite: "
                           + ", ".join(f"{c} ({v:.1f}%)" for c, v in falhas.items()))
        if args.gate_leiloeiro and outliers:
            motivos.append(f"{len(outliers)} leiloeiro(s) despencando")
        if motivos:
            print("  RESULTADO: ✗ FALHOU — " + "; ".join(motivos))
        else:
            print("  RESULTADO: ✓ OK — cobertura dentro dos limites.")

    sys.exit(1 if (falhas or (args.gate_leiloeiro and outliers)) else 0)


if __name__ == "__main__":
    main()
