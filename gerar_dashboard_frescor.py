# -*- coding: utf-8 -*-
"""
gerar_dashboard_frescor.py — dashboard HTML de cobertura e frescor dos dados.

Referência: captura_dados_leiloes_master.md (Parte IX.4 "métricas por fonte" e
"% de campos None subiu → sinal de redesign"). Lê o banco e gera um HTML autocontido
(sem dependências externas) mostrando:
  - cobertura por campo (global) com barras e limite;
  - frescor: nº de imóveis por data de importação;
  - tabela por leiloeiro com cobertura de campos-chave (ordenável), para flagrar
    redesigns antes que virem buraco no banco.

Uso:
  python gerar_dashboard_frescor.py
  python gerar_dashboard_frescor.py --db imoveis_leiloeiros.db --out dashboard_frescor.html
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PADRAO = "imoveis_leiloeiros.db"
OUT_PADRAO = "dashboard_frescor.html"

CAMPOS = ["titulo", "descricao", "endereco", "cidade", "uf", "lance_inicial",
          "avaliacao", "data_leilao", "imagem", "tipo", "url"]
LIMITES = {"titulo": 95, "descricao": 70, "cidade": 60, "uf": 90,
           "lance_inicial": 80, "data_leilao": 60, "imagem": 70, "url": 99}


def _vazio(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def coletar(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    linhas = [dict(r) for r in con.execute("SELECT * FROM imoveis")]

    # imagem efetiva: coluna OU galeria 1→N
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='imovel_imagens'").fetchone():
        com_galeria = {r[0] for r in con.execute("SELECT DISTINCT imovel_id FROM imovel_imagens")}
        for ln in linhas:
            if _vazio(ln.get("imagem")) and ln["id"] in com_galeria:
                ln["imagem"] = "<galeria>"
    con.close()

    total = len(linhas)

    def cob(ls):
        n = len(ls)
        return {c: (round(100 * sum(1 for x in ls if not _vazio(x.get(c))) / n, 1) if n else 0.0)
                for c in CAMPOS}

    cob_global = cob(linhas)

    # frescor por data de importação
    frescor = {}
    for ln in linhas:
        d = (ln.get("importado_em") or "")[:10] or "(sem data)"
        frescor[d] = frescor.get(d, 0) + 1
    frescor = dict(sorted(frescor.items()))

    # por leiloeiro
    grupos = {}
    for ln in linhas:
        grupos.setdefault(ln.get("leiloeiro") or "(sem leiloeiro)", []).append(ln)
    por_leil = []
    for nome, ls in grupos.items():
        c = cob(ls)
        por_leil.append({"leiloeiro": nome, "total": len(ls),
                         "campos": {k: c[k] for k in ["titulo", "descricao", "cidade",
                                                      "lance_inicial", "data_leilao", "imagem"]}})
    por_leil.sort(key=lambda x: -x["total"])

    return {"total": total, "cob_global": cob_global, "frescor": frescor,
            "por_leiloeiro": por_leil, "limites": LIMITES,
            "gerado_em": datetime.now().isoformat(timespec="seconds")}


HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard de Frescor — Leilões</title>
<style>
 :root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e6e6;--muted:#8a93a3;--ok:#3fb950;--bad:#f85149;--bar:#2d333b;--accent:#58a6ff}
 *{box-sizing:border-box} body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
 header{padding:20px 28px;border-bottom:1px solid #262b33}
 h1{margin:0;font-size:20px} .sub{color:var(--muted);font-size:13px;margin-top:4px}
 .grid{display:grid;gap:18px;padding:24px 28px;grid-template-columns:1fr 1fr}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
 .card{background:var(--card);border:1px solid #262b33;border-radius:10px;padding:18px 20px}
 .card h2{margin:0 0 14px;font-size:15px;font-weight:600}
 .row{display:flex;align-items:center;gap:10px;margin:7px 0}
 .row .lbl{width:120px;color:var(--muted);font-size:13px} .row .num{width:54px;text-align:right;font-variant-numeric:tabular-nums}
 .track{flex:1;height:9px;background:var(--bar);border-radius:6px;overflow:hidden;position:relative}
 .fill{height:100%;border-radius:6px} .ok{background:var(--ok)} .bad{background:var(--bad)}
 .lim{position:absolute;top:-3px;width:2px;height:15px;background:#fff;opacity:.5}
 .bars{display:flex;align-items:flex-end;gap:8px;height:140px;padding-top:8px}
 .bcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:6px}
 .bcol .b{width:100%;background:var(--accent);border-radius:4px 4px 0 0;min-height:3px}
 .bcol .cap{font-size:11px;color:var(--muted)} .bcol .v{font-size:12px}
 table{width:100%;border-collapse:collapse;margin-top:6px} th,td{padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums}
 th{color:var(--muted);font-weight:500;cursor:pointer;user-select:none;border-bottom:1px solid #262b33}
 td:first-child,th:first-child{text-align:left} tbody tr:hover{background:#21262d}
 .full{grid-column:1/-1} .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px}
 .cell-bad{color:var(--bad)} .cell-ok{color:var(--fg)} input{background:#0d1117;border:1px solid #30363d;color:var(--fg);border-radius:6px;padding:6px 10px;width:240px}
</style></head><body>
<header><h1>Dashboard de Frescor &amp; Cobertura</h1>
<div class="sub" id="sub"></div></header>
<div class="grid">
  <div class="card"><h2>Cobertura por campo (global)</h2><div id="cov"></div></div>
  <div class="card"><h2>Frescor — imóveis por data de importação</h2><div class="bars" id="fresh"></div></div>
  <div class="card full"><h2>Por leiloeiro <span class="sub" style="font-weight:400">(clique no cabeçalho para ordenar)</span>
    <input id="q" placeholder="filtrar leiloeiro..." oninput="render()"></h2>
    <table id="tbl"><thead><tr>
      <th onclick="sort('leiloeiro')">Leiloeiro</th><th onclick="sort('total')">n</th>
      <th onclick="sort('titulo')">título</th><th onclick="sort('descricao')">descrição</th>
      <th onclick="sort('cidade')">cidade</th><th onclick="sort('lance_inicial')">lance</th>
      <th onclick="sort('data_leilao')">data</th><th onclick="sort('imagem')">imagem</th>
    </tr></thead><tbody></tbody></table></div>
</div>
<script>
const D = __DADOS__;
document.getElementById('sub').textContent =
  D.total.toLocaleString('pt-BR') + ' imóveis · ' + D.por_leiloeiro.length +
  ' leiloeiros · gerado em ' + D.gerado_em.replace('T',' ');

// cobertura global
const cov = document.getElementById('cov');
for (const c of Object.keys(D.cob_global)){
  const v = D.cob_global[c], lim = D.limites[c];
  const bad = lim && v < lim;
  cov.insertAdjacentHTML('beforeend',
    `<div class="row"><div class="lbl">${c}</div>
     <div class="track"><div class="fill ${bad?'bad':'ok'}" style="width:${v}%"></div>
     ${lim?`<div class="lim" style="left:${lim}%"></div>`:''}</div>
     <div class="num ${bad?'cell-bad':''}">${v.toFixed(1)}%</div></div>`);
}
// frescor
const fr = document.getElementById('fresh');
const fe = Object.entries(D.frescor); const max = Math.max(1,...fe.map(x=>x[1]));
for (const [d,n] of fe){
  fr.insertAdjacentHTML('beforeend',
    `<div class="bcol"><div class="v">${n}</div>
     <div class="b" style="height:${Math.round(100*n/max)}%"></div>
     <div class="cap">${d.slice(5)}</div></div>`);
}
// tabela por leiloeiro
let key='total', asc=false;
function sort(k){ asc = (key===k)?!asc:false; key=k; render(); }
function cellClass(c,v){ const l=D.limites[c]; return (l&&v<l)?'cell-bad':'cell-ok'; }
function render(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  let rows=D.por_leiloeiro.filter(r=>r.leiloeiro.toLowerCase().includes(q));
  rows.sort((a,b)=>{
    const va = key==='leiloeiro'?a.leiloeiro:(key==='total'?a.total:a.campos[key]);
    const vb = key==='leiloeiro'?b.leiloeiro:(key==='total'?b.total:b.campos[key]);
    return (va<vb?-1:va>vb?1:0)*(asc?1:-1);
  });
  const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
  for(const r of rows.slice(0,200)){
    const c=r.campos;
    tb.insertAdjacentHTML('beforeend',
      `<tr><td>${r.leiloeiro}</td><td>${r.total}</td>
       <td class="${cellClass('titulo',c.titulo)}">${c.titulo}</td>
       <td class="${cellClass('descricao',c.descricao)}">${c.descricao}</td>
       <td class="${cellClass('cidade',c.cidade)}">${c.cidade}</td>
       <td class="${cellClass('lance_inicial',c.lance_inicial)}">${c.lance_inicial}</td>
       <td class="${cellClass('data_leilao',c.data_leilao)}">${c.data_leilao}</td>
       <td class="${cellClass('imagem',c.imagem)}">${c.imagem}</td></tr>`);
  }
}
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Gera dashboard de frescor/cobertura.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--out", default=OUT_PADRAO)
    args = ap.parse_args()

    dados = coletar(args.db)
    html = HTML.replace("__DADOS__", json.dumps(dados, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] {args.out} gerado — {dados['total']} imóveis, "
          f"{len(dados['por_leiloeiro'])} leiloeiros, "
          f"{len(dados['frescor'])} datas de importação")


if __name__ == "__main__":
    main()
