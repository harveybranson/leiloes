# -*- coding: utf-8 -*-
"""
probe_flaresolverr.py — verifica se o FlareSolverr está no ar e consegue resolver uma URL.

Referência: captura_dados_leiloes_master.md (Parte VI.2 / Parte XI). Use após subir o
docker-compose.flaresolverr.yml, antes de rodar o scraper, para confirmar que o fallback
de bloqueio (sc.fetch_flaresolverr) vai funcionar.

Uso:
  python probe_flaresolverr.py
  python probe_flaresolverr.py --url https://www.megaleiloes.com.br/imoveis
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper_commons as sc


def main():
    ap = argparse.ArgumentParser(description="Probe do FlareSolverr.")
    ap.add_argument("--url", default="https://www.google.com")
    args = ap.parse_args()
    print(f"FlareSolverr: {sc.FLARESOLVERR_URL}")
    html = sc.fetch_flaresolverr(args.url, timeout_ms=45000)
    if html is None:
        print("✗ indisponível — suba com:")
        print("  docker compose -f docker-compose.flaresolverr.yml up -d")
        sys.exit(1)
    bloq = sc.parece_bloqueio(html)
    print(f"✓ respondeu ({len(html)} bytes) para {args.url}"
          + ("  ⚠ ainda parece bloqueio" if bloq else ""))
    sys.exit(0)


if __name__ == "__main__":
    main()
