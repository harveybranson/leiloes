# Guia Completo de Captura de Dados de Sites de Leilão (v2)

> Documento de referência para extração exaustiva de informações de qualquer tipo de site de leilão — incluindo sites públicos, autenticados (login/senha), com JavaScript pesado, APIs ocultas e proteções anti-bot.
>
> **Novidade da v2:** seção dedicada a contornar o **Cloudflare Managed Challenge / Turnstile** que bloqueia paginação (`?pag=2` em diante), com fluxo completo de sessão persistida no Playwright (seção 13).

---

## 1. Princípio fundamental: sempre procure a fonte de dados mais "limpa" primeiro

Antes de escrever qualquer scraper de HTML, investigue se existe uma forma estruturada de obter os dados. A ordem de preferência, da mais eficiente/confiável para a menos, é:

1. **API oficial / pública** do site (documentada).
2. **API interna (XHR/Fetch)** que o front-end consome — visível nas DevTools.
3. **Dados embutidos no HTML** (JSON-LD, `__NEXT_DATA__`, `window.__INITIAL_STATE__`, microdata).
4. **Feeds estruturados** (RSS, Atom, sitemaps XML, exports CSV/Excel).
5. **Scraping de HTML renderizado** (último recurso).

A regra de ouro: **APIs retornam dados exatos e tipados; o HTML é frágil e muda com frequência.** Sempre que possível, mire na API.

---

## 2. Reconhecimento do site (etapa obrigatória antes de codar)

Para cada site novo, faça este diagnóstico nas DevTools do navegador (F12):

### 2.1. Aba Network (a mais importante)
- Filtre por **Fetch/XHR**. Navegue pelo site (busca, abrir um lote, paginar).
- Observe as requisições que retornam **JSON** — essas são as APIs internas.
- Anote: URL do endpoint, método (GET/POST), parâmetros de query, corpo (payload), e headers necessários (`Authorization`, `X-CSRF-Token`, `Cookie`, `User-Agent`, `Referer`).
- Clique com o botão direito numa requisição → **Copy → Copy as cURL**. Isso replica a chamada exata, com todos os headers e cookies.

### 2.2. Identifique a tecnologia
- **HTML estático/servidor (SSR):** dados já vêm no HTML inicial → scraping direto funciona.
- **SPA (React/Vue/Angular):** o HTML inicial é quase vazio; os dados chegam via XHR → use a API interna ou navegador automatizado.
- **Next.js:** procure `<script id="__NEXT_DATA__">` — contém um JSON completo da página.
- **Nuxt:** procure `window.__NUXT__`.
- Use a extensão **Wappalyzer** para identificar o stack rapidamente.

### 2.3. Verifique fontes estruturadas
- Acesse `/robots.txt` e `/sitemap.xml` → mapeiam URLs e às vezes revelam endpoints.
- Procure JSON-LD: `<script type="application/ld+json">` — comum em e-commerce/leilões, traz dados de produto padronizados (schema.org).

---

## 3. Métodos de captura (do mais leve ao mais robusto)

### 3.1. API oficial (sempre primeiro)
Muitos sites de leilão (carros, judiciais, imóveis, arte) oferecem APIs ou exports. Procure por seções "Desenvolvedores", "API", "Integração" ou contate o suporte. Vantagens: dados exatos, estáveis, legais e com suporte a paginação/filtros.

### 3.2. API interna via requisições HTTP
A abordagem mais eficiente para a maioria dos casos. Replica o que o front-end faz:

```python
import requests

headers = {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept": "application/json",
    "Referer": "https://leilao.com/lotes",
}
params = {"categoria": "veiculos", "pagina": 1, "ordenar": "preco"}

r = requests.get("https://api.leilao.com/v1/lotes", headers=headers, params=params)
dados = r.json()
for lote in dados["resultados"]:
    print(lote["id"], lote["titulo"], lote["lance_atual"])
```

### 3.3. Scraping de HTML estático
Quando os dados vêm no HTML do servidor:

```python
import requests
from bs4 import BeautifulSoup

r = requests.get("https://leilao.com/lote/123", headers=headers)
soup = BeautifulSoup(r.text, "html.parser")

titulo = soup.select_one("h1.lote-titulo").get_text(strip=True)
lance = soup.select_one(".lance-atual").get_text(strip=True)
```

