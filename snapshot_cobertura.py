# -*- coding: utf-8 -*-
"""
snapshot_cobertura.py — histórico de cobertura + detecção de regressão (sinal de redesign).

Referência: captura_dados_leiloes_master.md (Parte IX.4 "% de campos None subiu → sinal de
redesign"). O dashboard mostra o *estado* atual; este script mostra o *delta*: salva um
snapshot por execução em cobertura_historico.jsonl e compara com o snapshot anterior,
alertando quando a cobertura de um campo-chave de um leiloeiro CAI mais que o limite — o
sinal precoce de que o site mudou de layout e o extrator quebrou.

Uso:
  python snapshot_cobertura.py                       # grava snapshot + reporta regressões
  python snapshot_cobertura.py --limite-queda 15     # pontos percentuais (padrão 15)
  python snapshot_cobertura.py --min-volume 20       # ignora leiloeiros com poucos imóveis
  python snapshot_cobertura.py --strict              # exit≠0 se houver regressão (gate)
  python snapshot_cobertura.py --sem-gravar          # só compara, não anexa ao histórico

Código de saída: 0 normal; 1 (só com --strict) se houver regressão; 2 erro.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PADRAO = "imoveis_leiloeiros.db"
HIST_PADRAO = "cobertura_historico.jsonl"
CAMPOS_CHAVE = ["titulo", "descricao", "cidade", "uf", "lance_inicial", "data_leilao", "imagem"]


def _vazio(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def snapshot(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    linhas = [dict(r) for r in con.execute("SELECT * FROM imoveis")]
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='imovel_imagens'").fetchone():
        com_galeria = {r[0] for r in con.execute("SELECT DISTINCT imovel_id FROM imovel_imagens")}
        for ln in linhas:
            if _vazio(ln.get("imagem")) and ln["id"] in com_galeria:
                ln["imagem"] = "<galeria>"
    con.close()

    def cob(ls):
        n = len(ls)
        return {c: (round(100 * sum(1 for x in ls if not _vazio(x.get(c))) / n, 1) if n else 0.0)
                for c in CAMPOS_CHAVE}

    grupos = {}
    for ln in linhas:
        grupos.setdefault(ln.get("leiloeiro") or "(sem leiloeiro)", []).append(ln)
    leiloeiros = {nome: {"total": len(ls), "campos": cob(ls)} for nome, ls in grupos.items()}
    return {"ts": datetime.now().isoformat(timespec="seconds"),
            "total": len(linhas), "global": cob(linhas), "leiloeiros": leiloeiros}


def ultimo_snapshot(hist_path):
    try:
        with open(hist_path, encoding="utf-8") as f:
            linhas = [l for l in f if l.strip()]
        return json.loads(linhas[-1]) if linhas else None
    except FileNotFoundError:
        return None


def regressoes(anterior, atual, limite, min_volume):
    if not anterior:
        return []
    out = []
    for nome, info in atual["leiloeiros"].items():
        if info["total"] < min_volume:
            continue
        ant = anterior["leiloeiros"].get(nome)
        if not ant:
            continue
        for c in CAMPOS_CHAVE:
            antes, agora = ant["campos"].get(c, 0), info["campos"].get(c, 0)
            queda = antes - agora
            if queda >= limite:
                out.append({"leiloeiro": nome, "campo": c, "total": info["total"],
                            "antes": antes, "agora": agora, "queda": round(queda, 1)})
    out.sort(key=lambda x: -x["queda"])
    return out


def main():
    ap = argparse.ArgumentParser(description="Snapshot e regressão de cobertura.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--hist", default=HIST_PADRAO)
    ap.add_argument("--limite-queda", type=float, default=15.0)
    ap.add_argument("--min-volume", type=int, default=20)
    ap.add_argument("--strict", action="store_true", help="exit≠0 se houver regressão")
    ap.add_argument("--sem-gravar", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        atual = snapshot(args.db)
    except Exception as e:  # noqa: BLE001
        print(f"[erro] {e}", file=sys.stderr)
        sys.exit(2)

    anterior = ultimo_snapshot(args.hist)
    regs = regressoes(anterior, atual, args.limite_queda, args.min_volume)

    if not args.sem_gravar:
        with open(args.hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(atual, ensure_ascii=False) + "\n")

    if args.json:
        print(json.dumps({"snapshot": atual["ts"], "comparado_com":
                          (anterior or {}).get("ts"), "regressoes": regs},
                         ensure_ascii=False, indent=2))
        sys.exit(1 if (regs and args.strict) else 0)

    print(f"\n  SNAPSHOT {atual['ts']}  ({atual['total']} imóveis, "
          f"{len(atual['leiloeiros'])} leiloeiros)")
    print("  cobertura global: " + "  ".join(f"{c}={atual['global'][c]:.0f}%"
                                             for c in CAMPOS_CHAVE))
    if anterior is None:
        print("  (primeiro snapshot — sem base de comparação ainda)")
    elif not regs:
        print(f"  ✓ nenhuma regressão ≥{args.limite_queda:.0f}pp vs {anterior['ts']}")
    else:
        print(f"\n  ⚠ {len(regs)} REGRESSÃO(ÕES) ≥{args.limite_queda:.0f}pp "
              f"vs {anterior['ts']} (provável redesign):")
        for r in regs[:25]:
            print(f"    {r['leiloeiro'][:34]:<34} {r['campo']:<14} "
                  f"{r['antes']:5.1f}% → {r['agora']:5.1f}%  (−{r['queda']:.1f}pp, n={r['total']})")
    print()
    sys.exit(1 if (regs and args.strict) else 0)


if __name__ == "__main__":
    main()
