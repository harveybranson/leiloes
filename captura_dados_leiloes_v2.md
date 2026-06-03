# Guia Completo de Captura de Dados de Sites de Leilão (v2)

> Documento de referência para extração exaustiva de informações de qualquer tipo de site de leilão — incluindo sites públicos, autenticados (login/senha), com JavaScript pesado, APIs ocultas e proteções anti-bot.
>
> **Novidade da v2:** seção dedicada a contornar o **Cloudflare Managed Challenge / Turnstile** que bloqueia paginação (`?pag=2` em diante), com fluxo completo de sessão persistida no Playwright (seção 13).
>
> **Adições mai/2026 (seções 19-21):** scraping do diretório BomValor (113 leiloeiros, session limit, mapeamento de colunas), armadilhas no Windows (Python via Bash → exit 127, stdout reconfigure, desync CSV/JSON, monitoramento Playwright), migration de colunas ausentes no banco, e pipeline completo de importação com sequência correta de operações.
>
> **Adições mai/2026 (seções 22-23):** diferenciação imóvel vs produto/veículo (`categoria_bem`, classificação em camadas) e captura de documentos para download (edital, matrícula) no scraper genérico do `leilao-scraper` — complementa a seção 17 com a implementação real em produção.
>
> **Adições jun/2026 (seção 27):** arquitetura de referência para scraper genérico de leiloeiros — princípios, seleção de ferramenta por tipo de site, camada de adaptadores por plataforma, esqueleto de código Playwright + httpx e boas práticas operacionais.
>
> **Adições jun/2026 (seções 24-25):** pipeline end-to-end completo — de site único, lista `.txt`, planilha `.csv`/`.xlsx` até os cards do sistema com documentos (edital/matrícula), deduplicação global, inserção de únicos e exportação automática de CSV datado para a pasta `/csv`; sincronização obrigatória de imóveis com `/admin` e de novos leiloeiros com `/admin` e aba **Leiloeiros** do frontend.
>
> **Adições jun/2026 (`baixar-docs`):** implementação do `DocumentoDownloader` — após o scraping, **obrigatório** rodar `python run.py baixar-docs --limite 200` para baixar os PDFs (edital, matrícula, laudos) para disco local em `storage/docs/`, com fallback automático via FlareSolverr para sites com Cloudflare. O comando atualiza o campo `arquivos` no banco com `path_local` e `hash_md5`. Integrado no `pipeline`, Celery beat (a cada hora) e endpoint `GET /imoveis/{id}/documentos/{idx}/download` na API.
>
> **Adições jun/2026 (seção 26):** scraper standalone da Caixa Econômica Federal (`scraping/scraper_caixa.py`) — bypassa Radware Bot Manager via Playwright + playwright-stealth + `expect_download`; novo formato de CSV (colunas `N° do imóvel`, `Preço`, sem matrícula separada); URL de matrícula determinística; filtro por data da 1ª praça (seção 8.1); 27.363 imóveis coletados em ~2 min para todos os 27 estados.

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
- Documentos (editais, matrículas) em PDF → baixe e extraia texto com `pdfplumber` ou OCR (`pytesseract`) para PDFs escaneados. Ver **seções 17 e 23** para captura de links e enrichers.
- A mesma listagem mistura imóveis e outros bens — classifique com `categoria_bem` (**seção 22**).
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
9. **Classifique** imóvel vs produto (`categoria_bem`, seção 22) e capture documentos na página de detalhe (seção 23).
10. **Baixe os documentos** para disco: `python run.py baixar-docs --limite 200` (obrigatório após qualquer scraping — ver seção 23.6).
11. **Respeite** rate limits, robots.txt e os termos do site.

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

### 15.7. Python não encontrado no Git Bash no Windows (exit 127)

**Problema:** `python script.py` via Bash tool ou Git Bash retorna exit code 127.

**Causa:** o Python instalado no Windows não está no PATH do Git Bash/WSL.

**Solução:** usar PowerShell para todo comando Python no Windows. Ver detalhes em **seção 20.1**.

### 15.8. `sys.stdout.reconfigure` produz log vazio quando stdout é redirecionado

**Problema:** log file fica com 0 bytes mesmo o processo estando ativo.

**Causa:** `reconfigure` substitui o objeto stdout e pode quebrar o descriptor do arquivo redirecionado.

**Solução:** monitorar via arquivo de progresso JSON, não via stdout. Ver **seção 20.2**.

### 15.9. CSV e progress JSON desincronizados após mover arquivo

**Problema:** progress JSON reporta N rows, CSV tem menos linhas.

**Solução:** mover CSV junto com o progress JSON, ou usar `--reset`. Ver **seção 20.3**.

### 15.10. Coluna ausente no banco quebra importação (UndefinedColumn)

**Problema:** `psycopg2.errors.UndefinedColumn` ao tentar importar CSV quando `models.py` tem colunas novas não aplicadas ao PostgreSQL.

**Solução:** rodar `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` antes de importar. Ver **seção 20.5**.

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

---

## 17. Enriquecimento com documentos — edital, matrícula e PDFs

Após coletar as URLs dos imóveis, uma segunda passagem ("enricher") visita cada página individual e extrai os documentos vinculados ao lote: edital do leilão, matrícula do imóvel, laudo de avaliação e outros PDFs.

### 17.1. Modelo de dados

Três campos adicionados à tabela `imoveis` (migration via `ALTER TABLE`):

| Campo | Tipo | Conteúdo |
|---|---|---|
| `edital_url` | VARCHAR(1000) | Link direto para o PDF do edital |
| `matricula_url` | VARCHAR(1000) | Link direto para a matrícula |
| `documentos` | TEXT (JSON) | Array: `[{"tipo": "edital"|"matricula"|"documento", "url": "...", "descricao": "..."}]` |

O mesmo modelo existe no `ImovelRaw` do scraper base, permitindo que scrapers primários já populem esses campos diretamente quando os encontrarem na fonte.

### 17.2. Estratégia do enricher

O enricher (`pipeline/enricher_documentos.py`) processa imóveis sem documentos em três camadas:

**Camada 1 — Extrator específico por fonte (mais confiável)**
Fontes conhecidas têm extratores dedicados que sabem exatamente onde buscar os documentos, sem precisar navegar nem fazer parsing genérico. Exemplo: Caixa Econômica Federal (ver 17.3).

**Camada 2 — httpx rápido + regex genérico**
Para fontes desconhecidas, tenta com `httpx` (sem browser). Extrai todos os `href` que batem nos padrões:
- Edital: `edital`, `aviso.*leil`, `notice.*sale`
- Matrícula: `matr[íi]cula`, `certid[aã]o`, `registro.*im[oó]vel`
- Outros docs: `\.pdf$`, `laudo`, `avalia[çc][aã]o`, `vistoria`, `ônus`

**Camada 3 — Playwright (fallback para SPA/JS)**
Se httpx não achou nada, abre o Playwright e repete a extração no DOM renderizado.

```python
async_extractor = FONTE_EXTRACTORS_ASYNC.get(dominio)
if async_extractor and browser:
    resultado = await async_extractor(imovel, browser)   # extrator dedicado
else:
    docs = await _scrape_with_httpx(url)                  # camada 2
    if not docs:
        docs = await _scrape_with_playwright(browser, url) # camada 3
```

### 17.3. Estudo de caso: Caixa Econômica Federal

A Caixa protege seus endpoints com **Radware Bot Manager** — httpx e curl retornam a página de CAPTCHA em vez do HTML real. Playwright passa sem problemas.

**Descoberta do padrão (inspeção manual com Playwright):**

A página de detalhe do imóvel (`/sistema/detalhe-imovel.asp?hdnimovel=XXXXXXX`) renderiza dois botões:
```html
<a onclick="javascript:ExibeDoc('/editais/matricula/SP/8787705673395.pdf')">
  Baixar matrícula do imóvel
</a>
<a onclick="javascript:ExibeDoc('/editais/EA00110326CPVERE.PDF')">
  Baixar edital e anexos
</a>
```

O padrão real confirmado (mai/2026):
- **Matrícula**: `https://venda-imoveis.caixa.gov.br/editais/matricula/{UF}/{hdnimovel}.pdf`
- **Edital**: `https://venda-imoveis.caixa.gov.br/editais/{CODIGO_EDITAL}.PDF` (o código varia por lote/leilão — extraído do `onclick`)

> **Armadilha:** Os padrões de URL da Caixa **não são previsíveis** para o edital — o código (`EA00110326CPVERE`) é gerado internamente e só aparece no HTML renderizado. Para a matrícula, a URL é determinística a partir do `hdnimovel` e da UF. Nunca tente construir a URL do edital sem visitar a página primeiro.

**Extrator implementado:**

```python
async def _caixa_docs_playwright(imovel, browser) -> dict:
    # Visita a página com Playwright
    html = await page.content()
    # Extrai todos os ExibeDoc('/...') do HTML
    paths = re.findall(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", html)
    for path in paths:
        if "matricula" in path.lower():
            resultado["matricula_url"] = base + path
        else:
            resultado["edital_url"] = base + path
```

**Resultado observado:**
- 356 imóveis Caixa ativos
- ~38 com edital + matrícula (~11%): propriedades com leilão ativo publicado
- ~70 só com matrícula (~20%): venda direta sem edital
- ~248 sem documentos (~70%): imóveis com página expirada ("Nenhum imóvel encontrado") — a Caixa remove o detalhe do lote após o leilão encerrar

**Implicação prática:** enriquecer imóveis Caixa logo após o scraping, enquanto as páginas ainda estão ativas. Imóveis com mais de ~30 dias tendem a ter a página expirada.

### 17.4. Uso do enricher

```bash
# Enriquecer imóveis Caixa (usa Playwright — ~5s/imóvel)
docker exec leilao_api bash -c "cd /app && python run.py enriquecer-documentos --fonte caixa.gov.br --limite 500"

# Enriquecer todos os imóveis sem documentos (httpx primeiro, Playwright como fallback)
docker exec leilao_api bash -c "cd /app && python run.py enriquecer-documentos --limite 1000"

# Só httpx (mais rápido, sem Playwright — para fontes que não requerem JS)
docker exec leilao_api bash -c "cd /app && python run.py enriquecer-documentos --limite 2000 --sem-playwright"

# Reprocessar imóveis já enriquecidos (força re-fetch)
docker exec leilao_api bash -c "cd /app && python run.py enriquecer-documentos --reset --limite 200"
```

### 17.5. Não rode múltiplas instâncias simultaneamente

Cada instância do enricher faz um SELECT inicial dos imóveis sem `edital_url IS NULL`, lança o Playwright e processa a lista. Se duas instâncias rodarem ao mesmo tempo, ambas leram a mesma lista inicial e vão processar as mesmas páginas em paralelo, desperdiçando recursos e gerando requisições duplicadas ao servidor alvo.

**Problema observado:** 3 instâncias rodando simultaneamente reduziram a taxa efetiva para ~3 imóveis/minuto por instância (vs ~8/min com instância única), porque o Playwright fica competindo por CPU/memória no container.

**Regra:** matar instâncias anteriores antes de iniciar uma nova execução.

### 17.6. Checklist para enriquecimento de documentos

1. Confirmar que a migration foi aplicada (`edital_url`, `matricula_url`, `documentos` existem em `imoveis`).
2. Para fontes com bot protection (Caixa, etc.), usar Playwright — nunca assumir que httpx funciona.
3. Inspecionar manualmente a página de um imóvel ativo antes de escrever o extrator — botões com `href="#"` e `onclick` são o padrão para downloads protegidos.
4. Rodar o enricher logo após a coleta inicial, enquanto as páginas estão ativas.
5. Não assumir padrões de URL para editais — extrair do HTML sempre.
6. Para matrícula da Caixa, a URL é determinística: `/editais/matricula/{UF}/{hdnimovel}.pdf`.
7. Monitorar progresso via query: `SELECT COUNT(*) FROM imoveis WHERE edital_url IS NOT NULL`.

---

## 18. Coluna de documentos nos cards e no admin

Após o enriquecimento, os documentos aparecem automaticamente na interface:

**Cards do site (`index.html`):**
- Snippet de descrição (2 linhas, 120 chars) abaixo do score
- Badges inline 📄 Edital e 📋 Matrícula no card (clicáveis, abre em nova aba, não propaga o click para o detalhe)
- Seção "📁 Documentos" no detalhe do imóvel listando todos os PDFs encontrados

**Admin (`/admin`, aba Imóveis):**
- Coluna "Docs" na tabela: ícones 📄 📋 quando presentes, contador `+N` para extras
- Snippet de descrição (80 chars) abaixo do título na tabela

**Frontend — lógica de exibição:**
```js
// Cards: exibe badges apenas quando as URLs estão preenchidas
${im.edital_url
  ? `<a href="${im.edital_url}" target="_blank" onclick="event.stopPropagation()">📄 Edital</a>`
  : ''}

// Detalhe: monta seção completa incluindo documentos do array JSON
const docs = [];
if (im.edital_url)    docs.push({tipo:'edital',    url: im.edital_url});
if (im.matricula_url) docs.push({tipo:'matricula', url: im.matricula_url});
try { docs.push(...JSON.parse(im.documentos || '[]')); } catch(e) {}
```

O campo `documentos` (JSON) é incluído em `ImovelResumo` (não apenas em `ImovelDetalhe`) para que os cards na listagem já recebam as URLs sem precisar de uma segunda chamada de detalhe.

---

## 19. Scraping de diretórios de leiloeiros — BomValor.com.br

