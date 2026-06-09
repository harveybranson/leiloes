#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Re-scraper JUCETINS via FlareSolverr (etapa 1 das melhorias).
Reprocessa os sites que retornaram 0 no scraper_jucetins_completo.py — a maioria
e SPA/Cloudflare cujo HTML so aparece apos JS. FlareSolverr (Docker :8191) renderiza
o JS e contorna Cloudflare; depois reaproveitamos extrair()/inserir_banco() do
scraper original (mesmas regras: 1a praca futura, dedup por URL, insere no banco).
Conforme captura_dados_leiloes_v2.md secoes 13/14/27 e REGRA OBRIGATORIA (secao 0).
"""
import json, time, re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
import csv as _csv

import requests
import scraper_jucetins_completo as S  # reaproveita LEILOEIROS_REGULAR, extrair, inserir_banco, HOJE

FS_URL = "http://localhost:8191/v1"
BASE_DIR = S.BASE_DIR
PROGRESS_FILE = BASE_DIR / "scraper_jucetins_completo_progress.json"
OUTPUT_DIR = S.OUTPUT_DIR
RELATORIO_FILE = S.RELATORIO_FILE
NOW = datetime.now()

# sufixos de listagem renderizada a tentar quando a home nao basta
SUFIXOS = ("", "imoveis", "lotes", "leiloes", "lotes/imovel", "categoria/imoveis",
           "busca?categoria=imoveis", "bens/imoveis", "leiloes/imoveis")

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)

def fs_get(url, timeout=60000):
    try:
        r = requests.post(FS_URL, json={"cmd": "request.get", "url": url,
                                        "maxTimeout": timeout}, timeout=timeout/1000 + 30)
        sol = r.json().get("solution", {})
        return sol.get("response", "") or "", sol.get("status")
    except Exception as e:
        return "", str(e)[:100]

def wait_fs():
    try:
        return requests.get(FS_URL.rsplit("/v1", 1)[0] + "/", timeout=5).status_code == 200
    except Exception:
        return False

def main():
    if not wait_fs():
        log("ERRO: FlareSolverr indisponivel em :8191. Inicie o container.")
        return
    prog = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    # sites que precisam de re-scrape: marcados "sem imoveis" (SPA/Cloudflare/JS)
    alvos_nomes = {k for k, v in prog["sites_problema"].items() if "sem imoveis" in v}
    site_de = {n: (s, c, u) for n, s, c, u in S.LEILOEIROS_REGULAR if s}
    alvos = [(n, *site_de[n]) for n in alvos_nomes if n in site_de]
    log(f"FlareSolverr OK. Re-scrape de {len(alvos)} sites que retornaram 0.")

    novos = []
    por = {}
    falhas = {}
    for i, (nome, site, cidade, uf) in enumerate(alvos, 1):
        ims = []
        for suf in SUFIXOS:
            url = urljoin(site + "/", suf) if suf else site
            html, st = fs_get(url)
            if not html or len(html) < 2000:
                continue
            try:
                got = S.extrair(html, url, nome, cidade, uf)
            except Exception as e:
                falhas[nome] = f"extrair: {str(e)[:60]}"; got = []
            # dedup local por URL
            seen = {x["url"] for x in ims}
            for g in got:
                if g["url"] not in seen:
                    ims.append(g); seen.add(g["url"])
            if ims and suf in ("", "imoveis", "lotes", "leiloes"):
                break  # ja achou na home/listagem principal
        por[nome] = len(ims)
        novos.extend(ims)
        log(f"  [{i}/{len(alvos)}] {nome}: {len(ims)} imovel(is) renderizados [{site}]")
        time.sleep(1)

    # REGRA OBRIGATORIA secao 0: inserir TODOS os validos no banco (dedup por URL)
    inseridos = S.inserir_banco(novos)
    log(f"Imoveis recuperados via FlareSolverr: {len(novos)} | Inseridos novos no banco: {inseridos}")

    # append ao CSV de imoveis do dia (mantem artefato)
    if novos:
        ci = OUTPUT_DIR / f"imoveis_jucetins_{NOW.strftime('%Y-%m-%d')}.csv"
        campos = ["leiloeiro","junta","site","titulo","descricao","endereco","cidade","uf",
                  "lance_inicial","avaliacao","data_leilao","url","tipo","imagem","anexos"]
        existe = ci.exists()
        with open(ci, "a", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=campos)
            if not existe: w.writeheader()
            w.writerows(novos)
        log(f"CSV atualizado: {ci.name} (+{len(novos)} linhas)")

    # relatorio etapa 1
    com = {k: v for k, v in por.items() if v}
    linhas = "\n".join(f"| {k} | {v} |" for k, v in sorted(com.items(), key=lambda x: -x[1])) or "| — | 0 |"
    rel = f"""

---

## ETAPA 1 — Recuperação via FlareSolverr (SPA/Cloudflare) — {NOW.strftime('%d/%m/%Y %H:%M')}

Reprocessados {len(alvos)} sites que retornaram 0 no scraper inicial (SPA/JS/Cloudflare).
FlareSolverr renderiza o JS e a `extrair()` é reaplicada (mesmas regras: 1ª praça futura, dedup por URL).

- Imóveis recuperados (1ª praça futura): **{len(novos)}** | Inseridos novos no banco: **{inseridos}**
- Sites com recuperação > 0: {len(com)} de {len(alvos)}

| Leiloeiro (recuperado) | Imóveis |
|---|---|
{linhas}

**Diagnóstico-chave:** o erro original não era Cloudflare na maioria — era SPA cujo HTML só
aparece após JS (ex.: `webleiloes` home = 472 KB renderizada vs. ~1,4 KB no shell estático).
A correção genérica (render via FlareSolverr na **home** antes de tentar sufixos) resolve a
maior parte sem parser dedicado por domínio. Sites ainda em 0 exigem API interna (XHR/JSON).

**Gerado em:** {NOW.strftime('%d/%m/%Y %H:%M:%S')}
"""
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f:
        f.write(rel)
    log("Relatorio etapa 1 anexado ao .md")

    print("\n=== ETAPA 1 — RESUMO (recuperados via FlareSolverr) ===", flush=True)
    for k, v in sorted(com.items(), key=lambda x: -x[1]):
        print(f"  {v:3d}  {k}", flush=True)
    print(f"TOTAL recuperado: {len(novos)} | Novos no banco: {inseridos}", flush=True)

if __name__ == "__main__":
    main()
