# -*- coding: utf-8 -*-
"""
gerar_viewer_galeria.py — viewer HTML com CARROSSEL de fotos por imóvel.

Referência: captura_dados_leiloes_master.md (Parte VII "capture TODAS as fotos"). Os
viewers antigos mostram 1 foto; este lê a tabela 1→N `imovel_imagens` (e `imovel_anexos`)
e renderiza um carrossel por imóvel + links para edital/matrícula/laudo. Aproveita a
captura completa que agora persiste.

Uso:
  python gerar_viewer_galeria.py
  python gerar_viewer_galeria.py --db imoveis_leiloeiros.db --out viewer_galeria.html --limite 3000
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PADRAO = "imoveis_leiloeiros.db"
OUT_PADRAO = "viewer_galeria.html"


def coletar(db_path, limite):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tem_img = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='imovel_imagens'").fetchone()
    tem_anx = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='imovel_anexos'").fetchone()

    galerias, anexos = {}, {}
    if tem_img:
        for r in con.execute("SELECT imovel_id, url FROM imovel_imagens "
                             "ORDER BY imovel_id, principal DESC, ordem"):
            galerias.setdefault(r["imovel_id"], []).append(r["url"])
    if tem_anx:
        for r in con.execute("SELECT imovel_id, tipo, url FROM imovel_anexos"):
            anexos.setdefault(r["imovel_id"], []).append({"tipo": r["tipo"], "url": r["url"]})

    itens = []
    for r in con.execute("SELECT id, titulo, leiloeiro, cidade, uf, lance_inicial, "
                         "data_leilao, url, tipo FROM imoveis"):
        fotos = galerias.get(r["id"]) or ([r["id"]] and [])
        if not fotos:
            continue  # viewer de galeria: só imóveis COM foto
        itens.append({
            "titulo": r["titulo"] or "(sem título)", "leiloeiro": r["leiloeiro"] or "",
            "cidade": r["cidade"] or "", "uf": r["uf"] or "",
            "lance": r["lance_inicial"], "data": r["data_leilao"] or "",
            "url": r["url"] or "", "tipo": r["tipo"] or "",
            "fotos": fotos, "anexos": anexos.get(r["id"], []),
        })
        if limite and len(itens) >= limite:
            break
    con.close()
    ufs = sorted({i["uf"] for i in itens if i["uf"]})
    return {"itens": itens, "ufs": ufs, "total": len(itens),
            "gerado_em": datetime.now().isoformat(timespec="seconds")}


HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Galeria de Imóveis — Leilões</title>
<style>
 :root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e6e6;--muted:#8a93a3;--accent:#58a6ff;--line:#262b33}
 *{box-sizing:border-box} body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
 header{padding:16px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
 h1{margin:0;font-size:18px} .sub{color:var(--muted);font-size:13px;margin-top:3px}
 .controls{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
 input,select{background:#0d1117;border:1px solid #30363d;color:var(--fg);border-radius:6px;padding:7px 10px}
 input{flex:1;min-width:200px}
 .grid{display:grid;gap:16px;padding:20px 24px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
 .gal{position:relative;aspect-ratio:4/3;background:#000;overflow:hidden}
 .gal img{width:100%;height:100%;object-fit:cover;display:none}
 .gal img.on{display:block}
 .nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.5);color:#fff;border:none;cursor:pointer;font-size:20px;padding:4px 10px;border-radius:6px}
 .nav.prev{left:6px} .nav.next{right:6px}
 .count{position:absolute;bottom:6px;right:8px;background:rgba(0,0,0,.6);font-size:11px;padding:1px 7px;border-radius:10px}
 .body{padding:10px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
 .tit{font-size:13px;font-weight:600;line-height:1.35;max-height:3.6em;overflow:hidden}
 .meta{font-size:12px;color:var(--muted)} .lance{color:var(--accent);font-weight:600}
 .pills{display:flex;gap:5px;flex-wrap:wrap;margin-top:auto}
 .pill{font-size:11px;padding:1px 7px;border-radius:10px;background:#21262d;color:var(--fg);text-decoration:none}
 .pill.uf{background:#1f6feb33;color:#9cc4ff} .pill.anx{background:#23863633;color:#7ee787}
 a.lk{color:var(--accent);font-size:12px;text-decoration:none} a.lk:hover{text-decoration:underline}
 .empty{padding:40px;text-align:center;color:var(--muted)}
</style></head><body>
<header><h1>Galeria de Imóveis</h1><div class="sub" id="sub"></div>
 <div class="controls">
   <input id="q" placeholder="buscar título, cidade, leiloeiro..." oninput="render()">
   <select id="uf" onchange="render()"><option value="">Todas UFs</option></select>
   <select id="anx" onchange="render()"><option value="">Com ou sem anexos</option>
     <option value="1">Só com anexos</option></select>
 </div></header>
<div class="grid" id="grid"></div>
<script>
const D = __DADOS__;
const ufSel = document.getElementById('uf');
for (const u of D.ufs){ const o=document.createElement('option'); o.value=u; o.textContent=u; ufSel.appendChild(o); }
const brl = v => v==null ? '' : 'R$ '+Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2});

function card(it, idx){
  const imgs = it.fotos.map((u,i)=>`<img class="${i===0?'on':''}" data-i="${i}" src="${u}" loading="lazy" alt="">`).join('');
  const navs = it.fotos.length>1
    ? `<button class="nav prev" onclick="mover(${idx},-1)">‹</button>
       <button class="nav next" onclick="mover(${idx},1)">›</button>
       <span class="count" id="c${idx}">1/${it.fotos.length}</span>` : '';
  const anx = it.anexos.map(a=>`<a class="pill anx" href="${a.url}" target="_blank">${a.tipo}</a>`).join('');
  const loc = [it.cidade, it.uf].filter(Boolean).join('/');
  return `<div class="card" data-i="${idx}">
    <div class="gal" id="g${idx}">${imgs}${navs}</div>
    <div class="body">
      <div class="tit">${it.titulo}</div>
      <div class="meta">${it.leiloeiro}</div>
      <div class="meta">${loc} ${it.data?('· 1ª praça: '+it.data):''}</div>
      ${it.lance!=null?`<div class="lance">${brl(it.lance)}</div>`:''}
      <div class="pills">
        ${it.uf?`<span class="pill uf">${it.uf}</span>`:''}
        ${anx}
        ${it.url?`<a class="lk pill" href="${it.url}" target="_blank">abrir ↗</a>`:''}
      </div>
    </div></div>`;
}
const pos = {};
function mover(idx, d){
  const it = filtrados[idx]; const n = it.fotos.length;
  pos[idx] = ((pos[idx]||0)+d+n)%n;
  const g = document.getElementById('g'+idx);
  g.querySelectorAll('img').forEach(im=>im.classList.toggle('on', +im.dataset.i===pos[idx]));
  document.getElementById('c'+idx).textContent = (pos[idx]+1)+'/'+n;
}
let filtrados = [];
function render(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  const uf=ufSel.value; const soAnx=document.getElementById('anx').value;
  filtrados = D.itens.filter(it=>{
    if(uf && it.uf!==uf) return false;
    if(soAnx && !it.anexos.length) return false;
    if(q){ const hay=(it.titulo+' '+it.cidade+' '+it.leiloeiro).toLowerCase(); if(!hay.includes(q)) return false; }
    return true;
  });
  for(const k in pos) delete pos[k];
  const grid=document.getElementById('grid');
  grid.innerHTML = filtrados.length
    ? filtrados.slice(0,600).map((it,i)=>card(it,i)).join('')
    : '<div class="empty">Nenhum imóvel com foto para esse filtro.</div>';
  document.getElementById('sub').textContent =
    `${filtrados.length.toLocaleString('pt-BR')} de ${D.total.toLocaleString('pt-BR')} imóveis com foto`+
    (filtrados.length>600?' (mostrando 600)':'')+` · gerado ${D.gerado_em.replace('T',' ')}`;
}
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Viewer de galeria (1→N).")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--out", default=OUT_PADRAO)
    ap.add_argument("--limite", type=int, default=3000, help="máx. de imóveis no HTML")
    args = ap.parse_args()
    dados = coletar(args.db, args.limite)
    html = HTML.replace("__DADOS__", json.dumps(dados, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] {args.out} — {dados['total']} imóveis com foto, "
          f"{len(dados['ufs'])} UFs.")


if __name__ == "__main__":
    main()
