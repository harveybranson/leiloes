"""
monitor_jucerja.py  —  Lê scraper_jucerja_run.log e regenera monitor_jucerja.html
Rode em paralelo ao scraper: python monitor_jucerja.py
Abra monitor_jucerja.html no browser (auto-refresh a cada 8s).
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_FILE      = Path(__file__).parent / "scraper_jucerja_run.log"
OUT_HTML      = Path(__file__).parent / "monitor_jucerja.html"
TOTAL_SITES   = 107
SCRAPER_PID   = 46424   # atualiza aqui se reiniciar o scraper
REFRESH_SEC   = 8

# ── Parsing do log ─────────────────────────────────────────────────────────────

def parse_log():
    if not LOG_FILE.exists():
        return {}, [], 0, 0, []

    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()

    leiloeiros   = []   # [{nome, url, imoveis, inseridos}]
    erros        = []
    total_ins    = 0
    total_col    = 0
    current      = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Início de um site: "> nomeslug https://..."
        m = re.match(r'^>\s+(\S+)\s+(https?://\S+)', line)
        if m:
            current = {"nome": m.group(1), "url": m.group(2),
                       "coletados": 0, "inseridos": 0, "atualizados": 0, "status": "visitado"}
            leiloeiros.append(current)
            continue

        # Resultado: "  N coletados - M inseridos, K atualizados"
        m2 = re.match(r'(\d+) coletados - (\d+) inseridos, (\d+) atualizados', line)
        if m2 and current:
            c, ins, upd = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
            current["coletados"]   = c
            current["inseridos"]   = ins
            current["atualizados"] = upd
            current["status"]      = "com_dados" if c > 0 else "sem_dados"
            total_ins += ins
            total_col += c
            continue

        # "0 imoveis encontrados"
        if "0 imoveis encontrados" in line and current:
            current["status"] = "sem_dados"
            continue

        # Erros
        if ("Erro" in line or "erro" in line or "ERRO" in line) and current:
            current["status"] = "erro"
            erros.append({"site": current.get("url",""), "msg": line[:120]})

    return leiloeiros, erros, total_ins, total_col


def process_alive(pid):
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue) -ne $null"],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False


# ── Geração do HTML ────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>Monitor JUCERJA — Scraping Leiloeiros</title>
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --accent: #7c6eff; --accent2: #00c9a7; --accent3: #ff6b6b;
    --text: #e8eaf0; --muted: #8890a4; --border: #2d3148;
    --ok: #00c9a7; --warn: #ffb347; --err: #ff6b6b; --zero: #525870;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
          min-height: 100vh; padding: 24px 20px; }}

  /* Header */
  .header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }}
  .header h1 {{ font-size: 1.5rem; font-weight: 700; }}
  .header h1 span {{ color: var(--accent); }}
  .badge {{ font-size: .7rem; padding: 3px 10px; border-radius: 20px; font-weight: 600; }}
  .badge-live  {{ background: #1a3a2a; color: var(--ok); border: 1px solid var(--ok); animation: pulse 2s infinite; }}
  .badge-done  {{ background: #2a1a3a; color: var(--accent); border: 1px solid var(--accent); }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.5}} }}

  /* Cards topo */
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 14px; margin-bottom: 28px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
           padding: 18px 20px; }}
  .card .label {{ font-size: .72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px; }}
  .card .value {{ font-size: 2rem; font-weight: 800; line-height: 1; }}
  .card .sub   {{ font-size: .75rem; color: var(--muted); margin-top: 4px; }}
  .c-accent {{ color: var(--accent); }}
  .c-ok     {{ color: var(--ok); }}
  .c-warn   {{ color: var(--warn); }}
  .c-err    {{ color: var(--err); }}

  /* Barra de progresso */
  .progress-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
                    padding: 18px 22px; margin-bottom: 24px; }}
  .progress-header {{ display: flex; justify-content: space-between; align-items: center;
                      margin-bottom: 12px; font-size: .85rem; }}
  .progress-header strong {{ font-size: 1rem; }}
  .bar-bg {{ background: var(--surface2); border-radius: 8px; height: 14px; overflow: hidden; }}
  .bar-fg {{ height: 100%; border-radius: 8px; background: linear-gradient(90deg, var(--accent), var(--accent2));
             transition: width .6s ease; }}

  /* Seção leiloeiros */
  .section-title {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .07em;
                    color: var(--muted); margin-bottom: 10px; font-weight: 600; }}

  /* Tabs */
  .tabs {{ display: flex; gap: 4px; margin-bottom: 16px; }}
  .tab {{ padding: 6px 16px; border-radius: 8px; font-size: .82rem; cursor: pointer;
          border: 1px solid var(--border); background: var(--surface); color: var(--muted);
          transition: all .2s; }}
  .tab.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}

  /* Tabela */
  .table-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
                 overflow: hidden; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .84rem; }}
  thead th {{ background: var(--surface2); padding: 11px 14px; text-align: left;
              font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
              color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border); }}
  tbody tr {{ border-bottom: 1px solid var(--border); transition: background .15s; }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface2); }}
  tbody td {{ padding: 10px 14px; vertical-align: middle; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: .72rem; font-weight: 600; }}
  .pill-ok   {{ background:#0d2e22; color:var(--ok); }}
  .pill-zero {{ background:#1a1d27; color:var(--zero); }}
  .pill-err  {{ background:#2e1010; color:var(--err); }}
  .pill-run  {{ background:#1a1a2e; color:var(--accent); animation: pulse 2s infinite; }}
  .num {{ font-weight: 700; font-size: 1rem; }}
  .url-link {{ color: var(--accent); text-decoration: none; font-size: .78rem; }}
  .url-link:hover {{ text-decoration: underline; }}

  /* Erros */
  .err-box {{ background: #1e1010; border: 1px solid #4a1a1a; border-radius: 10px;
              padding: 14px 18px; font-size: .8rem; color: var(--err); }}
  .err-box summary {{ cursor: pointer; font-weight: 600; margin-bottom: 6px; }}
  .err-line {{ padding: 3px 0; color: #cc8888; }}

  /* Footer */
  .footer {{ color: var(--muted); font-size: .75rem; margin-top: 20px; text-align: center; }}

  /* Hidden tabs */
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
</style>
</head>
<body>

<div class="header">
  <h1>📋 <span>JUCERJA</span> — Monitor de Scraping</h1>
  {status_badge}
</div>

<!-- Cards de métricas -->
<div class="cards">
  <div class="card">
    <div class="label">Sites visitados</div>
    <div class="value c-accent">{sites_done}</div>
    <div class="sub">de {total_sites} total</div>
  </div>
  <div class="card">
    <div class="label">Imóveis coletados</div>
    <div class="value c-ok">{total_col}</div>
    <div class="sub">registros brutos</div>
  </div>
  <div class="card">
    <div class="label">Inseridos no banco</div>
    <div class="value c-ok">{total_ins}</div>
    <div class="sub">PostgreSQL</div>
  </div>
  <div class="card">
    <div class="label">Com imóveis</div>
    <div class="value c-warn">{sites_com_dados}</div>
    <div class="sub">{pct_dados:.0f}% dos visitados</div>
  </div>
  <div class="card">
    <div class="label">Sem imóveis</div>
    <div class="value c-err">{sites_sem_dados}</div>
    <div class="sub">offline ou sem leilão</div>
  </div>
  <div class="card">
    <div class="label">Atualizado em</div>
    <div class="value" style="font-size:1rem">{hora}</div>
    <div class="sub">refresh a cada {refresh}s</div>
  </div>
</div>

<!-- Barra de progresso -->
<div class="progress-wrap">
  <div class="progress-header">
    <strong>Progresso geral</strong>
    <span style="color:var(--muted)">{sites_done} / {total_sites} sites &nbsp;•&nbsp; {pct_prog:.1f}%</span>
  </div>
  <div class="bar-bg">
    <div class="bar-fg" style="width:{pct_prog:.1f}%"></div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <button class="tab active" onclick="showTab('all')">Todos ({sites_done})</button>
  <button class="tab" onclick="showTab('dados')">Com imóveis ({sites_com_dados})</button>
  <button class="tab" onclick="showTab('vazio')">Sem imóveis ({sites_sem_dados})</button>
</div>

<!-- Tabela: todos -->
<div class="tab-content active" id="tab-all">
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Leiloeiro</th><th>Site</th>
        <th style="text-align:right">Coletados</th>
        <th style="text-align:right">Inseridos</th>
        <th>Status</th>
      </tr></thead>
      <tbody>{rows_all}</tbody>
    </table>
  </div>
</div>

<!-- Tabela: com dados -->
<div class="tab-content" id="tab-dados">
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Leiloeiro</th><th>Site</th>
        <th style="text-align:right">Coletados</th>
        <th style="text-align:right">Inseridos</th>
      </tr></thead>
      <tbody>{rows_dados}</tbody>
    </table>
  </div>
</div>

<!-- Tabela: vazio -->
<div class="tab-content" id="tab-vazio">
  <div class="table-wrap">
    <table>
      <thead><tr><th>#</th><th>Leiloeiro</th><th>Site</th><th>Motivo</th></tr></thead>
      <tbody>{rows_vazio}</tbody>
    </table>
  </div>
</div>

{erros_html}

<div class="footer">
  Fonte: <code>scraper_jucerja_run.log</code> &nbsp;•&nbsp;
  Script: <code>python run.py scrape-csv leiloeiros_jucerja_regulares_2024.csv</code> &nbsp;•&nbsp;
  Monitor: <code>python monitor_jucerja.py</code>
</div>

<script>
function showTab(id) {{
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['all','dados','vazio'][i]===id));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
}}
</script>
</body>
</html>
"""