**Contexto:** o portal `comunidades.bomvalor.com.br/leiloeiros-oficiais/` lista 113 leiloeiros oficiais com seus perfis no marketplace `mercado.bomvalor.com.br`. É uma fonte rápida e sem proteção anti-bot para descobrir leiloeiros ativos.

### 19.1. Estrutura da paginação

```
GET https://comunidades.bomvalor.com.br/leiloeiros-oficiais/?q=&page=N
```

- 20 leiloeiros por página, total de 113 → 6 páginas.
- Cada card tem: nome do leiloeiro, link de perfil no formato `/leiloeiro/nome-slugificado/`, e o site (`mercado.bomvalor.com.br/slug`).
- O diretório indica "113 leiloeiros encontrados" no HTML — use para saber quantas páginas buscar.

### 19.2. O que as páginas de perfil no mercado contêm (e o que não contêm)

**Validado empiricamente para todos os 113 perfis** (`mercado.bomvalor.com.br/<slug>`):

| O que existe | O que NÃO existe |
|---|---|
| Número de WhatsApp (`wa.me/55...`) | Link para site externo do leiloeiro |
| FAQ do BomValor (Google Sites) | Instagram / Facebook / LinkedIn |
| Links internos do ecossistema BomValor | Site próprio do leiloeiro |

**Conclusão:** o `mercado.bomvalor.com.br/<slug>` **é** o site oficial da grande maioria dos leiloeiros. Apenas 3 dos 113 possuem site próprio externo (identificáveis diretamente no diretório):

| Leiloeiro | Site externo |
|---|---|
| Emílio Matos Rocha | emiliomatosleiloes.com.br |
| Eduardo Macario de Melo | www.macariosleiloes.com.br |
| Sérgio Sousa Rodrigues | bhleiloaria.com.br |

### 19.3. Session limit por aba/sessão

O BomValor aplica um **limite de requisições por sessão de navegador**. Ao usar `WebFetch` em paralelo com muitas abas simultâneas, a partir de certo ponto a resposta muda para:

```
"You've hit your session limit · resets 12:40pm (America/Sao_Paulo)"
```

**Solução:** aguardar o horário de reset indicado na mensagem antes de retomar. Em scripts Python, use `requests.Session()` com delay entre chamadas (1-2 s) para evitar o limite.

### 19.4. Importando leiloeiros BomValor no banco

O importador padrão (`scrapers/leiloeiros/importar_csv.py`) espera colunas FENAJU (`nome`, `uf`, `site`, etc.). O CSV gerado pelo scraping BomValor usa colunas diferentes (`Nome`, `Site Oficial`, `WhatsApp`, `Status`). Mapeamento manual necessário:

```python
import csv
from database.models import Leiloeiro
from sqlalchemy.orm import Session

with open("leiloeiros_bomvalor.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        nome = (row.get("Nome") or "").strip()
        site_raw = (row.get("Site Oficial") or "").strip()
        whatsapp = (row.get("WhatsApp") or "").strip()
        status   = (row.get("Status") or "").strip()

        if "404" in status or not site_raw or not nome:
            continue  # ignora perfis inativos

        site = site_raw if site_raw.startswith("http") else "https://" + site_raw
        session.add(Leiloeiro(nome=nome, site=site, telefone=whatsapp or None, situacao="Regular"))

session.commit()
```

**Resultado observado (mai/2026):** 96 inseridos, 17 ignorados (páginas 404).

### 19.5. Checklist BomValor

1. Buscar 6 páginas em paralelo: `?q=&page=1` até `?q=&page=6`.
2. Extrair nome + slug de cada card.
3. Visitar `mercado.bomvalor.com.br/<slug>` apenas para obter o WhatsApp (não há site externo).
4. Para os 3 leiloeiros com site próprio, o URL externo aparece diretamente no diretório.
5. Usar delays de 1-2 s entre requisições para evitar session limit.
6. Ao importar, mapear colunas manualmente (não usar o importador FENAJU direto).

---

## 20. Armadilhas adicionais documentadas (mai/2026)

### 20.1. Python no Windows: use PowerShell, não Bash

**Problema:** executar `python script.py` via Bash (Git Bash / WSL) no Windows retorna **exit code 127** ("command not found").

**Causa:** o `python` instalado via Microsoft Store ou instalador Windows fica no PATH do PowerShell/CMD mas não no PATH do Git Bash.

**Solução:** sempre use PowerShell para rodar scripts Python no Windows:

```powershell
# Correto
cd "C:\caminho\projeto"
python scraper_completo.py

# Errado (Git Bash no Windows)
cd /c/caminho/projeto && python scraper_completo.py   # → exit 127
```

### 20.2. `sys.stdout.reconfigure` quebra redirecionamento de arquivo

**Problema:** scripts que chamam `sys.stdout.reconfigure(encoding="utf-8")` na inicialização produzem **arquivo de log vazio** quando o processo é iniciado com `stdout` redirecionado para arquivo (`-RedirectStandardOutput`).

**Causa:** `reconfigure` substitui o objeto stdout pelo wrapper UTF-8, que pode não herdar o file descriptor do redirecionamento original.

**Solução:** para monitorar progresso de processos externos, leia o **arquivo de progresso JSON** gerado pelo script, não o stdout:

```powershell
# Em vez de depender do log:
python -c "
import json
d = json.load(open('scraper_completo_progress.json', encoding='utf-8'))
print('done:', d['done'], '| rows:', len(d['rows']))
"
```

### 20.3. CSV e progress JSON desincronizados

**Problema:** o `scraper_completo_progress.json` pode reportar N rows enquanto o `ofertas_completo.csv` tem menos linhas (o CSV foi movido, renomeado ou sobrescrito por outra execução).

**Causa:** `save_csv()` e `save_progress()` são chamadas separadas. Se o arquivo CSV for movido entre chamadas, o progress JSON acumula rows que não estão no CSV.

**Diagnóstico:**
```powershell
# Comparar contagens
python -c "import json; d=json.load(open('scraper_completo_progress.json', encoding='utf-8')); print('JSON rows:', len(d['rows']))"
(Get-Content "ofertas_completo.csv" | Measure-Object -Line).Lines  # deve ser JSON rows + 1 (header)
```

**Solução:** ao mover o CSV, mover junto (ou apagar) o arquivo de progresso; ou usar `--reset` para recomeçar do zero.

### 20.4. Monitorar processo Playwright indiretamente

Quando o scraper Playwright roda em background sem output visível, monitore o processo filho do Chromium:

```powershell
# Identifica processos Python e seu consumo
Get-Process python -ErrorAction SilentlyContinue |
    Select-Object Id, CPU, WorkingSet, StartTime

# Sinal de atividade normal:
# - CPU aumentando ~4-10 s por minuto
# - WorkingSet do Chromium: 150-200 MB (ativo)
# - WorkingSet do Python: 50-70 MB

# Sinal de problema:
# - CPU estagnada por >5 min → Playwright provavelmente preso em timeout
# - WorkingSet caindo → processo encerrado
```

Se o scraper parar de responder, mate e reinicie via PowerShell (não via Bash):
```powershell
Stop-Process -Id <PID> -Force
python scraper_completo.py  # retoma de onde parou (progress.json)
```

### 20.5. Migration obrigatória antes de importar quando modelo à frente do banco

**Problema:** `sqlalchemy.exc.ProgrammingError: column "arquivos" of relation "imoveis" does not exist` ao tentar importar CSV no banco.

**Causa:** `models.py` foi atualizado com novas colunas (ex.: `arquivos`, `edital_url`, `matricula_url`, `documentos`) mas a migration correspondente nunca foi aplicada ao PostgreSQL.

**Diagnóstico:**
```python
from sqlalchemy import create_engine, text
engine = create_engine(DATABASE_URL_SYNC)
with engine.connect() as conn:
    cols = [r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='imoveis' ORDER BY ordinal_position"
    ))]
    print(cols)
```

**Solução:** aplicar migration manual antes de qualquer importação:
```python
migrations = [
    "ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS arquivos TEXT",
    "ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS edital_url VARCHAR(1000)",
    "ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS matricula_url VARCHAR(1000)",
    "ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS documentos TEXT",
]
with engine.connect() as conn:
    for sql in migrations:
        conn.execute(text(sql))
    conn.commit()
```

**Regra:** sempre verificar se todas as colunas do `models.py` existem no banco antes de importar. `ADD COLUMN IF NOT EXISTS` é seguro de rodar múltiplas vezes.

---

## 21. Pipeline completo de importação — sequência correta

Após rodar o `scraper_completo.py` e gerar o `ofertas_completo.csv`, a sequência correta de operações para popular e limpar o banco é:

```
1. Migration (se modelo atualizado)
2. Importar CSV → banco
3. Classificar imóveis
4. Normalizar cidades
5. Deduplicar
6. Desativar encerrados
7. Geocodificar
```

### 21.1. Comandos (executar nesta ordem)

```powershell
$DB = "postgresql://leilao:leilao123@localhost:5432/leilao_db"
$DIR = "C:\caminho\leilao-scraper\leilao-scraper"
$CSV = "C:\caminho\leiloes\ofertas_completo.csv"

Set-Location $DIR

# 1. Migration (só se novas colunas no models.py)
# → ver seção 20.5

# 2. Importar CSV gerado pelo scraper
$env:DATABASE_URL_SYNC = $DB
python -m pipeline.importar_ofertas_csv --csv $CSV

# 3. Classificar (calcula score_oportunidade e tipo_imovel)
python run.py classificar --limite 5000

# 4. Normalizar cidades (requer acesso IBGE — pode falhar SSL em alguns ambientes)
python run.py normalizar-cidades

# 5. Deduplicar (remove duplicatas por URL e por título+local)
python run.py deduplicar

# 6. Desativar leilões encerrados (datas passadas)
python run.py devoltaparaofuturo

# 7. Geocodificar (lat/lng via Google Maps ou Nominatim)
python run.py geocodificar --limite 500
```

### 21.2. Importar leiloeiros de CSV não-FENAJU

Quando o CSV não segue o formato FENAJU (`nome`, `uf`, `site`, etc.) — como o gerado pelo scraping BomValor — use importação direta:

```powershell
# Adaptar colunas e importar via script inline (ver seção 19.4)
python -c "
import csv, sys
sys.path.insert(0, '.')
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from database.models import Leiloeiro

engine = create_engine('$DB')
Session = sessionmaker(bind=engine)
session = Session()
ins = 0

with open('leiloeiros_bomvalor.csv', encoding='utf-8') as f:
    for row in csv.DictReader(f):
        nome = (row.get('Nome') or '').strip()
        site = (row.get('Site Oficial') or '').strip()
        wa   = (row.get('WhatsApp') or '').strip()
        if not nome or '404' in (row.get('Status') or ''):
            continue
        if not site.startswith('http'):
            site = 'https://' + site
        session.add(Leiloeiro(nome=nome, site=site, telefone=wa or None, situacao='Regular'))
        ins += 1

session.commit()
print(f'{ins} leiloeiros inseridos')
"
```

### 21.3. Stack Docker — serviços em execução

O sistema completo usa os seguintes containers:

| Container | Função | Porta |
|---|---|---|
| `leilao_api` | FastAPI (site + API REST) | 8000 |
| `leilao_postgres` | PostgreSQL | 5432 |
| `leilao_redis` | Redis (broker Celery) | 6379 |
| `leilao_worker` | Celery worker (tarefas async) | — |
| `leilao_beat` | Celery beat (agendamento) | — |
| `leilao_flower` | Monitor Celery | 5555 |
| `flaresolverr` | Bypass Cloudflare | 8191 |

**Comandos úteis:**
```powershell
# Ver status de todos
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Rodar pipeline dentro do container da API
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 5000"

# Ver logs da API em tempo real
docker logs -f leilao_api
```

### 21.4. Checklist pós-scraping

1. Verificar se progress JSON e CSV estão sincronizados (ver 20.3).
2. Aplicar migration se necessário (ver 20.5).
3. Importar CSV (`importar_ofertas_csv`).
4. Confirmar inserções: `SELECT leiloeiro, COUNT(*) FROM imoveis WHERE criado_em > NOW() - INTERVAL '1 hour' GROUP BY leiloeiro`.
5. Classificar → deduplicar → desativar encerrados → geocodificar (nesta ordem).
6. Verificar total ativo: `SELECT COUNT(*) FROM imoveis WHERE ativo=true`.

---

## 22. Diferenciar imóveis de outros produtos (veículos, máquinas, mercadorias)

Em leilões judiciais e extrajudiciais, a mesma listagem mistura **imóveis**, **veículos**, **equipamentos** e **mercadorias**. Tratar tudo como imóvel polui busca, mapa, filtros e score de oportunidade.

### 22.1. Problema do modelo atual (armadilhas comuns)

| Abordagem frágil | Por que falha |
|---|---|
| `tipo_imovel = outro` como “não-imóvel” | `OUTRO` também significa imóvel de tipo desconhecido |
| Corte por preço (ex.: &lt; R$ 20.000 = produto) | Terreno barato vira produto; veículo caro vira imóvel |
| Palavras-chave só no título | “Galpão com 2 caminhões” é imóvel, não veículo |
| URL `/lote/` genérica | Lote pode ser qualquer bem penhorado |

**Implementação atual no `leilao-scraper`:** a API filtra imóveis por `tipo_imovel ∈ {apartamento, casa, terreno…}` **e** exclui itens com `valor_minimo < 20_000`; o botão “Produtos” no frontend inverte esse filtro. Funciona como heurística rápida, mas não é confiável o suficiente para escalar.

