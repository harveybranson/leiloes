"""
Opção D — Probe de API pública do Milan Leilões.
Testa endpoints REST/JSON conhecidos antes de qualquer bypass de CF.
"""
import sys, json, warnings, re
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from curl_cffi import requests as cffi

BASE = "https://www.milanleiloes.com.br"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

CANDIDATES = [
    # API REST típica
    f"{BASE}/api/imoveis",
    f"{BASE}/api/lotes",
    f"{BASE}/api/v1/imoveis",
    f"{BASE}/api/v2/imoveis",
    f"{BASE}/api/leiloes",
    f"{BASE}/api/produtos",
    f"{BASE}/api/ofertas",
    # Endpoints com paginação
    f"{BASE}/api/imoveis?page=1",
    f"{BASE}/api/imoveis?pagina=1",
    f"{BASE}/api/lotes?page=1&pageSize=20",
    # JSON feed
    f"{BASE}/feed/imoveis.json",
    f"{BASE}/feed/lotes.json",
    f"{BASE}/sitemap.xml",
    f"{BASE}/sitemap_imoveis.xml",
    # App / mobile endpoints
    f"{BASE}/app/api/imoveis",
    f"{BASE}/mobile/api/lotes",
    # Alternativas de subdomínio
    "https://api.milanleiloes.com.br/imoveis",
    "https://app.milanleiloes.com.br/api/imoveis",
    "https://app.milanleiloes.com.br/imoveis",
]

print("=== Probe API Milan Leilões ===\n")
found = []

for url in CANDIDATES:
    try:
        r = cffi.get(url, headers=HEADERS, impersonate="chrome124",
                     timeout=15, verify=False, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        is_json = "json" in ct or (r.text.lstrip().startswith(("{", "[")))
        is_xml  = "xml" in ct or r.text.lstrip().startswith("<")
        is_cf   = "just a moment" in r.text.lower() or r.status_code == 403 and "cloudflare" in r.text.lower()

        tag = ""
        if is_cf:
            tag = "CF-BLOCKED"
        elif is_json:
            tag = "JSON ✓"
            # tentar contar itens
            try:
                data = r.json()
                if isinstance(data, list):
                    tag += f" [{len(data)} items]"
                elif isinstance(data, dict):
                    tag += f" [keys: {list(data.keys())[:5]}]"
            except Exception:
                tag += " [parse-err]"
        elif is_xml:
            tag = "XML"
            urls_found = re.findall(r"<loc>([^<]+)</loc>", r.text)
            tag += f" [{len(urls_found)} locs]"
        elif r.status_code == 404:
            tag = "404"
        else:
            tag = f"HTML {r.status_code}"

        print(f"  {r.status_code}  {tag:<35}  {url}")
        if is_json and not is_cf:
            found.append((url, r.text[:500]))
    except Exception as e:
        print(f"  ERR  {str(e)[:60]:<35}  {url}")

print(f"\n=== Endpoints com JSON: {len(found)} ===")
for url, preview in found:
    print(f"\n  URL: {url}")
    print(f"  Preview: {preview[:300]}")
