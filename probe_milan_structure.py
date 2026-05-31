"""
Explora estrutura do Milan Leilões — TUDO via sessão FlareSolverr (mesmo IP/browser).
"""
import sys, re, json
import requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS_URL = "http://localhost:8191/v1"
BASE   = "https://www.milanleiloes.com.br"

def fs(cmd, **kw):
    r = requests.post(FS_URL, json={"cmd": cmd, **kw}, timeout=120)
    r.raise_for_status()
    return r.json()

def fs_get(url, sid, max_timeout=60000):
    return fs("request.get", url=url, session=sid, maxTimeout=max_timeout).get("solution", {})

def is_cf(html):
    l = html.lower()
    return "just a moment" in l or "cf_chl_f_tk" in l or "<title>just a" in l

# ── Criar sessão ──────────────────────────────────────────────────
SID = fs("sessions.create")["session"]
print(f"Sessão: {SID}\n")

# ── 1. Home ───────────────────────────────────────────────────────
sol = fs_get(BASE, SID)
h_home = sol.get("response", "")
print(f"[home]  cf={is_cf(h_home)}  len={len(h_home)}")

with open("milan_home_real.html","w",encoding="utf-8") as f: f.write(h_home)

# Leilões na home
leiloes = list(dict.fromkeys(re.findall(r'href=["\']([^"\']*leilao[^"\']{3,})["\']', h_home, re.I)))
print(f"  Links com 'leilao': {leiloes[:15]}")

# ── 2. /imoveis via sessão ────────────────────────────────────────
sol2 = fs_get(f"{BASE}/imoveis", SID)
h_imoveis = sol2.get("response","")
print(f"\n[/imoveis]  cf={is_cf(h_imoveis)}  len={len(h_imoveis)}")
with open("milan_imoveis_real.html","w",encoding="utf-8") as f: f.write(h_imoveis)
lks = list(dict.fromkeys(re.findall(r'href=["\']([^"\']+)["\']', h_imoveis)))
print(f"  hrefs: {len(lks)}")
for lk in lks[:10]: print(f"    {lk}")

# ── 3. /leilao/imoveis/{lid} via sessão ──────────────────────────
LIDS = ["15141","15294","15309"]
for lid in LIDS:
    url = f"{BASE}/leilao/imoveis/{lid}"
    sol3 = fs_get(url, SID)
    h3 = sol3.get("response","")
    lotes = list(dict.fromkeys(re.findall(r'/leilao/\d+/lote/(\w+)', h3)))
    print(f"\n[{url}]  cf={is_cf(h3)}  len={len(h3)}  lotes={lotes[:10]}")

# ── 4. /leilao/15141/catalogo ─────────────────────────────────────
url_cat = f"{BASE}/leilao/15141/catalogo"
sol4 = fs_get(url_cat, SID)
h4 = sol4.get("response","")
lotes_cat = list(dict.fromkeys(re.findall(r'/leilao/\d+/lote/(\w+)', h4)))
print(f"\n[catalogo]  cf={is_cf(h4)}  len={len(h4)}  lotes={lotes_cat[:10]}")
with open("milan_catalogo.html","w",encoding="utf-8") as f: f.write(h4)

# ── 5. Tentar API Next.js ─────────────────────────────────────────
for api_url in [
    f"{BASE}/api/leiloes/imoveis",
    f"{BASE}/api/leiloes",
    f"{BASE}/api/lotes?codLeilao=15141",
    f"{BASE}/api/catalogo/15141",
]:
    sol5 = fs_get(api_url, SID, max_timeout=20000)
    h5 = sol5.get("response","")
    is_json = h5.lstrip().startswith(("{","["))
    status = sol5.get("status","?")
    print(f"\n  API {api_url}: status={status} json={is_json} cf={is_cf(h5)} len={len(h5)}")
    if is_json: print(f"  Preview: {h5[:200]}")

fs("sessions.destroy", session=SID)
print(f"\nSessão destruída.")
