# -*- coding: utf-8 -*-
"""
Utilitarios reaproveitaveis pelos scrapers de juntas (RN, AC, AP, RR/RO, PA, PE, BA...).
Implementa as 4 correcoes sugeridas na secao 41 de captura_dados_leiloes_v2.md:

  1. site_from_email()      -> inferencia automatica de dominio a partir do e-mail corporativo
  2. site_health()          -> sondagem de saude do site ANTES de renderizar (offline/nginx/ok)
  3. cards_from_json()      -> adapter SPA: extrai imoveis de payloads JSON de XHR/API interna
  4. upsert_multijunta()    -> dedup ciente de multi-junta (acrescenta junta em vez de descartar)

Sao funcoes puras/dependencia-leve (requests + stdlib). Os scrapers importam o que precisarem.
"""
import json
import os
import re
import socket
import sqlite3
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import urllib3

urllib3.disable_warnings()

# Provedores de e-mail genericos: dominio NAO representa site do leiloeiro.
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.com.br", "ymail.com",
    "hotmail.com", "hotmail.com.br", "outlook.com", "outlook.com.br", "live.com",
    "live.com.br", "msn.com", "icloud.com", "me.com", "bol.com.br", "uol.com.br",
    "ig.com.br", "terra.com.br", "globomail.com", "r7.com", "zipmail.com.br",
    "aol.com", "protonmail.com", "proton.me", "web.com", "lwmail.com.br",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Paginas-default que indicam "sem site publicado" (servidor no ar, conteudo ausente).
_PLACEHOLDER_RE = re.compile(
    r"welcome to nginx|apache2 (ubuntu|debian) default|it works!|"
    r"index of /|default web site page|site em constru|em constru[cç][aã]o|"
    r"this domain (is for sale|may be for sale)|domain (for sale|parking)|"
    r"plesk|cpanel|hostgator|website coming soon|coming soon|account suspended",
    re.I,
)


# ---------------------------------------------------------------------------
# 1. Inferencia de dominio a partir do e-mail corporativo
# ---------------------------------------------------------------------------
def site_from_email(email, scheme="https", www=True):
    """Deriva o site do dominio do e-mail, SE for dominio proprio (nao generico).

    >>> site_from_email("contato@colossoleiloes.com.br")
    'https://www.colossoleiloes.com.br'
    >>> site_from_email("fulano@gmail.com")   # generico -> None
    """
    if not email or "@" not in email:
        return None
    dom = email.split("@")[-1].strip().strip(".").lower()
    if not dom or "." not in dom or dom in GENERIC_EMAIL_DOMAINS:
        return None
    host = ("www." + dom) if www and not dom.startswith("www.") else dom
    return f"{scheme}://{host}"


def candidate_sites(site, email):
    """Lista ordenada de URLs a tentar: o site do cadastro primeiro, depois o
    dominio inferido do e-mail corporativo (que as vezes e o site REAL quando o
    PDF traz dominio errado/ausente, ex.: dgleiloes.com.br vs danielgarcialeiloes)."""
    out = []
    if site and site.strip():
        out.append(site.strip())
    alt = site_from_email(email)
    if alt:
        # nao duplicar o mesmo host ja presente
        alt_host = urlparse(alt).netloc.replace("www.", "")
        if not any(urlparse(u).netloc.replace("www.", "") == alt_host for u in out):
            out.append(alt)
    return out


# ---------------------------------------------------------------------------
# 2. Sondagem de saude do site (antes de gastar Playwright + FlareSolverr)
# ---------------------------------------------------------------------------
def site_health(url, timeout=10):
    """Retorna (vivo: bool, status: str). Detecta DNS invalido, conexao recusada,
    timeout e paginas-default (nginx/apache/parking/em-construcao).

    status e' uma das chaves: 'ok', 'dns_invalido', 'offline', 'timeout',
    'sem_site_publicado', 'http_<codigo>', 'erro'. So 'ok' (e http_403/503 que
    costumam ser Cloudflare) justificam render."""
    if not url or not url.strip():
        return False, "sem_site"
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or ""
    # resolucao DNS rapida — corta NXDOMAIN sem abrir socket HTTP
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "dns_invalido"
    except Exception:
        pass
    try:
        r = requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"},
                         timeout=timeout, verify=False, allow_redirects=True)
    except requests.exceptions.ConnectionError:
        return False, "offline"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception:
        return False, "erro"
    head = (r.text or "")[:6000]
    if 403 in (r.status_code,) or r.status_code == 503:
        # tipico de Cloudflare/anti-bot: vale a pena tentar o render (FlareSolverr)
        return True, f"http_{r.status_code}_challenge"
    if r.status_code >= 400:
        return False, f"http_{r.status_code}"
    if _PLACEHOLDER_RE.search(head):
        return False, "sem_site_publicado"
    if len(head.strip()) < 200:
        return False, "vazio"
    return True, "ok"