Para extração mais precisa e tolerante a mudanças, prefira seletores por atributos estáveis (`data-*`, `id`) em vez de classes CSS voláteis. XPath via `lxml` também é uma opção robusta.

### 3.4. Dados embutidos (JSON no HTML)
Mais confiável que raspar texto da DOM:

```python
import json, re

# Exemplo Next.js
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
data = json.loads(m.group(1))

# Exemplo JSON-LD
soup = BeautifulSoup(r.text, "html.parser")
for tag in soup.find_all("script", type="application/ld+json"):
    item = json.loads(tag.string)
    # item contém nome, preço, datas etc. no padrão schema.org
```

### 3.5. Navegador automatizado (sites com JS pesado / anti-bot)
Use **Playwright** (recomendado) ou Selenium quando o conteúdo só aparece após execução de JavaScript, ou quando há proteções que bloqueiam requisições diretas.

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://leilao.com/lotes", wait_until="networkidle")
    page.wait_for_selector(".lote-card")

    lotes = page.eval_on_selector_all(".lote-card", """
        els => els.map(e => ({
            titulo: e.querySelector('.titulo')?.innerText,
            lance: e.querySelector('.lance')?.innerText,
            link: e.querySelector('a')?.href
        }))
    """)
    print(lotes)
    browser.close()
```

**Dica avançada:** mesmo usando Playwright, intercepte as respostas de rede (`page.on("response", ...)`) para capturar o JSON da API interna em vez de raspar a DOM — combina robustez com dados limpos.

---

## 4. Sites com login e senha (autenticação)

A área restrita é onde estão os dados mais valiosos (histórico de lances, valores de avaliação, documentos). Estratégias, da mais simples à mais robusta:

### 4.1. Login por formulário + sessão HTTP
Funciona quando o login é um POST simples:

```python
import requests

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 ..."})

# 1. Às vezes é preciso pegar um token CSRF da página de login primeiro
login_page = session.get("https://leilao.com/login")
# extrair csrf do HTML (campo hidden ou meta)

payload = {
    "email": "usuario@exemplo.com",
    "senha": "minhasenha",
    "_csrf_token": "valor_extraido",
}
session.post("https://leilao.com/login", data=payload)

# 2. A sessão agora carrega os cookies de autenticação
restrito = session.get("https://leilao.com/minha-area/lances")
```

Inspecione na aba Network qual é o endpoint real de login (pode ser `/api/auth/login`), o formato do payload (form-data vs JSON) e os campos obrigatórios.

### 4.2. Reaproveitamento de cookies/token (driblar o login)
A forma mais prática quando há CAPTCHA, 2FA ou login complexo: **faça login manualmente no navegador** e reutilize a sessão.

```python
# Copie os cookies da sessão logada (DevTools → Application → Cookies)
session.cookies.set("session_id", "valor_copiado", domain="leilao.com")

# Ou, para APIs com token Bearer:
session.headers["Authorization"] = "Bearer eyJhbGc..."
```

Atenção a validade: cookies/tokens expiram. Para automação contínua, você precisará renovar periodicamente.

### 4.3. Persistência de sessão com Playwright (recomendado para autenticados)
O Playwright salva o estado de autenticação em arquivo e o reutiliza, evitando logar a cada execução:

```python
from playwright.sync_api import sync_playwright

# --- Execução única: logar e salvar o estado ---
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # visível p/ resolver CAPTCHA/2FA manual
    page = browser.new_page()
    page.goto("https://leilao.com/login")
    page.fill("#email", "usuario@exemplo.com")
    page.fill("#senha", "minhasenha")
    page.click("button[type=submit]")
    page.wait_for_url("**/minha-area/**")
    page.context.storage_state(path="auth.json")  # salva cookies + localStorage
    browser.close()

# --- Execuções seguintes: reutilizar o estado salvo ---
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(storage_state="auth.json")
    page = context.new_page()
    page.goto("https://leilao.com/minha-area/lances")
    # já autenticado, sem refazer login
    browser.close()
