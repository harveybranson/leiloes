"""
Explora quantos lotes cada leilão tem e como navegar entre páginas de lotes.
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
    return "just a moment" in l or "cf_chl_f_tk" in l

SID = fs("sessions.create")["session"]
print(f"Sessão: {SID}\n")

# ── Inspecionar leilão 15294 (tem 10 lotes) ───────────────────────
sol = fs_get(f"{BASE}/leilao/imoveis/15294", SID)
h = sol.get("response","")
print(f"[15294] cf={is_cf(h)} len={len(h)}")

# Todos os lotes
lotes = list(dict.fromkeys(re.findall(r'/leilao/\d+/lote/(\w+)', h)))
print(f"  Lotes no HTML: {len(lotes)} → {lotes}")

# Paginação de lotes
pag_refs = re.findall(r'.{50}(?:page|pagina|total|count|lotes).{50}', h, re.I)
for p in pag_refs[:5]: print(f"  pag: {repr(p)}")

# Contar cards
cards = len(re.findall(r'card_lote_link', h))
print(f"  Cards card_lote_link: {cards}")

# Tentar página 2 de lotes
for pg_param in ["?page=2", "?pagina=2", "?p=2", "/2"]:
    url2 = f"{BASE}/leilao/imoveis/15294{pg_param}"
    s2 = fs_get(url2, SID, max_timeout=20000)
    h2 = s2.get("response","")
    l2 = list(dict.fromkeys(re.findall(r'/leilao/\d+/lote/(\w+)', h2)))
    status = s2.get("status","?")
    print(f"  {pg_param}: status={status} cf={is_cf(h2)} lotes={l2[:5]}")

# ── Tentar URLs de tipo 'todos os lotes' ─────────────────────────
for url in [
    f"{BASE}/leilao/15294/lotes",
    f"{BASE}/leilao/imoveis/15294/lotes",
    f"{BASE}/leilao/imoveis/15294?todos=1",
    f"{BASE}/leilao/imoveis/15294?pageSize=999",
]:
    sol_u = fs_get(url, SID, max_timeout=20000)
    h_u = sol_u.get("response","")
    l_u = list(dict.fromkeys(re.findall(r'/leilao/\d+/lote/(\w+)', h_u)))
    print(f"  {url.split('/')[-1]}: status={sol_u.get('status')} cf={is_cf(h_u)} lotes={len(l_u)} {l_u[:5]}")

# ── Verificar se lotes são sequenciais e iteráveis ────────────────
print("\n[teste iteração lote por lote de 15294]")
for lnum in ["001","002","010","011","012","015","020","050"]:
    url_lote = f"{BASE}/leilao/15294/lote/{lnum}"
    s = fs_get(url_lote, SID, max_timeout=15000)
    h_l = s.get("response","")
    status = s.get("status","?")
    has_content = "card_lote" in h_l or "lanceMinimo" in h_l or "lance" in h_l.lower()
    is_404 = status == "404" or "404" in h_l[:200] or "not found" in h_l.lower()[:200]
    print(f"  lote/{lnum}: status={status} 404={is_404} has_content={has_content}")

fs("sessions.destroy", session=SID)
print(f"\nSessão destruída.")