# ---------------------------------------------------------------------------
# 3. Adapter SPA: extrair imoveis de payloads JSON (XHR/API interna)
# ---------------------------------------------------------------------------
_JSON_TITLE_KEYS = ("titulo", "title", "nome", "name", "descricao", "description",
                    "tituloLote", "nomeLote", "bem", "descricaoBem")
_JSON_URL_KEYS = ("url", "link", "href", "slug", "permalink", "urlLote", "detalhe")
_JSON_IMG_KEYS = ("imagem", "image", "img", "foto", "thumbnail", "thumb", "capa",
                  "urlImagem", "imagemPrincipal")
_JSON_PRICE_KEYS = ("preco", "price", "valor", "lance", "lanceInicial", "valorInicial",
                    "lance_inicial", "valorLance", "valorAvaliacao")
_JSON_DATE_KEYS = ("data", "date", "dataLeilao", "dataPraca", "data1Praca", "dataInicio",
                   "primeiraPraca", "data_primeira_praca", "encerramento", "dataAbertura")
_DATE_RE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})|(\d{4})-(\d{2})-(\d{2})")


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    # match case-insensitive
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, "", []):
            return low[k.lower()]
    return None


def _looks_like_lote(d):
    """Heuristica: dict que tem titulo/descricao E (preco OU data OU url) parece um lote."""
    if not isinstance(d, dict):
        return False
    has_title = _first(d, _JSON_TITLE_KEYS) is not None
    has_meta = any(_first(d, ks) is not None for ks in
                   (_JSON_PRICE_KEYS, _JSON_DATE_KEYS, _JSON_URL_KEYS))
    return has_title and has_meta


