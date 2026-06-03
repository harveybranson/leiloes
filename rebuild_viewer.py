"""
Gera viewer_imoveis_jucesp.html — renderização 100% via JS (arquivo leve).
Fontes: imoveis_completo (cp1252) + CSV de scraping extra.
"""
import sys, csv, json, re, hashlib
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TODAY    = date.today()
BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"

COMPLETO_CSV = CSV_DIR  / "imoveis_completo_20260601_1708.csv"
LEIS_CSV     = BASE_DIR / "leiloeiros_regulares.csv"
EXTRA_CSV    = CSV_DIR  / f"imoveis_jucesp_{TODAY}.csv"  # gerado pelo scraping
OUT_CSV      = CSV_DIR  / f"imoveis_jucesp_{TODAY}.csv"
OUT_HTML     = BASE_DIR / "viewer_imoveis_jucesp.html"


# ── helpers ────────────────────────────────────────────────────────────────────
def mkid(s): return hashlib.md5(str(s).encode()).hexdigest()[:12]

def money(txt):
    if not txt: return None
    t = re.sub(r"[^\d,.]", "", str(txt))
    if "," in t and "." in t: t = t.replace(".", "").replace(",", ".")
    elif "," in t: t = t.replace(",", ".")
    try: return float(t)
    except: return None

def parse_dt(txt):
    if not txt: return None
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", str(txt))
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2: y = "20" + y
        try: return date(int(y), int(mo), int(d)).isoformat()
        except: pass
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(txt))
    if m2: return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None

def fut(ds):
    if not ds: return True  # sem data → inclui
    try: return date.fromisoformat(ds[:10]) >= TODAY
    except: return True


