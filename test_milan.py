import sys, re, warnings
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from curl_cffi import requests as cffi

try:
    r = cffi.get("https://www.milanleiloes.com.br", impersonate="chrome124", timeout=20, verify=False)
    print(f"Status: {r.status_code}")
    if "just a moment" not in r.text.lower():
        print("CF BYPASS!")
        links = re.findall(r'href=["\']([^"\']*(?:imovel|lote|bem|leilao)[^"\']{10,})["\']', r.text, re.I)
        print(f"Links: {len(links)}")
        for l in links[:5]: print(f"  {l}")
    else:
        print("Milan ainda bloqueado pelo Cloudflare")
        # Tenta subdomínios
        for url in ["https://app.milanleiloes.com.br/imoveis", "https://milanleiloes.com.br"]:
            try:
                r2 = cffi.get(url, impersonate="chrome124", timeout=15, verify=False)
                print(f"{url}: {r2.status_code} CF={('just a moment' in r2.text.lower())}")
                if "just a moment" not in r2.text.lower():
                    ls = re.findall(r'href=["\']([^"\']+(?:imovel|lote)[^"\']{5,})["\']', r2.text, re.I)
                    print(f"  Links: {ls[:3]}")
            except Exception as e:
                print(f"  {url}: {e}")
except Exception as e:
    print(f"ERRO: {e}")