### 22.2. Solução recomendada: campo `categoria_bem`

Separar **categoria do bem** (imóvel ou não) de **subtipo do imóvel**:

```
categoria_bem: imovel | veiculo | maquina | mercadoria | outro
tipo_imovel:   apartamento | casa | terreno | …   (só quando categoria_bem = imovel)
```

| Campo | Uso |
|---|---|
| `categoria_bem` | Filtros do site, mapa, SEO, exclusão de produtos |
| `tipo_imovel` | Subtipo habitacional/comercial/rural |
| `classificacao_confianca` | `alta` \| `media` \| `baixa` — fila de revisão manual |

**Regra:** filtros do site e mapa usam `categoria_bem = imovel`, nunca preço ou `OUTRO`.

### 22.3. Classificação em cascata (ordem de confiança)

```
1. Categoria do site        → /imoveis/, API categoria=Imóveis, menu “Imóveis”
2. JSON-LD @type            → RealEstateListing vs Product / Vehicle
3. URL do lote              → /imovel/, /veiculo/, /imoveis/ (lote é ambíguo)
4. Campos estruturados      → area_m2, quartos, matrícula vs renavam, placa, chassi
5. Regex de keywords        → pipeline/separar_produtos.py
6. Preço                    → sinal fraco apenas (nunca critério único)
7. LLM fallback             → extrator_llm.py retorna {"_nao_imovel": true}
```

**JSON-LD (já usado em `scraping/leiloeiros.py` e `extrator_generico.py`):**

```python
_TIPOS_IMOVEL  = {"RealEstateListing", "Residence", "House", "Apartment", ...}
_TIPOS_PRODUTO = {"Product", "IndividualProduct", "Offer", "AggregateOffer"}

def rank(node):
    t = set([node["@type"]] if isinstance(node.get("@type"), str) else node.get("@type") or [])
    if t & _TIPOS_IMOVEL:   return 0   # prioridade máxima
    if t & _TIPOS_PRODUTO: return 1
    return 2
```

**Keywords de produto (pipeline `separar_produtos.py`):** moto, veículo, caminhão, renavam, placa, escavadeira, notebook, sucata, mercadoria…

**Keywords de imóvel:** apartamento, terreno, matrícula, m², rua, edificação, gleba, hectare…

**Regra de desempate:** se o **título** contém marcador claro de imóvel, não reclassificar como produto mesmo com keywords ambíguas no corpo.

### 22.4. Quando classificar

| Momento | Ação |
|---|---|
| **Ingestão (scrape/import)** | Definir `categoria_bem` com sinais 1–4 |
| **Pós-import (`separar-produtos`)** | Corrigir erros com keywords + preço fraco |
| **Classificador (`classificar`)** | Score/risco só para `categoria_bem = imovel` |

```bash
python run.py separar-produtos   # reclassifica OUTRO ↔ imóvel real
python run.py classificar --limite 5000
```

### 22.5. Frontend e API

- Modo padrão: `categoria_bem=imovel` (substituir filtro por preço)
- Botão “Produtos”: `categoria_bem != imovel`
- Ocultar filtro “Tipo de imóvel” no modo produtos
- Mapa / Street View / preço-m²: apenas imóveis
- Cards distintos: imóvel (m², quartos) vs produto (marca/modelo, sem geocoding)

### 22.6. Métricas de qualidade

Monitorar por fonte:

```sql
SELECT f.nome,
       COUNT(*) FILTER (WHERE tipo_imovel = 'outro') AS outros,
       COUNT(*) FILTER (WHERE valor_minimo < 20000) AS abaixo_20k
FROM imoveis i JOIN fontes f ON f.id = i.fonte_id
WHERE i.ativo = true
GROUP BY f.nome ORDER BY outros DESC;
```

Fontes com alto % de reclassificação em `separar-produtos` precisam de parser dedicado ou filtro de categoria na coleta.

### 22.7. Checklist imóvel vs produto

1. Na coleta, filtrar por seção/categoria do site quando existir (`categoria=imoveis`, `/imoveis/`).
2. Gravar `categoria_bem` na importação — não depender só do pós-processamento.
3. Manter `separar-produtos` como correção, não como única linha de defesa.
4. Não usar preço como critério único.
5. Separar `OUTRO` (tipo desconhecido) de produto (categoria diferente).
6. Expor `classificacao_confianca` para revisão manual dos casos ambíguos.

---

## 23. Captura de documentos para download — edital, matrícula e PDFs (leilao-scraper)

Complementa a **seção 17** (enricher dedicado com `edital_url` / `matricula_url`). No projeto **`leilao-scraper`**, a implementação em produção usa um **array JSON unificado** no campo `arquivos`, populado automaticamente pelo scraper genérico na visita à página de detalhe.

### 23.1. Modelo de dados (implementação atual)

| Camada | Campo | Formato |
|---|---|---|
| Scraper (`ImovelRaw`) | `arquivos` | `list[dict]` |
| Banco (`imoveis`) | `arquivos` | TEXT — JSON serializado |
| API / frontend | `arquivos` | `[{tipo, url, nome}]` |

Tipos reconhecidos: `edital`, `matricula`, `laudo`, `certidao`, `memorial`, `processo`, `pdf`, `documento`.

```python
# scrapers/base.py
arquivos: list[dict] = field(default_factory=list)  # [{tipo, url, nome}]
```

Migration (se coluna ausente):

```bash
python migrar_arquivos.py
# ou: ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS arquivos TEXT;
```

O normalizer **preserva** `arquivos` existente se um novo scrape vier vazio (`PRESERVAR_SE_NULL`).

### 23.2. Fluxo no scraper genérico

```
Listagem de lotes
    → URL do lote (url_original)
    → _enriquecer_com_pagina()  [visita página individual]
    → _extrair_arquivos(soup, page_url)
    → salvar_imoveis() → coluna arquivos
    → frontend: seção "Documentos" no detalhe
```

O enriquecimento dispara quando faltam dados **ou** quando `arquivos` está vazio:

```python
precisa = (
    not im.data_primeiro_leilao or
    not im.endereco_completo or
    not im.descricao or
    not im.arquivos              # documentos (edital/matrícula)
)
```

**Comando:**

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\leilao-scraper"
python run.py scrape-lista --site https://exemplo-leiloes.com.br
python run.py scrape-csv caminho\sites.csv

# OBRIGATÓRIO após o scraping — baixa PDFs para disco local
python run.py baixar-docs --limite 200
```

Sites com JS pesado: Playwright é acionado automaticamente quando `_is_js_heavy(html)`.

### 23.3. Lógica de `_extrair_arquivos`

Arquivo: `scrapers/leiloeiros/generic_scraper.py`

Para cada `<a href>` da página de detalhe:

1. Ignora `#`, `javascript:`, `mailto:`, `tel:`
2. Resolve URL relativa com `urljoin(page_url, href)`
3. Aceita se **PDF** (`.pdf`) **ou** texto/href contém keyword:
   - `edital`, `matr[íi]cula`, `laudo`, `avaliação`, `certidão`, `memorial`, `processo`, `penhora`…
4. Classifica o `tipo` pela keyword dominante
5. Limite: 15 documentos por lote

```python
RE_PDF_EXT = re.compile(r'\.pdf(\?[^"\']*)?$', re.IGNORECASE)
RE_DOC_KW  = re.compile(
    r'edital|matr[íi]cula|laudo|avalia[cç][ãa]o|certid[ãa]o|memorial|'
    r'escritura|penhora|registro|processo',
    re.IGNORECASE,
)
```

### 23.4. Onde os sites guardam edital e matrícula

| Padrão | Onde procurar | Cobertura atual |
|---|---|---|
| Link direto `<a href="...edital.pdf">` | Seção “Documentos”, “Anexos” | ✅ `_extrair_arquivos` |
| Botão JS `onclick="ExibeDoc('/path.pdf')"` | Caixa, alguns judiciais | ❌ precisa regex no HTML bruto |
| `<iframe src="...pdf">` | Tribunais, visualizadores embutidos | ❌ estender extrator |
| API interna `/api/lote/{id}/anexos` | Milan, Frazão, Superbid | ❌ parser por site |
| Edital na **página do leilão** (não do lote) | 1 edital para N lotes | ❌ scrape da página pai |
| `data-url` / `data-href` em botões | SPAs React/Next.js | ❌ estender extrator |
| Download via POST autenticado | Área logada | ❌ sessão + replay Network |

### 23.5. Extensões recomendadas do extrator genérico

**A) iframes com PDF**

```python
for iframe in soup.find_all("iframe", src=True):
    src = urljoin(page_url, iframe["src"])
    if RE_PDF_EXT.search(src) or RE_DOC_KW.search(src):
        arquivos.append({"tipo": "pdf", "url": src, "nome": "Documento"})
```

**B) onclick / ExibeDoc (padrão Caixa)**

```python
for path in re.findall(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", html):
    url = urljoin(base, path)
    tipo = "matricula" if "matricula" in path.lower() else "edital"
    arquivos.append({"tipo": tipo, "url": url, "nome": tipo.capitalize()})
```

**C) atributos data-* em botões SPA**

```python
for el in soup.find_all(attrs={"data-url": True}):
    url = urljoin(page_url, el["data-url"])
    ...
```

**D) Playwright quando link só aparece após render**

```python
await page.wait_for_selector('a[href*=".pdf"], a:has-text("Edital")', timeout=8000)
links = await page.eval_on_selector_all(
    'a[href*=".pdf"], a[href*="edital"], a[href*="matricula"]',
    "els => els.map(a => ({href: a.href, text: a.innerText.trim()}))"
)
```

**E) Interceptar API no Playwright**

```python
async def handle_response(response):
    if "application/json" in response.headers.get("content-type", ""):
        if "anexo" in response.url or "documento" in response.url:
            data = await response.json()
            # extrair URLs de PDF
page.on("response", handle_response)
```

### 23.6. Guardar link vs baixar o arquivo

| Estratégia | Prós | Contras |
|---|---|---|
| **Só URL** (atual) | Simples, sem storage, link sempre do site oficial | Link expira; depende do site estar no ar |
| **Download local/S3** | Disponível offline; sobrevive a páginas removidas | Storage, copyright, links com cookie/sessão |

**Download implementado — `pipeline/document_downloader.py`:**

```powershell
# Obrigatório após qualquer scraping
python run.py baixar-docs --limite 200

# Opções adicionais
python run.py baixar-docs --limite 500 --storage "D:\docs_leilao"
```

O `DocumentoDownloader` (implementado em jun/2026):
- **Camada 1:** download direto via `httpx`, validação por magic bytes (`%PDF`, `PK`, OLE2)
- **Camada 2:** fallback automático via FlareSolverr (`http://localhost:8191`) para sites com Cloudflare
- **Armazenamento:** `storage/docs/{fonte}/{id_externo}/{tipo}_{hash8}.pdf`
- **Banco:** campo `arquivos` atualizado com `path_local`, `hash_md5`, `baixado: true/false`
- **Celery:** task `baixar_documentos` agendada automaticamente a cada hora (`:45`)
- **API:** `GET /imoveis/{id}/documentos/{idx}/download` — serve o arquivo local ou redireciona para URL original se ainda não baixado

JSON resultante no campo `arquivos`:

```json
{
  "tipo": "edital",
  "url": "https://site.com/edital.pdf",
  "nome": "Edital do Leilão",
  "path_local": "storage/docs/mega_leiloes/abc123/edital_a1b2c3d4.pdf",
  "hash_md5": "a1b2c3d4e5f6...",
  "baixado": true
}
```

**Cuidados:** rate limit, PDFs grandes (limite ex.: 20 MB), URLs POST-only, cookies de sessão. URLs temporárias (S3 pre-signed) expiram em horas — rodar `baixar-docs` logo após o scraping.

### 23.7. Parser dedicado por leiloeiro (quando genérico não basta)

Para sites com API previsível, criar extrator no scraper específico:

```python
async def _extrair_documentos_frazao(self, lote_id: str) -> list[dict]:
    resp = await self._get(f"{API}/lote/{lote_id}/anexos")
    return [
        {
            "tipo": "edital" if "edital" in a["nome"].lower() else "matricula",
            "url": a["url"],
            "nome": a["nome"],
        }
        for a in resp.json()
    ]
```

Registrar em mapa `FONTE_EXTRACTORS` (padrão descrito na seção 17.2).

### 23.8. Exibição no frontend

Seção **Documentos** em `frontend/index.html` — parse de `im.arquivos`:

```javascript
const docs = JSON.parse(im.arquivos || '[]');
// ícones: edital 📋, matricula 📄, laudo 🔍, certidao 📜 ...
docs.map(d => `<a href="${d.url}" target="_blank">${d.nome}</a>`)
```

### 23.9. Diagnóstico quando documentos não aparecem

| Sintoma | Causa provável | Ação |
|---|---|---|
| `arquivos` sempre null | Página não visitada no enrich | Confirmar `_enriquecer_com_pagina` |
| Só edital, sem matrícula | Matrícula em iframe/JS | Playwright + extensões 23.5 |
| Funcionava, parou | `arquivos` preservado de scrape antigo vazio | `--reset` ou forçar re-enrich |
| 403 no link | Bot protection (Caixa/Radware) | Playwright na mesma sessão (seção 17.3) |
| Edital no leilão, não no lote | Escopo errado | Scrape URL do evento/leilão pai |

