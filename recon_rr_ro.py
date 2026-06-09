# -*- coding: utf-8 -*-
import csv, requests, urllib3, re
urllib3.disable_warnings()
H={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
rows=list(csv.DictReader(open("csv/leiloeiros_roraima_rondonia_2026-06-09.csv",encoding="utf-8")))
for r in rows:
    url=r["site"]; nome=r["nome"][:34]
    try:
        resp=requests.get(url,headers=H,verify=False,timeout=15,allow_redirects=True)
        srv=resp.headers.get("server","")
        cf="CF" if ("cloudflare" in srv.lower() or "cf-ray" in resp.headers or "challenge" in resp.text[:5000].lower() or "cf-chl" in resp.text[:8000].lower()) else "-"
        t=resp.text.lower()
        spa = "NEXT" if "__next_data__" in t else ("VUE" if "__nuxt__" in t else ("WP" if "wp-content" in t else "-"))
        n_imovel=len(re.findall(r"im[oó]vel|apartamento|casa|terreno|lote",t))
        print(f"{resp.status_code} cf={cf:2} {spa:4} hits={n_imovel:4} len={len(resp.text):7} | {nome:34} {url}")
    except Exception as e:
        print(f"ERR {type(e).__name__:18} | {nome:34} {url}")
