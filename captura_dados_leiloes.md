# Guia Completo de Captura de Dados de Sites de Leilão

> Documento de referência para extração exaustiva de informações de qualquer tipo de site de leilão — incluindo sites públicos, autenticados (login/senha), com JavaScript pesado, APIs ocultas e proteções anti-bot.

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
- **Cloudflare / WAF:** ferramentas como `curl_cffi` (imita TLS de navegador) ou navegador real automatizado costumam passar onde `requests` falha.
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
5. Se houver **lances ao vivo**, conecte ao WebSocket.
6. **Valide e tipe** cada campo com `pydantic`.
7. **Armazene** com deduplicação e histórico.
8. **Respeite** rate limits, robots.txt e os termos do site.

---

## 12. Considerações legais e éticas

- **Leia os Termos de Serviço** — muitos sites proíbem scraping, especialmente de áreas autenticadas; violá-los pode gerar consequências contratuais e legais.
- **Dados pessoais (LGPD):** o tratamento de dados de pessoas físicas exige base legal; tenha cautela ao capturar nomes, CPFs ou documentos.
- **robots.txt:** respeite as diretrizes de crawling do site.
- **Carga no servidor:** limite a frequência de requisições para não prejudicar a operação do site.
- **Contornar autenticação/CAPTCHA** de sistemas que você não está autorizado a acessar pode configurar violação de termos ou de lei — só faça em contas e sistemas que você tem direito de usar.
- Em caso de dúvida sobre licitude, **consulte um advogado** — este documento é técnico e não constitui aconselhamento jurídico.