**Query de cobertura:**

```sql
SELECT f.nome,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE arquivos IS NOT NULL AND arquivos != '[]') AS com_docs
FROM imoveis i
JOIN fontes f ON f.id = i.fonte_id
WHERE i.ativo = true
GROUP BY f.nome
ORDER BY com_docs::float / NULLIF(total, 0) ASC;
```

### 23.10. Checklist captura de documentos

1. Confirmar coluna `arquivos` no banco (`migrar_arquivos.py`).
2. Inspecionar **Network → filtro pdf** em um lote ativo antes de codar.
3. Se HTML estático → `_extrair_arquivos` costuma bastar.
4. Se SPA/API → parser dedicado ou interceptação Playwright.
5. Se `onclick`/`ExibeDoc` → regex no HTML bruto, não confiar só em `href`.
6. Rodar enrich logo após coleta (páginas expiram — ver Caixa, seção 17.3).
7. Não assumir URL de edital previsível; matrícula Caixa é exceção (`/editais/matricula/{UF}/{id}.pdf`).
8. Decidir cedo: **só links** (atual) vs **download para storage** (23.6).
9. Uma instância por vez em enrichers com Playwright (seção 17.5).

### 23.11. Relação com a seção 17

| Aspecto | Seção 17 (enricher dedicado) | Seção 23 (scraper genérico) |
|---|---|---|
| Campos | `edital_url`, `matricula_url`, `documentos` | `arquivos` (JSON unificado) |
| Quando roda | Comando `enriquecer-documentos` | Durante `scrape-lista` / `scrape-csv` |
| Melhor para | Caixa, fontes com bot protection | Leiloeiros com links `<a href>` visíveis |
| Evolução | Unificar em `arquivos` ou migrar para colunas dedicadas | Estender `_extrair_arquivos` + parsers por site |

---

## 24. Pipeline completo: de site/lista até os cards do sistema

Fluxo end-to-end para scraping de um site isolado ou de uma lista/planilha de sites, com extração completa de dados (leiloeiro, descrição, preços, datas, documentos), deduplicação global, inserção no banco e exportação CSV para `/csv`.

### 24.1. Entradas suportadas

| Entrada | Formato | Comando |
|---|---|---|
| URL única | `https://leiloeiro.com.br` | `python run.py scrape-lista --site URL` |
| Lista em arquivo `.txt` | Uma URL por linha | `python run.py scrape-lista --arquivo sites.txt` |
| Planilha `.csv` | Coluna `site` ou `url` | `python run.py scrape-csv planilha.csv` |
| Planilha `.xlsx` | Coluna `site` ou `url` | Converter para CSV primeiro (ver 24.2) |

### 24.2. Converter planilha Excel para CSV antes de scraping

```powershell
python -c "
import pandas as pd
df = pd.read_excel('sites.xlsx')
df.to_csv('sites.csv', index=False, encoding='utf-8')
print(df.columns.tolist())   # confirmar nome da coluna de URL
print(f'{len(df)} sites')
"
```

Se a coluna de URL não se chamar `site` ou `url`, renomeie:

```powershell
python -c "
import pandas as pd
df = pd.read_excel('sites.xlsx')
df = df.rename(columns={'Link': 'site', 'Endereço': 'site'})  # ajustar ao nome real
df.to_csv('sites.csv', index=False, encoding='utf-8')
"
```

### 24.3. Dados extraídos por imóvel

O scraper genérico coleta — em cada lote visitado — os seguintes campos:

| Campo | Descrição |
|---|---|
| `leiloeiro` | Nome ou domínio do leiloeiro |
| `titulo` | Título completo do lote |
| `descricao` | Descrição detalhada do imóvel |
| `cidade` / `estado` | Localização |
| `endereco_completo` | Endereço quando disponível |
| `tipo_imovel` | apartamento, casa, terreno, etc. |
| `area_m2` | Área útil/total em m² |
| `valor_minimo` | Valor mínimo de arrematação (1ª praça) |
| `valor_avaliacao` | Valor de avaliação do imóvel |
| `desconto_pct` | Desconto calculado: `(1 - minimo/avaliacao) * 100` |
| `data_primeiro_leilao` | Data/hora da 1ª praça |
| `data_segundo_leilao` | Data/hora da 2ª praça (quando houver) |
| `url_original` | URL do lote no site do leiloeiro |
| `imagem_principal` | URL da foto principal |
| `arquivos` | JSON: `[{tipo, url, nome}]` — edital, matrícula, laudos |
| `latitude` / `longitude` | Coordenadas para o mapa (geocodificadas depois) |

### 24.4. Campos de documentos (edital e matrícula)

O enriquecimento automático ocorre durante o scraping (`_enriquecer_com_pagina`). Para cada lote, o scraper visita a página individual e extrai links de documentos:

```python
# Exemplo de saída do campo arquivos:
[
  {"tipo": "edital",    "url": "https://site.com/edital.pdf",    "nome": "Edital do Leilão"},
  {"tipo": "matricula", "url": "https://site.com/matricula.pdf", "nome": "Matrícula"},
  {"tipo": "laudo",     "url": "https://site.com/laudo.pdf",     "nome": "Laudo de Avaliação"}
]
```

Se o scraping primário não capturou documentos, rodar o enricher em seguida:

```powershell
# Dentro do container Docker (produção)
docker exec leilao_api bash -c "cd /app && python run.py enriquecer-documentos --limite 500"

# Fora do container (desenvolvimento local)
cd "C:\Users\arthur\leilao-scraper"
python run.py enriquecer-documentos --limite 500
```

### 24.5. Execução do scraping (passo a passo)

```powershell
# ── Preparação ──────────────────────────────────────────────────────────────
cd "C:\Users\arthur\leilao-scraper"
$env:DATABASE_URL_SYNC = "postgresql://leilao:leilao123@localhost:5432/leilao_db"

# ── Opção 1: site único ─────────────────────────────────────────────────────
python run.py scrape-lista --site https://www.megaleiloes.com.br

# ── Opção 2: arquivo .txt com lista de URLs ─────────────────────────────────
python run.py scrape-lista --arquivo sites.txt

# ── Opção 3: planilha .csv (coluna "site") ──────────────────────────────────
python run.py scrape-csv planilha.csv

# ── Opção 4: todas as fontes cadastradas no banco ───────────────────────────
python run.py scrape-todos --limite-por-fonte 500
```

Progresso salvo automaticamente em `scraper_progress.json` — se interrompido, retoma de onde parou.

### 24.6. Verificação de duplicatas em todo o sistema

A deduplicação compara **novos lotes** contra **tudo que já existe no banco** usando dois critérios em cascata:

```
1. URL exata (url_original)          → duplicata certa
2. Título + cidade + estado + preço  → duplicata provável (fuzzy)
```

```powershell
# Deduplicar todo o banco (marca campo duplicado=true nos repetidos)
python run.py deduplicar

# Ver relatório de duplicatas por fonte antes de inserir
python run.py deduplicar --dry-run --verbose
```

Saída do `--dry-run`:

```
Fontes com mais duplicatas:
  mega_leiloes:    312 únicos,  47 duplicados (13%)
  grupo_lance:     289 únicos,  17 duplicados  (6%)
  central_sul:     339 únicos,   2 duplicados  (1%)
Total: 940 únicos, 66 duplicados
```

A deduplicação **não apaga** — apenas marca `duplicado=true`. O campo pode ser revisado manualmente quando necessário.

### 24.7. Inserção apenas dos imóveis únicos

Após a deduplicação, somente imóveis com `duplicado=false` ficam visíveis na API e nos cards:

```python
# Filtro padrão na API (api/routers/imoveis.py)
query = query.filter(
    Imovel.ativo == True,
    Imovel.duplicado == False,
    Imovel.categoria_bem == 'imovel',
)
```

Para forçar reprocessamento de possíveis falsos-positivos:

```powershell
# Reabrir para revisão imóveis marcados como duplicados de uma fonte
python run.py deduplicar --reset-fonte mega_leiloes
```

### 24.8. Transportar documentos para os cards

Os documentos ficam disponíveis nos cards automaticamente quando `arquivos` está preenchido. O frontend lê o campo JSON e renderiza badges e links:

**Card de listagem** — badges clicáveis (não propagam o clique para o detalhe):
```javascript
const arquivos = JSON.parse(im.arquivos || '[]');
const edital    = arquivos.find(a => a.tipo === 'edital');
const matricula = arquivos.find(a => a.tipo === 'matricula');

// Badges inline no card
${edital    ? `<a href="${edital.url}"    target="_blank" onclick="event.stopPropagation()">📋 Edital</a>`    : ''}
${matricula ? `<a href="${matricula.url}" target="_blank" onclick="event.stopPropagation()">📄 Matrícula</a>` : ''}
```

**Detalhe do imóvel** — seção completa com todos os documentos:
```javascript
const docs = JSON.parse(im.arquivos || '[]');
if (docs.length) {
  const icones = { edital:'📋', matricula:'📄', laudo:'🔍', certidao:'📜', pdf:'📁' };
  html += `<div class="doc-section"><h4>📁 Documentos</h4>` +
    docs.map(d =>
      `<a href="${d.url}" target="_blank" class="doc-badge">
         ${icones[d.tipo]||'📁'} ${d.nome || d.tipo}
       </a>`
    ).join('') + `</div>`;
}
```

### 24.9. Exportar resultado para CSV em /csv

Após a inserção e deduplicação, gerar o arquivo CSV dos imóveis únicos e salvá-lo em `/csv`:

```powershell
# Criar pasta /csv se não existir
New-Item -ItemType Directory -Force -Path "C:\Users\arthur\leilao-scraper\csv"

# Exportar imóveis únicos e ativos
python -c "
import os, csv, json
from datetime import datetime
from sqlalchemy import create_engine, text

engine = create_engine(os.environ['DATABASE_URL_SYNC'])
hoje = datetime.now().strftime('%Y%m%d_%H%M')
caminho = fr'C:\Users\arthur\leilao-scraper\csv\imoveis_{hoje}.csv'

campos = [
    'id','leiloeiro','titulo','descricao','cidade','estado',
    'tipo_imovel','area_m2','valor_minimo','valor_avaliacao','desconto_pct',
    'data_primeiro_leilao','data_segundo_leilao',
    'url_original','imagem_principal','arquivos',
    'latitude','longitude','score_oportunidade','criado_em'
]

with engine.connect() as conn:
    rows = conn.execute(text(
        f'SELECT {chr(44).join(campos)} FROM imoveis '
        'WHERE ativo=true AND duplicado=false AND categoria_bem=\'imovel\' '
        'ORDER BY score_oportunidade DESC NULLS LAST'
    ))
    with open(caminho, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(campos)
        w.writerows(rows)
    total = rows.rowcount

print(f'{total} imóveis exportados → {caminho}')
"
```

O sufixo `-sig` no encoding garante que o Excel abra o CSV com acentos corretamente.

### 24.10. Pipeline completo em um único bloco (copiar e executar)

```powershell
cd "C:\Users\arthur\leilao-scraper"
$env:DATABASE_URL_SYNC = "postgresql://leilao:leilao123@localhost:5432/leilao_db"
$SITE = "https://www.megaleiloes.com.br"   # ou --arquivo sites.txt / --csv planilha.csv

# 1. Scraping + enriquecimento automático de documentos
python run.py scrape-lista --site $SITE

# 2. Enriquecer documentos que o scraper não pegou
python run.py enriquecer-documentos --limite 500

# 3. *** OBRIGATÓRIO *** Baixar PDFs para disco (edital, matrícula, laudos)
python run.py baixar-docs --limite 500

# 4. Classificar (score_oportunidade, tipo_imovel, categoria_bem)
python run.py classificar --limite 5000

# 5. Normalizar cidades
python run.py normalizar-cidades

# 6. Deduplicar (marca duplicado=true nos repetidos)
python run.py deduplicar

# 7. Desativar leilões encerrados
python run.py devoltaparaofuturo

# 8. Geocodificar novos imóveis
python run.py geocodificar --limite 500

# 9. Exportar CSV com únicos para /csv
$hoje = Get-Date -Format "yyyyMMdd_HHmm"
python -c "
import os, csv
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['DATABASE_URL_SYNC'])
campos = ['id','leiloeiro','titulo','cidade','estado','tipo_imovel','area_m2',
          'valor_minimo','valor_avaliacao','desconto_pct',
          'data_primeiro_leilao','url_original','arquivos','score_oportunidade']
with engine.connect() as conn:
    rows = list(conn.execute(text(
        f'SELECT {chr(44).join(campos)} FROM imoveis '
        'WHERE ativo=true AND duplicado=false AND categoria_bem=\'imovel\' '
        'ORDER BY score_oportunidade DESC NULLS LAST'
    )))
import pathlib
pathlib.Path('csv').mkdir(exist_ok=True)
with open(f'csv/imoveis_$hoje.csv', 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f); w.writerow(campos); w.writerows(rows)
print(f'{len(rows)} imóveis → csv/imoveis_$hoje.csv')
"
```

### 24.11. Verificar o que foi inserido

```sql
-- Novos imóveis da última hora por fonte
SELECT leiloeiro, COUNT(*) AS novos,
       COUNT(*) FILTER (WHERE duplicado=true)  AS duplicados,
       COUNT(*) FILTER (WHERE arquivos IS NOT NULL AND arquivos != '[]') AS com_docs
FROM imoveis
WHERE criado_em > NOW() - INTERVAL '1 hour'
GROUP BY leiloeiro ORDER BY novos DESC;

-- Total geral do sistema
SELECT COUNT(*) FILTER (WHERE ativo=true AND duplicado=false) AS ativos_unicos,
       COUNT(*) FILTER (WHERE duplicado=true)                 AS duplicados,
       COUNT(*) FILTER (WHERE arquivos IS NOT NULL AND arquivos != '[]') AS com_documentos
FROM imoveis;
```

