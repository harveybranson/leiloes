# Sistema de Scraping de Leilões

Documentação técnica para coleta estruturada de dados de sites de leiloeiros: lotes, fotos, datas, valores e anexos (editais, matrículas, laudos).

## Princípios

- **Tente o caminho mais barato primeiro.** HTTP estático → API JSON interna → Playwright. Só escale a complexidade quando o site exigir.
- **Robustez, não força.** Retries com backoff, detecção de mudança de layout e logs cobrem mais casos de forma sustentável do que tentar burlar proteções.
- **Respeite o site.** Cheque `robots.txt` e os Termos de Uso, aplique rate limiting e não contorne CAPTCHA / proteção anti-bot ativa. Muitos dados de leilão são públicos por obrigação legal, então raramente é preciso forçar.
- **Login só com credenciais próprias.** Use sessões que você mesmo cadastrou; persista cookies/storage state para não relogar a cada execução.

## Escolha de ferramenta por tipo de site

| Tipo de site | Ferramenta | Observação |
|---|---|---|
| HTML estático (servidor renderiza) | `httpx` + `selectolax`/`BeautifulSoup` | Rápido e escalável. Teste primeiro. |
| SPA / JavaScript pesado | Playwright | Renderiza tudo, mais lento. |
| API JSON interna | `httpx` direto no endpoint | Mais eficiente. Descubra via DevTools → Network. |

> **Dica:** muitos sites de leilão carregam lotes via API JSON interna. Encontrar esse endpoint (DevTools → aba Network) permite pular HTML e Playwright e obter dados já estruturados.

## Arquitetura

1. **Descoberta** — dado um link inicial, identifique a plataforma. Muitos leiloeiros usam as mesmas plataformas (Superbid, Sodré Santoro, Mega Leilões, sistemas white-label). Detectar a plataforma permite reaproveitar o mesmo extrator para vários sites.
2. **Login** (quando necessário) — Playwright com sessão persistente.
3. **Listagem** — pagine pelos lotes coletando URLs.
4. **Extração por lote** — título, descrição, datas (1ª/2ª praça), valores, status.
5. **Mídia e anexos** — baixe imagens e PDFs seguindo os links.

### Camada de adaptadores

Um extrator por plataforma, selecionado por detecção de domínio/HTML. Resolve vários sites com pouco código.

- **Fallback inteligente:** tenta HTTP → tenta JSON interno → cai pro Playwright só se necessário.
- **Schema unificado:** normalize tudo (datas, valores, status) num formato único, independente da origem.

## Esqueleto de código

```python
from playwright.sync_api import sync_playwright
import httpx, pathlib, time

class LeilaoScraper:
    def __init__(self, state_file="session.json"):
        self.state_file = pathlib.Path(state_file)

    def login(self, login_url, user, pwd, user_sel, pwd_sel, submit_sel):
        with sync_playwright() as p:
            b = p.chromium.launch(headless=False)  # headful no 1º login
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto(login_url)
            pg.fill(user_sel, user)
            pg.fill(pwd_sel, pwd)
            pg.click(submit_sel)
            pg.wait_for_load_state("networkidle")
            ctx.storage_state(path=str(self.state_file))  # salva sessão
            b.close()

    def coletar_lote(self, url):
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(
                storage_state=str(self.state_file) if self.state_file.exists() else None
            )
            pg = ctx.new_page()
            pg.goto(url, wait_until="networkidle")
            dado = {
                "url": url,
                "titulo": pg.locator("h1").first.inner_text(),
                "imagens": pg.locator("img.lote-foto").evaluate_all(
                    "els => els.map(e => e.src)"),
                "anexos": pg.locator("a[href$='.pdf']").evaluate_all(
                    "els => els.map(e => e.href)"),
            }
            b.close()
            return dado

    def baixar(self, urls, pasta="anexos"):
        pathlib.Path(pasta).mkdir(exist_ok=True)
        with httpx.Client(timeout=60) as c:
            for u in urls:
                r = c.get(u)
                nome = u.split("/")[-1].split("?")[0]
                (pathlib.Path(pasta) / nome).write_bytes(r.content)
                time.sleep(1)  # educado com o servidor
```

> Os seletores (`h1`, `img.lote-foto`, etc.) mudam por site — é aí que entra a camada de extrator por plataforma.

## Boas práticas operacionais

- Rate limiting entre requisições (`time.sleep`) para não sobrecarregar o servidor.
- Retries com backoff exponencial em falhas de rede.
- Persistência de sessão para evitar logins repetidos.
- Logs e detecção de mudança de layout para manutenção.
- Schema de saída unificado (JSON/banco) para consumo posterior.