```

### 4.4. Lidando com 2FA / MFA
- **TOTP (app autenticador):** se você possui o segredo (seed), gere o código com `pyotp.TOTP(seed).now()`.
- **SMS/e-mail:** normalmente exige intervenção manual; use modo `headless=False` e insira o código, depois salve o `storage_state`.
- **Sessões longas:** marque "lembrar-me" no login manual para obter cookies de longa duração e reduzir a frequência de reautenticação.

---

## 5. Tipos de sites de leilão e particularidades

### 5.1. Leilões judiciais / extrajudiciais (imóveis, veículos)
- Frequentemente SSR com HTML estático → scraping direto + JSON-LD funciona bem.
- Documentos (editais, matrículas) em PDF → baixe e extraia texto com `pdfplumber` ou OCR (`pytesseract`) para PDFs escaneados.
- Datas de praça/leilão, valor de avaliação e valor mínimo são campos críticos — capture-os de forma tipada.

### 5.2. Leilões de veículos (seguradoras, financeiras, pátios)
- Muitas vezes exigem login de comprador credenciado.
- Galerias de fotos carregam via JS → use Playwright ou capture as URLs das imagens da API interna.
- Dados de lance costumam atualizar em tempo real (ver seção 6).

### 5.3. Marketplaces de leilão online (arte, colecionáveis, geral)
- Quase sempre SPAs com API interna rica em JSON → priorize a API interna.
- Paginação por cursor/offset; respeite os parâmetros descobertos na Network.

### 5.4. Leilões com lances em tempo real (ao vivo)
- Os valores chegam via **WebSocket** ou **polling**. Na aba Network, filtre por **WS** para ver o WebSocket.
- Para capturar, conecte-se ao WebSocket diretamente:

```python
import websocket, json

def on_message(ws, msg):
    evento = json.loads(msg)
    print("Novo lance:", evento)

ws = websocket.WebSocketApp("wss://leilao.com/socket",
                            on_message=on_message,
                            header=["Cookie: session_id=..."])
ws.run_forever()
```

---

## 6. Captura de dados em tempo real e atualizações

- **WebSocket:** conexão persistente, ideal para lances ao vivo. Replique os headers/cookies da sessão autenticada.
- **Polling de API:** requisitar o endpoint de lance em intervalos regulares (respeitando rate limits).
- **Server-Sent Events (SSE):** alguns sites usam `text/event-stream`; consuma com `requests` em modo stream ou `httpx`.

---

## 7. Contornando proteções anti-bot (legitimamente)

Use apenas em sites cujos termos permitem, e sempre com moderação:

- **Headers realistas:** copie `User-Agent`, `Accept`, `Accept-Language`, `Referer` de um navegador real.
- **Rate limiting / delays:** insira pausas aleatórias entre requisições (ex.: 2–6 s) para imitar comportamento humano e não sobrecarregar o servidor.
- **Rotação de IP/proxies:** para volumes grandes, distribua requisições entre proxies (residenciais quando necessário). Configure no `requests` via `proxies=` ou no Playwright via `proxy=`.
- **Stealth:** plugins como `playwright-stealth` reduzem fingerprints de automação.
- **Cloudflare / WAF:** ferramentas como `curl_cffi` (imita TLS de navegador) ou navegador real automatizado costumam passar onde `requests` falha. Para o **Managed Challenge / Turnstile**, ver a seção 13 dedicada.
- **CAPTCHA:** prefira reaproveitar sessão logada manualmente. Serviços de resolução existem, mas avalie a legalidade e os termos antes.

---

## 8. Extração e validação de dados exatos

Para garantir precisão de cada campo:

- **Tipagem:** converta preços para `Decimal`, datas para `datetime`, limpando símbolos (`R$`, `.`, `,`).
- **Normalização:** padronize unidades, fusos horários e formatos de data por site.
- **Validação com schema:** use `pydantic` para validar cada lote extraído e detectar campos faltantes/malformados antes de salvar.

```python
from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal

class Lote(BaseModel):
    id: str
    titulo: str
    lance_atual: Decimal
    data_leilao: datetime
    url: str