### 24.12. Checklist completo

1. **Preparar entrada:** URL única, `.txt`, `.csv` ou `.xlsx` (converter Excel antes).
2. **Rodar scraping:** `scrape-lista --site URL` ou `scrape-csv planilha.csv`.
3. **Conferir dados brutos:** verificar se `titulo`, `valor_minimo`, `data_primeiro_leilao` e `arquivos` foram preenchidos.
4. **Enriquecer documentos** caso `arquivos` esteja vazio: `enriquecer-documentos --limite 500`.
5. **⚠️ OBRIGATÓRIO — Baixar PDFs para disco:** `baixar-docs --limite 500` (salva edital/matrícula/laudos em `storage/docs/`; atualiza `path_local` e `hash_md5` no banco).
6. **Classificar:** `classificar --limite 5000` (gera `score_oportunidade` e `tipo_imovel`).
7. **Deduplicar:** `deduplicar` — conferir % de duplicatas com `--dry-run` antes.
8. **Desativar encerrados:** `devoltaparaofuturo`.
9. **Geocodificar:** `geocodificar --limite 500`.
10. **Sincronizar imóveis com /admin:** confirmar que todos os imóveis ativos e únicos aparecem no painel (ver seção 25.1).
11. **Sincronizar leiloeiros novos com /admin e aba Leiloeiros:** confirmar que novos leiloeiros foram inseridos e aparecem no frontend (ver seção 25.2).
12. **Conferir cards:** abrir `http://localhost:8000` e verificar badges de Edital/Matrícula — o link de download deve apontar para `GET /imoveis/{id}/documentos/{idx}/download`.
13. **Exportar CSV:** script da seção 24.9 → arquivo salvo em `/csv/imoveis_YYYYMMDD_HHMM.csv`.

---

## 25. Sincronização com /admin e aba Leiloeiros do frontend

Todo scraping deve ser seguido de sincronização: imóveis no painel `/admin` e novos leiloeiros tanto no `/admin` quanto na aba **Leiloeiros** do site público. Esse passo fecha o ciclo — dados coletados ficam visíveis e gerenciáveis para o operador.

### 25.1. Sincronização de imóveis com /admin

O `/admin` consome a mesma API que o frontend público (`GET /api/v1/imoveis`). Não há carga separada — os imóveis aparecem automaticamente assim que estiverem com `ativo=true` e `duplicado=false` no banco.

**Verificar se os imóveis estão aparecendo no /admin:**

```sql
-- Quantos imóveis estão visíveis para o admin agora
SELECT COUNT(*) AS visiveis_admin
FROM imoveis
WHERE ativo = true AND duplicado = false;

-- Breakdown por fonte (útil para confirmar que o scraping novo chegou)
SELECT leiloeiro,
       COUNT(*)                                                    AS total,
       COUNT(*) FILTER (WHERE criado_em > NOW() - INTERVAL '2h') AS novos_2h
FROM imoveis
WHERE ativo = true AND duplicado = false
GROUP BY leiloeiro
ORDER BY novos_2h DESC, total DESC;
```

**Se imóveis não aparecem no /admin após o pipeline:**

| Sintoma | Causa provável | Ação |
|---|---|---|
| Imóvel existe no banco mas `ativo=false` | `devoltaparaofuturo` marcou como encerrado | Verificar `data_primeiro_leilao` — pode estar errada |
| Imóvel existe mas `duplicado=true` | Deduplicação falso-positivo | `python run.py deduplicar --reset-fonte <fonte>` |
| Imóvel não existe no banco | Scraping não inseriu | Verificar `scraper_progress.json` e logs |
| Admin mostra 0 imóveis | Filtro de `categoria_bem` excluindo tudo | Confirmar `categoria_bem='imovel'` nos registros |

**Forçar refresh do cache da API (se aplicável):**

```powershell
# Reiniciar o uvicorn dentro do container para limpar cache em memória
docker exec leilao_api bash -c "kill -HUP 1"
# ou reiniciar o container completo
docker restart leilao_api
```

**Campos exibidos no /admin (tabela de imóveis):**

| Coluna admin | Campo no banco | Observação |
|---|---|---|
| ID | `id` | Link para detalhe |
| Leiloeiro | `leiloeiro` | Fonte do imóvel |
| Título | `titulo` | Truncado a 80 chars |
| Cidade/UF | `cidade`, `estado` | |
| Tipo | `tipo_imovel` | |
| Valor mín. | `valor_minimo` | Formatado em R$ |
| Desconto | `desconto_pct` | % em relação à avaliação |
| Score | `score_oportunidade` | 0–100 |
| Docs | `arquivos` | Ícones 📋📄 quando preenchidos |
| Data leilão | `data_primeiro_leilao` | |
| Ativo | `ativo` | Toggle on/off |
| Duplicado | `duplicado` | Badge vermelho quando true |

### 25.2. Sincronização de novos leiloeiros com /admin e aba Leiloeiros

Cada site scrapado implica um leiloeiro. Se o leiloeiro ainda não está cadastrado no banco (`tabela leiloeiros`), ele deve ser inserido — caso contrário não aparece na aba **Leiloeiros** do frontend nem no painel `/admin`.

#### 25.2.1. Identificar leiloeiros novos após scraping

```sql
-- Leiloeiros presentes em imóveis mas sem cadastro na tabela leiloeiros
SELECT DISTINCT i.leiloeiro
FROM imoveis i
LEFT JOIN leiloeiros l ON lower(l.nome) = lower(i.leiloeiro)
                      OR l.site ILIKE '%' || split_part(i.leiloeiro, '.', 1) || '%'
WHERE l.id IS NULL
ORDER BY i.leiloeiro;
```

#### 25.2.2. Inserir leiloeiros novos no banco

```powershell
python -c "
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from database.models import Leiloeiro

engine = create_engine(os.environ['DATABASE_URL_SYNC'])
Session = sessionmaker(bind=engine)
session = Session()

# Buscar leiloeiros presentes em imóveis mas sem cadastro
with engine.connect() as conn:
    rows = conn.execute(text('''
        SELECT DISTINCT i.leiloeiro,
               MIN(i.url_original) AS url_exemplo
        FROM imoveis i
        LEFT JOIN leiloeiros l
               ON lower(l.nome) = lower(i.leiloeiro)
        WHERE l.id IS NULL
          AND i.ativo = true
        GROUP BY i.leiloeiro
    ''')).fetchall()

inseridos = 0
for nome, url_exemplo in rows:
    # Extrai domínio da URL como site
    from urllib.parse import urlparse
    site = ''
    if url_exemplo:
        p = urlparse(url_exemplo)
        site = f'{p.scheme}://{p.netloc}' if p.netloc else ''
    session.add(Leiloeiro(
        nome=nome,
        site=site or None,
        situacao='Regular',
    ))
    inseridos += 1

session.commit()
print(f'{inseridos} leiloeiros inseridos')
"
```

#### 25.2.3. Campos obrigatórios do leiloeiro no banco

| Campo | Obrigatório | Descrição |
|---|---|---|
| `nome` | Sim | Nome completo ou domínio |
| `site` | Recomendado | URL raiz do site (ex.: `https://megaleiloes.com.br`) |
| `situacao` | Sim | `'Regular'` para leiloeiros ativos |
| `uf` | Opcional | Estado de atuação principal |
| `telefone` | Opcional | WhatsApp ou telefone de contato |
| `logo_url` | Opcional | URL do logo para exibição no card |

#### 25.2.4. Exibição na aba Leiloeiros do frontend

A aba **Leiloeiros** em `http://localhost:8000` exibe todos os registros da tabela `leiloeiros` onde `situacao='Regular'`. O card de cada leiloeiro mostra:

- Nome
- Logo (quando `logo_url` preenchido)
- Link para o site
- Quantidade de imóveis ativos vinculados
- Botão de contato (WhatsApp quando `telefone` preenchido)

**Verificar se o leiloeiro novo aparece no frontend:**

```sql
-- Leiloeiros cadastrados com imóveis ativos
SELECT l.nome, l.site, l.situacao,
       COUNT(i.id) AS imoveis_ativos
FROM leiloeiros l
LEFT JOIN imoveis i ON lower(i.leiloeiro) = lower(l.nome)
                   AND i.ativo = true AND i.duplicado = false
GROUP BY l.id, l.nome, l.site, l.situacao
ORDER BY imoveis_ativos DESC;
```

**Se o leiloeiro foi inserido mas não aparece no frontend:**

| Sintoma | Causa | Ação |
|---|---|---|
| Leiloeiro sem imóveis no card | `leiloeiro` em `imoveis` não bate com `nome` em `leiloeiros` | Padronizar o campo `leiloeiro` na importação |
| Leiloeiro não aparece na aba | `situacao != 'Regular'` | `UPDATE leiloeiros SET situacao='Regular' WHERE nome='...'` |
| Aba Leiloeiros vazia | API retorna erro | Verificar `docker logs leilao_api` |

#### 25.2.5. Sincronização via /admin (interface)

No painel `/admin`, aba **Leiloeiros**:

1. Todos os leiloeiros do banco aparecem na tabela (independente de ter imóveis).
2. É possível editar nome, site, logo, UF, telefone e situação diretamente.
3. Leiloeiros com `situacao='Inativo'` ficam ocultos no frontend mas visíveis no admin.
4. O campo **"Imóveis ativos"** no admin é calculado em tempo real — atualiza automaticamente após novo scraping.

#### 25.2.6. Atualizar logo e dados do leiloeiro

```powershell
python -c "
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ['DATABASE_URL_SYNC'])
with engine.connect() as conn:
    conn.execute(text('''
        UPDATE leiloeiros SET
            site      = :site,
            logo_url  = :logo,
            telefone  = :tel,
            uf        = :uf,
            situacao  = 'Regular'
        WHERE lower(nome) = lower(:nome)
    '''), {
        'nome': 'Mega Leilões',
        'site': 'https://www.megaleiloes.com.br',
        'logo': 'https://www.megaleiloes.com.br/logo.png',
        'tel':  '5511999999999',
        'uf':   'SP',
    })
    conn.commit()
print('Leiloeiro atualizado')
"
```

### 25.3. Pipeline completo com sincronização (bloco final)

Extensão do bloco da seção 24.10 com os passos de sincronização:

```powershell
cd "C:\Users\arthur\leilao-scraper"
$env:DATABASE_URL_SYNC = "postgresql://leilao:leilao123@localhost:5432/leilao_db"

# ── Scraping + enrichment ────────────────────────────────────────────────────
python run.py scrape-lista --site $SITE
python run.py enriquecer-documentos --limite 500

# ── OBRIGATÓRIO: baixar PDFs para disco ──────────────────────────────────────
python run.py baixar-docs --limite 500

# ── Pós-processamento ────────────────────────────────────────────────────────
python run.py classificar --limite 5000
python run.py normalizar-cidades
python run.py deduplicar
python run.py devoltaparaofuturo
python run.py geocodificar --limite 500

# ── Sincronização: novos leiloeiros → banco → /admin → aba Leiloeiros ───────
python -c "
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from database.models import Leiloeiro
from urllib.parse import urlparse

engine = create_engine(os.environ['DATABASE_URL_SYNC'])
session = sessionmaker(bind=engine)()

rows = engine.connect().execute(text('''
    SELECT DISTINCT i.leiloeiro, MIN(i.url_original) AS url_ex
    FROM imoveis i
    LEFT JOIN leiloeiros l ON lower(l.nome) = lower(i.leiloeiro)
    WHERE l.id IS NULL AND i.ativo = true
    GROUP BY i.leiloeiro
''')).fetchall()

ins = 0
for nome, url_ex in rows:
    p = urlparse(url_ex or '')
    site = f'{p.scheme}://{p.netloc}' if p.netloc else None
    session.add(Leiloeiro(nome=nome, site=site, situacao='Regular'))
    ins += 1
session.commit()
print(f'{ins} leiloeiro(s) novo(s) inserido(s)')
"

# ── Exportar CSV ─────────────────────────────────────────────────────────────
$hoje = Get-Date -Format "yyyyMMdd_HHmm"
python -c "
import os, csv, pathlib
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['DATABASE_URL_SYNC'])
campos = ['id','leiloeiro','titulo','cidade','estado','tipo_imovel','area_m2',
          'valor_minimo','valor_avaliacao','desconto_pct',
          'data_primeiro_leilao','url_original','arquivos','score_oportunidade']
with engine.connect() as conn:
    rows = list(conn.execute(text(
        f'SELECT {chr(44).join(campos)} FROM imoveis '
        'WHERE ativo=true AND duplicado=false AND categoria_bem=\'imovel\' '
        'ORDER BY score_oportunidade DESC NULLS LAST'
    )))
pathlib.Path('csv').mkdir(exist_ok=True)
with open(f'csv/imoveis_$hoje.csv', 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f); w.writerow(campos); w.writerows(rows)
print(f'{len(rows)} imóveis → csv/imoveis_$hoje.csv')
"

# ── Confirmar no admin ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "Verificar em: http://localhost:8000/admin"
Write-Host "  → Aba Imóveis : todos os ativos aparecem?"
Write-Host "  → Aba Leiloeiros: novos leiloeiros aparecem?"
Write-Host "  → Site público : http://localhost:8000 → aba Leiloeiros"
```

