"""
Opção A — FlareSolverr para bypass do Cloudflare no Milan Leilões.
Envia requisições via proxy local na porta 8191.
"""
import sys, json, re, time
import requests
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS_URL = "http://localhost:8191/v1"
MILAN  = "https://www.milanleiloes.com.br"

def fs_get(url, max_timeout=60000):
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max_timeout,
    }
    r = requests.post(FS_URL, json=payload, timeout=90)
    r.raise_for_status()
    return r.json()


def is_blocked(solution):
    html = solution.get("response", "")
    return "just a moment" in html.lower() or "enable javascript" in html.lower()


def report(solution):
    html   = solution.get("response", "")
    status = solution.get("status", "")
    cookies = solution.get("cookies", [])
    ua      = solution.get("userAgent", "")
    print(f"  status_code: {solution.get('status')}")
    print(f"  userAgent: {ua[:60]}")
    print(f"  cookies: {[c['name'] for c in cookies]}")
    links = re.findall(r'href=["\']([^"\']*(?:imovel|lote|bem|leilao)[^"\']{5,})["\']', html, re.I)
    print(f"  Links relevantes: {len(links)}")
    for l in links[:8]:
        print(f"    {l}")
    return cookies, ua


print("=== Opção A: FlareSolverr ===\n")

# ── Tentativa 1: página principal ────────────────────────────────
print(f"[1/3] {MILAN}")
try:
    resp = fs_get(MILAN)
    sol  = resp.get("solution", {})
    blocked = is_blocked(sol)
    print(f"  CF-blocked: {blocked}")
    if not blocked:
        cookies, ua = report(sol)
        print("\n✓ SUCESSO — FlareSolverr na home!")
        sys.exit(0)
except Exception as e:
    print(f"  ERRO: {e}")

# ── Tentativa 2: página de imóveis ───────────────────────────────
IMOVEIS = f"{MILAN}/imoveis"
print(f"\n[2/3] {IMOVEIS}")
try:
    resp = fs_get(IMOVEIS)
    sol  = resp.get("solution", {})
    blocked = is_blocked(sol)
    print(f"  CF-blocked: {blocked}")
    if not blocked:
        cookies, ua = report(sol)
        print("\n✓ SUCESSO — FlareSolverr em /imoveis!")
        sys.exit(0)
except Exception as e:
    print(f"  ERRO: {e}")

# ── Tentativa 3: timeout maior ────────────────────────────────────
print(f"\n[3/3] {MILAN} (timeout=90s)")
try:
    resp = fs_get(MILAN, max_timeout=90000)
    sol  = resp.get("solution", {})
    blocked = is_blocked(sol)
    print(f"  CF-blocked: {blocked}")
    if not blocked:
        cookies, ua = report(sol)
        print("\n✓ SUCESSO — FlareSolverr timeout estendido!")
        sys.exit(0)
    else:
        print(f"  response preview: {sol.get('response','')[:300]}")
except Exception as e:
    print(f"  ERRO: {e}")

print("\n✗ Opção A (FlareSolverr) também falhou.")
