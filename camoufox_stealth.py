#!/usr/bin/env python3
"""
camoufox_stealth.py — sessao stealth (Firefox endurecido) p/ as fontes que detectam
ate o navegador transiente.

camoufox: Firefox patcheado com fingerprint spoofado no nivel do C++ (NAO observavel do
JS, ao contrario do playwright-stealth), geo-match do fingerprint com o IP do proxy, e
imunidade estrutural a deteccao de CDP do Chrome (porque e Firefox).

Modos:
  session  resolve o desafio, persiste storage_state e EXPORTA cookies+UA (sessao_httpx.json)
           -> promova pro fetch_evasive.py com --impersonate firefox  (COERENCIA DE TLS!)
  content  renderiza e devolve o page.content()  -> alimenta html_parser / html_json_extractor

No seu VPS headless use --virtual (headless="virtual", roda um Firefox real sob Xvfb,
bem menos detectavel que o headless puro). Requer: apt-get install -y xvfb

Deps:
  pip install "camoufox[geoip]"
  python -m camoufox fetch          # baixa o Firefox patcheado (GitHub releases)

Exemplos:
  python camoufox_stealth.py session --url https://site --proxy http://user:pass@host:porta --geoip --virtual
  python camoufox_stealth.py content --url https://site/imoveis --geoip --virtual --out pagina.html
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox


def proxy_dict(p: str | None) -> dict | None:
    """http://user:pass@host:porta -> dict no formato do Playwright/camoufox."""
    if not p:
        return None
    u = urlparse(p)
    d = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        d["username"] = u.username
    if u.password:
        d["password"] = u.password
    return d


def launch(args) -> Camoufox:
    kw = dict(humanize=True, block_webrtc=True, block_images=not args.images)
    # headless: "virtual" (Xvfb, melhor p/ stealth no servidor) > True > False (visivel)
    kw["headless"] = "virtual" if args.virtual else (False if args.headed else True)
    pr = proxy_dict(args.proxy)
    if pr:
        kw["proxy"] = pr
    if args.geoip:
        kw["geoip"] = True               # deriva timezone/locale/coords do IP do proxy
    elif args.locale:
        kw["locale"] = args.locale
    if args.os:
        kw["os"] = args.os
    return Camoufox(**kw)


def cmd_session(args) -> None:
    with launch(args) as browser:
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        if args.headed:
            print("Resolva o desafio (Cloudflare/captcha) se aparecer e tecle ENTER...")
            input()
        page.wait_for_timeout(1500)
        state = page.context.storage_state(path=args.state)
        cookies = {c["name"]: c["value"] for c in state["cookies"]}
        ua = page.evaluate("() => navigator.userAgent")

    export = {"headers": {"User-Agent": ua, "Accept": "application/json"}, "cookies": cookies}
    with open(args.export, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"Sessao salva em {args.state}; {len(cookies)} cookies -> {args.export}")
    print(f"Promova:  python fetch_evasive.py --session {args.export} --impersonate firefox  (coerencia de TLS)")


def cmd_content(args) -> None:
    with launch(args) as browser:
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        html = page.content()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"OK: HTML salvo em {args.out} ({len(html)} bytes) -> passe pro html_parser/html_json_extractor")
    else:
        print(html[:1000] + ("\n... (truncado)" if len(html) > 1000 else ""))


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", required=True)
    common.add_argument("--proxy", help="http://user:pass@host:porta (residencial BR, sticky)")
    common.add_argument("--geoip", action="store_true", help="geo-match do fingerprint com o IP (extra geoip)")
    common.add_argument("--locale", default="pt-BR")
    common.add_argument("--os", help="windows | macos | linux")
    common.add_argument("--virtual", action="store_true", help="headless=virtual (Xvfb) — use no VPS")
    common.add_argument("--headed", action="store_true", help="visivel, p/ resolver desafio na mao (local)")
    common.add_argument("--images", action="store_true", help="carrega imagens (default: bloqueia)")

    ap = argparse.ArgumentParser(description="Sessao stealth com camoufox (Firefox endurecido)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("session", parents=[common], help="estabelece sessao e exporta cookies")
    s.add_argument("--state", default="state.json")
    s.add_argument("--export", default="sessao_httpx.json")
    s.set_defaults(func=cmd_session)

    c = sub.add_parser("content", parents=[common], help="renderiza e devolve o HTML")
    c.add_argument("--out")
    c.set_defaults(func=cmd_content)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