### 25.4. Checklist de sincronização

1. Após o pipeline, confirmar com SQL que imóveis têm `ativo=true` e `duplicado=false`.
2. **⚠️ OBRIGATÓRIO — Confirmar que `baixar-docs` foi executado:** verificar no banco se os registros com `arquivos` têm `path_local` preenchido:
   ```sql
   SELECT COUNT(*) FILTER (WHERE arquivos LIKE '%path_local%') AS com_arquivo_local,
          COUNT(*) FILTER (WHERE arquivos IS NOT NULL)         AS total_com_arquivos
   FROM imoveis WHERE ativo = true;
   ```
3. Abrir `/admin` → aba **Imóveis** — conferir que novos imóveis aparecem com score, docs e data.
4. Rodar query da seção 25.2.1 para identificar leiloeiros sem cadastro.
5. Inserir leiloeiros novos com `situacao='Regular'` (script da seção 25.2.2).
6. Abrir `/admin` → aba **Leiloeiros** — confirmar que novos leiloeiros estão listados.
7. Abrir `http://localhost:8000` → aba **Leiloeiros** — confirmar exibição pública.
8. Completar dados do leiloeiro (site, logo, telefone) via admin ou script da seção 25.2.6.
9. Exportar CSV final para `/csv` (seção 24.9).

---

## 26. Scraper standalone da Caixa Econômica Federal

Script de referência: **`scraping/scraper_caixa.py`** — extrai imóveis de todos os 27 estados diretamente dos CSVs públicos da Caixa, com bypass do Radware Bot Manager via Playwright + stealth.

### 26.1. Proteção: Radware Bot Manager

A Caixa usa **Radware Bot Manager** — diferente do Cloudflare — que bloqueia:

| Ferramenta | Resultado |
|---|---|
| `httpx` / `requests` | 200 OK, mas retorna HTML do CAPTCHA Radware |
| `curl_cffi` | Idem — Radware não é bypassado por TLS fingerprint |
| Playwright headless (sem stealth) | Detectado e bloqueado |
| **Playwright + `playwright-stealth`** | ✅ Passa — retorna o CSV real |

O FlareSolverr **não funciona** para Radware (é específico para Cloudflare).

### 26.2. Padrão crítico: captura de download com `expect_download`

O endpoint `/listaweb/Lista_imoveis_{UF}.csv` **dispara um download direto** em vez de renderizar a página. O `page.goto()` lança `"Download is starting"` — isso é comportamento esperado.

**Padrão correto:**

```python
async with page.expect_download(timeout=30000) as dl_info:
    try:
        await page.goto(url, timeout=30000)
    except Exception:
        pass  # "Download is starting" é esperado — download já foi capturado

# FORA do bloco with: aguarda o download completar
download = await dl_info.value
await download.save_as(tmp_path)
```

**Erro comum:** colocar `download = await dl_info.value` DENTRO do `try/except` que captura o erro do `goto`. O `except` sai do bloco `async with` antes de `dl_info.value` ser resolvido.

**Warm-up obrigatório:** visitar a home (`https://venda-imoveis.caixa.gov.br`) antes de solicitar o CSV acumula cookies legítimos e reduz a chance de bloqueio.

```python
await page.goto(CAIXA_BASE, timeout=20000, wait_until="domcontentloaded")
await page.wait_for_timeout(1200)  # simula navegação humana

async with page.expect_download(timeout=30000) as dl_info:
    try:
        await page.goto(CSV_URL, timeout=30000)
    except Exception:
        pass

download = await dl_info.value
```

### 26.3. Formato do CSV (atualizado mai/2026)

O formato mudou em relação às versões anteriores. Colunas atuais:

| Coluna CSV | Campo normalizado | Observação |
|---|---|---|
| `N° do imóvel` | `id_imovel` | ID único da Caixa (ex: `8787708470452`) |
| `UF` | `estado` | Sigla do estado |
| `Cidade` | `cidade` | |
| `Bairro` | `bairro` | |
| `Endereço` | `endereco` | |
| `Preço` | `valor_minimo` | Valor mínimo de venda (≠ avaliação) |
| `Valor de avaliação` | `valor_avaliacao` | |
| `Desconto` | `desconto_pct` | Percentual como string com `%` |
| `Financiamento` | `financiamento` | `"Sim"` / `"Não"` |
| `Descrição` | `descricao` | Texto livre com área, quartos, tipo |
| `Modalidade de venda` | `modalidade` | `"Licitação Aberta"`, `"Venda Direta"`, etc. |
| `Link de acesso` | `url_original` | URL da página de detalhe do imóvel |

**Colunas removidas** (existiam em versões anteriores):
- `Número Matrícula` → substituída por `N° do imóvel`
- `Valor Mínimo de Venda` → renomeada para `Preço`
- `Área Total` / `Área Privativa` → agora dentro da `Descrição`
- `Tipo` / `CEP` → removidas; tipo inferido da descrição

**Localização do header no CSV:**
```
Linha 0: (vazia)
Linha 1: " Lista de Imóveis da Caixa;;Data de geração:;DD/MM/YYYY..."
Linha 2: " N° do imóvel;UF;Cidade;Bairro;Endereço;Preço;..."  ← HEADER REAL
Linha 3: (vazia)
Linha 4+: dados
```

Para encontrar o header programaticamente:
```python
for i, line in enumerate(lines):
    if line.strip().count(";") >= 4 and "Lista de Im" not in line:
        header_idx = i
        break
```

### 26.4. Normalização de chaves

As chaves do CSV contêm `°` (grau), espaços e BOM. Normalizar antes de acessar:

```python
def normaliza_chaves(row: dict) -> dict:
    return {
        re.sub(r"[°\xb0﻿\s]+", " ", k).strip(): v.strip()
        for k, v in row.items() if k
    }
# "N° do imóvel" → "N do imóvel" → acessar com row.get("N do imovel") etc.
```

Variações observadas da mesma coluna (encoding diferente em cada execução):
- `"N do imovel"`, `"N do imóvel"`, `"N do Im vel"`, `"Numero do imovel"`
- `"Pre o"`, `"Preço"`, `"Preco"`
- `"Valor de avalia o"`, `"Valor de avaliação"`
- `"Link de acesso"`, `"Link de Acesso"`

Sempre usar `.get()` com múltiplas variações via `or`:
```python
id_imovel = (row.get("N do imovel") or row.get("N do imóvel") or
             row.get("Numero do imovel") or "").strip()
```

### 26.5. URL de matrícula determinística

A URL de matrícula de qualquer imóvel da Caixa pode ser construída sem visitar a página de detalhe:

```
https://venda-imoveis.caixa.gov.br/editais/matricula/{UF}/{hdnimovel}.pdf
```

O `hdnimovel` é extraído da `Link de acesso`:
```python
m = re.search(r"hdnimovel=(\d+)", url_detalhe, re.IGNORECASE)
hdnimovel = m.group(1) if m else id_imovel
matricula_url = f"https://venda-imoveis.caixa.gov.br/editais/matricula/{uf}/{hdnimovel}.pdf"
```

O edital ainda requer Playwright na página de detalhe (seção 17.3).

### 26.6. Tipo de imóvel inferido da descrição

O CSV não tem uma coluna de tipo estruturada — inferir da `Descrição`:

```python
_TIPO_KW = {
    "apartamento": ["apartamento", "apto"],
    "casa":        ["casa", "residência", "sobrado"],
    "terreno":     ["terreno", "lote", "gleba"],
    "comercial":   ["sala", "loja", "galpão", "garagem"],
    "rural":       ["fazenda", "sítio", "chácara"],
}

def infer_tipo(descricao: str) -> str:
    desc_l = descricao.lower()
    for tipo, kws in _TIPO_KW.items():
        if any(kw in desc_l for kw in kws):
            return tipo
    return "outro"
```

### 26.7. Filtro por data da 1ª praça (seção 8.1)

O CSV da Caixa **não contém a data da praça** — ela só aparece na página de detalhe. Para aplicar o filtro da seção 8.1 seria necessário visitar cada detalhe (lento). Estratégia recomendada:

1. Inserir todos os imóveis do CSV (sem filtro de data)
2. Rodar `python run.py devoltaparaofuturo` após importar — esse comando desativa lotes com datas passadas

### 26.8. Resultados validados (jun/2026)

| Estado | Imóveis | | Estado | Imóveis |
|---|---|---|---|---|
| RJ | 10.493 | | PB | 872 |
| GO | 4.571 | | MG | 838 |
| SP | 2.932 | | BA | 795 |
| PE | 1.504 | | PI | 762 |
| CE | 721 | | RS | 718 |
| RN | 730 | | PR | 619 |
| SE | 452 | | AM | 228 |
| PA | 218 | | MA | 145 |
| MS | 161 | | SC | 150 |
| MT | 135 | | AL | 111 |
| ES | 70 | | DF | 59 |
| RO | 24 | | TO | 22 |
| AC | 25 | | AP | 4 |
| RR | 4 | | **Total** | **27.363** |

Tempo total: ~2 minutos para os 27 estados (4 s/estado via Playwright headless).

### 26.9. Uso do script standalone

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\scraping"

# Todos os estados
python scraper_caixa.py

# Estado específico
python scraper_caixa.py --estado SP

# Vários estados
python scraper_caixa.py --estado SP RJ MG PR

# Reiniciar do zero (ignora progresso anterior)
python scraper_caixa.py --reset

# Arquivo de saída customizado
python scraper_caixa.py --saida "C:\dados\caixa_jun2026.csv"
```

**Saídas geradas:**
- `caixa_imoveis_YYYYMMDD.csv` — planilha com todos os campos
- `caixa_imoveis_YYYYMMDD.jsonl` — linha por imóvel em JSON
- `caixa_progress.json` — progresso (permite retomar se interrompido)

**Importar no banco após coleta:**
```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leilao-scraper\leilao-scraper"
$env:PYTHONIOENCODING = "utf-8"

# Via run.py (usa scraper integrado)
python run.py scrape --fonte caixa

# Ou via CSV gerado pelo script standalone
python -m pipeline.importar_ofertas_csv --csv "C:\...\caixa_imoveis_YYYYMMDD.csv"
```

### 26.10. Checklist Caixa

1. Playwright e playwright-stealth instalados: `pip install playwright playwright-stealth && playwright install chromium`
2. Warm-up na home antes do CSV — evita bloqueio Radware.
3. `expect_download` com `goto` dentro do `try/except` — o `"Download is starting"` é esperado.
4. `await dl_info.value` **fora** do `async with` — funciona após o bloco.
5. Normalizar chaves do CSV antes de acessar — caracteres especiais variam por encoding.
6. Header real na linha 2 (índice 2) — não na 0 nem na 1.
7. `Preço` = valor mínimo de venda; `Valor de avaliação` = avaliação.
8. Tipo de imóvel inferido da descrição — não há coluna estruturada.
9. URL de matrícula construída deterministicamente com `hdnimovel`.
10. Rodar `devoltaparaofuturo` após importar para desativar lotes com datas passadas.

---

## 27. Arquitetura de referência para scraper genérico de leiloeiros

Documentação técnica para coleta estruturada de dados de sites de leiloeiros: lotes, fotos, datas, valores e anexos (editais, matrículas, laudos).

### 27.1. Princípios

- **Tente o caminho mais barato primeiro.** HTTP estático → API JSON interna → Playwright. Só escale a complexidade quando o site exigir.
- **Robustez, não força.** Retries com backoff, detecção de mudança de layout e logs cobrem mais casos de forma sustentável do que tentar burlar proteções.
- **Respeite o site.** Cheque `robots.txt` e os Termos de Uso, aplique rate limiting e não contorne CAPTCHA / proteção anti-bot ativa. Muitos dados de leilão são públicos por obrigação legal, então raramente é preciso forçar.
- **Login só com credenciais próprias.** Use sessões que você mesmo cadastrou; persista cookies/storage state para não relogar a cada execução.

### 27.2. Escolha de ferramenta por tipo de site

| Tipo de site | Ferramenta | Observação |
|---|---|---|
| HTML estático (servidor renderiza) | `httpx` + `selectolax`/`BeautifulSoup` | Rápido e escalável. Teste primeiro. |
| SPA / JavaScript pesado | Playwright | Renderiza tudo, mais lento. |
| API JSON interna | `httpx` direto no endpoint | Mais eficiente. Descubra via DevTools → Network. |

> **Dica:** muitos sites de leilão carregam lotes via API JSON interna. Encontrar esse endpoint (DevTools → aba Network) permite pular HTML e Playwright e obter dados já estruturados.

### 27.3. Arquitetura em camadas

1. **Descoberta** — dado um link inicial, identifique a plataforma. Muitos leiloeiros usam as mesmas plataformas (Superbid, Sodré Santoro, Mega Leilões, sistemas white-label). Detectar a plataforma permite reaproveitar o mesmo extrator para vários sites.
2. **Login** (quando necessário) — Playwright com sessão persistente.
3. **Listagem** — pagine pelos lotes coletando URLs.
4. **Extração por lote** — título, descrição, datas (1ª/2ª praça), valores, status.
5. **Mídia e anexos** — baixe imagens e PDFs seguindo os links.

**Camada de adaptadores:** um extrator por plataforma, selecionado por detecção de domínio/HTML. Resolve vários sites com pouco código.

- **Fallback inteligente:** tenta HTTP → tenta JSON interno → cai pro Playwright só se necessário.
- **Schema unificado:** normalize tudo (datas, valores, status) num formato único, independente da origem.

### 27.4. Esqueleto de código (Playwright + httpx)

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

### 27.5. Boas práticas operacionais

- **Rate limiting** entre requisições (`time.sleep`) para não sobrecarregar o servidor.
- **Retries com backoff exponencial** em falhas de rede.
- **Persistência de sessão** para evitar logins repetidos (ver seção 4.3).
- **Logs e detecção de mudança de layout** para manutenção contínua.
- **Schema de saída unificado** (JSON/banco) para consumo posterior — ver modelo `ImovelRaw` em `scrapers/base.py`.
- **Identificar a plataforma antes de codar** — muitos sites compartilham o mesmo sistema (leilaoprocore, VIP Leilões, suporteleiloes.com.br, e-leiloes). Um adaptador serve vários leiloeiros.

### 27.6. Plataformas comuns identificadas (leiloeiros credenciados Caixa)

| Plataforma | URL padrão de listagem | URL do lote | Exemplos |
|---|---|---|---|
| **leilaoprocore** | `/leiloes` → `/leilao/{slug}/lotes/lista` | `/leilao/{slug}/lote_id/{id}` | leffaleiloes.com.br, soleiloes.com.br |
| **VIP Leilões** | `/filtro/imoveis` | `/leilao/{code}/lote/{id}` | lancecertoleiloes.com.br |
| **e-leiloes / Stefanelli** | `/eventos` | `/eventos/leilao/{id}/{slug}/lote` | e-leiloes.com.br, stefanellileiloes.com.br |
| **suporteleiloes.com.br** | `/oferta/leilao/imoveis/...` | `/oferta/lote/{id}` | edgarcarvalholeiloeiro.com.br |
| **ASP.NET custom** | `/ResultadoPesquisaCategoria.aspx?Categoria=Imóveis` | `/DetalheOferta.aspx?...` | leiloeiropublico.com.br |
| **leil.br** | Varia por leiloeiro | Varia | moacira.lel.br, hammer.lel.br |

**Detecção automática de plataforma:** checar o HTML inicial por marcadores únicos antes de tentar scraping:

```python
def detectar_plataforma(html: str, url: str) -> str:
    if "leilaoprocore" in html or "/leilao/" in html and "/lotes/lista" in html:
        return "leilaoprocore"
    if "vipleiloes.com.br" in html or "/filtro/imoveis" in html:
        return "vipleiloes"
    if "/eventos/leilao/" in html:
        return "eleiloes"
    if "suporteleiloes" in html or "/oferta/leilao/" in html:
        return "suporteleiloes"
    if "ResultadoPesquisaCategoria" in html:
        return "aspnet_custom"
    if ".lel.br" in url:
        return "leilbr"
    return "generico"