def pill(status):
    if status == "com_dados": return '<span class="pill pill-ok">✓ Com imóveis</span>'
    if status == "sem_dados": return '<span class="pill pill-zero">— Vazio</span>'
    if status == "erro":      return '<span class="pill pill-err">✗ Erro</span>'
    return                           '<span class="pill pill-run">⏳ Visitado</span>'


def build_html(leiloeiros, erros, total_ins, total_col, alive):
    now    = datetime.now()
    done   = len(leiloeiros)
    pct    = done / TOTAL_SITES * 100

    com    = [l for l in leiloeiros if l["status"] == "com_dados"]
    sem    = [l for l in leiloeiros if l["status"] in ("sem_dados", "visitado")]
    err    = [l for l in leiloeiros if l["status"] == "erro"]

    pct_d  = len(com) / done * 100 if done else 0

    status_badge = (
        '<span class="badge badge-live">🔴 AO VIVO</span>'
        if alive else
        '<span class="badge badge-done">✓ CONCLUÍDO</span>'
    )

    # Rows: todos (ordenado por inseridos desc)
    sorted_all = sorted(leiloeiros, key=lambda x: -x["inseridos"])
    rows_all = ""
    for i, l in enumerate(sorted_all, 1):
        host = re.sub(r'^https?://(www\.)?', '', l["url"]).rstrip("/")
        rows_all += (
            f"<tr>"
            f"<td style='color:var(--muted)'>{i}</td>"
            f"<td><strong>{l['nome'][:30]}</strong></td>"
            f"<td><a class='url-link' href='{l['url']}' target='_blank'>{host}</a></td>"
            f"<td style='text-align:right'><span class='num'>{l['coletados'] or '—'}</span></td>"
            f"<td style='text-align:right'><span class='num c-ok'>{l['inseridos'] or '—'}</span></td>"
            f"<td>{pill(l['status'])}</td>"
            f"</tr>"
        )

    # Rows: com dados
    rows_dados = ""
    for i, l in enumerate(sorted(com, key=lambda x: -x["inseridos"]), 1):
        host = re.sub(r'^https?://(www\.)?', '', l["url"]).rstrip("/")
        rows_dados += (
            f"<tr>"
            f"<td style='color:var(--muted)'>{i}</td>"
            f"<td><strong>{l['nome'][:35]}</strong></td>"
            f"<td><a class='url-link' href='{l['url']}' target='_blank'>{host}</a></td>"
            f"<td style='text-align:right'><span class='num'>{l['coletados']}</span></td>"
            f"<td style='text-align:right'><span class='num c-ok'>{l['inseridos']}</span></td>"
            f"</tr>"
        )
    if not rows_dados:
        rows_dados = "<tr><td colspan='5' style='text-align:center;color:var(--muted);padding:24px'>Nenhum ainda</td></tr>"

    # Rows: vazio / erro
    rows_vazio = ""
    for i, l in enumerate(sem + err, 1):
        host = re.sub(r'^https?://(www\.)?', '', l["url"]).rstrip("/")
        motivo = "Sem leilão ativo" if l["status"] == "sem_dados" else "Erro de conexão"
        rows_vazio += (
            f"<tr>"
            f"<td style='color:var(--muted)'>{i}</td>"
            f"<td><strong>{l['nome'][:35]}</strong></td>"
            f"<td><a class='url-link' href='{l['url']}' target='_blank'>{host}</a></td>"
            f"<td style='color:var(--muted);font-size:.8rem'>{motivo}</td>"
            f"</tr>"
        )
    if not rows_vazio:
        rows_vazio = "<tr><td colspan='4' style='text-align:center;color:var(--muted);padding:24px'>Nenhum ainda</td></tr>"

    # Erros
    if erros:
        err_lines = "".join(f"<div class='err-line'>{e['site']} — {e['msg']}</div>" for e in erros[:20])
        erros_html = f"""<details class="err-box" style="margin-bottom:20px">
  <summary>{len(erros)} erros registrados</summary>
  {err_lines}
</details>"""
    else:
        erros_html = ""

    return HTML_TEMPLATE.format(
        refresh      = REFRESH_SEC,
        hora         = now.strftime("%H:%M:%S"),
        status_badge = status_badge,
        sites_done   = done,
        total_sites  = TOTAL_SITES,
        total_col    = total_col,
        total_ins    = total_ins,
        sites_com_dados = len(com),
        sites_sem_dados = len(sem) + len(err),
        pct_dados    = pct_d,
        pct_prog     = pct,
        rows_all     = rows_all or "<tr><td colspan='6' style='text-align:center;color:var(--muted);padding:24px'>Aguardando início...</td></tr>",
        rows_dados   = rows_dados,
        rows_vazio   = rows_vazio,
        erros_html   = erros_html,
    )


# ── Loop principal ─────────────────────────────────────────────────────────────

def main():
    print(f"[monitor] Lendo {LOG_FILE.name} → {OUT_HTML.name}  (Ctrl+C para parar)")
    print(f"[monitor] Abra no browser: {OUT_HTML}")

    while True:
        try:
            leiloeiros, erros, total_ins, total_col = parse_log()
            alive = process_alive(SCRAPER_PID)
            html  = build_html(leiloeiros, erros, total_ins, total_col, alive)
            OUT_HTML.write_text(html, encoding="utf-8")

            done = len(leiloeiros)
            ins_sum = sum(l["inseridos"] for l in leiloeiros)
            print(f"[{datetime.now():%H:%M:%S}] {done}/{TOTAL_SITES} sites | {ins_sum} inseridos | ativo={alive}")

            if not alive and done >= TOTAL_SITES:
                print("[monitor] Scraping concluído. HTML final gerado.")
                break

        except KeyboardInterrupt:
            print("\n[monitor] Interrompido.")
            break
        except Exception as e:
            print(f"[monitor] Erro: {e}")

        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main()