def walk_json(obj, depth=0):
    """Itera recursivamente todos os dicts dentro de um JSON (listas/objetos aninhados)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v, depth + 1)


def cards_from_json(payloads, base_url=""):
    """Recebe lista de objetos JSON (capturados de XHR) e devolve dicts de card
    {titulo,url,imagem,preco,datas_txt} para os objetos que parecem lotes/imoveis."""
    from urllib.parse import urljoin
    cards = {}
    for payload in payloads:
        for d in walk_json(payload):
            if not _looks_like_lote(d):
                continue
            titulo = str(_first(d, _JSON_TITLE_KEYS) or "").strip()
            if len(titulo) < 8:
                continue
            url = _first(d, _JSON_URL_KEYS) or ""
            if url and base_url and not str(url).startswith("http"):
                url = urljoin(base_url, str(url))
            img = _first(d, _JSON_IMG_KEYS) or ""
            if img and base_url and not str(img).startswith("http") and not str(img).startswith("data:"):
                img = urljoin(base_url, str(img))
            preco = _first(d, _JSON_PRICE_KEYS)
            data_raw = _first(d, _JSON_DATE_KEYS)
            key = str(url) or titulo
            cards[key] = {
                "titulo": titulo[:200], "url": str(url), "imagem": str(img),
                "preco": _fmt_price(preco), "datas_txt": _fmt_dates(d, data_raw),
            }
    return cards


def _fmt_price(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(v)


def _fmt_dates(d, data_raw):
    """Concatena qualquer texto de data encontrado no dict para o parser de datas do scraper."""
    txt = " ".join(str(_first(d, (k,)) or "") for k in _JSON_DATE_KEYS)
    if data_raw:
        txt += " " + str(data_raw)
    return txt


# ---------------------------------------------------------------------------
# 4. Dedup ciente de multi-junta
# ---------------------------------------------------------------------------
def merge_juntas(atual, nova):
    """Une a junta existente com a nova preservando ordem e sem duplicar.
    >>> merge_juntas('JUCER/RR-RO', 'JUCERN/RN')
    'JUCER/RR-RO; JUCERN/RN'
    """
    juntas = [j.strip() for j in (atual or "").split(";") if j.strip()]
    if nova and nova.strip() and nova.strip() not in juntas:
        juntas.append(nova.strip())
    return "; ".join(juntas)


# ---------------------------------------------------------------------------
# 5. Roteamento por plataforma (le plataformas.json)
# ---------------------------------------------------------------------------
_PLAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plataformas.json")
_PLAT_CACHE = None


def _load_plataformas():
    global _PLAT_CACHE
    if _PLAT_CACHE is None:
        try:
            with open(_PLAT_PATH, encoding="utf-8") as f:
                _PLAT_CACHE = json.load(f).get("plataformas", {})
        except Exception:
            _PLAT_CACHE = {}
    return _PLAT_CACHE


def detectar_plataforma(site, html=None, leiloeiro=None):
    """Identifica a plataforma de um site usando plataformas.json.

    Casa por dominio (deteccao.dominio_contem), marcador no HTML
    (deteccao.marcadores_html), ou nome do leiloeiro (deteccao.leiloeiro_igual).
    Retorna dict {chave, ...config} pronto para escolher adapter/tier, ou o
    fallback 'html_generico' quando nada casa. Assim o scraper pula o
    reconhecimento (Parte III) em fontes ja conhecidas e vai direto ao extrator.

    >>> detectar_plataforma("https://www.megaleiloes.com.br/imoveis")["chave"]
    'megaleiloes'
    """
    plats = _load_plataformas()
    host = urlparse(site if "//" in (site or "") else "//" + (site or "")).netloc.lower()
    html_l = (html or "").lower()
    fallback = None
    for chave, cfg in plats.items():
        det = cfg.get("deteccao", {})
        if det.get("fallback"):
            fallback = {"chave": chave, **cfg}
            continue
        if any(d in host for d in det.get("dominio_contem", [])):
            return {"chave": chave, **cfg}
        if leiloeiro and leiloeiro in det.get("leiloeiro_igual", []):
            return {"chave": chave, **cfg}
        if html_l and any(m.lower() in html_l for m in det.get("marcadores_html", [])):
            return {"chave": chave, **cfg}
    return fallback


# ---------------------------------------------------------------------------
# 6. Extracao de galeria completa de fotos e de anexos (PDFs)
#    Parte VII do master: "capture TODAS as fotos" + "anexos edital/matricula/laudo"
# ---------------------------------------------------------------------------
# Lixo que nao e foto de imovel (logo, icone, placeholder, sprite...).
_IMG_LIXO_RE = re.compile(
    r"logo|sprite|icon|placeholder|avatar|blank|pixel|spacer|loading|"
    r"selo|bandeira|favicon|whatsapp|facebook|instagram|/ads?/|banner",
    re.I,
)
_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?[^\s\"']*)?$", re.I)
# Atributos onde a URL real da imagem costuma estar (lazy-load).
_IMG_ATTR_RE = re.compile(
    r'(?:data-src|data-lazy|data-original|data-srcset|srcset|src|content)\s*=\s*'
    r'["\']([^"\']+)["\']',
    re.I,
)
_PDF_RE = re.compile(r'href\s*=\s*["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', re.I)


def _maior_do_srcset(valor):
    """De um srcset ('a.jpg 320w, b.jpg 1024w') devolve a URL de maior largura."""
    melhor, melhor_w = None, -1
    for parte in valor.split(","):
        toks = parte.strip().split()
        if not toks:
            continue
        url = toks[0]
        w = 0
        if len(toks) > 1 and toks[1].rstrip("wx").isdigit():
            w = int(toks[1].rstrip("wx"))
        if w >= melhor_w:
            melhor, melhor_w = url, w
    return melhor


def extrair_galeria(html, base_url=""):
    """Lista ordenada e deduplicada de URLs de imagens reais do imovel.

    Resolve relativas->absolutas, prefere a maior resolucao do srcset, ignora
    logos/icones/placeholders. Cobre lazy-load (data-src/data-lazy/data-original).
    """
    urls, vistos = [], set()
    for tag in re.findall(r"<(?:img|source|meta)[^>]+>", html or "", re.I):
        for m in _IMG_ATTR_RE.finditer(tag):
            val = m.group(1).strip()
            if not val or val.startswith("data:"):
                continue
            if " " in val and ("w," in val or val.rstrip().endswith(("w", "x"))):
                val = _maior_do_srcset(val) or val
            url = urljoin(base_url, val) if base_url else val
            if not _IMG_EXT_RE.search(url) and "og:image" not in tag.lower():
                continue
            if _IMG_LIXO_RE.search(url):
                continue
            if url not in vistos:
                vistos.add(url)
                urls.append(url)
    return urls


def _tipo_anexo(texto):
    t = (texto or "").lower()
    if "edital" in t:
        return "edital"
    if "matr" in t:           # matricula / matrícula
        return "matricula"
    if "laudo" in t or "avalia" in t:
        return "laudo"
    return "outro"


def extrair_anexos(html, base_url=""):
    """Lista de {tipo, url} para os PDFs linkados (edital/matricula/laudo/outro)."""
    out, vistos = [], set()
    for m in re.finditer(
        r'<a[^>]+href\s*=\s*["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a>',
        html or "", re.I | re.S,
    ):
        href, rotulo = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        url = urljoin(base_url, href) if base_url else href
        if url in vistos:
            continue
        vistos.add(url)
        out.append({"tipo": _tipo_anexo(rotulo + " " + href), "url": url})
    # PDFs fora de <a> (ex.: em data-*), sem rotulo:
    for m in _PDF_RE.finditer(html or ""):
        url = urljoin(base_url, m.group(1)) if base_url else m.group(1)
        if url not in vistos:
            vistos.add(url)
            out.append({"tipo": _tipo_anexo(url), "url": url})
    return out


# ---------------------------------------------------------------------------
# 7. Persistencia nas tabelas 1->N (criadas por migrar_imagens_anexos.py)
# ---------------------------------------------------------------------------
def _con(db):
    return db if isinstance(db, sqlite3.Connection) else sqlite3.connect(db)


def salvar_galeria(db, imovel_id, urls, larguras=None):
    """Upsert da galeria em imovel_imagens. urls em ordem; 1a = principal.
    Aceita um caminho de banco OU uma sqlite3.Connection (nao fecha se passada)."""
    if not urls:
        return 0
    con = _con(db)
    agora = datetime.now().isoformat(timespec="seconds")
    larguras = larguras or {}
    con.executemany(
        "INSERT OR IGNORE INTO imovel_imagens "
        "(imovel_id, url, ordem, principal, largura, capturado_em) VALUES (?,?,?,?,?,?)",
        [(imovel_id, u, i, 1 if i == 0 else 0, larguras.get(u), agora)
         for i, u in enumerate(urls)],
    )
    con.commit()
    if not isinstance(db, sqlite3.Connection):
        con.close()
    return len(urls)


def salvar_anexos(db, imovel_id, anexos):
    """Upsert de anexos em imovel_anexos. anexos = [{tipo,url,caminho_local?,descricao?}]."""
    if not anexos:
        return 0
    con = _con(db)
    agora = datetime.now().isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR IGNORE INTO imovel_anexos "
        "(imovel_id, tipo, url, caminho_local, descricao, capturado_em) VALUES (?,?,?,?,?,?)",
        [(imovel_id, a.get("tipo", "outro"), a["url"],
          a.get("caminho_local"), a.get("descricao"), agora) for a in anexos],
    )
    con.commit()
    if not isinstance(db, sqlite3.Connection):
        con.close()
    return len(anexos)


if __name__ == "__main__":
    # smoke test rapido das funcoes puras
    assert site_from_email("contato@colossoleiloes.com.br") == "https://www.colossoleiloes.com.br"
    assert site_from_email("x@gmail.com") is None
    assert site_from_email("davieduardopaulim@yahoo.com.br") is None
    assert site_from_email("contato@dgleiloes.com.br") == "https://www.dgleiloes.com.br"
    assert merge_juntas("JUCER/RR-RO", "JUCERN/RN") == "JUCER/RR-RO; JUCERN/RN"
    assert merge_juntas("JUCERN/RN", "JUCERN/RN") == "JUCERN/RN"
    assert candidate_sites("https://danielgarcialeiloes.com.br", "contato@dgleiloes.com.br") == \
        ["https://danielgarcialeiloes.com.br", "https://www.dgleiloes.com.br"]
    cs = cards_from_json([{"lotes": [{"titulo": "Casa em Natal/RN", "valorInicial": 150000.0,
                                      "dataLeilao": "10/07/2026", "url": "/lote/1"}]}],
                         base_url="https://x.com")
    assert any("Casa em Natal" in c["titulo"] for c in cs.values()), cs

    # 5. detectar_plataforma
    assert detectar_plataforma("https://www.megaleiloes.com.br/imoveis")["chave"] == "megaleiloes"
    assert detectar_plataforma("https://qualquer-site-desconhecido.com.br")["chave"] == "html_generico"
    assert detectar_plataforma("https://x.com", leiloeiro="Central Sul de Leilões")["chave"] == "central_sul"

    # 6. galeria: pega maior do srcset, ignora logo, resolve relativa, dedup
    _html = ('<img src="/logo.png"><img data-src="/fotos/casa1.jpg">'
             '<img srcset="t.jpg 320w, /fotos/casa1-grande.jpg 1024w">'
             '<img src="https://cdn.x.com/fotos/casa1.jpg">')
    g = extrair_galeria(_html, base_url="https://site.com.br/lote/1")
    assert "https://site.com.br/fotos/casa1.jpg" in g
    assert "https://site.com.br/fotos/casa1-grande.jpg" in g
    assert all("logo" not in u for u in g), g

    # 6b. anexos: tipa por rotulo/href, dedup
    _a = ('<a href="/docs/edital.pdf">Edital do leilão</a>'
          '<a href="/docs/matricula-123.pdf">Matrícula</a>'
          '<a href="/docs/edital.pdf">repetido</a>')
    ax = extrair_anexos(_a, base_url="https://site.com.br")
    tipos = {x["tipo"] for x in ax}
    assert "edital" in tipos and "matricula" in tipos, ax
    assert len(ax) == 2, ax  # dedup do edital repetido

    print("scraper_commons: todos os smoke tests passaram OK")