```

### 27.7. Checklist para novo site de leiloeiro

1. **Identificar a plataforma** — acessar homepage e procurar marcadores no HTML.
2. **Procurar API JSON interna** — DevTools → Network → filtrar XHR ao navegar pelos lotes.
3. **Mapear a URL de listagem** — `/imoveis`, `/filtro/imoveis`, `/leiloes`, `/eventos`, etc.
4. **Testar com `httpx` primeiro** — se o HTML já contém os dados, não precisa de Playwright.
5. **Adicionar Playwright só se necessário** — conteúdo JS-pesado ou proteção anti-bot.
6. **Capturar imagens** — URLs de `<img>` nos cards/detalhes; usar CDN URL quando disponível.
7. **Capturar documentos** — `<a href="*.pdf">` com palavras-chave (edital, matrícula, laudo).
8. **Normalizar campos** — datas para `datetime`, valores para `Decimal`, limpar `R$`, `.`, `,`.
9. **Integrar ao pipeline** — usar `salvar_imoveis()` do normalizer para upsert no banco.
10. **Registrar na tabela `fontes`** — nome do leiloeiro + URL base para rastreabilidade.

---

## 29. Estudo de caso: Leilões Judiciais (03/06/2026 09:50)

Coleta realizada em `https://www.leiloesjudiciais.com.br` — portal nacional de leilões judiciais online.

### 29.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros únicos identificados | 3 |
| Total de imóveis coletados | 3 |
| CSV de leiloeiros gerado | `leiloeiros_leiloesjudiciais_2026-06-03.csv` |
| CSV de imóveis gerado | `imoveis_leiloesjudiciais_2026-06-03.csv` |
| Total de erros registrados | 0 |

### 29.2. Distribuição por leiloeiro

- **Leiloeiro(a): Conceição Maria Fixer Para participar do leilão, acesse o site do(a) Leiloeiro(a) clicando abaixo: ir para o leilão**: 1 imóveis
- **Leiloeiro(a): Leonice Fixer Para participar do leilão, acesse o site do(a) Leiloeiro(a) clicando abaixo: ir para o leilão**: 1 imóveis
- **Leiloeiro(a): Paulo Cézar Rocha Teixeira Para participar do leilão, acesse o site do(a) Leiloeiro(a) clicando abaixo: ir para o leilão**: 1 imóveis

### 29.3. Principais dificuldades enfrentadas

#### 29.3.1. Renderização JavaScript (SPA)

**Problema:** O site leiloesjudiciais.com.br é uma SPA (React/Next.js). O HTML inicial
retornado via `requests`/`httpx` está quase vazio — sem cards de lotes. O conteúdo
(listagem de lotes, detalhes do imóvel) só aparece após execução de JavaScript.

**Impacto:** Impossível usar scraping HTTP simples; Playwright é obrigatório.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)`.

**Solução de escala recomendada:**
```python
# Interceptar as chamadas de API internas durante a navegação Playwright
page.on("response", lambda r: capturar_json(r) if "api" in r.url else None)
```

#### 29.3.2. Robots.txt bloqueia paginação via `?pagina=N`

**Problema:** O `robots.txt` do site declara explicitamente:
```
Disallow: /imoveis?pagina=
```
Isso sinaliza que o operador não quer scrapers paginando via esse parâmetro.

**Impacto:** Risco legal/contratual de scraping massivo via paginação direta.

**Solução recomendada:**
1. Contatar o operador (leiloesjudiciais.com.br) para API de parceiro.
2. Usar o sitemap.xml (que é público e completo) como fonte de URLs de lotes.
3. Fatiamento por categoria (`/imoveis/apartamentos`, `/imoveis/casas`, etc.)
   em vez de `?pagina=`.

```python
# Coletar URLs via sitemap em vez de paginação
import xml.etree.ElementTree as ET
import requests

r = requests.get('https://www.leiloesjudiciais.com.br/sitemap.xml')
root = ET.fromstring(r.text)
lote_urls = [loc.text for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
             if '/lote/' in (loc.text or '')]
```

#### 29.3.3. Identificação do leiloeiro no HTML

**Problema:** O nome do leiloeiro não aparece nos cards da listagem — apenas
na página de detalhe do lote. Isso força uma visita extra por lote.

**Impacto:** Volume de requisições ~2× maior (listagem + detalhe).

**Solução recomendada:**
1. Interceptar a resposta JSON da API interna na página de listagem (que provavelmente
   inclui o nome do leiloeiro no payload).
2. Ou cachear o leiloeiro por ID de leilão para evitar re-fetch.

```python
leiloeiros_cache: dict[str, str] = {}

def get_leiloeiro(page, leilao_id: str) -> str:
    if leilao_id in leiloeiros_cache:
        return leiloeiros_cache[leilao_id]
    # ... fetch da página do leilão
    leiloeiros_cache[leilao_id] = nome_leiloeiro
    return nome_leiloeiro
```

#### 29.3.4. Seletores CSS instáveis (classes geradas dinamicamente)

**Problema:** Sites React/Next.js com CSS Modules ou Tailwind geram nomes de
classes dinâmicos (ex.: `sc-bdfxgf`, `css-1a2b3c`). Seletores por classe
quebram a cada deploy.

**Solução aplicada:** Fallback em cascata — texto, regex, data-attributes.

**Solução recomendada:**
1. Priorizar `data-*` attributes (ex.: `data-testid`, `data-lote-id`).
2. Usar XPath por texto ("Lance mínimo") em vez de classe.
3. Interceptar JSON da API interna — imune a mudanças de CSS.

```python
# XPath robusto por label de texto
preco_el = page.locator('//dt[contains(text(), "Lance")]/following-sibling::dd[1]')
```


### 29.4. Erros por tipo

| Tipo | Ocorrências | Causa |
|---|---|---|

### 29.5. Checklist específico Leilões Judiciais

1. **Playwright obrigatório** — SPA sem dados no HTML estático.
2. **Coletar via sitemap** em vez de `?pagina=` — respeita robots.txt.
3. **Interceptar API interna** para obter JSON limpo com leiloeiro já incluído.
4. **Seletores por data-attribute** — mais estáveis que classes CSS dinâmicas.
5. **Cache de leiloeiro por leilão** — evita visitas repetidas à página do leilão.
6. **Filtrar por categoria `/imoveis/`** para coletar só imóveis.
7. **Verificar status "Aberto para Lances"** antes de processar — evita lotes encerrados.
8. **Documentos** (edital, matrícula) estão na página de detalhe como links diretos.

### 29.6. Sugestões de melhoria para o pipeline

1. **Adicionar interceptação de API** no Playwright para capturar JSON da listagem.
2. **Usar sitemap.xml** como fonte primária de URLs de lotes.
3. **Paralelizar** visitas de detalhe com `asyncio` + Playwright assíncrono.
4. **Salvar progresso** em JSON para retomar de onde parou se interrompido.
5. **Adicionar verificação de status** do lote antes do scraping completo.

---

## 29. Estudo de caso: Leilões Judiciais (03/06/2026 09:52)

Coleta realizada em `https://www.leiloesjudiciais.com.br` — portal nacional de leilões judiciais online.

### 29.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros únicos identificados | 4 |
| Total de imóveis coletados | 4 |
| CSV de leiloeiros gerado | `leiloeiros_leiloesjudiciais_2026-06-03.csv` |
| CSV de imóveis gerado | `imoveis_leiloesjudiciais_2026-06-03.csv` |
| Total de erros registrados | 0 |

### 29.2. Distribuição por leiloeiro

- **Conceição Maria Fixer**: 1 imóveis
- **Leonice Fixer**: 1 imóveis
- **Paulo Cézar Rocha Teixeira**: 1 imóveis
- **Álvaro Sérgio Fuzo**: 1 imóveis

### 29.3. Principais dificuldades enfrentadas

#### 29.3.1. Renderização JavaScript (SPA)

**Problema:** O site leiloesjudiciais.com.br é uma SPA (React/Next.js). O HTML inicial
retornado via `requests`/`httpx` está quase vazio — sem cards de lotes. O conteúdo
(listagem de lotes, detalhes do imóvel) só aparece após execução de JavaScript.

**Impacto:** Impossível usar scraping HTTP simples; Playwright é obrigatório.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)`.

**Solução de escala recomendada:**
```python
# Interceptar as chamadas de API internas durante a navegação Playwright
page.on("response", lambda r: capturar_json(r) if "api" in r.url else None)
```

#### 29.3.2. Robots.txt bloqueia paginação via `?pagina=N`

**Problema:** O `robots.txt` do site declara explicitamente:
```
Disallow: /imoveis?pagina=
```
Isso sinaliza que o operador não quer scrapers paginando via esse parâmetro.

**Impacto:** Risco legal/contratual de scraping massivo via paginação direta.

**Solução recomendada:**
1. Contatar o operador (leiloesjudiciais.com.br) para API de parceiro.
2. Usar o sitemap.xml (que é público e completo) como fonte de URLs de lotes.
3. Fatiamento por categoria (`/imoveis/apartamentos`, `/imoveis/casas`, etc.)
   em vez de `?pagina=`.

```python
# Coletar URLs via sitemap em vez de paginação
import xml.etree.ElementTree as ET
import requests

r = requests.get('https://www.leiloesjudiciais.com.br/sitemap.xml')
root = ET.fromstring(r.text)
lote_urls = [loc.text for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
             if '/lote/' in (loc.text or '')]
```

#### 29.3.3. Identificação do leiloeiro no HTML

**Problema:** O nome do leiloeiro não aparece nos cards da listagem — apenas
na página de detalhe do lote. Isso força uma visita extra por lote.

**Impacto:** Volume de requisições ~2× maior (listagem + detalhe).

**Solução recomendada:**
1. Interceptar a resposta JSON da API interna na página de listagem (que provavelmente
   inclui o nome do leiloeiro no payload).
2. Ou cachear o leiloeiro por ID de leilão para evitar re-fetch.

```python
leiloeiros_cache: dict[str, str] = {}

def get_leiloeiro(page, leilao_id: str) -> str:
    if leilao_id in leiloeiros_cache:
        return leiloeiros_cache[leilao_id]
    # ... fetch da página do leilão
    leiloeiros_cache[leilao_id] = nome_leiloeiro
    return nome_leiloeiro
