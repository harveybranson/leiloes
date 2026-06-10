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
import re
import socket
from urllib.parse import urlparse

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
    print("scraper_commons: todos os smoke tests passaram OK")