# ── carrega leiloeiros JUCESP Atuante Regular ──────────────────────────────────
def load_regulares():
    nomes = set()
    with open(LEIS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("junta_comercial","").upper() == "JUCESP":
                nomes.add(re.sub(r"[^a-z]","", r["nome"].lower()))
    return nomes

reg = load_regulares()
print(f"Leiloeiros JUCESP regulares: {len(reg)}")


# ── carrega imoveis_completo (cp1252 correto) ──────────────────────────────────
def load_completo():
    items, seen = [], set()
    with open(COMPLETO_CSV, encoding="cp1252", errors="replace") as f:
        for row in csv.DictReader(f):
            lei = row.get("leiloeiro","")
            if "jucesp" not in lei.lower():
                clean = re.sub(r"[^a-z]","", lei.lower())
                if not any(clean in n or n in clean for n in reg if len(n) > 8):
                    continue

            ds  = parse_dt(row.get("data_primeiro_leilao","") or row.get("data_encerramento",""))
            if not fut(ds): continue

            url = row.get("url_original","")
            parsed = urlparse(url)
            site = f"{parsed.scheme}://{parsed.netloc}" if url else ""
            lei_clean = re.sub(r"(?i)\boficial\b|\bJUCESP\b|\d+","", lei).strip()

            rid = mkid(row.get("id","") + url)
            if rid in seen: continue
            seen.add(rid)

            items.append({
                "id":   rid,
                "lei":  lei_clean[:70],
                "site": site,
                "tit":  row.get("titulo","")[:130],
                "cid":  row.get("cidade","")[:50],
                "uf":   row.get("estado","SP") or "SP",
                "lan":  money(row.get("valor_minimo","")),
                "aval": money(row.get("valor_avaliacao","")),
                "data": ds or "",
                "url":  url,
                "tipo": (row.get("tipo_imovel","") or "imovel").upper()[:12],
                "img":  row.get("imagem_principal",""),
            })
    print(f"  completo: {len(items)}")
    return items, seen


# ── carrega extras do CSV gerado pelo scraping ─────────────────────────────────
def load_extra(existing_ids):
    items = []
    if not EXTRA_CSV.exists():
        return items
    with open(EXTRA_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = row.get("id","")
            if not rid or rid in existing_ids: continue
            existing_ids.add(rid)
            items.append({
                "id":   rid,
                "lei":  row.get("leiloeiro","")[:70],
                "site": row.get("site",""),
                "tit":  row.get("titulo","")[:130],
                "cid":  row.get("cidade","")[:50],
                "uf":   row.get("uf","SP") or "SP",
                "lan":  money(row.get("lance_inicial","")),
                "aval": money(row.get("avaliacao","")),
                "data": row.get("data_leilao","") or "",
                "url":  row.get("url",""),
                "tipo": (row.get("tipo","") or "imovel").upper()[:12],
                "img":  row.get("imagem",""),
            })
    print(f"  extra scraping: {len(items)}")
    return items


# ── salva CSV de saída ─────────────────────────────────────────────────────────
def save_csv(items):
    FIELDS = ["id","lei","site","tit","cid","uf","lan","aval","data","url","tipo","img"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(items)
    print(f"CSV: {OUT_CSV} ({len(items)} linhas)")


# ── gera HTML (JS-driven, arquivo leve) ───────────────────────────────────────
def render_html(items):
    now   = datetime.now().strftime("%d/%m/%Y %H:%M")
    total = len(items)

    # JSON compacto — chaves curtas para menor tamanho
    data_js = json.dumps(items, ensure_ascii=False, default=str, separators=(",", ":"))
    csv_fn  = OUT_CSV.name
    kb      = len(data_js) // 1024

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Imoveis JUCESP — {now}</title>
<style>
:root{{--bg:#0f172a;--s:#1e293b;--b:#334155;--a:#3b82f6;--d:#ef4444;--ok:#22c55e;--t:#e2e8f0;--m:#94a3b8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--t);font-family:'Segoe UI',system-ui,sans-serif;font-size:.84rem}}
header{{background:var(--s);border-bottom:1px solid var(--b);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
header h1{{font-size:.95rem;color:#fff}}
.stats{{display:flex;gap:8px;flex-wrap:wrap}}
.st{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:3px 10px;font-size:.76rem;color:var(--m)}}
.st b{{color:var(--t)}}
.pbar{{height:3px;background:var(--b)}}.pbar-f{{height:100%;background:var(--a);width:100%}}
.tb{{padding:8px 16px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--b);background:var(--s);position:sticky;top:0;z-index:10}}
.tb input,.tb select{{background:var(--bg);border:1px solid var(--b);border-radius:5px;padding:5px 8px;color:var(--t);font-size:.8rem;outline:none}}
.tb input{{flex:1;min-width:150px}}.tb input:focus,.tb select:focus{{border-color:var(--a)}}
.btn{{padding:5px 12px;border-radius:5px;border:none;cursor:pointer;font-size:.78rem;font-weight:600;transition:.15s}}
.bd{{background:var(--d);color:#fff}}.bok{{background:var(--ok);color:#fff}}
.cnt{{margin-left:auto;color:var(--m);font-size:.76rem;white-space:nowrap}}
.pg{{display:flex;gap:4px;align-items:center}}
.pg button{{background:var(--bg);border:1px solid var(--b);color:var(--t);border-radius:4px;padding:3px 9px;cursor:pointer;font-size:.75rem}}
.pg button:hover{{border-color:var(--a)}}.pg button.on{{background:var(--a);border-color:var(--a);color:#fff}}
.tw{{overflow-x:auto;padding:0 16px 24px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
thead th{{background:var(--s);color:var(--m);text-align:left;padding:8px 6px;border-bottom:2px solid var(--b);white-space:nowrap;cursor:pointer;user-select:none}}
thead th:hover{{color:var(--t)}}
tbody tr{{border-bottom:1px solid rgba(51,65,85,.4)}}
tbody tr:hover{{background:rgba(59,130,246,.06)}}
td{{padding:7px 6px;vertical-align:middle}}
.bx{{background:var(--d);color:#fff;border:none;border-radius:3px;width:21px;height:21px;cursor:pointer;font-size:.75rem}}
.tl{{color:var(--a);text-decoration:none;font-weight:500;font-size:.82rem}}.tl:hover{{text-decoration:underline}}
.sl{{color:var(--m);font-size:.74rem;text-decoration:none}}.sl:hover{{color:var(--t)}}
.ln{{font-weight:700;color:var(--ok);white-space:nowrap;font-size:.82rem}}
.lei{{color:var(--m);font-size:.75rem}}
.dt{{white-space:nowrap;font-size:.78rem}}
.uf{{background:var(--b);border-radius:3px;padding:1px 4px;font-size:.68rem;color:var(--m);margin-left:3px}}
.badge{{background:rgba(59,130,246,.12);color:var(--a);border-radius:3px;padding:1px 5px;font-size:.66rem;margin-left:4px;vertical-align:middle}}
.th{{width:44px;height:33px;object-fit:cover;border-radius:3px;margin-right:5px;vertical-align:middle}}
.empty{{text-align:center;padding:40px;color:var(--m);font-size:.9rem}}
.toast{{position:fixed;bottom:16px;right:16px;background:var(--ok);color:#fff;padding:8px 14px;border-radius:6px;font-size:.82rem;display:none;z-index:99;box-shadow:0 3px 10px rgba(0,0,0,.3)}}
#mo{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;align-items:center;justify-content:center}}
#mo.open{{display:flex}}
.mid{{background:var(--s);border:1px solid var(--b);border-radius:10px;padding:20px;max-width:420px;width:90%}}
.mid h2{{margin-bottom:8px;font-size:.95rem}}
.mid p{{color:var(--m);margin-bottom:14px;font-size:.82rem}}
.mid .btns{{display:flex;gap:6px;justify-content:flex-end}}
</style>
</head>
<body>
<header>
  <h1>&#127968; Imoveis — Leiloeiros JUCESP Atuante Regular</h1>
  <div class="stats">
    <span class="st">Total: <b>{total}</b></span>
    <span class="st">JSON: <b>{kb} KB</b></span>
    <span class="st">Data: <b>{now}</b></span>
    <span class="st">Selecionados: <b id="n-sel">—</b></span>
  </div>
</header>
<div class="pbar"><div class="pbar-f"></div></div>

<div class="tb">
  <input id="q" placeholder="Filtrar titulo, leiloeiro, cidade..." oninput="fil()">
  <select id="uf" onchange="fil()">
    <option value="">UF</option>
    <option>SP</option><option>RJ</option><option>MG</option><option>RS</option>
    <option>SC</option><option>PR</option><option>BA</option><option>GO</option><option>SE</option>
  </select>
  <select id="tp" onchange="fil()">
    <option value="">Tipo</option>
    <option value="CASA">Casa</option><option value="APARTAMENTO">Apto</option>
    <option value="TERRENO">Terreno</option><option value="COMERCIAL">Comercial</option>
    <option value="RURAL">Rural</option><option value="GALPAO">Galpao</option><option value="OUTRO">Outro</option>
  </select>
  <button class="btn bd" onclick="exclVis()">&#x2715; Excluir visíveis</button>
  <button class="btn bok" onclick="openModal()">&#x2193; Exportar CSV</button>
  <span class="cnt" id="cnt">carregando…</span>
  <div class="pg" id="pager"></div>
</div>

<div class="tw">
  <table>
    <thead><tr>
      <th style="width:24px"></th>
      <th data-col="tit">Título &#x21F5;</th>
      <th data-col="lei">Leiloeiro &#x21F5;</th>
      <th data-col="cid">Cidade/UF &#x21F5;</th>
      <th data-col="lan">Lance &#x21F5;</th>
      <th data-col="data">Data &#x21F5;</th>
      <th>Site</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<div class="toast" id="toast"></div>
<div id="mo"><div class="mid">
  <h2>Exportar CSV</h2>
  <p>Exportar <b id="mc">0</b> imóveis não excluídos.</p>
  <div class="btns">
    <button class="btn" style="background:var(--b)" onclick="closeModal()">Cancelar</button>
    <button class="btn bok" onclick="doExp()">Confirmar</button>
  </div>
</div></div>

<script>
const RAW = {data_js};
const CSVFN = "{csv_fn}";
const PAGE = 100;

const excl = new Set();
let filtered = [...RAW];
let cur = 1;
let sortCol = 'data', sortAsc = true;

// ── render ────────────────────────────────────────────────────────────────────
function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

function fmtLance(v) {{
  if (!v && v!==0) return '—';
  try {{
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    return 'R$ ' + n.toLocaleString('pt-BR',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  }} catch(e) {{ return String(v); }}
}}

function makeRow(d) {{
  const img = d.img ? `<img class="th" src="${{esc(d.img)}}" loading="lazy" onerror="this.style.display='none'">` : '';
  const site_ = String(d.site||'').replace(/https?:\\/\\//,'').substring(0,35);
  return `<tr id="r-${{esc(d.id)}}" data-uf="${{esc(d.uf)}}" data-tipo="${{esc(d.tipo)}}">
  <td><button class="bx" onclick="X('${{esc(d.id)}}')">&#x2715;</button></td>
  <td>${{img}}<a href="${{esc(d.url||d.site)}}" target="_blank" class="tl">${{esc(d.tit)}}</a><span class="badge">${{esc(d.tipo)}}</span></td>
  <td class="lei">${{esc(d.lei)}}</td>
  <td>${{esc(d.cid)}} <span class="uf">${{esc(d.uf)}}</span></td>
  <td class="ln">${{fmtLance(d.lan)}}</td>
  <td class="dt">${{esc(d.data)||'—'}}</td>
  <td><a href="${{esc(d.site)}}" target="_blank" class="sl">${{esc(site_)}}</a></td>
</tr>`;
}}

function render() {{
  const total = filtered.length;
  const pages = Math.max(1, Math.ceil(total/PAGE));
  if (cur > pages) cur = pages;

  const slice = filtered.slice((cur-1)*PAGE, cur*PAGE);
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = slice.length
    ? slice.map(makeRow).join('')
    : '<tr><td colspan="7" class="empty">Nenhum imóvel encontrado.</td></tr>';

  document.getElementById('cnt').textContent = total + ' registros';
  document.getElementById('n-sel').textContent = RAW.filter(d=>!excl.has(d.id)).length;
  renderPager(pages);
}}

// ── pager ─────────────────────────────────────────────────────────────────────
function renderPager(pages) {{
  const el = document.getElementById('pager');
  if (pages <= 1) {{ el.innerHTML=''; return; }}
  let h = `<button onclick="go(${{cur-1}})" ${{cur===1?'disabled':''}}>&#8249;</button>`;
  for (let i=1;i<=pages;i++) {{
    if (i===1||i===pages||Math.abs(i-cur)<=2)
      h += `<button onclick="go(${{i}})" class="${{i===cur?'on':''}}">${{i}}</button>`;
    else if (Math.abs(i-cur)===3)
      h += '<span style="color:var(--m);padding:0 2px">…</span>';
  }}
  h += `<button onclick="go(${{cur+1}})" ${{cur===Math.max(1,pages)?'disabled':''}}>&#8250;</button>`;
  el.innerHTML = h;
}}

function go(p) {{
  const pages = Math.max(1, Math.ceil(filtered.length/PAGE));
  cur = Math.max(1,Math.min(p,pages));
  render();
  window.scrollTo({{top:0,behavior:'smooth'}});
}}

// ── filtro ────────────────────────────────────────────────────────────────────
function fil() {{
  const q  = document.getElementById('q').value.toLowerCase().trim();
  const uf = document.getElementById('uf').value.toUpperCase();
  const tp = document.getElementById('tp').value.toUpperCase();

  filtered = RAW.filter(d => {{
    if (excl.has(d.id)) return false;
    if (uf && d.uf !== uf) return false;
    if (tp && !d.tipo.includes(tp)) return false;
    if (q) {{
      const s = (d.tit+d.lei+d.cid+d.uf+d.tipo+d.data).toLowerCase();
      if (!s.includes(q)) return false;
    }}
    return true;
  }});

  cur = 1;
  render();
}}

// ── sort ──────────────────────────────────────────────────────────────────────
document.querySelectorAll('thead th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.col;
    if (sortCol===col) sortAsc=!sortAsc; else {{sortCol=col;sortAsc=true;}}
    filtered.sort((a,b) => {{
      let av = a[col]??'', bv = b[col]??'';
      if (col==='lan'||col==='aval') {{
        av = parseFloat(av)||0; bv = parseFloat(bv)||0;
        return sortAsc ? av-bv : bv-av;
      }}
      return sortAsc ? String(av).localeCompare(String(bv),'pt')
                     : String(bv).localeCompare(String(av),'pt');
    }});
    cur=1; render();
  }});
}});

// ── excluir ───────────────────────────────────────────────────────────────────
function X(id) {{
  excl.add(id);
  fil();
}}
function exclVis() {{
  filtered.forEach(d => excl.add(d.id));
  fil();
}}

// ── export CSV ────────────────────────────────────────────────────────────────
function openModal() {{
  document.getElementById('mc').textContent = RAW.filter(d=>!excl.has(d.id)).length;
  document.getElementById('mo').classList.add('open');
}}
function closeModal() {{ document.getElementById('mo').classList.remove('open'); }}
function doExp() {{
  closeModal();
  const sel = RAW.filter(d=>!excl.has(d.id));
  const f = ['id','lei','site','tit','cid','uf','lan','aval','data','url','tipo'];
  const lines = [f.join(',')];
  sel.forEach(d => lines.push(
    f.map(k=>'"'+String(d[k]??'').replace(/\\n/g,' ').replace(/"/g,'""')+'"').join(',')
  ));
  const b = new Blob(['\\uFEFF'+lines.join('\\n')],{{type:'text/csv;charset=utf-8;'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b);
  a.download = CSVFN;
  a.click();
  toast('Exportados ' + sel.length + ' imóveis → ' + CSVFN);
}}
function toast(msg,d=4000){{
  const e=document.getElementById('toast');
  e.textContent=msg; e.style.display='block';
  setTimeout(()=>e.style.display='none',d);
}}

// Init
fil();
</script>
</body>
</html>"""

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    sz = len(html) // 1024
    print(f"HTML: {OUT_HTML} ({sz} KB)")
    return OUT_HTML


# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Carregando dados...")
    items, seen = load_completo()

    extras = load_extra(seen)
    items += extras

    # Ordena por data (futuro primeiro, sem data por último)
    items.sort(key=lambda x: x.get("data","") or "9999")

    print(f"Total final: {len(items)}")
    save_csv(items)
    render_html(items)
    print("Pronto!")