```

#### 29.3.4. Seletores CSS instáveis (classes geradas dinamicamente)

**Problema:** Sites React/Next.js com CSS Modules ou Tailwind geram nomes de
classes dinâmicos (ex.: `sc-bdfxgf`, `css-1a2b3c`). Seletores por classe
quebram a cada deploy.

**Solução aplicada:** Fallback em cascata — texto, regex, data-attributes.

**Solução recomendada:**
1. Priorizar `data-*` attributes (ex.: `data-testid`, `data-lote-id`).
2. Usar XPath por texto ("Lance mínimo") em vez de classe.
3. Interceptar JSON da API interna — imune a mudanças de CSS.

```python
# XPath robusto por label de texto
preco_el = page.locator('//dt[contains(text(), "Lance")]/following-sibling::dd[1]')
```


### 29.4. Erros por tipo

| Tipo | Ocorrências | Causa |
|---|---|---|

### 29.5. Checklist específico Leilões Judiciais

1. **Playwright obrigatório** — SPA sem dados no HTML estático.
2. **Coletar via sitemap** em vez de `?pagina=` — respeita robots.txt.
3. **Interceptar API interna** para obter JSON limpo com leiloeiro já incluído.
4. **Seletores por data-attribute** — mais estáveis que classes CSS dinâmicas.
5. **Cache de leiloeiro por leilão** — evita visitas repetidas à página do leilão.
6. **Filtrar por categoria `/imoveis/`** para coletar só imóveis.
7. **Verificar status "Aberto para Lances"** antes de processar — evita lotes encerrados.
8. **Documentos** (edital, matrícula) estão na página de detalhe como links diretos.

### 29.6. Sugestões de melhoria para o pipeline

1. **Adicionar interceptação de API** no Playwright para capturar JSON da listagem.
2. **Usar sitemap.xml** como fonte primária de URLs de lotes.
3. **Paralelizar** visitas de detalhe com `asyncio` + Playwright assíncrono.
4. **Salvar progresso** em JSON para retomar de onde parou se interrompido.
5. **Adicionar verificação de status** do lote antes do scraping completo.

---

## 29. Estudo de caso: Leilões Judiciais (03/06/2026 09:53)

Coleta realizada em `https://www.leiloesjudiciais.com.br` — portal nacional de leilões judiciais online.

### 29.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros únicos identificados | 3 |
| Total de imóveis coletados | 3 |
| CSV de leiloeiros gerado | `leiloeiros_leiloesjudiciais_2026-06-03.csv` |
| CSV de imóveis gerado | `imoveis_leiloesjudiciais_2026-06-03.csv` |
| Total de erros registrados | 0 |

### 29.2. Distribuição por leiloeiro

- **Conceição Maria Fixer**: 1 imóveis
- **Leonice Fixer**: 1 imóveis
- **Paulo Cézar Rocha Teixeira**: 1 imóveis

### 29.3. Principais dificuldades enfrentadas

#### 29.3.1. Renderização JavaScript (SPA)

**Problema:** O site leiloesjudiciais.com.br é uma SPA (React/Next.js). O HTML inicial
retornado via `requests`/`httpx` está quase vazio — sem cards de lotes. O conteúdo
(listagem de lotes, detalhes do imóvel) só aparece após execução de JavaScript.

**Impacto:** Impossível usar scraping HTTP simples; Playwright é obrigatório.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)`.

**Solução de escala recomendada:**
```python
# Interceptar as chamadas de API internas durante a navegação Playwright
page.on("response", lambda r: capturar_json(r) if "api" in r.url else None)
```

#### 29.3.2. Robots.txt bloqueia paginação via `?pagina=N`

**Problema:** O `robots.txt` do site declara explicitamente:
```
Disallow: /imoveis?pagina=
```
Isso sinaliza que o operador não quer scrapers paginando via esse parâmetro.

**Impacto:** Risco legal/contratual de scraping massivo via paginação direta.

**Solução recomendada:**
1. Contatar o operador (leiloesjudiciais.com.br) para API de parceiro.
2. Usar o sitemap.xml (que é público e completo) como fonte de URLs de lotes.
3. Fatiamento por categoria (`/imoveis/apartamentos`, `/imoveis/casas`, etc.)
   em vez de `?pagina=`.

```python
# Coletar URLs via sitemap em vez de paginação
import xml.etree.ElementTree as ET
import requests

r = requests.get('https://www.leiloesjudiciais.com.br/sitemap.xml')
root = ET.fromstring(r.text)
lote_urls = [loc.text for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
             if '/lote/' in (loc.text or '')]
```

#### 29.3.3. Identificação do leiloeiro no HTML

**Problema:** O nome do leiloeiro não aparece nos cards da listagem — apenas
na página de detalhe do lote. Isso força uma visita extra por lote.

**Impacto:** Volume de requisições ~2× maior (listagem + detalhe).

**Solução recomendada:**
1. Interceptar a resposta JSON da API interna na página de listagem (que provavelmente
   inclui o nome do leiloeiro no payload).
2. Ou cachear o leiloeiro por ID de leilão para evitar re-fetch.

```python
leiloeiros_cache: dict[str, str] = {}

def get_leiloeiro(page, leilao_id: str) -> str:
    if leilao_id in leiloeiros_cache:
        return leiloeiros_cache[leilao_id]
    # ... fetch da página do leilão
    leiloeiros_cache[leilao_id] = nome_leiloeiro
    return nome_leiloeiro
```

#### 29.3.4. Seletores CSS instáveis (classes geradas dinamicamente)

**Problema:** Sites React/Next.js com CSS Modules ou Tailwind geram nomes de
classes dinâmicos (ex.: `sc-bdfxgf`, `css-1a2b3c`). Seletores por classe
quebram a cada deploy.

**Solução aplicada:** Fallback em cascata — texto, regex, data-attributes.

**Solução recomendada:**
1. Priorizar `data-*` attributes (ex.: `data-testid`, `data-lote-id`).
2. Usar XPath por texto ("Lance mínimo") em vez de classe.
3. Interceptar JSON da API interna — imune a mudanças de CSS.

```python
# XPath robusto por label de texto
preco_el = page.locator('//dt[contains(text(), "Lance")]/following-sibling::dd[1]')
```


### 29.4. Erros por tipo

| Tipo | Ocorrências | Causa |
|---|---|---|

### 29.5. Checklist específico Leilões Judiciais

1. **Playwright obrigatório** — SPA sem dados no HTML estático.
2. **Coletar via sitemap** em vez de `?pagina=` — respeita robots.txt.
3. **Interceptar API interna** para obter JSON limpo com leiloeiro já incluído.
4. **Seletores por data-attribute** — mais estáveis que classes CSS dinâmicas.
5. **Cache de leiloeiro por leilão** — evita visitas repetidas à página do leilão.
6. **Filtrar por categoria `/imoveis/`** para coletar só imóveis.
7. **Verificar status "Aberto para Lances"** antes de processar — evita lotes encerrados.
8. **Documentos** (edital, matrícula) estão na página de detalhe como links diretos.

### 29.6. Sugestões de melhoria para o pipeline

1. **Adicionar interceptação de API** no Playwright para capturar JSON da listagem.
2. **Usar sitemap.xml** como fonte primária de URLs de lotes.
3. **Paralelizar** visitas de detalhe com `asyncio` + Playwright assíncrono.
4. **Salvar progresso** em JSON para retomar de onde parou se interrompido.
5. **Adicionar verificação de status** do lote antes do scraping completo.

---

## 29. Estudo de caso: Leilões Judiciais (03/06/2026 12:55)

Coleta realizada em `https://www.leiloesjudiciais.com.br` — portal nacional de leilões judiciais online.

### 29.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros únicos identificados | 38 |
| Total de imóveis coletados | 990 |
| CSV de leiloeiros gerado | `leiloeiros_leiloesjudiciais_2026-06-03.csv` |
| CSV de imóveis gerado | `imoveis_leiloesjudiciais_2026-06-03.csv` |
| Total de erros registrados | 0 |

### 29.2. Distribuição por leiloeiro

- **Joyce Ribeiro**: 141 imóveis
- **Álvaro Sérgio Fuzo**: 106 imóveis
- **Giordano Bruno Coan Amador**: 77 imóveis
- **Carlo Ferrari**: 73 imóveis
- **Thaís Costa Bastos Teixeira**: 58 imóveis
- **Deonizia Kiratch**: 54 imóveis
- **Rodrigo Aparecido Rigolon da Silva**: 46 imóveis
- **Hidirlene Duszeiko**: 40 imóveis
- **Renato Guedes Rocha**: 39 imóveis
- **Francisco Freitas**: 39 imóveis
- **Paulo Cézar Rocha Teixeira**: 35 imóveis
- **Fábio Manoel Guimarães**: 26 imóveis
- **José Antônio Rodovalho Júnior**: 23 imóveis
- **Alessandro de Assis Teixeira**: 22 imóveis
- **Conceição Maria Fixer**: 19 imóveis
- **Rosimeire Maia**: 19 imóveis
- **José David Gonçalves de Melo**: 18 imóveis
- **Helton Verri**: 14 imóveis
- **Rafael Galvani Ferreira**: 14 imóveis
- **Daniel Oliveira Júnior**: 13 imóveis

### 29.3. Principais dificuldades enfrentadas

#### 29.3.1. Renderização JavaScript (SPA)

**Problema:** O site leiloesjudiciais.com.br é uma SPA (React/Next.js). O HTML inicial
retornado via `requests`/`httpx` está quase vazio — sem cards de lotes. O conteúdo
(listagem de lotes, detalhes do imóvel) só aparece após execução de JavaScript.

**Impacto:** Impossível usar scraping HTTP simples; Playwright é obrigatório.

**Solução aplicada:** Playwright com `wait_until='networkidle'` + `wait_for_timeout(2000)`.

**Solução de escala recomendada:**
```python
# Interceptar as chamadas de API internas durante a navegação Playwright
page.on("response", lambda r: capturar_json(r) if "api" in r.url else None)
```

#### 29.3.2. Robots.txt bloqueia paginação via `?pagina=N`

**Problema:** O `robots.txt` do site declara explicitamente:
```
Disallow: /imoveis?pagina=
```
Isso sinaliza que o operador não quer scrapers paginando via esse parâmetro.

**Impacto:** Risco legal/contratual de scraping massivo via paginação direta.

**Solução recomendada:**
1. Contatar o operador (leiloesjudiciais.com.br) para API de parceiro.
2. Usar o sitemap.xml (que é público e completo) como fonte de URLs de lotes.
3. Fatiamento por categoria (`/imoveis/apartamentos`, `/imoveis/casas`, etc.)
   em vez de `?pagina=`.

```python
# Coletar URLs via sitemap em vez de paginação
import xml.etree.ElementTree as ET
import requests

r = requests.get('https://www.leiloesjudiciais.com.br/sitemap.xml')
root = ET.fromstring(r.text)
lote_urls = [loc.text for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
             if '/lote/' in (loc.text or '')]
```

#### 29.3.3. Identificação do leiloeiro no HTML

**Problema:** O nome do leiloeiro não aparece nos cards da listagem — apenas
na página de detalhe do lote. Isso força uma visita extra por lote.

**Impacto:** Volume de requisições ~2× maior (listagem + detalhe).

**Solução recomendada:**
1. Interceptar a resposta JSON da API interna na página de listagem (que provavelmente
   inclui o nome do leiloeiro no payload).
2. Ou cachear o leiloeiro por ID de leilão para evitar re-fetch.

```python
leiloeiros_cache: dict[str, str] = {}

def get_leiloeiro(page, leilao_id: str) -> str:
    if leilao_id in leiloeiros_cache:
        return leiloeiros_cache[leilao_id]
    # ... fetch da página do leilão
    leiloeiros_cache[leilao_id] = nome_leiloeiro
    return nome_leiloeiro
```

#### 29.3.4. Seletores CSS instáveis (classes geradas dinamicamente)

**Problema:** Sites React/Next.js com CSS Modules ou Tailwind geram nomes de
classes dinâmicos (ex.: `sc-bdfxgf`, `css-1a2b3c`). Seletores por classe
quebram a cada deploy.

**Solução aplicada:** Fallback em cascata — texto, regex, data-attributes.

**Solução recomendada:**
1. Priorizar `data-*` attributes (ex.: `data-testid`, `data-lote-id`).
2. Usar XPath por texto ("Lance mínimo") em vez de classe.
3. Interceptar JSON da API interna — imune a mudanças de CSS.

```python
# XPath robusto por label de texto
preco_el = page.locator('//dt[contains(text(), "Lance")]/following-sibling::dd[1]')
```


### 29.4. Erros por tipo

| Tipo | Ocorrências | Causa |
|---|---|---|

### 29.5. Checklist específico Leilões Judiciais

1. **Playwright obrigatório** — SPA sem dados no HTML estático.
2. **Coletar via sitemap** em vez de `?pagina=` — respeita robots.txt.
3. **Interceptar API interna** para obter JSON limpo com leiloeiro já incluído.
4. **Seletores por data-attribute** — mais estáveis que classes CSS dinâmicas.
5. **Cache de leiloeiro por leilão** — evita visitas repetidas à página do leilão.
6. **Filtrar por categoria `/imoveis/`** para coletar só imóveis.
7. **Verificar status "Aberto para Lances"** antes de processar — evita lotes encerrados.
8. **Documentos** (edital, matrícula) estão na página de detalhe como links diretos.

### 29.6. Sugestões de melhoria para o pipeline

1. **Adicionar interceptação de API** no Playwright para capturar JSON da listagem.
2. **Usar sitemap.xml** como fonte primária de URLs de lotes.
3. **Paralelizar** visitas de detalhe com `asyncio` + Playwright assíncrono.
4. **Salvar progresso** em JSON para retomar de onde parou se interrompido.
5. **Adicionar verificação de status** do lote antes do scraping completo.