```

- **Detecção de mudança de layout:** se um seletor retornar vazio inesperadamente, gere alerta — o site provavelmente mudou.

---

## 9. Armazenamento e organização

- **Volumes pequenos:** CSV, JSON ou SQLite.
- **Volumes maiores / consultas:** PostgreSQL (com índices por leilão, lote, data).
- **Deduplicação:** use o ID do lote como chave única; faça *upsert* para atualizar lances sem duplicar registros.
- **Histórico:** mantenha snapshots com timestamp para acompanhar a evolução dos lances.

---

## 10. Stack recomendada (Python)

| Necessidade | Ferramenta |
|---|---|
| Requisições HTTP | `requests`, `httpx` |
| Parsing HTML | `BeautifulSoup`, `lxml` (XPath) |
| Navegador automatizado | `Playwright` (preferido), `Selenium` |
| Anti-bloqueio TLS | `curl_cffi` |
| WebSocket | `websocket-client`, `websockets` |
| TOTP/2FA | `pyotp` |
| PDF | `pdfplumber`, `pytesseract` (OCR) |
| Validação de dados | `pydantic` |
| Framework de scraping em escala | `Scrapy` |
| Orquestração/agendamento | `APScheduler`, `cron`, Airflow |

---

## 11. Fluxo de trabalho recomendado (resumo)

1. **Investigue** o site nas DevTools (Network/XHR/WS, `__NEXT_DATA__`, JSON-LD, sitemap).
2. **Priorize a API** (oficial → interna) antes de raspar HTML.
3. Se houver **login**, capture o fluxo de autenticação ou reutilize sessão (`storage_state`).
4. Se for **SPA ou anti-bot**, use Playwright (intercepte a API interna quando possível).
5. Se houver **Cloudflare Managed Challenge / Turnstile** bloqueando paginação, ver seção 13.
6. Se houver **lances ao vivo**, conecte ao WebSocket.
7. **Valide e tipe** cada campo com `pydantic`.
8. **Armazene** com deduplicação e histórico.
9. **Respeite** rate limits, robots.txt e os termos do site.

---

## 12. Considerações legais e éticas

- **Leia os Termos de Serviço** — muitos sites proíbem scraping, especialmente de áreas autenticadas; violá-los pode gerar consequências contratuais e legais.
- **Dados pessoais (LGPD):** o tratamento de dados de pessoas físicas exige base legal; tenha cautela ao capturar nomes, CPFs ou documentos.
- **robots.txt:** respeite as diretrizes de crawling do site.
- **Carga no servidor:** limite a frequência de requisições para não prejudicar a operação do site.
- **Contornar autenticação/CAPTCHA** de sistemas que você não está autorizado a acessar pode configurar violação de termos ou de lei — só faça em contas e sistemas que você tem direito de usar.
- **Sinal de intenção do site:** uma proteção forte (como o Managed Challenge cobrindo dezenas de milhares de itens) é um indicativo claro de que o operador não deseja coleta em massa. Antes de investir em contornar, procure uma rota oficial (API de parceiro, export para corretores/integradores, contato comercial) — é mais estável e sem atrito legal.
- Em caso de dúvida sobre licitude, **consulte um advogado** — este documento é técnico e não constitui aconselhamento jurídico.

---

## 13. Estudo de caso: Cloudflare Managed Challenge / Turnstile bloqueando paginação

Cenário real enfrentado: um portal de leilão de imóveis com ~45 mil itens, protegido por Cloudflare. As **primeiras páginas** de cada seção (leilão extrajudicial + perfis de leiloeiros parceiros) são acessíveis, mas **qualquer URL paginada (`?pag=2` em diante) dispara a tela de desafio do Cloudflare (Managed Challenge / Turnstile)**. Resultado sem tratamento: apenas ~169 itens coletáveis.

### 13.1. Por que adicionar mais slugs/seeds não resolve

Ampliar `LEILOEIRO_SLUGS` ou `SEED_PAGES` apenas multiplica a coleta das *primeiras páginas* de mais seções. O gargalo está na **camada de acesso** (o desafio na paginação), não na configuração de entrada. O problema precisa ser atacado onde ele realmente está.

### 13.2. Por que o desafio interativo muda a estratégia

No **Managed Challenge / Turnstile interativo**, uma engine HTTP isolada — mesmo o `curl_cffi`, que imita o TLS de navegador — **geralmente não basta**. O que efetivamente libera o acesso é o cookie **`cf_clearance`**, emitido somente após o desafio ser resolvido por um navegador real. A boa notícia: uma vez emitido, esse cookie vale para **todas** as páginas (inclusive as paginadas) até expirar. A estratégia, portanto, deixa de ser "evitar o desafio" e passa a ser **"obter o `cf_clearance` uma vez e reutilizá-lo"**.

### 13.3. Detalhe crítico: o `cf_clearance` é amarrado ao User-Agent e ao IP

O cookie `cf_clearance` é vinculado ao **User-Agent exato** e ao **IP** do navegador que o gerou. Se for extraído para uso em `requests`/`curl_cffi`, é preciso enviar **rigorosamente o mesmo User-Agent** e a partir do **mesmo IP** — caso contrário o Cloudflare invalida. Por isso a abordagem mais robusta é **permanecer dentro do mesmo navegador Playwright** que resolveu o desafio (Opção A abaixo), evitando qualquer descasamento de fingerprint.

### 13.4. Antes de tudo: existe API interna por trás da paginação?

Com o desafio já resolvido no navegador, abra DevTools → Network → filtro **XHR/Fetch** e clique para a página 2. Se a paginação disparar uma chamada **JSON** (algo como `/api/imoveis?page=2&offset=...`), pagine por essa API usando o mesmo `cf_clearance` — muito mais leve e confiável do que raspar HTML. APIs internas às vezes têm regras de WAF mais brandas que as rotas HTML.

### 13.5. Opção A (recomendada): tudo dentro do Playwright com sessão persistida

A abordagem escolhida e mais robusta: resolver o desafio manualmente **uma vez**, salvar o estado completo (`storage_state`, que inclui o `cf_clearance`) e reutilizá-lo. Como toda a navegação continua no mesmo navegador, o User-Agent e o fingerprint que geraram o cookie permanecem idênticos, sem risco de invalidação.

```python
from playwright.sync_api import sync_playwright
import os, time

AUTH_FILE = "cf_auth.json"
BASE = "https://www.leilaoimovel.com.br"

def ensure_session():
    """Gera cf_auth.json (resolução manual única do desafio Cloudflare)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)   # precisa ser visível
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE}/leiloes")
        input("Resolva o desafio Cloudflare no navegador e tecle ENTER aqui...")
        context.storage_state(path=AUTH_FILE)          # salva cf_clearance + cookies
        browser.close()

def scrape_all(seed_urls):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_FILE)
        page = context.new_page()

        for base_url in seed_urls:
            pag = 1
            while True:
                url = f"{base_url}?pag={pag}"
                resp = page.goto(url, wait_until="networkidle")

                # Detecta se a sessão caiu (Cloudflare voltou a desafiar)
                if "challenge" in page.url or (resp and resp.status in (403, 503)):
                    print("Sessão expirou — refazer ensure_session()")
                    browser.close()
                    return  # ou chamar ensure_session() e retomar deste ponto

                imoveis = extrair_imoveis(page)   # sua lógica de extração atual
                if not imoveis:                   # página vazia = fim da seção
                    break

                salvar(imoveis)                   # sua função de persistência
                pag += 1
                time.sleep(2)                     # respeitar o servidor

        browser.close()

if __name__ == "__main__":
    if not os.path.exists(AUTH_FILE):
        ensure_session()
    scrape_all(SEED_PAGES)
```

Os pontos de integração com um scraper existente são `extrair_imoveis()` e `salvar()` (lógica que você já tem) e a montagem de URLs a partir de `LEILOEIRO_SLUGS` / `SEED_PAGES`. A **detecção de sessão caída** é o que torna a varredura dos 45k viável: quando o `cf_clearance` expira no meio da coleta, o script percebe (volta ao desafio ou recebe 403/503) e você reabre a sessão em vez de coletar lixo.

### 13.6. Opção B (alternativa): extrair o cookie para o `curl_cffi`

Mais rápida para volume grande, porém mais frágil — exige reproduzir o mesmo User-Agent e IP (ver 13.3):

```python
import json
from curl_cffi import requests as cffi

state = json.load(open("cf_auth.json"))
cookies = {c["name"]: c["value"] for c in state["cookies"]}
UA = "Mozilla/5.0 ..."  # EXATAMENTE o User-Agent do navegador que gerou o cookie

r = cffi.get(
    "https://www.leilaoimovel.com.br/leiloes?pag=2",
    cookies=cookies,
    headers={"User-Agent": UA},
    impersonate="chrome120",
)
```

### 13.7. Paginação alternativa (driblar especificamente o `?pag=N`)

Se apenas o parâmetro `?pag=` aciona a regra do WAF, teste formas de paginação que escapem dela:
- **Scroll infinito**, que costuma acionar a API interna (ver 13.4) em vez de uma URL paginada.
- **Fatiamento por filtros** — URLs por estado/cidade/categoria que dividam os 45k em conjuntos menores, cada um abaixo do limite da primeira página.
- **Ordenações diferentes** (preço asc/desc, data) para alcançar lotes distintos sem paginar.

### 13.8. Limites e manutenção da sessão

O `cf_clearance` **expira** (tipicamente de ~30 min a algumas horas, conforme a configuração do site). Para varrer 45k itens será necessário **renovar a sessão periodicamente** — ou seja, repetir a resolução manual quando o cookie morrer (é o que a detecção de sessão caída em 13.5 sinaliza). Para automação totalmente sem intervenção humana, a única forma de gerar o cookie sem resolver o desafio à mão seria um **serviço de resolução de Turnstile/CAPTCHA**, que tem custo e deve ser avaliado à luz dos Termos de Serviço (ver seção 12).

### 13.9. Checklist resumido para o caso Cloudflare

1. Confirmar que o bloqueio é Managed Challenge/Turnstile (tela de desafio), não 403 puro.
2. Procurar **API interna** na paginação (13.4) — se existir, é o melhor caminho.
3. Resolver o desafio uma vez no Playwright visível e salvar `cf_auth.json` (13.5).
4. Paginar reutilizando o `storage_state`, mantendo tudo no mesmo navegador.
5. Implementar **detecção de sessão caída** para renovar o `cf_clearance` quando expirar.
6. Considerar **paginação alternativa** (13.7) se só o `?pag=` for bloqueado.
7. Antes de escalar, procurar **rota oficial** (API de parceiro/export) e revisar os ToS (seção 12).

---

## 14. Estudo de caso: Cloudflare Turnstile total — FlareSolverr via Docker

Cenário: **Milan Leilões** (`milanleiloes.com.br`) — proteção Cloudflare que bloqueia **todas** as URLs, inclusive a home page e qualquer endpoint de API. Nem `curl_cffi`, nem `playwright-stealth` v2 (com janela visível por 25 s), nem cookies extraídos manualmente conseguem passar — porque o `cf_clearance` é vinculado ao IP do processo que o gerou, e ao mudar de contexto (ex.: `requests` no host Windows vs. browser no container) o Cloudflare invalida.

### 14.1. O que foi testado e falhou

| Abordagem | Resultado |
|---|---|
| `curl_cffi` com `impersonate="chrome124"` | 403 em todas as URLs |
| `playwright-stealth` v2, `headless=True` | "Um momento…" |
| `playwright-stealth` v2, `headless=False`, 25 s | "Um momento…" |
| Cookies `cf_clearance` extraídos → `requests` | Invalidados (IP diferente) |
| API pública (`/api/imoveis`, `/sitemap.xml`, etc.) | 403 em todos os endpoints |

### 14.2. Solução: FlareSolverr com sessão persistente

**FlareSolverr** é um serviço Docker que roda um Chromium real com stealth próprio. Ao usar **sessões persistentes** (`sessions.create`), todas as requisições saem do mesmo IP/browser que resolveu o desafio — eliminando o problema de descasamento descrito em 13.3.

```bash
# Instalar (uma vez)
docker run -d --name flaresolverr -p 8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest

# Verificar
curl http://localhost:8191/
```

```python
import requests, re

FS = "http://localhost:8191/v1"

def fs_post(cmd, **kw):
    return requests.post(FS, json={"cmd": cmd, **kw}, timeout=120).json()

def fs_get(url, sid, max_timeout=60000):
    return fs_post("request.get", url=url,
                   session=sid, maxTimeout=max_timeout).get("solution", {})

# Criar sessão — o mesmo IP/browser é reutilizado em todas as chamadas
sid = fs_post("sessions.create")["session"]

# Primeira requisição resolve o CF challenge automaticamente
sol = fs_get("https://www.milanleiloes.com.br", sid)
html = sol["response"]           # HTML real da página (não o desafio)
cookies = sol["cookies"]         # inclui cf_clearance

# Requisições subsequentes na mesma sessão já passam direto
sol2 = fs_get("https://www.milanleiloes.com.br/leilao/imoveis/15294", sid)

# Destruir sessão ao terminar
fs_post("sessions.destroy", session=sid)
```

### 14.3. Armadilha crítica: `$` em regex Python

Ao extrair preços do HTML do Milan (`R$ 315.000,00`), o padrão `r'R\$'` **não funciona** da forma esperada:

```python
import re
# ERRADO — \$ em regex Python age como âncora de fim de string ($)
re.search(r'R\$\s*([\d.,]+)', 'Lance: R$ 315.000,00')  # → None

# CORRETO — $ dentro de [] é sempre literal
re.search(r'R[\$]\s*([\d.,]+)', 'Lance: R$ 315.000,00')  # → '315.000,00'
```

**Regra:** sempre que precisar casar o caractere `$` em regex Python, use `[\$]`.

### 14.4. Armadilha: janela de captura do card HTML

As páginas do Milan renderizam todos os lotes no HTML (sem paginação adicional), mas o elemento de preço (`card_lote_lanceMinimo`) pode estar a **mais de 2000 caracteres** do início do card. Usar `.{0,2000}` na captura causa preço = 0. Use no mínimo `.{0,4000}`:

```python
# ERRADO — preço fica fora dos 2000 chars
card = re.search(rf'href=".../lote/{num}".{{0,2000}}', html, re.S)

# CORRETO
card = re.search(rf'href=".../lote/{num}".{{0,4000}}', html, re.S)
```

### 14.5. Render lazy em SPA Next.js

Alguns leilões do Milan usam Next.js App Router. O FlareSolverr às vezes devolve o HTML antes dos componentes React hidratarem (0 lotes visíveis). Solução: retry único com `maxTimeout=90000`:

```python
lote_urls = extract_lotes(html, lid)
if not lote_urls:
    import time; time.sleep(3)
    sol2 = fs_get(lurl, sid, max_timeout=90000)
    lote_urls = extract_lotes(sol2["response"], lid)
```

### 14.6. Checklist FlareSolverr

1. Subir container: `docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest`
2. Criar sessão com `sessions.create` — usar o mesmo `session_id` em todas as chamadas.
3. Primeira requisição à home resolve o CF; requisições seguintes na mesma sessão passam direto.
4. Se a página for SPA (Next.js/React), fazer retry com `maxTimeout` maior se 0 lotes forem retornados.
5. Destruir sessão com `sessions.destroy` ao terminar.
6. Usar `R[\$]` em regex para casar o cifrão literal.
7. Janela de captura do card HTML: mínimo 4000 chars.

---

## 15. Armadilhas técnicas documentadas

Problemas reais encontrados durante o desenvolvimento dos scrapers desta base de código, com causa raiz e solução.

### 15.1. `R\$` em Python regex não casa o cifrão

**Problema:** `re.search(r'R\$', 'R$ 100,00')` retorna `None`.

**Causa:** Em Python's `re`, `\$` não é uma sequência de escape reconhecida. O `$` mantém seu papel de âncora de fim de string mesmo com a barra invertida.

**Solução:** `r'R[\$]'` — dentro de `[...]` o `$` perde o significado especial e é sempre literal.

### 15.2. Cookies `cf_clearance` são inválidos fora do IP de origem

**Problema:** Obter `cf_clearance` via Playwright/FlareSolverr e reutilizá-lo em `requests` ou `curl_cffi` no host retorna 403.

**Causa:** O Cloudflare vincula o cookie ao par `(User-Agent, IP)`. Mudar qualquer um dos dois invalida o cookie.

**Solução:** Manter todas as requisições dentro da mesma sessão FlareSolverr (mesmo processo, mesmo IP) ou dentro do mesmo contexto Playwright que gerou o cookie.

### 15.3. `playwright-stealth` v2 — API mudou

**Problema:** `from playwright_stealth import stealth_async` gera `ImportError` na v2.

**Solução:** A API nova é `Stealth` (classe) com método `apply_stealth_async(page)`:

```python
from playwright_stealth import Stealth
stealth = Stealth(navigator_user_agent_override=UA)
await stealth.apply_stealth_async(page)
```

### 15.4. SPA / Next.js App Router sem `__NEXT_DATA__`

**Problema:** Não há `<script id="__NEXT_DATA__">` para extrair dados estruturados.

**Causa:** Next.js 13+ com App Router usa React Server Components (RSC) — os dados chegam como streams RSC, não como JSON embutido.

**Solução:** Esperar o render completo (aumentar `wait`/`maxTimeout`) e raspar a DOM renderizada, ou interceptar chamadas de API no Playwright com `page.on("response", ...)`.

### 15.5. `[^R]*` em regex bloqueado por letra no atributo HTML

**Problema:** `re.search(r'lanceMinimo[^>]*>[^R]*R[\$]', html)` retorna `None` quando o card começa com `<img alt="Lote RJ ...">` — o `R` no alt text quebra `[^R]*`.

**Solução:** Usar um padrão mais específico que não dependa de "nenhum R antes":

```python
# Em vez de [^R]*, usar .* não-greedy que para no primeiro R$
re.search(r'lanceMinimo[^>]*>.*?R[\$]\s*([\d.,]+)', card_html, re.I | re.S)
```

### 15.6. Paginação instável em SPA (render lazy)

**Problema:** A mesma URL de leilão às vezes retorna 0 lotes e às vezes retorna todos, dependendo do tempo de render.

**Causa:** O FlareSolverr (ou Playwright headless) pode devolver o HTML antes de o JavaScript terminar de hidratar os componentes.

**Solução:** Retry único com timeout maior:

```python
if not lote_urls:
    time.sleep(3)
    lote_urls = extract_lotes(fs_get(url, sid, max_timeout=90000)["response"])
```

---

## 16. Fontes de leilões de imóveis — resultados validados

Tabela consolidada das fontes testadas com método, volume observado e status. Implementação de referência: **`scraper_completo.py`**.

### 16.1. Fontes funcionais

| Leiloeiro | Método | Volume (~lotes) | Observações |
|---|---|---|---|
| **Central Sul de Leilões** | API REST (`/api/v2/web/next-auctions` + `/api/v2/web/auction/{id}/lots`) | ~339 | Melhor fonte: dados tipados, preço + avaliação + desconto |
| **Mega Leilões** | Playwright, paginação `/imoveis?pagina=N` | ~669 | Maior volume; parar quando página vazia |
| **Grupo Lance** | Playwright, paginação `/imoveis?pagina=N` | ~306 | Cidade/estado extraíveis do slug da URL |
| **Sold Leilões** | Playwright, paginação `/h/imoveis?pageNumber=N&pageSize=30` | ~108 | Selector: `a[href*="/oferta/"]` |
| **Portal Zuk** | Playwright, skip `/leilao-de-imoveis?skip=N` (step=20) | ~51 | Selector: `a[href*="/leilao-de-imoveis/v/"]` |
| **Franco Leilões** | Playwright, paginação `/proximos_leiloes/{N}/1/` | ~22 | Selector: `a[href*="/lote/"]` |
| **Frazão Leilões** | Playwright, página única `/leiloes` | ~20 | Todos os lotes em uma página; selector `a[href*="/lote/"]` |
| **Milan Leilões** | FlareSolverr (Docker `:8191`), sessão persistente | ~20 | CF Turnstile total — ver seções 14 e 15 |

### 16.2. Fontes que não funcionaram (e por quê)

| Leiloeiro | Problema | Alternativa sugerida |
|---|---|---|
| **Porto Leilões** | 0 resultados — estrutura do site mudou | Re-inspecionar DevTools; tentar `/lotes` em vez de `/eventos` |
| **Luis Leiloeiro** | 0 resultados — seletores desatualizados | Re-inspecionar; o site usa SPA |
| **Rodolfo Schontag** | 0 resultados — site possivelmente fora do ar | Verificar disponibilidade |
| **Lance no Leilão** | Requer login de comprador credenciado | Autenticação via `storage_state` (seção 4.3) |

### 16.3. Uso do `scraper_completo.py`

```bash
# Roda todas as 8 fontes (retoma de onde parou se interrompido)
python scraper_completo.py

# Recomeça do zero
python scraper_completo.py --reset

# Roda apenas fontes específicas
python scraper_completo.py --only central_sul milan

# Pula uma fonte
python scraper_completo.py --skip milan

# Lista os identificadores disponíveis
python scraper_completo.py --list
```

**Saída:** `ofertas_completo.csv` com campos `leiloeiro, url, titulo, cidade, estado, preco, avaliacao, desconto_pct, duplicado`.

**Progresso:** salvo em `scraper_completo_progress.json`; se o processo for interrompido, a próxima execução retoma a partir do site seguinte automaticamente.

**Dependência para Milan:** container FlareSolverr deve estar rodando. O script tenta iniciá-lo automaticamente via `docker start flaresolverr`; se o container não existir, exibe o comando de criação e pula Milan.
