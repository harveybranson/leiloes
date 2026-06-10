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
>
> **Adições jun/2026 (seção 32):** correção e deduplicação de nomes de cidades no PostgreSQL Docker — diagnóstico de mojibake, endpoint `/imoveis/cidades` sem `unaccent`/`municipios_ibge`, enums maiúsculos, e script `corrigir_cidades.py` com modos `--todos`, `--cidade`, `--deduplicar` e `--listar`.
>
> **Adições jun/2026 (seção 34):** scraping JUCEMS — parse robusto de arquivo `.txt` com encoding corrompido (U+FFFD), leiloeiros Regular do MS, 593 imóveis de 49 sites, diagnóstico de armadilhas: enums PostgreSQL maiúsculos, `WinError 206` (SQL muito longo no Windows), sites offline/DNS inválido, leiloeiro com endereço em UF diferente do MS, `sys.stdout.reconfigure` quebrando redirecionamento de log.
>
> **Adições jun/2026 (seção 36):** correção crítica de importação PostgreSQL — rollback em cascata com psycopg2 (cada falha apagava até 99 linhas pendentes), NUL bytes (0x00) em campos de texto rejeitados pelo driver, overflow numérico, e duplicatas de `id_externo` no CSV. Solução com `SAVEPOINT` por linha. Checagem obrigatória pós-scraping (`verificar_importacao.py`) que compara CSV vs banco e reimporta os faltantes automaticamente.

---

## 0. REGRA OBRIGATÓRIA: todo imóvel capturado vai para o banco ao fim do scraping

> ⚠️ **Obrigatório e não-negociável.** Ao concluir **qualquer** rotina de scraping, **todos** os
> imóveis capturados (válidos — i.e., 1ª praça posterior à data da captura) **devem ser inseridos no
> banco de dados** antes de a tarefa ser considerada concluída. Exportar CSV não basta: o CSV é
> artefato intermediário; a fonte de verdade é o banco.

**O que isso significa na prática:**

1. **A inserção no banco é a última etapa obrigatória de toda execução**, depois de coletar e antes de
   gerar o relatório final. Nenhum scraper pode terminar com imóveis válidos só em memória/CSV.
2. **Inserir 100% dos imóveis válidos**, aplicando **dedup por URL canônica** (não pular registros por
   outro motivo). Dedup ≠ descarte: o que já existe no banco é ignorado; o que é novo entra sempre.
3. **Sincronizar o destino correto:**
   - SQLite local `imoveis_leiloeiros.db` (tabela `imoveis`) para os scrapers standalone deste diretório;
   - PostgreSQL Docker via pipeline (`run.py importar` / `importar_*`) quando o alvo for o sistema em produção.
4. **Verificação pós-inserção obrigatória:** comparar a contagem de imóveis válidos coletados com os
   efetivamente gravados (ver `verificar_importacao.py`, seção 36). Se houver divergência, **reimportar
   os faltantes automaticamente** — a execução só termina quando `coletados_válidos == gravados (novos + já existentes)`.
5. **Registrar no relatório final** quantos imóveis foram coletados, quantos eram novos, quantos já
   existiam (dedup) e a contagem final no banco por junta/leiloeiro.
6. **Em caso de falha de inserção** (enum, NUL byte, overflow, SQL longo no Windows — seções 20/34/36),
   tratar linha a linha com `SAVEPOINT`/try-except e **nunca** abortar o lote inteiro: salvar o que for
   válido e reportar as linhas rejeitadas, em vez de deixar imóveis de fora do banco silenciosamente.

**Definição de "concluído":** uma rodada de scraping só está concluída quando os imóveis válidos estão
**no banco** (não apenas no CSV) e a verificação CSV↔banco fecha sem faltantes.

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


---

## 30. Algoritmo de adição automática de imóveis ao sistema

Descreve o pipeline completo implementado para `https://www.leiloesjudiciais.com.br` —
desde a coleta via Playwright até a inserção no banco e ativação no front-end.
Os dois scripts principais são `scraper_leiloesjudiciais.py` e `importar_leiloesjudiciais.py`.

---

### 30.1. Visão geral do fluxo

```
┌─────────────────────────────────────────────────────────────────┐
│  FASE 1 — Coleta de URLs                                        │
│  Playwright pagina /imoveis?pagina=N → extrai /lote/{id}/{lot} │
└───────────────────────────┬─────────────────────────────────────┘
                            │ lista de URLs (ex.: 1.001)
┌───────────────────────────▼─────────────────────────────────────┐
│  FASE 2 — Extração de detalhes por lote                         │
│  Playwright visita cada URL → parser multi-estratégia           │
│  extrai: leiloeiro, tipo, endereço, preços, datas, fotos, docs  │
└───────────────────────────┬─────────────────────────────────────┘
                            │ lista de dicts (imoveis[])
┌───────────────────────────▼─────────────────────────────────────┐
│  FASE 3 — Persistência em CSV                                   │
│  csv/imoveis_leiloesjudiciais_YYYY-MM-DD.csv  (990 registros)  │
│  csv/leiloeiros_leiloesjudiciais_YYYY-MM-DD.csv (38 únicos)    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  FASE 4 — Importação (importar_leiloesjudiciais.py)             │
│  4a. SQLite  → imoveis_leiloeiros.db (viewer local)             │
│  4b. PostgreSQL → leilao_db via SQLAlchemy (sistema Docker)     │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  FASE 5 — Pós-processamento (docker exec leilao_api)            │
│  classificar → deduplicar → restart API                         │
└─────────────────────────────────────────────────────────────────┘
```

---

### 30.2. FASE 1 — Coleta de URLs de lotes

**Script:** `scraper_leiloesjudiciais.py` → `coletar_urls_listagem()`

#### Algoritmo

```
Para pg = 1 até max_paginas (padrão: 24):
    url_pg = /imoveis          (pg == 1)
           | /imoveis?pagina=N (pg > 1)

    Playwright.goto(url_pg, wait_until='networkidle')
    wait_for_timeout(2000)           ← aguarda hidratação JS

    Na primeira página:
        detecta total de páginas via regex "Página X de N"

    Para cada <a href> no HTML:
        se href bate /lote/\d+/\d+:
            adiciona à lista (sem duplicatas, sem ?query)

    sleep(2s)
```

#### Saída
Lista de URLs únicas no formato `https://www.leiloesjudiciais.com.br/lote/{leilao_id}/{lot_id}`.

#### Por que Playwright e não requests?
O site é uma SPA (React). O HTML retornado via `requests` contém apenas o shell da
página — os cards de lotes só aparecem após execução do JavaScript.

---

### 30.3. FASE 2 — Extração de dados por lote

**Script:** `scraper_leiloesjudiciais.py` → `extrair_lote()`

#### Algoritmo por campo

```
Para cada url em lot_urls:
    Playwright.goto(url, wait_until='networkidle')
    wait_for_timeout(1500)
    html = page.content()
    soup = BeautifulSoup(html)
    texto = soup.get_text()

    ── LEILOEIRO ──────────────────────────────────────────────────
    1. Regex no texto completo:
       r'Leiloeir[oa]\(?[as]?\)?[:\s]+([A-ZÀ-Ú][^\.]{4,80}?)\s+Para\s'
    2. Seletores CSS específicos: .leiloeiro-nome, [data-leiloeiro]
    3. Seletores genéricos: [class*="leiloeiro"] + limpeza de boilerplate
    4. Fallback: h2/h3 que contenha "leilões" com 5–100 chars

    ── SITE DO LEILOEIRO ──────────────────────────────────────────
    Procura <a> com texto "ir para" / "acesse o site" cujo href:
      - começa com http/https
      - não é leiloesjudiciais.com.br
      - não é PDF
    Fallback: https://www.leiloesjudiciais.com.br

    ── TÍTULO ─────────────────────────────────────────────────────
    1. <h1> da página
    2. <meta property="og:title">
    Filtro: descarta lotes que não são imóveis (veículo, moto, etc.)

    ── TIPO DE IMÓVEL ─────────────────────────────────────────────
    Cascata de keywords (ordem de prioridade):
      fazenda/sítio/hectare → rural
      apart/flat/studio     → apartamento
      casa/sobrado          → casa
      terreno/gleba         → terreno
      galpão/armazém        → galpao
      loja/comercial        → comercial
      sala/conjunto         → sala
      (fallback)            → outro

    ── PREÇOS ─────────────────────────────────────────────────────
    Regex: r'R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)'
    Primeira ocorrência  → valor_minimo
    Segunda ocorrência   → valor_avaliacao
    Seletores: [class*="avaliacao"], [class*="lance-minimo"]

    ── DATAS ──────────────────────────────────────────────────────
    Para cada campo (data_primeiro_leilao, data_segundo_leilao, data_encerramento):
        Procura keyword (1º Encerramento, 2º Encerramento, Prazo...)
        seguida de data no formato dd/mm/yyyy
    Fallback: primeira data válida encontrada no texto (2020–2035)

    ── LOCALIZAÇÃO ────────────────────────────────────────────────
    Regex: padrão "Cidade/UF" no título + endereço
    RE_UF captura sigla do estado
    RE_CEP captura CEP

    ── IMAGENS ────────────────────────────────────────────────────
    Todos os <img src / data-src> que não são logo/ícone
    Limite: 10 imagens por lote
    Principal: primeira da lista

    ── DOCUMENTOS ─────────────────────────────────────────────────
    Para cada <a href>:
        aceita se href termina em .pdf
                ou texto/href contém edital|matrícula|laudo|certidão
        classifica: edital | matricula | laudo | pdf | documento
    Busca extra: onclick="ExibeDoc('/path.pdf')" (padrão tribunais)
    Limite: 15 documentos por lote

    ── NÚMERO DO PROCESSO ─────────────────────────────────────────
    Regex: r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}'

    Salva progresso em JSON após cada lote (para monitoramento)
    sleep(2s)
```

#### Monitoramento em tempo real

A cada lote processado, grava `scraper_leiloesjudiciais_progress.json`:

```json
{
  "atualizado": "2026-06-03T11:24:05",
  "lotes_visitados": 149,
  "total_lotes": 1001,
  "pct": 14.9,
  "total_imoveis": 148,
  "por_leiloeiro": {
    "Álvaro Sérgio Fuzo": 26,
    "Joyce Ribeiro": 17
  },
  "erros": 0
}
```

A cada 5 minutos a thread de relatório imprime a tabela de imóveis por leiloeiro no terminal.

---

### 30.4. FASE 3 — Exportação para CSV

**Script:** `scraper_leiloesjudiciais.py` → `salvar_csv_leiloeiros()` + `salvar_csv_imoveis()`

Dois arquivos gerados em `csv/`:

| Arquivo | Conteúdo |
|---|---|
| `leiloeiros_leiloesjudiciais_YYYY-MM-DD.csv` | nome, site — 1 linha por leiloeiro único |
| `imoveis_leiloesjudiciais_YYYY-MM-DD.csv` | 23 campos por imóvel |

**Campos do CSV de imóveis:**

| Campo | Origem |
|---|---|
| `id_externo` | MD5(URL do lote)[:24] — chave de deduplicação |
| `leiloeiro` | Extraído da página de detalhe |
| `leiloeiro_site` | Link externo ou URL da plataforma |
| `titulo` | `<h1>` da página |
| `tipo_imovel` | Classificação por keywords em cascata |
| `tipo_leilao` | judicial / extrajudicial |
| `estado` | Sigla UF extraída do título/endereço |
| `cidade` | Padrão "Cidade/UF" no texto |
| `cep` | Regex `\d{5}-?\d{3}` |
| `endereco_completo` | Seletor CSS de endereço |
| `valor_minimo` | 1ª ocorrência de R$ no texto |
| `valor_avaliacao` | 2ª ocorrência de R$ no texto |
| `area_total` | Regex `\d+ m²` |
| `quartos` | Regex `\d+ quarto` |
| `data_primeiro_leilao` | Próxima data após "1º Encerramento" |
| `data_encerramento` | Próxima data após "Encerramento" |
| `url_original` | URL do lote |
| `imagem_principal` | Primeira imagem não-logo |
| `numero_processo` | Regex padrão CNJ |

---

### 30.5. FASE 4 — Importação para o banco

**Script:** `importar_leiloesjudiciais.py`

#### 4a. SQLite (viewer local — `imoveis_leiloeiros.db`)

Mapeamento de colunas CSV → SQLite:

```
id_externo        → id              (PRIMARY KEY — INSERT OR IGNORE)
leiloeiro         → leiloeiro
leiloeiro_site    → site
titulo            → titulo
tipo_imovel       → tipo            (uppercase)
estado            → uf
cidade            → cidade
valor_minimo      → lance_inicial
valor_avaliacao   → avaliacao
data_primeiro_leilao → data_leilao
url_original      → url
imagem_principal  → imagem
```

#### 4b. PostgreSQL (sistema Docker — `leilao_db`)

```
Passo 1: upsert em fontes
  Fonte(nome='Leilões Judiciais', url_base='https://www.leiloesjudiciais.com.br')

Passo 2: upsert em leiloeiros (1 por nome único)
  Leiloeiro(nome, site, situacao='regular', junta_comercial='Leilões Judiciais')

Passo 3: upsert em lotes de 100 em imoveis
  Se id_externo já existe na fonte → UPDATE campos não-nulos
  Se não existe → INSERT novo registro

Tipos SQLAlchemy:
  TipoImovel:   rural | apartamento | casa | terreno | comercial | galpao | sala | outro
  TipoLeilao:   JUDICIAL | EXTRAJUDICIAL
  StatusLeilao: ABERTO
  CategoriaItem: IMOVEL
```

---

### 30.6. FASE 5 — Pós-processamento

```powershell
# 1. Classificar: calcula score_oportunidade e confirma tipo_imovel
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 2000"

# 2. Deduplicar: desativa duplicatas por URL e por título+local
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"

# 3. Reiniciar API: limpa cache em memória
docker restart leilao_api
```

---

### 30.7. Comandos completos — do zero ao sistema

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes"

# 1. Scraping completo (~60–90 min)
python scraper_leiloesjudiciais.py --sem-banco

# Monitorar em outro terminal:
while ($true) {
    $d = Get-Content scraper_leiloesjudiciais_progress.json | ConvertFrom-Json
    Write-Host "$($d.pct)% | $($d.lotes_visitados)/$($d.total_lotes) | $($d.total_imoveis) imoveis"
    Start-Sleep 30
}

# 2. Importar para os bancos
python importar_leiloesjudiciais.py

# 3. Pós-processamento
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 2000"
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"
docker restart leilao_api
```

---

### 30.8. Resultados observados (coleta de 03/06/2026)

| Métrica | Valor |
|---|---|
| Páginas de listagem percorridas | 24 |
| URLs de lotes coletadas | 1.001 |
| Imóveis extraídos | 990 |
| Lotes descartados (não-imóvel) | 11 |
| Erros de rede/timeout | 0 |
| Leiloeiros únicos identificados | 38 |
| Tempo total de coleta | ~115 min |
| Ritmo médio | ~35 lotes/5min (~8,6s/lote) |
| Inseridos no PostgreSQL | 990 |
| Inseridos no SQLite | 990 |
| Ativos após deduplicação | 1.024 |

**Top 8 leiloeiros por volume:**

| Leiloeiro | Imóveis |
|---|---|
| Joyce Ribeiro | 141 |
| Álvaro Sérgio Fuzo | 106 |
| Giordano Bruno Coan Amador | 77 |
| Carlo Ferrari | 73 |
| Thaís Costa Bastos Teixeira | 58 |
| Deonizia Kiratch | 54 |
| Rodrigo Aparecido Rigolon da Silva | 46 |
| Hidirlene Duszeiko | 40 |

---

### 30.9. Arquitetura de arquivos

```
leiloes/
├── scraper_leiloesjudiciais.py              ← scraper principal (Playwright)
├── importar_leiloesjudiciais.py             ← importador CSV → SQLite + PostgreSQL
├── scraper_leiloesjudiciais_progress.json   ← progresso em tempo real
├── imoveis_leiloeiros.db                    ← SQLite (viewer local)
└── csv/
    ├── leiloeiros_leiloesjudiciais_YYYY-MM-DD.csv
    └── imoveis_leiloesjudiciais_YYYY-MM-DD.csv
```

---

### 30.10. Checklist de execução

- [ ] Playwright instalado: `pip install playwright && playwright install chromium`
- [ ] BeautifulSoup instalado: `pip install beautifulsoup4`
- [ ] Containers Docker rodando: `docker ps` mostra `leilao_api` e `leilao_postgres`
- [ ] Rodar `scraper_leiloesjudiciais.py --sem-banco` e aguardar conclusão
- [ ] Confirmar que `csv/imoveis_leiloesjudiciais_*.csv` foi gerado com >900 linhas
- [ ] Rodar `importar_leiloesjudiciais.py` — confirmar "990 inseridos, 0 erros"
- [ ] Rodar `classificar` e `deduplicar` via `docker exec`
- [ ] Fazer `docker restart leilao_api`
- [ ] Verificar no sistema em `http://localhost:8000`

---

## 31. Scraping JUCERJA — Leiloeiros Regulares (jun/2026)

Relatório da sessão de scraping dos **107 sites** de leiloeiros com situação **REGULAR** extraídos dos 3 documentos oficiais da JUCERJA/CGJ-RJ:
1. `737323064-LeiloeirosJUCERJA.pdf` — 25 pág., com campo "SITUAÇÃO FUNCIONAL: REGULAR"
2. `LeiloeirosJUCERJA.pdf` — 28 pág., versão mais recente (até mat. 366, jun/2026)
3. `atualizacao-15-4-abril-2024-relacao-de-leiloeiros-oficiais-RJpdf.pdf` — CGJ-RJ, inclui site e validade de credenciamento

### 31.1. Resultados obtidos (parcial — scraping em andamento)

| Leiloeiro | Site | Imóveis |
|---|---|---|
| ALEXWILLIAN HOPPE | hoppeleiloes.com.br | 13 |
| ALEXANDRO DA SILVA LACERDA | alexandroleiloeiro.com.br | 8 |
| ALINE FREITAS BASTOS MARQUES | alinemarquesleiloeira.lel.br | 27 |
| ANDREA ROSA COSTA | andrealeiloeira.lel.br | 1 |
| CAMILA NOGUEIRA LIMA | camilaleiloes.com.br | 8 |
| CRISTIANE BORGUETTI MORAES (Lanceja) | lanceja.com.br | 33 |
| CRISTINA FAÇANHA | facanhaleiloes.com.br | 21 |
| DANIELE DE LIMA DE PAULA | depaulaonline.com.br | 27 |
| DANIEL ELIAS GARCIA | dgleiloes.com.br | 2 |
| Demais sites (18+ visitados) | — | 0 (sem leilões ativos) |
| **TOTAL INSERIDOS** | — | **≥140** |

- Tempo de scraping: ~8 min para os primeiros 18 sites
- CSV gerado: `csv/leiloeiros_jucerja_regulares_2024.csv` (107 leiloeiros)
- CSV de leiloeiros: `csv/leiloeiros_jucerja_com_sites.csv`
- Log completo: `scraper_jucerja_run.log`
- Script: `scraper_jucerja_leiloeiros.py` + `run.py scrape-csv` (leilao-scraper)

### 31.2. Principais dificuldades encontradas

#### 31.2.1. Sites sem URL nos PDFs originais — extração de site por e-mail

**Problema:** Os PDFs da JUCERJA listam apenas **e-mail** dos leiloeiros, não o endereço do site. Apenas o documento da CGJ-RJ (PDF 3) contém a coluna "Site". Dos 333+ leiloeiros listados, apenas ~70 tinham site no CGJ.

**Causa:** A JUCERJA não exige publicação de site nos dados cadastrais. Leiloeiros mais antigos (matrículas ≤ 100) frequentemente não possuem site próprio — operam por telefone/e-mail ou via plataformas terceiras (leiloesjudiciais.com.br, leilaoimovel.com.br, etc.).

**Solução implementada:** Cruzamento manual dos e-mails com domínios de site derivados (ex.: `contato@britesleiloeiro.com.br` → `www.britesleiloeiro.com.br`). Para leiloeiros sem site detectável, o campo foi deixado vazio e eles foram excluídos do scraping.

**Solução recomendada:** 
1. Usar o Google Custom Search API (`site:leiloeiro.com.br`) para descobrir sites automaticamente.
2. Consultar a lista da FENAJU nacional que inclui mais metadados.
3. Cruzar com os dados do `leiloeiros_regulares.csv` já existente no projeto.

---

#### 31.2.2. Sites sem leilões ativos (maioria retorna 0 imóveis)

**Problema:** Dos primeiros 18 sites visitados, apenas 9 tinham imóveis em leilão ativo. Sites como `alanleiloeiro.lel.br`, `analucialeiloeira.com.br`, `andersonleiloeiro.lel.br` retornaram 0 imóveis.

**Causa:** Leiloeiros individuais (pessoas físicas) têm leilões **intermitentes** — ficam sem lotes entre um processo judicial e outro. O site pode estar ativo mas sem leilão corrente.

**Como detectar:** Sites em `.lel.br` (domínio de leiloeiro oficial do CFI) tendem a ter interface mais simples, frequentemente com HTML estático. A ausência de cards/lotes não indica problema técnico — é operacional. 

**Solução recomendada:** 
```python
# Verificar se o site ao menos carrega (HTTP 200) e tem algum conteúdo
# Classificar como "sem_leilao_ativo" em vez de "erro"
status = "sem_leilao_ativo" if resp.status_code == 200 else "offline"
```

---

#### 31.2.3. Domínios `.lel.br` — DNS e estrutura antiga

**Problema:** Vários domínios no formato `*.lel.br` (ex.: `alanleiloeiro.lel.br`, `andrealeiloeira.lel.br`, `marioricart.lel.br`) apresentam:
- DNS não resolvido (NXDOMAIN) — domínio expirado
- HTTP 200 mas página em construção / sem lotes
- Redirecionamento para domínio genérico CFI

**Causa:** O registro `.lel.br` é administrado pelo Conselho Federal dos Leiloeiros (CFI). Domínios abandonados ficam resolvendo por tempo limitado antes de expirar.

**Solução recomendada:**
```python
import socket
def dominio_ativo(url: str) -> bool:
    try:
        host = urlparse(url).netloc
        socket.gethostbyname(host)
        return True
    except socket.gaierror:
        return False
```
Filtrar previamente para evitar timeouts desnecessários.

---

#### 31.2.4. Sites JS-heavy — Playwright necessário para extração

**Problema:** Muitos sites modernos de leiloeiro (React/Next.js/Vue) não renderizam lotes no HTML inicial. A requisição httpx retorna HTML vazio ou com placeholder `<div id="root">`.

**Sites identificados como JS-heavy (detectado pelo generic_scraper):**
- `hoppeleiloes.com.br` — React SPA
- `lanceja.com.br` — Next.js
- `facanhaleiloes.com.br` — SPA com paginação AJAX
- `depaulaonline.com.br` — SPA

**Causa:** A tendência de sites de leilão migrarem para SPAs modernas, especialmente após 2020. Sites com matrícula > 200 são mais propensos a usar tecnologia moderna.

**Solução implementada:** O `generic_scraper.py` do leilao-scraper detecta automaticamente sites JS-heavy via `_is_js_heavy(html)` e aciona o Playwright. Isso aumenta o tempo por site de ~2s para ~30-90s.

**Tempo de scraping comparativo:**
| Tipo de site | Tempo médio | Imóveis extraídos |
|---|---|---|
| HTML estático | 2-5 s | Variável |
| SPA (Playwright) | 30-90 s | Maior cobertura |
| Com paginação | +15s/página | Proporcional |

---

#### 31.2.5. Campos incompletos — cidade, estado, imagem, área

**Problema:** Muitos imóveis são inseridos com `cidade=NULL`, `estado=NULL`, `imagem=NULL`, `area=NULL`. Exemplo detectado:

```
[alexwillianhoppe] 1ª praça: encerra 05/06/2026 - 10:00 → 1/6 campos | nulos: ['cidade', 'estado', 'imagem', 'area', 'quartos']
```

**Causa:** Leiloeiros da JUCERJA-RJ frequentemente listam imóveis em **outros estados** (MG, SP, ES) sem estrutura padronizada no HTML. A extração de cidade/estado falha quando o endereço não segue o padrão reconhecido pelo parser.

**Solução recomendada:**
1. Extrair cidade/estado da URL do imóvel (ex.: `/lote/sp/sao-paulo/...`).
2. Usar regex de UF como fallback: `\b(SP|RJ|MG|...)\b`.
3. Enriquecer com geocoding reverso após inserção.

```python
# Extração de UF da URL ou do título como fallback
uf_match = re.search(r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b', 
                     titulo + " " + url, re.IGNORECASE)
uf = uf_match.group().upper() if uf_match else "RJ"  # fallback para RJ (estado do leiloeiro)
```

---

#### 31.2.6. Sites com padrões de URL de leiloeiros judiciais misturados

**Problema:** Sites como `depaulaonline.com.br` e `lanceja.com.br` listam tanto imóveis **extrajudiciais** (propriedade do leiloeiro) quanto **judiciais** (por ordem do juízo). A URL de listagem pode variar:
- `/extrajudicial/imoveis`
- `/judicial/lotes`
- `/leiloes/ativos`

O scraper genérico precisa descobrir a URL de listagem correta por heurística de links.

**Solução implementada no generic_scraper:** Varredura da homepage por links contendo `LISTING_KEYWORDS` e tentativa de múltiplas URLs de paginação (`?pagina=N`, `/N/`, `?page=N`).

**Limitação:** Sites que paginam via scroll infinito (lazy loading) não são cobertos pelo scraper atual sem Playwright explícito com scroll.

---

#### 31.2.7. Imóveis duplicados entre leiloeiros (portais compartilhados)

**Problema:** Leiloeiros parceiros do **DepaulaOnline** (LUIZ TENÓRIO + DANIELE DE LIMA DE PAULA) compartilham o mesmo site `depaulaonline.com.br`. O scraper tenta inserir os mesmos lotes duas vezes.

**Solução implementada:** O `run.py scrape-csv` deduplica por URL antes de processar. O banco usa upsert por `url_original`. Os 27 imóveis foram inseridos na primeira visita (Daniele); a segunda tentativa (Luiz Tenório) resultaria em 0 inserções (atualizações).

**Solução recomendada:** No CSV de entrada, marcar sites compartilhados como um único entry.

---

#### 31.2.8. Leiloeiros com sites fora do ar ou em construção

**Detectados como offline ou sem conteúdo:**
- `andersonleiloeiro.lel.br` — página em construção
- `andrealeiloeira.lel.br` — 1 imóvel apenas (site parcial)
- `murilochaves.com.br` — certificado SSL expirado
- `fernandobraga.lel.br` — domínio não resolúvel
- `bussiereleiloes.lel.br` — domínio fictício (não existe)
- `walterrezende.com.br` — HTTP 200 mas sem lotes detectados
- Vários `*.lel.br` — DNS NXDOMAIN

**Causa:** Sites pessoais de leiloeiros são pouco mantidos. Muitos leiloeiros operam via plataformas parceiras (Alfa Leilões, Portella Leilões, etc.) e não mantêm site próprio atualizado.

**Impacto:** ~40-60% dos sites retornam 0 imóveis, seja por ausência de leilões ou por site inativo.

---

### 31.3. Sugestões de melhoria para o pipeline

#### 31.3.1. Pré-filtro de domínios ativos

```python
# Antes de visitar, verificar se o domínio resolve e responde
import socket, httpx

def checar_site(url: str, timeout=5.0) -> bool:
    try:
        host = urlparse(url).netloc
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(host)  # DNS check
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False
```

Rodando este filtro antes do scraping, economiza ~40% do tempo total.

---

#### 31.3.2. Descoberta automática de sites via FENAJU + busca web

```python
# Para leiloeiros sem site cadastrado, usar Google CSE
import requests

def descobrir_site(nome_leiloeiro: str, cidade: str) -> str | None:
    query = f'site:*.lel.br OR site:*.com.br leiloeiro "{nome_leiloeiro}" {cidade}'
    r = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": API_KEY, "cx": CX_ID, "q": query, "num": 1}
    )
    items = r.json().get("items", [])
    return items[0]["link"] if items else None
```

---

#### 31.3.3. Integração com leiloesjudiciais.com.br por nome de leiloeiro

Sites como `leiloesjudiciais.com.br` e `leilaoimovel.com.br` indexam leiloeiros por nome e número de matrícula JUCERJA. Para leiloeiros sem site próprio, é possível raspar diretamente:

```
GET https://www.leiloesjudiciais.com.br/leiloeiro/{matricula}/imoveis
```

Isso capturaria imóveis de leiloeiros que **não têm site próprio** mas publicam leilões em portais agregadores.

---

#### 31.3.4. Agendamento de re-scraping periódico

Leiloeiros individuais têm leilões esporádicos. Um re-scraping semanal ou quinzenal é mais eficiente que um varredura única. Sugestão de cron via Celery beat:

```python
# celery_beat_schedule (leilao-scraper/scheduler/tasks.py)
"scrape-jucerja-weekly": {
    "task": "scrapers.tasks.scrape_csv",
    "schedule": crontab(hour=3, minute=0, day_of_week="monday"),
    "args": ["csv/leiloeiros_jucerja_regulares_2024.csv"],
}
```

---

#### 31.3.5. Captura de fotos — limitações e soluções

**Problema:** A maioria dos sites de leiloeiro individual usa imagens com URL dinâmica (assinada, expirando em 24h). Salvar apenas a URL não garante acesso futuro.

**Exemplo de URL dinâmica:**
```
https://s3.amazonaws.com/leiloes/img/lote_123.jpg?X-Amz-Expires=86400&X-Amz-Signature=abc123...
```

**Solução:** Usar `baixar-docs` do pipeline para baixar as imagens junto com os documentos, ou usar serviço de CDN próprio para re-hospedar.

---

#### 31.3.6. Documentos (edital, matrícula) — cobertura baixa nos sites individuais

**Problema:** Sites de leiloeiros individuais raramente publicam documentos diretamente nas páginas de listagem. O edital geralmente está:
- Incorporado no processo judicial (não acessível publicamente)
- Disponível apenas via e-mail/contato
- Publicado no portal do tribunal (ex.: eProc, SAJ)

**Cobertura estimada:** < 20% dos sites têm links diretos para edital/matrícula nas páginas de lote.

**Solução recomendada:** Para leilões judiciais dos leiloeiros JUCERJA, cruzar o número do processo (CNJ) com a API dos tribunais:
```
GET https://api.tjrj.jus.br/v1/processo/{numero_cnj}/documentos
```

---

### 31.4. Resumo executivo de métricas (parcial — 03/06/2026)

| Métrica | Valor |
|---|---|
| Total leiloeiros REGULAR identificados | 333+ (PDFs 1 e 2) |
| Leiloeiros com site identificado | 107 |
| Sites com imóveis ativos | ~35% |
| Imóveis coletados (parcial, ~18 sites) | 140+ |
| Tempo médio por site (httpx) | 3-8 s |
| Tempo médio por site (Playwright) | 30-90 s |
| Taxa de insucesso (0 imóveis) | ~65% |
| Arquivos gerados | `leiloeiros_jucerja_regulares_2024.csv`, `leiloeiros_jucerja_com_sites.csv`, `imoveis_jucerja_*.csv` |
| Banco de dados | PostgreSQL via `run.py scrape-csv` |

### 31.5. Arquivos criados nesta sessão

```
leiloes/
├── scraper_jucerja_leiloeiros.py         ← script standalone de scraping
├── scraper_jucerja_run.log               ← log completo da execução
├── csv/
│   ├── leiloeiros_jucerja_regulares_2024.csv  ← 107 leiloeiros REGULAR com site
│   ├── leiloeiros_jucerja_com_sites.csv       ← idem (saída do scraper)
│   └── imoveis_jucerja_YYYYMMDD_HHMM.csv      ← imóveis coletados
```

### 31.6. Checklist de execução

- [x] CSV de leiloeiros REGULAR gerado: `csv/leiloeiros_jucerja_regulares_2024.csv`
- [x] Leiloeiros importados no banco: 111 inseridos + 16 atualizados (127 total)
- [x] Scraping iniciado via `python run.py scrape-csv csv/leiloeiros_jucerja_regulares_2024.csv --max-paginas 8`
- [x] Imóveis inseridos no PostgreSQL em tempo real (sem etapa adicional de importação)
- [ ] Aguardar conclusão do scraping (~107 sites, ~45-90 min total)
- [ ] Rodar `classificar` e `deduplicar` após conclusão
- [ ] Re-executar semanalmente para capturar novos leilões

---

## 32. Correção e Deduplicação de Nomes de Cidades no PostgreSQL Docker

### 32.1. Contexto e problema

O banco PostgreSQL do `leilao-scraper` acumula nomes de cidades corrompidos vindos de múltiplas fontes:

| Tipo de problema | Exemplo | Causa |
|---|---|---|
| **Mojibake** | `FlorianÃ³polis` | CSV em UTF-8 lido como Latin-1 |
| **Maiúsculas sem acento** | `SAO PAULO`, `GOIANIA` | Caixa Econômica e outros scrapers |
| **Variantes de capitalização** | `Sao Paulo`, `sao paulo` | Fontes diversas |
| **Duplicatas mistas** | `São Paulo` + `SAO PAULO` | Mesma cidade em dois scrapers |

O efeito no frontend: o filtro de cidades (`autocomplete`) exibia entradas como `FlorianÃ³polis` separadas de `Florianópolis`, e imóveis ficavam espalhados em múltiplas entradas da mesma cidade.

---

### 32.2. Armadilha: dois PostgreSQL rodando simultaneamente

**Problema crítico descoberto:** o sistema tem **dois PostgreSQL**: um local Windows (porta 5432 nativa) e o container Docker `leilao_postgres` (também mapeado na porta 5432). Scripts Python com `psycopg2` conectando em `localhost:5432` podem acertar o banco **errado**.

**Solução confiável:** sempre usar `docker exec` para operar no banco correto:

```bash
# Verificar
docker exec leilao_postgres psql -U leilao -d leilao_db -c "SELECT COUNT(*) FROM imoveis;"

# Aplicar UPDATE diretamente
docker exec leilao_postgres psql -U leilao -d leilao_db -c \
  "UPDATE imoveis SET cidade = 'Florianópolis' WHERE cidade = 'FlorianÃ³polis';"
```

---

### 32.3. Problemas no endpoint `/imoveis/cidades`

O endpoint original tinha três dependências que **não existem** no banco de produção:

| Dependência | Problema | Correção |
|---|---|---|
| Tabela `municipios_ibge` | Não foi criada → erro 500 | Remover o `LEFT JOIN` |
| Extensão `unaccent` | Não instalada no PostgreSQL → erro 500 | Remover todas as chamadas `unaccent()` |
| Enums em minúsculo (`'imovel'`) | Enums no banco são **maiúsculos** (`'IMOVEL'`) | Usar `'IMOVEL'`, `'OUTRO'`, `'ABERTO'` |

**Versão correta do endpoint** (`api/routes/imoveis.py`):

```python
@router.get("/cidades", response_model=list[str])
async def listar_cidades(
    estado: Optional[str] = None,
    q: Optional[str] = None,
    somente_produtos: bool = False,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text
    params: dict = {}
    extra = []
    if estado:
        extra.append("AND i.estado = :estado")
        params["estado"] = estado.upper()
    if q:
        extra.append("AND i.cidade ILIKE :q")
        params["q"] = f"{q}%"
    if somente_produtos:
        extra.append("AND i.categoria IN ('PRODUTO', 'VEICULO')")
    else:
        extra.append("AND i.categoria IN ('IMOVEL', 'OUTRO')")
    sql = text(f"""
        SELECT DISTINCT i.cidade AS nome
        FROM imoveis i
        WHERE i.ativo = true
          AND i.status = 'ABERTO'
          AND i.cidade IS NOT NULL
          AND length(trim(i.cidade)) >= 3
          AND i.cidade !~ '^[A-Z]{{2}}$'
          AND i.cidade !~ '^\\d'
          {' '.join(extra)}
        ORDER BY 1
        LIMIT 100
    """)
    result = await db.execute(sql, params)
    return [r[0] for r in result.fetchall() if r[0]]
```

**Filtro por cidade em `_aplicar_filtros`** — remover `unaccent`:

```python
# ANTES (quebrado):
conds.append(or_(
    Imovel.cidade.ilike(f"%{cidade_norm}%"),
    sf.unaccent(Imovel.cidade).ilike(sf.unaccent(f"%{cidade_norm}%")),
))

# DEPOIS (correto):
conds.append(Imovel.cidade.ilike(f"%{cidade_norm}%"))
```

Após alterar `imoveis.py`, copiar para o container e reiniciar:

```bash
docker cp api/routes/imoveis.py leilao_api:/app/api/routes/imoveis.py
docker restart leilao_api
```

---

### 32.4. Enums PostgreSQL são maiúsculos

Os enums `categoriaitem` e `statusleilao` no banco são **maiúsculos**:

```sql
-- Verificar valores reais
SELECT enumlabel FROM pg_enum
JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
WHERE pg_type.typname = 'categoriaitem';
-- Resultado: IMOVEL, PRODUTO, VEICULO, OUTRO

SELECT enumlabel FROM pg_enum
JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
WHERE pg_type.typname = 'statusleilao';
-- Resultado: ABERTO, ENCERRADO, CANCELADO, ARREMATADO
```

Usar **sempre maiúsculo** em queries SQL raw: `'IMOVEL'`, `'ABERTO'`, etc.

---

### 32.5. Script `corrigir_cidades.py`

Localização: `leilao-scraper/leilao-scraper/corrigir_cidades.py`

Script standalone que corrige nomes de cidades diretamente via `docker exec`. Não depende de psycopg2 nem de conexão direta ao banco.

#### Modos de uso

```bash
# Ver cidades com encoding corrompido (não altera nada)
python corrigir_cidades.py --listar

# Corrigir mojibake em todas as cidades de uma vez
python corrigir_cidades.py --todos

# Corrigir variantes de uma cidade específica
python corrigir_cidades.py --cidade "Florianópolis"
python corrigir_cidades.py --cidade "São Paulo" --cidade "Goiânia"

# Deduplicar: SAO PAULO + São Paulo + Sao Paulo → São Paulo
python corrigir_cidades.py --deduplicar

# Simular sem executar
python corrigir_cidades.py --deduplicar --dry-run
```

#### Lógica de escolha do nome canônico (`--deduplicar`)

Agrupa cidades pelo nome normalizado (sem acento, lowercase). Para cada grupo, escolhe o canônico pelo score:

1. **Mais acentos** — `São Paulo` > `Sao Paulo` > `SAO PAULO`
2. **Tem letras minúsculas** — title case > ALL CAPS
3. **Tem letras maiúsculas** — title case > all lower
4. **Mais registros** — desempate

#### Lógica de detecção de mojibake

```python
def fix_mojibake(s: str) -> str:
    """FlorianÃ³polis → Florianópolis"""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s  # já está correto ou irrecuperável
```

A função é segura: cidades já corretas como `São Paulo` levantam `UnicodeDecodeError` ao tentar `b'S\xe3o Paulo'.decode('utf-8')` (sequência UTF-8 inválida) e são devolvidas sem alteração.

#### Resultados da execução em jun/2026

| Operação | Resultado |
|---|---|
| `--todos` (mojibake) | 277 cidades corrigidas, 1.238 registros |
| `--deduplicar` | 793 grupos deduplicados, 17.634 registros migrados |
| `São Paulo` após deduplicação | 2.692 registros unificados |
| `Goiânia` após deduplicação | 365 registros unificados |
| `Florianópolis` após deduplicação | 191 registros unificados |

---

### 32.6. Ordem de execução recomendada após novo scraping

Sempre que um novo scraper importar dados, rodar nesta sequência:

```bash
cd leilao-scraper/leilao-scraper

# 1. Corrigir mojibake (encoding corrompido)
python corrigir_cidades.py --todos

# 2. Deduplicar variantes (MAIUSCULAS, sem acento, etc.)
python corrigir_cidades.py --deduplicar

# 3. Verificar se restou algum problema
python corrigir_cidades.py --listar
```

> **Nota:** `--deduplicar` deve rodar **depois** de `--todos`, pois o deduplicador agrupa por nome normalizado — mojibake ainda presente (`FlorianÃ³polis`) não seria agrupado com `Florianópolis`.

---

### 32.7. Diagnóstico rápido de problemas no filtro de cidades

```bash
# 1. A API está respondendo?
curl http://localhost:8000/api/v1/imoveis/cidades?q=Flori

# 2. Qual container serve a porta 8000?
docker ps --format "table {{.Names}}\t{{.Ports}}"

# 3. O código dentro do container está atualizado?
docker exec leilao_api python -c \
  "from api.routes.imoveis import listar_cidades; import inspect; print(inspect.getsource(listar_cidades)[:200])"

# 4. Ver cidades com 'Florian' direto no banco
docker exec leilao_postgres psql -U leilao -d leilao_db \
  -c "SELECT cidade, COUNT(*) FROM imoveis WHERE cidade ILIKE '%florian%' GROUP BY cidade ORDER BY cidade"

# 5. Verificar extensões e tabelas existentes
docker exec leilao_postgres psql -U leilao -d leilao_db \
  -c "SELECT extname FROM pg_extension WHERE extname = 'unaccent';"
docker exec leilao_postgres psql -U leilao -d leilao_db \
  -c "SELECT to_regclass('public.municipios_ibge');"
```

---

## 32. Correção e Deduplicação de Nomes de Cidades no PostgreSQL Docker

### 32.1. Contexto e problema

O banco PostgreSQL do `leilao-scraper` acumula nomes de cidades corrompidos vindos de múltiplas fontes:

| Tipo de problema | Exemplo | Causa |
|---|---|---|
| **Mojibake** | `FlorianÃ³polis` | CSV em UTF-8 lido como Latin-1 |
| **Maiúsculas sem acento** | `SAO PAULO`, `GOIANIA` | Caixa Econômica e outros scrapers |
| **Variantes de capitalização** | `Sao Paulo`, `sao paulo` | Fontes diversas |
| **Duplicatas mistas** | `São Paulo` + `SAO PAULO` | Mesma cidade em dois scrapers |

O efeito no frontend: o filtro de cidades (`autocomplete`) exibia entradas como `FlorianÃ³polis` separadas de `Florianópolis`, e imóveis ficavam espalhados em múltiplas entradas da mesma cidade.

---

### 32.2. Armadilha: dois PostgreSQL rodando simultaneamente

**Problema crítico:** o sistema tem **dois PostgreSQL** — um local Windows (porta 5432) e o container Docker `leilao_postgres` (também porta 5432). Scripts Python com `psycopg2` conectando em `localhost:5432` podem acertar o banco **errado**.

**Solução confiável:** sempre usar `docker exec` para operar no banco correto:

```bash
docker exec leilao_postgres psql -U leilao -d leilao_db -c "SELECT COUNT(*) FROM imoveis;"
docker exec leilao_postgres psql -U leilao -d leilao_db -c \
  "UPDATE imoveis SET cidade = 'Florianópolis' WHERE cidade = 'FlorianÃ³polis';"
```

---

### 32.3. Problemas no endpoint `/imoveis/cidades`

O endpoint original tinha três dependências que **não existem** no banco de produção:

| Dependência | Problema | Correção |
|---|---|---|
| Tabela `municipios_ibge` | Não foi criada → erro 500 | Remover o `LEFT JOIN` |
| Extensão `unaccent` | Não instalada → erro 500 | Remover todas as chamadas `unaccent()` |
| Enums em minúsculo (`'imovel'`) | Enums no banco são **maiúsculos** (`'IMOVEL'`) | Usar `'IMOVEL'`, `'OUTRO'`, `'ABERTO'` |

**Versão correta do endpoint** (`api/routes/imoveis.py`):

```python
@router.get("/cidades", response_model=list[str])
async def listar_cidades(estado=None, q=None, somente_produtos=False, db=Depends(get_db)):
    from sqlalchemy import text
    params, extra = {}, []
    if estado:
        extra.append("AND i.estado = :estado"); params["estado"] = estado.upper()
    if q:
        extra.append("AND i.cidade ILIKE :q"); params["q"] = f"{q}%"
    cat = "('PRODUTO','VEICULO')" if somente_produtos else "('IMOVEL','OUTRO')"
    extra.append(f"AND i.categoria IN {cat}")
    sql = text(f"""
        SELECT DISTINCT i.cidade FROM imoveis i
        WHERE i.ativo=true AND i.status='ABERTO' AND i.cidade IS NOT NULL
          AND length(trim(i.cidade))>=3 AND i.cidade !~ '^[A-Z]{{2}}$' AND i.cidade !~ '^\\d'
          {' '.join(extra)} ORDER BY 1 LIMIT 100
    """)
    result = await db.execute(sql, params)
    return [r[0] for r in result.fetchall() if r[0]]
```

Após alterar, copiar e reiniciar:
```bash
docker cp api/routes/imoveis.py leilao_api:/app/api/routes/imoveis.py
docker restart leilao_api
```

---

### 32.4. Enums PostgreSQL são maiúsculos

```sql
-- Verificar
SELECT enumlabel FROM pg_enum
JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
WHERE pg_type.typname IN ('categoriaitem','statusleilao','tipoimovel','tipoimovel');
-- categoriaitem: IMOVEL, PRODUTO, VEICULO, OUTRO
-- statusleilao:  ABERTO, ENCERRADO, CANCELADO, ARREMATADO
-- tipoimovel:    APARTAMENTO, CASA, TERRENO, COMERCIAL, RURAL, GALPAO, SALA, VAGA, OUTRO
```

---

### 32.5. Script `corrigir_cidades.py`

Localização: `leilao-scraper/leilao-scraper/corrigir_cidades.py`

```bash
python corrigir_cidades.py --listar          # ver encoding corrompido
python corrigir_cidades.py --todos           # corrigir mojibake
python corrigir_cidades.py --cidade "São Paulo"  # corrigir cidade específica
python corrigir_cidades.py --deduplicar      # unificar duplicatas
python corrigir_cidades.py --deduplicar --dry-run
```

**Lógica do canônico:** mais acentos > tem minúsculas > tem maiúsculas > mais registros.

**Resultados jun/2026:** 277 cidades mojibake corrigidas (1.238 registros), 793 grupos deduplicados (17.634 registros).

---

### 32.6. Ordem após novo scraping

```bash
cd leilao-scraper/leilao-scraper
python corrigir_cidades.py --todos       # 1. mojibake
python corrigir_cidades.py --deduplicar  # 2. duplicatas
python corrigir_cidades.py --listar      # 3. verificar
```

> `--deduplicar` deve rodar **depois** de `--todos` — mojibake ainda presente não agrupa com o nome correto.

---

### 32.7. Diagnóstico rápido do filtro de cidades

```bash
curl "http://localhost:8000/api/v1/imoveis/cidades?q=Flori"
docker ps --format "table {{.Names}}\t{{.Ports}}"
docker exec leilao_postgres psql -U leilao -d leilao_db \
  -c "SELECT cidade, COUNT(*) FROM imoveis WHERE cidade ILIKE '%florian%' GROUP BY cidade;"
docker exec leilao_postgres psql -U leilao -d leilao_db \
  -c "SELECT to_regclass('public.municipios_ibge'), extname FROM pg_extension WHERE extname='unaccent';"
```

---

## 33. Scraping JUCESC — Leiloeiros Regulares de SC (jun/2026)

Coleta em `https://leiloeiros.jucesc.sc.gov.br/site/` — portal oficial de leiloeiros do Estado de SC.
Scripts: `scraper_jucesc.py` + `importar_jucesc.py`.

### 33.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros REGULAR na JUCESC (oficial) | 198 |
| Leiloeiros no CSV FENAJU (SC) | 118 |
| Total merged (deduplicated) | 207 |
| Leiloeiros com site identificado | 72 |
| Leiloeiros sem site (só e-mail) | 135 |
| Sites visitados | 72 |
| Imóveis coletados (bruto) | 707 |
| Imóveis classificados como IMOVEL/ABERTO | 367 |
| Sites com imóveis | 30 |
| Sites sem leilão ativo | 42 |
| Sites com erro | 0 |
| CSV leiloeiros | `csv/leiloeiros_jucesc_2026-06-03.csv` |
| CSV imóveis | `csv/imoveis_jucesc_2026-06-03.csv` |

### 33.2. Distribuição por leiloeiro (com imóveis)

| Leiloeiro | Imóveis (bruto) | Imóveis (classificados) |
|---|---|---|
| Ulisses Donizete Ramos | 63 | — |
| Giovanni Silva Wersdoefer | 46 | 16 |
| Daniel Elias Garcia | 45 | 38 |
| Guilherme Antônio Scarpari De Lucca | 42 | 15 |
| Vicente Alves Pereira Neto | 35 | 15 |
| Rodrigo Schmitz | 35 | 16 |
| José Sergio Della Giustina | 32 | 26 |
| Júlio Ramos Luz | 30 | — |
| Fábio Marlon Machado | 29 | 20 |
| Giovano Ávila Alves | 28 | 15 |
| Marinilce Viana Quadrado | 26 | 13 |
| Andrea Baldissera | 26 | 22 |
| Jean Fernando Ribeiro Pavesi | 25 | 8 |
| Marciano Mauro Pagliarini | 23 | 22 |
| Odilson Fumagalli Avila | 23 | 14 |
| Andréia Cristina Nunes | 20 | 9 |
| Alex Willian Hoppe | 19 | 19 |
| Guilherme E. Stutz Toporoski | 19 | 15 |
| César Luis Moresco | 18 | 16 |
| Sandro Luis De Souza | 17 | — |

### 33.3. Principais dificuldades encontradas

#### 33.3.1. JUCESC não expõe URL dos leiloeiros

**Problema:** O portal lista apenas AARC, Nome, Data de Matrícula e Situação — sem site, e-mail ou telefone.

**Solução aplicada:** Cruzamento com `leiloeiros_regulares.csv` (FENAJU) + derivação do site pelo domínio do e-mail.

**Solução recomendada:**
```python
def descobrir_site_por_email(email: str) -> str | None:
    ignorados = {"gmail.com","hotmail.com","yahoo.com","outlook.com","terra.com.br"}
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m or m.group(1) in ignorados: return None
    return f"https://www.{m.group(1)}"
```

#### 33.3.2. JUCESC SSL com certificado inválido

**Problema:** `requests` falha com `SSLError` ao acessar `leiloeiros.jucesc.sc.gov.br`.

**Solução aplicada:** `verify=False` + `urllib3.disable_warnings()`.

**Solução recomendada:** Instalar `pip install --upgrade certifi` ou usar `requests.Session` com bundle personalizado.

#### 33.3.3. Alta proporção sem site próprio (~135/207)

**Problema:** Maioria dos leiloeiros JUCESC atua via plataformas terceiras ou só presencialmente.

**Solução recomendada:** Rastrear por nome em `leiloesjudiciais.com.br/leiloeiro/{slug}` e `leilaoimovel.com.br`.

#### 33.3.4. Scraper captura não-imóveis (veículos, máquinas)

**Problema:** O `is_imovel()` com filtro por palavras-chave não é 100% preciso. 707 itens brutos → 367 classificados como imóvel (48% de precisão).

**Causa:** Leiloeiros SC atuam em leilões mistos (imóveis + veículos + equipamentos).

**Solução aplicada:** Classifier do pipeline (`run.py classificar`) filtra pela `categoria` correta.

**Solução recomendada:** Melhorar `is_imovel()` com lista negra mais agressiva e threshold de confiança.

#### 33.3.5. Datas inválidas geradas pelo parser

**Problema:** Regex de datas capturou strings como `2023-24-25` (dia/mês invertidos) causando erros no PostgreSQL.

**Solução aplicada:** Função `valid_date()` com `datetime.date()` para validar antes de inserir.

```python
def valid_date(s):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", (s or "").strip())
    if m:
        try: datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))); return m.group(0)
        except ValueError: return None
    return None
```

#### 33.3.6. Valores invertidos (avaliação < mínimo)

**Problema:** O scraper pega o 1º e 2º R$ do texto — às vezes a avaliação aparece antes do mínimo, gerando `valor_avaliacao < valor_minimo`.

**Solução aplicada:**
```sql
UPDATE imoveis SET valor_avaliacao = NULL
WHERE fonte_id = 943 AND valor_avaliacao < valor_minimo;
```

#### 33.3.7. SQL muito longo para Windows (`WinError 206`)

**Problema:** `docker exec ... psql -c "INSERT ... VALUES (...)"` com registros grandes ultrapassa o limite de comprimento de argumento do Windows (~32.767 chars).

**Solução aplicada:** Copiar CSV + script Python para dentro do container e executar lá:
```bash
docker cp imoveis_jucesc.csv leilao_api:/tmp/imoveis_jucesc.csv
docker cp import_script.py   leilao_api:/tmp/import_script.py
docker exec leilao_api python /tmp/import_script.py
```

### 33.4. Ordem de execução

```bash
cd C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes

# 1. Scraping
python scraper_jucesc.py

# 2. Importação
python importar_jucesc.py

# 3. Pós-processamento
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 2000"
docker exec leilao_api bash -c "cd /app && python run.py normalizar-cidades"
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"

# 4. Correção de cidades SC
cd ..\leilao-scraper\leilao-scraper
python corrigir_cidades.py --todos
python corrigir_cidades.py --deduplicar

docker restart leilao_api
```

### 33.5. Arquivos criados

```
leiloes/
├── scraper_jucesc.py                     ← scraper (Playwright + requests)
├── importar_jucesc.py                    ← importador CSV → SQLite + PostgreSQL
├── scraper_jucesc.log                    ← log completo
├── scraper_jucesc_progress.json          ← progresso retomável
└── csv/
    ├── leiloeiros_jucesc_2026-06-03.csv  ← 207 leiloeiros (nome, site, email)
    └── imoveis_jucesc_2026-06-03.csv     ← 707 imóveis coletados
```

### 33.6. Checklist de execução

- [x] JUCESC oficial consultada: 198 leiloeiros REGULAR
- [x] CSV FENAJU cruzado: 207 leiloeiros merged
- [x] CSV leiloeiros gerado: `csv/leiloeiros_jucesc_2026-06-03.csv`
- [x] 72 sites visitados via Playwright/requests
- [x] 707 imóveis coletados → `csv/imoveis_jucesc_2026-06-03.csv`
- [x] 707 inseridos no SQLite (`imoveis_leiloeiros.db`)
- [x] 707 inseridos no PostgreSQL Docker
- [x] Classifier rodado: 367 classificados como IMOVEL/ABERTO
- [x] `normalizar-cidades` aplicado (406 cidades SC corrigidas)
- [x] `deduplicar` aplicado
- [x] API reiniciada

---

## 34. Scraping JUCEMS — Leiloeiros Regulares do MS (jun/2026)

Coleta de imóveis dos leiloeiros credenciados pela **JUCEMS** (Junta Comercial do Estado de Mato Grosso do Sul).
Fontes: arquivo `.txt` oficial da JUCEMS + `https://www.jucems.ms.gov.br/empresas/controles-especiais/agentes-auxiliares/leiloeiros/`.
Scripts: `scraper_jucems.py` + `importar_jucems.py` + `import_jucems_docker.py`.

### 34.1. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros Regular no arquivo TXT | 55 |
| Leiloeiros Regular do site JUCEMS | 80 |
| Total merged (deduplicado por nome) | 60 |
| Leiloeiros com site identificado | 49 |
| Leiloeiros sem site | 11 |
| Sites processados | 49 |
| Sites com imóveis ativos | 36 |
| Sites sem leilão ativo | 13 |
| Sites offline/DNS inválido | 2 |
| Total imóveis coletados (bruto) | 593 |
| Inseridos no SQLite | 514 |
| Inseridos no PostgreSQL | 593 |
| Tempo total de scraping | ~45 min |
| CSV leiloeiros | `csv/leiloeiros_jucems_2026-06-08.csv` |
| CSV imóveis | `csv/imoveis_jucems_2026-06-08.csv` |

### 34.2. Distribuição por leiloeiro (top 20)

| Leiloeiro | Site | Imóveis |
|---|---|---|
| LUCAS ANDREATTA DE OLIVEIRA | leiloariasmart.com.br | 47 |
| RODRIGO APARECIDO RIGOLON DA SILVA | rigolonleiloes.com.br | 31 |
| VLADMIR OLIANI | leiloesaguiar.com.br | 30 |
| CONCEIÇÃO MARIA FIXER | mariafixerleiloes.com.br | 27 |
| BRUNO BARRETO SANCHES | barretoleiloes.com.br | 19 |
| APARECIDA MARIA FIXER | cidafixerleiloes.com.br | 16 |
| MARCELO CARNEIRO BERNARDELLI | marcaleiloes.com.br | 14 |
| IGOR ALEXANDRE DE SOUZA SILVA | desouzaleiloes.com.br | 12 |
| DAVI BORGES DE AQUINO | alfaleiloes.com | 11 |
| FLARES AGUIAR DA SILVA | faleiloes.com.br | 10 |
| ALGLECIO BUENO DA SILVA | leiloesgoias.com.br | 10 |
| PATRICIA PIMENTEL GROCOSKI COSTA | pimentelleiloes.com.br | 10 |
| LETICIA DE ANDRADE VERRONE | ricoleiloes.com.br | 8 |
| CARLO FERRARI | carloferrarileiloes.com.br | 8 |
| FERNANDO JOSE CERELLO GONÇALVES PEREIRA | megaleiloes.com.br | 8 |
| ELTON LUIZ SIMON | simonleiloes.com.br | 8 |
| CECILIA DELZEIR SOBRINHO | ceciliadelzeirleiloes.com.br | 8 |
| TARCILIO LEITE | casadeleiloes.com.br | 5 |
| FABIO MARLON MACHADO | machadoleiloeiro.com.br | 5 |
| RODRIGO SCHMITZ | hammer.lel.br | 5 |

### 34.3. Principais dificuldades enfrentadas

#### 34.3.1. Arquivo .txt com encoding corrompido (U+FFFD)

**Problema:** O arquivo `.txt` da JUCEMS, ao ser processado, continha caracteres
substituídos (U+FFFD, `�`) em vez de acentos como `í`, `ç`, `ã`. Por exemplo:
`Matrícula: 003` chegava como `Matr�cula: 003`.

**Causa:** O arquivo original do usuário foi gerado com encoding misto (provavelmente
copiado de PDF → texto), e durante a transmissão/armazenamento os bytes inválidos
foram substituídos pelo caractere de reposição Unicode.

**Impacto:** O parser inicial usava `r"Matr[íi]cula\s*:\s*(\d+)"` que não capta
o caractere U+FFFD, resultando em 0 registros com matrícula.

**Solução aplicada:** Reescrever o parser linha a linha usando `.` (qualquer caractere)
em vez de acentos específicos:

```python
# ERRADO — não casa U+FFFD
mat_m = re.search(r"Matr[íi]cula\s*:\s*(\d+)", bloco)

# CORRETO — . casa qualquer caractere, incluindo U+FFFD
mat_m = re.search(r"Matr.cula\s*:\s*(\d+)", bloco, re.IGNORECASE)
```

**Regra geral:** para parsear arquivos .txt de origem PDF/copiar-e-colar, usar `.`
(ou `[^\s:]`) em posições de acentos. Nunca assumir que acentos chegam íntegros.

**Como corrigir proativamente:** salvar o arquivo com encoding explícito UTF-8 limpo
antes de parsear:
```python
# Reescreve arquivo sem replacement chars
txt = Path("arquivo.txt").read_bytes()
txt_clean = txt.decode("utf-8", errors="replace")  # já os substitui por ?
# Alternativa: tentar múltiplos encodings
for enc in ["utf-8", "cp1252", "latin-1"]:
    try:
        txt_clean = txt.decode(enc, errors="strict")
        break
    except UnicodeDecodeError:
        continue
```

---

#### 34.3.2. Parser de nome falhando com regex de maiúsculas

**Problema:** O parser original usava `re.match(r"^[A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ\s]{5,}$", linha)`
para identificar nomes de leiloeiros (totalmente em maiúsculas). Com U+FFFD presente,
nomes como `CONCEIÇÃO MARIA FIXER` chegavam como `CONCEI��O MARIA FIXER` e
não casavam o padrão.

**Solução aplicada:** Substituir regex de charset por verificação de proporção de
maiúsculas (>= 70% do texto é letra maiúscula):

```python
# ERRADO — charset com acentos não cobre U+FFFD
if re.match(r"^[A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ\s]{5,}$", linha)

# CORRETO — proporção de maiúsculas é agnóstica ao encoding
letras = [c for c in linha if c.isalpha()]
if letras and sum(1 for c in letras if c.isupper()) / len(letras) >= 0.7:
    nome = linha  # linha é um nome
```

---

#### 34.3.3. Enums PostgreSQL em MAIÚSCULAS

**Problema:** O scraper enviava valores lowercase (`'outro'`, `'extrajudicial'`) para
campos enum do PostgreSQL, que exigem **maiúsculas** (`'OUTRO'`, `'EXTRAJUDICIAL'`).

```
ERROR: invalid input value for enum tipoimovel: "outro"
```

**Causa:** Inconsistência entre a lógica Python (que valida com `TIPOS_IMOVEL_VALIDOS`
em minúsculas) e os enums criados no banco com uppercase.

**Solução:** Sempre fazer `.upper()` antes de inserir em campo enum:

```python
TIPOS_IMOVEL_VALIDOS = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
tipo_i = r.get("tipo_imovel","outro").upper()
if tipo_i not in TIPOS_IMOVEL_VALIDOS: tipo_i = "OUTRO"
```

**Como verificar os valores aceitos:**
```sql
SELECT enumlabel FROM pg_enum
JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
WHERE pg_type.typname = 'tipoimovel';
```

---

#### 34.3.4. WinError 206 — SQL muito longo para subprocess no Windows

**Problema:** O importador gerava um SQL `INSERT ... VALUES (...)` com 50 linhas
em lote e passava via `docker exec ... psql -c "INSERT ..."`. No Windows, o limite
de comprimento de argumento de processo (~32.767 chars) era ultrapassado:

```
[WinError 206] O nome do arquivo ou a extensão é muito grande
```

**Impacto:** 593/593 imóveis falharam na importação para o PostgreSQL.

**Causa:** O campo `arquivos` (JSON com até 4000 chars) e `descricao` (500 chars)
tornavam cada linha do VALUES muito longa. Com 50 linhas por lote, o SQL ultrapassava
o limite da linha de comando.

**Solução:** Copiar CSV e script Python para dentro do container e executar lá,
usando `psycopg2` em vez de passar SQL como argumento do `docker exec`:

```powershell
# 1. Copiar arquivos para o container
docker cp csv/imoveis_jucems.csv leilao_api:/tmp/imoveis_jucems.csv
docker cp import_jucems_docker.py leilao_api:/tmp/import_jucems_docker.py

# 2. Executar dentro do container (sem limite de argumento)
docker exec leilao_api python /tmp/import_jucems_docker.py
```

O script `import_jucems_docker.py` usa `psycopg2.connect(db_url)` onde
`db_url = os.environ.get("DATABASE_URL_SYNC")` — disponível no container.

**Regra:** qualquer INSERT com campos `TEXT` longos (>500 chars/row) deve usar
este padrão. Para lotes de até 50 linhas com campos curtos (como leiloeiros),
o `psql -c` ainda funciona.

---

#### 34.3.5. `sys.stdout.reconfigure` quebrando log por arquivo

**Problema:** O scraper foi iniciado com:
```powershell
Start-Process python -ArgumentList "scraper_jucems.py" `
  -RedirectStandardOutput scraper_jucems_out.txt
```
O arquivo `scraper_jucems_out.txt` ficou com 0 bytes durante toda a execução.

**Causa:** O script usa `sys.stdout.reconfigure(encoding="utf-8")` logo no início,
que substitui o objeto stdout pelo wrapper UTF-8. Esse wrapper perde a referência
ao file descriptor do redirecionamento original, quebrando o fluxo para arquivo.

**Solução aplicada:** Monitorar via `scraper_jucems_progress.json` (escrito a cada
lote) e via `scraper_jucems.log` (append com `open()` direto no código):

```python
# Em vez de depender do stdout rediirecionado:
def log(msg: str):
    print(msg)  # pode falhar se stdout redirecionado
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")  # sempre funciona
```

```powershell
# Monitorar progresso pelo JSON, não pelo log stdout
$prog = Get-Content scraper_jucems_progress.json | ConvertFrom-Json
Write-Host "Total: $($prog.total_imoveis) | OK: $($prog.sites_ok)"
```

---

#### 34.3.6. Sites offline e DNS inválido

**Problema:** Dois sites retornaram erro de DNS (`NameResolutionError`):
- `britoleiloes.com.br` — domínio inexistente/expirado
- `ericoleiloes.com.br` — domínio inexistente/expirado
- `mikedutraleiloeiro.com.br` — domínio inexistente/expirado
- `kronbergleiloes.com.br` — timeout de conexão

**Impacto:** Esses sites foram marcados como `sem_leilao` e nenhum imóvel
foi coletado deles.

**Solução recomendada:** Pré-filtrar domínios antes do scraping principal:

```python
import socket
def dominio_ativo(url: str, timeout: float = 5.0) -> bool:
    try:
        host = urlparse(url).netloc
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(host)
        return True
    except (socket.gaierror, OSError):
        return False

# Filtrar lista antes de scraping
leiloeiros_ativos = [l for l in leiloeiros if not l["site"] or dominio_ativo(l["site"])]
```

---

#### 34.3.7. Leiloeiros JUCEMS com endereço em outro estado

**Problema:** A JUCEMS credencia leiloeiros que operam em MS mas têm endereço
em outros estados (SP, PR, MG, GO, SC, MT, RO, PI). O scraper tentava extrair
a UF do lote usando a UF do leiloeiro como fallback (`uf_leiloeiro = "MS"`),
mas muitos imóveis eram de SP, GO, PR, etc.

**Exemplo:** `LUCAS ANDREATTA DE OLIVEIRA` (São Paulo/SP, credenciado MS) tinha
91 imóveis — todos em estados variados.

**Impacto:** Campo `estado` de vários imóveis ficou como `"MS"` incorretamente.

**Solução aplicada:** Manter o fallback mas extrair UF do texto do lote primeiro:

```python
uf_m = RE_UF.search(texto)
uf = uf_m.group() if uf_m else lei.get("uf_leiloeiro", "MS")
```

**Solução recomendada:** Usar geocoding pelo endereço completo após inserção:
```bash
docker exec leilao_api bash -c "cd /app && python run.py geocodificar --limite 500"
```

---

#### 34.3.8. Alta taxa de deduplicação (54% de duplicatas)

**Problema:** De 593 imóveis brutos, 320 foram marcados como duplicatas
(124 por URL exata + 196 por título+local), resultando em apenas 327 únicos.

**Causa:** O mesmo site é visitado duas vezes quando leiloeiros compartilham o
mesmo site (ex.: `ibecleiloes.com.br` para HELDER FIGUEIREDO + LUIZ FRANGE;
`megaleiloes.com.br/ms` para MILENA ROSA + FERNANDO CERELLO).

**Solução aplicada no scraper:** Deduplica sites antes de visitar:
```python
sites_vistos = set()
leiloeiros_unicos = []
for l in todos:
    site = l.get("site","").rstrip("/")
    if site and site not in sites_vistos:
        sites_vistos.add(site)
        leiloeiros_unicos.append(l)
```

**Solução recomendada:** No CSV de entrada, consolidar leiloeiros com site
compartilhado em uma única entrada.

---

### 34.4. Ordem de execução

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes"

# 1. Scraping (usa TXT + site JUCEMS)
python scraper_jucems.py --max-paginas 8

# 2. Importação SQLite (incluída no scraper automaticamente)
# — SQLite já é importado ao final do scraping

# 3. Importação PostgreSQL (via Docker — contorna WinError 206)
docker cp csv/imoveis_jucems_<data>.csv leilao_api:/tmp/imoveis_jucems.csv
docker cp import_jucems_docker.py leilao_api:/tmp/import_jucems_docker.py
docker exec leilao_api python /tmp/import_jucems_docker.py

# 4. Pós-processamento
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 2000"
docker exec leilao_api bash -c "cd /app && python run.py normalizar-cidades"
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"
docker restart leilao_api
```

### 34.5. Arquivos criados nesta sessão

```
leiloes/
├── scraper_jucems.py              ← scraper principal (requests + Playwright fallback)
├── importar_jucems.py             ← importador CSV → SQLite + PostgreSQL (via psql)
├── import_jucems_docker.py        ← importador via psycopg2 dentro do container
├── jucems_leiloeiros.txt          ← TXT oficial da JUCEMS salvo em disco
├── scraper_jucems.log             ← log completo da execução
├── scraper_jucems_progress.json   ← progresso em tempo real
└── csv/
    ├── leiloeiros_jucems_2026-06-08.csv  ← 60 leiloeiros (nome, site, email)
    └── imoveis_jucems_2026-06-08.csv     ← 593 imóveis coletados
```

### 34.6. Checklist de execução

- [x] TXT da JUCEMS salvo em disco: `jucems_leiloeiros.txt`
- [x] 55 leiloeiros Regular parseados do TXT
- [x] 60 leiloeiros merged com site JUCEMS online
- [x] 49 sites únicos identificados
- [x] CSV leiloeiros gerado: `csv/leiloeiros_jucems_2026-06-08.csv`
- [x] 593 imóveis coletados → `csv/imoveis_jucems_2026-06-08.csv`
- [x] SQLite: 514 inseridos (79 já existiam de scraping anterior)
- [x] PostgreSQL: 593 inseridos via `import_jucems_docker.py`
- [x] Classifier rodado: 590 classificados
- [x] `normalizar-cidades` aplicado
- [x] `deduplicar` aplicado: 327 únicos ativos
- [x] API reiniciada
- [ ] Geocodificar imóveis com estado incorreto
- [ ] Pré-filtrar domínios offline antes de próxima execução

---

## 35. Scraping JUCISRS — Leiloeiros Regulares do RS (jun/2026)

Coleta de imóveis dos leiloeiros credenciados pela **JUCISRS** (Junta Comercial do Estado do Rio Grande do Sul).
Fonte: `https://sistemas.jucisrs.rs.gov.br/leiloeiros/busca/listar` (POST com `CodMunicipio=0`).
Script: `scraper_jucisrs.py`.

### 35.1. Descoberta da API JUCISRS

A página raiz (`/leiloeiros/`) possui formulário com POST para `/busca/listar`:

```python
# GET direto retorna 500; necessário primeiro buscar cookie de sessão
sess = requests.Session()
sess.get('https://sistemas.jucisrs.rs.gov.br/leiloeiros/', verify=False)

# POST com CodMunicipio='0' retorna todos os municípios (244+ leiloeiros)
r = sess.post('https://sistemas.jucisrs.rs.gov.br/leiloeiros/busca/listar',
              data={'Nome': '', 'CodMunicipio': '0'}, verify=False)
r.encoding = 'latin-1'  # crítico: página em Latin-1
```

**Armadilha:** `GET /busca/listar` retorna `500 Database Error`. Necessário:
1. Fazer GET da home para obter `ci_session` (cookie)
2. Fazer POST com `CodMunicipio='0'` (todas as cidades)
3. Decodificar como `latin-1` (encoding do servidor)

### 35.2. Estrutura de dados no HTML

O HTML retornado não usa `<table>` — usa blocos delimitados por `<hr>`:

```
<b><font color="#A01A14">173</font> - ADEMIR MIGUEL CORRÊA</b>
www.correleiloes.com.br<br>
Posse : 06/08/2003<br>
RUA BORGES DE MEDEIROS, 415 - CANELA - RS<br>
CEP 95.680-000 Telefone : (54) 999738341<br>
e-Mail : correa@...<br>
<hr>
<b><font color="#A01A14">174</font> - CÍCERO VILAGRAN DA ROSA
<font color="#FF0000"> (Cancelado)</font></b>
...
<hr>
```

**Filtro para Regular:** blocos que **não contêm** `(Cancelado)` nem `(Suspenso)`.

```python
blocks = re.split(r'<hr>', html, flags=re.IGNORECASE)
for block in blocks[4:]:   # primeiros 4 são cabeçalho/formulário
    if 'cancelado' in block.lower(): continue
    if 'suspenso' in block.lower(): continue
    # → este bloco é Regular
```

### 35.3. Resultado da coleta

| Métrica | Valor |
|---|---|
| Leiloeiros Regular encontrados | 244 |
| Cancelados (filtrados) | 121 |
| Suspensos (filtrados) | 16 |
| Leiloeiros com site identificado | 183 |
| Leiloeiros sem site | 34 |
| Sites processados | 183 |
| Sites com imóveis ativos | 128 |
| Sites sem leilão ativo | 55 |
| Sites offline | 1 |
| Erros de rede | 0 |
| **Total imóveis coletados** | **3.946** |
| SQLite: inseridos | 3.629 (317 já existiam) |
| PostgreSQL: inseridos | 3.862 |
| Tempo total de scraping | ~203 min (3h23min) |
| CSV leiloeiros | `csv/leiloeiros_jucisrs_2026-06-08.csv` |
| CSV imóveis | `csv/imoveis_jucisrs_2026-06-08.csv` |

### 35.4. Distribuição por leiloeiro (top 20)

| Leiloeiro | Imóveis |
|---|---|
| DANIEL HAMOUI (dhleiloes.com.br) | 195 |
| IRANI FLORES (leilaobrasil.com.br) | 144 |
| GIANCARLO PETERLONGO LORENZINI (peterlongoleiloes.com.br) | 119 |
| EDUARDO VIVIAN (eduardovivian.com) | 105 |
| TIAGO TESSLER BLECHER (webleiloes.com.br) | 81 |
| LUCAS ANDREATTA DE OLIVEIRA (leiloariasmart.com.br) | 78 |
| CARMEN GOMES PIETOSO (pietosoleiloes.lel.br) | 76 |
| GILMAR THUME (gtleiloes.com.br) | 64 |
| MARCELO SOUZA SCHONARDIE (marceloleiloeiro.com.br) | 64 |
| JOSÉ CLÓVIS VAZ DE SOUZA (clovisleiloeiro.com.br) | 51 |
| DANIEL COSTA MÜLLER (mullerleiloes.com.br) | 51 |
| DANIEL ELIAS GARCIA (danielgarcialeiloes.com.br) | 47 |
| GUSTAVO EVALDO GAITSCH HUMOR (prhleiloes.com.br) | 45 |
| FRANCISCO HILLESHEIM (alemaoleiloeiro.com.br) | 44 |
| CATIELE BORGES LEFFA (leffaleiloes.com.br) | 43 |
| ... demais 113 leiloeiros | 1–42 cada |

### 35.5. Principais dificuldades enfrentadas

#### 35.5.1. Site retorna 500 com GET direto

**Problema:** `GET /leiloeiros/busca/listar` retorna `500 - A PHP Error was encountered`:
```
Undefined index: Nome (Model_leiloeiros.php, line 7)
```

**Causa:** O controller PHP espera os parâmetros `Nome` e `CodMunicipio` no corpo do POST.
Um GET simples não envia esses campos, causando o erro de índice indefinido.

**Solução:**
```python
# Obrigatório: sessão para cookie ci_session
sess = requests.Session()
sess.get(HOME_URL, verify=False)

# POST com CodMunicipio='0' = todas as cidades
r = sess.post(LISTAR_URL,
              data={'Nome': '', 'CodMunicipio': '0'},
              verify=False)
```

---

#### 35.5.2. Encoding Latin-1 na resposta

**Problema:** A página retorna encoding `LATIN1` (charset declarado no HTTP header).
Se lido como UTF-8, todos os caracteres acentuados ficam corrompidos (`Ã©`, `Ã§`, etc.).

**Solução:**
```python
r.encoding = 'latin-1'  # forçar antes de acessar r.text
```

---

#### 35.5.3. Sites falsos positivos: provedores de e-mail e ISPs

**Problema:** O parser extraía a URL de qualquer padrão `www.*.* ` no bloco HTML.
Três leiloeiros tinham registrado como "site" o URL do seu provedor de e-mail:

| Leiloeiro | URL registrada | Problema |
|---|---|---|
| LUIZ BARBOSA DE LIMA JUNIOR | `www.ymail.com` | Portal Yahoo Mail |
| NELSON BERTOLUCI SANTOS | `www.sinos.net` | ISP regional RS |
| VITOR HUGO ANTUNES FARIAS | `www.outlook.com.br` | Microsoft Outlook |

**Impacto:** Sites processados desnecessariamente, sem imóveis encontrados.

**Solução recomendada:** Expandir lista de domínios ignorados no parser:

```python
DOMINIOS_IGNORADOS = {
    "gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "ymail.com",
    "outlook.com.br", "terra.com.br", "uol.com.br", "bol.com.br",
    # ISPs regionais RS
    "sinos.net", "brturbo.com", "oi.com.br", "gvt.com.br",
    # Portais genéricos
    "facebook.com", "instagram.com", "whatsapp.com",
}
def _derivar_site_email(email: str) -> str | None:
    m = re.search(r'@([a-z0-9\-]+\.[a-z\.]+)', email.lower())
    if not m or m.group(1) in DOMINIOS_IGNORADOS: return None
    return f'https://www.{m.group(1)}'
```

---

#### 35.5.4. Alta proporção de sites JS-heavy (~40% precisam de Playwright)

**Problema:** ~75 dos 183 sites (~41%) retornaram HTML sem conteúdo de lotes
via HTTP simples (`requests`) e exigiram Playwright para render. Isso aumentou
o tempo médio de 3s/site (HTTP) para ~65s/site (Playwright), contribuindo para
o tempo total de 3h23min.

**Causa:** Tendência crescente de sites modernos de leilão usarem React/Next.js.
Sites mais novos (matrícula > 400) são mais propensos a JS-heavy.

**Sinais detectados automaticamente:**
```python
def is_js_heavy(html: str) -> bool:
    markers = ["__next_data__", "__nuxt__", "react-root", "vue-app",
               "ng-app", "window.__INITIAL_STATE__"]
    if any(m in html.lower() for m in markers): return True
    return len(BeautifulSoup(html, 'html.parser').get_text().strip()) < 300
```

**Solução recomendada:** Para scrapers futuros com muitos sites RS, executar
Playwright paralelizado com `asyncio` + `playwright.async_api`:
```python
async def scrape_all_async(leiloeiros: list[dict]) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [scrape_one_async(browser, l) for l in leiloeiros]
        return await asyncio.gather(*tasks, return_exceptions=True)
```

---

#### 35.5.5. Timeout em sites lentos ou instáveis

**Problema:** Alguns sites do RS apresentaram timeouts intermitentes:
- `leiloesdosul.com.br` — `ConnectTimeout`
- `rzleiloes.com.br` — `ReadTimeout`
- `sinos.net` — `ConnectTimeout`

**Causa:** Servidores com latência alta ou conexão instável. Alguns sites `.lel.br`
(domínio CFI) apresentaram instabilidade de DNS.

**Solução aplicada:** Fallback automático para Playwright quando HTTP falha:
```python
except Exception as e:
    log(f"[WARN] HTTP falhou ({type(e).__name__}). Playwright...")
    imoveis = scrape_playwright(lei, max_pags)
```

**Solução recomendada:** Retry com backoff exponencial antes de acionar Playwright:
```python
for attempt in range(3):
    try:
        r = sess.get(url, timeout=15, verify=False)
        break
    except requests.exceptions.Timeout:
        time.sleep(2 ** attempt)  # 1s, 2s, 4s
```

---

#### 35.5.6. Bloqueio de título `prhleiloes.com.br` (Playwright preso)

**Problema:** O site `www.prhleiloes.com.br` ficou com Playwright preso por
~10 minutos sem retornar conteúdo. Isso bloqueou a fila de scraping.

**Causa:** O site provavelmente usa proteção Cloudflare que não foi detectada
pelo `is_js_heavy()`, mas que bloqueia o Playwright headless sem stealth.

**Solução recomendada:** Adicionar timeout global por site e detectar Cloudflare:
```python
# Timeout global de 120s por site
try:
    imoveis, status = func_timeout(120, scrape_leiloeiro, args=(lei, max_pags))
except FunctionTimedOut:
    log(f"  [TIMEOUT] {lei['site']} ultrapassou 120s")
    imoveis, status = [], "timeout"
```

---

#### 35.5.7. Nomes duplicados com matrícula diferente

**Problema:** O parser retornou `GUSTAVO EVALDO GAITSCH HUMOR` com nome
truncado incorretamente pela regex de corte (leu `HUMOR` como parte do nome).

**Causa:** A regex `r"(\d+)\s*[-–]\s*([A-ZÁ...][A-Za-z...]+)"` não capturou
o nome completo de leiloeiros com nomes longos terminando antes de "Posse:".

**Exemplo observado:**
```
Bloco: "85 - GUSTAVO EVALDO GAITSCH HUMOR Posse : ..."
Nome extraído: "GUSTAVO EVALDO GAITSCH HUMOR"  ← correto
```
Neste caso o corte funcionou, mas `HUMOR` ficou no nome.

**Solução recomendada:** Usar stop-words mais precisos para o corte:
```python
nome = re.sub(r'\s+(Posse|www\.|http|Rua |Av\.|CEP|Fone|e-Mail)\s*:?.*$',
              '', nome, flags=re.IGNORECASE | re.DOTALL).strip()
```

---

### 35.6. Ordem de execução

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes"

# 1. Scraping (inclui importação automática para SQLite e PostgreSQL)
python scraper_jucisrs.py --max-paginas 8

# 2. Pós-processamento (se necessário manualmente)
docker exec leilao_api bash -c "cd /app && python run.py classificar --limite 5000"
docker exec leilao_api bash -c "cd /app && python run.py deduplicar"
docker restart leilao_api
```

### 35.7. Arquivos criados nesta sessão

```
leiloes/
├── scraper_jucisrs.py                  ← scraper principal
├── import_jucisrs_docker.py            ← script de importação (gerado dinamicamente)
├── scraper_jucisrs.log                 ← log completo (203 min)
├── scraper_jucisrs_progress.json       ← progresso em tempo real
└── csv/
    ├── leiloeiros_jucisrs_2026-06-08.csv ← 244 leiloeiros Regular
    └── imoveis_jucisrs_2026-06-08.csv   ← 3.946 imóveis
```

### 35.8. Checklist de execução

- [x] POST com sessão para JUCISRS: 244 Regular identificados
- [x] 183 sites únicos com site identificado
- [x] CSV leiloeiros: `csv/leiloeiros_jucisrs_2026-06-08.csv` (244 registros)
- [x] 3.946 imóveis coletados → `csv/imoveis_jucisrs_2026-06-08.csv`
- [x] SQLite: 3.629 inseridos
- [x] PostgreSQL: 3.862 inseridos via container
- [x] Classifier: 206 classificados
- [x] Deduplicar: 0 duplicatas (banco já consistente)
- [x] API reiniciada
- [ ] Expandir DOMINIOS_IGNORADOS para evitar ymail/sinos.net/outlook
- [ ] Adicionar timeout global de 120s por site para evitar Playwright preso
- [ ] Implementar Playwright async paralelo para reduzir tempo de 3h para ~40min

---

## 36. Checagem e correção de importação incompleta para o PostgreSQL

Toda vez que um scraper termina com "X imóveis coletados" mas o banco mostra menos,
o problema tem **três causas documentadas** — todas resolvidas pelo padrão desta seção.

---

### 36.1. As três causas de perda de dados na importação

#### Causa 1: Rollback em cascata (a mais grave)

**Sintoma:** log diz "N inseridos" mas banco tem muito menos.

O psycopg2 usa transações explícitas. Quando `conn.rollback()` é chamado ao
tratar um erro, ele **desfaz toda a transação pendente** — não só a linha que falhou.
Com commits a cada 100 linhas, cada erro pode apagar até 99 linhas anteriores.

```python
# PADRÃO ERRADO — usado em versões antigas dos scrapers
for r in rows:
    try:
        cur.execute(INSERT_SQL, params)
        ins += 1
    except Exception as e:
        err += 1
        conn.rollback()   # ← apaga até 99 linhas não commitadas!
    if ins % 100 == 0:
        conn.commit()
conn.commit()
```

```python
# PADRÃO CORRETO — SAVEPOINT isola a falha na linha atual
for r in rows:
    cur.execute('SAVEPOINT sp')
    try:
        cur.execute(INSERT_SQL, params)
        cur.execute('RELEASE SAVEPOINT sp')
        ins += 1
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT sp')
        cur.execute('RELEASE SAVEPOINT sp')
        err += 1
    if (ins + err) % 200 == 0:
        conn.commit()
conn.commit()
```

> **Regra:** nunca use `conn.rollback()` dentro de um loop de inserção.
> Use sempre `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` para isolar falhas por linha.

---

#### Causa 2: NUL bytes (0x00) em campos de texto

**Sintoma:** `psycopg2.errors.StringDataRightTruncation` ou
`A string literal cannot contain NUL (0x00) characters.`

HTML de alguns sites embute bytes nulos em títulos, descrições e URLs.
O psycopg2 rejeita qualquer string com `\x00`.

```python
# PADRÃO CORRETO — limpar NUL antes de qualquer INSERT
def clean(v: str | None, max_len: int | None = None) -> str:
    s = str(v or '').replace('\x00', '')   # remove NUL bytes
    return s[:max_len] if max_len else s
```

Aplicar em todos os campos de texto antes de passar para `cur.execute()`.

---

#### Causa 3: Overflow numérico em campos NUMERIC

**Sintoma:** `numeric field overflow — A field with precision 15, scale 2 must round to an absolute value less than 10^13.`

Valores de preço absurdos (ex.: `10000000000000.0`) surgem quando o scraper
captura uma área ou código como preço (parsing errado do HTML).

```python
# Validar antes de inserir
MAX_PRICE = 9_999_999_999_999.99   # limite do NUMERIC(15,2)

def _d(v) -> float | None:
    try:
        f = float(Decimal(str(v).replace(',', '.'))) if v else None
        if f is not None and abs(f) > MAX_PRICE:
            return None   # descarta valor impossível
        return f
    except Exception:
        return None
```

---

### 36.2. Script `verificar_importacao.py` — checagem obrigatória pós-scraping

Roda ao final de **todo scraping**, compara o CSV com o banco e reimporta
automaticamente as linhas faltantes usando o padrão correto (savepoints + limpeza).

Localização: `C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes\verificar_importacao.py`

**Uso:**
```powershell
# Verifica e reimporta se necessário (modo padrão)
python verificar_importacao.py

# Só verifica, não importa
python verificar_importacao.py --so-verificar

# Força reimportação de todos (mesmo os que já estão no banco)
python verificar_importacao.py --forcar
```

**O que o script faz:**
1. Lê o CSV mais recente de `csv/imoveis_*.csv`
2. Consulta o banco: `SELECT id_externo FROM imoveis WHERE fonte_id = ?`
3. Calcula `faltantes = ids_csv - ids_banco`
4. Se `faltantes > 0`: copia CSV para o container e reimporta usando savepoints
5. Repete a verificação e reporta o resultado final

**Critérios de sucesso:**
- `faltantes == 0` após reimportação → OK
- `faltantes > 0` por overflow/dado inválido → reporta as linhas problemáticas
- `faltantes > 0` por duplicata de `id_externo` → comportamento esperado (mesmo URL = mesmo hash)

---

### 36.3. Código completo do `verificar_importacao.py`

```python
"""
verificar_importacao.py
=======================
Checagem pós-scraping: compara CSV com banco e reimporta faltantes.
Executar após qualquer scraping de leiloeiros.

Uso:
    python verificar_importacao.py [--so-verificar] [--forcar] [--fonte NOME] [--csv ARQUIVO]
"""
import csv, os, sys, subprocess, argparse
from pathlib import Path
from decimal import Decimal

BASE    = Path(r"C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR = BASE / "csv"


def _d(v):
    try:
        f = float(Decimal(str(v).replace(',', '.'))) if v else None
        return f if f is None or abs(f) <= 9_999_999_999_999.99 else None
    except Exception:
        return None


def clean(v, max_len=None):
    s = str(v or '').replace('\x00', '')
    return s[:max_len] if max_len else s


def detectar_fonte(csv_path: Path) -> str:
    """Detecta o nome da fonte pelo nome do arquivo CSV."""
    nome = csv_path.stem.lower()
    for junta in ['jucisrs','jucems','jucesc','jucerja','jucemat','jucesp']:
        if junta in nome:
            return junta.upper()
    if 'caixa' in nome: return 'Caixa'
    if 'leiloesjudiciais' in nome: return 'LeilõesJudiciais'
    if 'bomvalor' in nome: return 'BomValor'
    return Path(nome).stem.replace('imoveis_','').replace('_',' ').title()


def psql_query(sql: str) -> list[str]:
    """Executa query no container e retorna lista de linhas."""
    proc = subprocess.run(
        ['docker','exec','leilao_postgres','psql','-U','leilao','-d','leilao_db',
         '--no-align','--tuples-only','-c', sql],
        capture_output=True, text=True, encoding='utf-8', timeout=30
    )
    return [l.strip() for l in proc.stdout.splitlines() if l.strip()]


def verificar(csv_path: Path, fonte_nome: str) -> dict:
    """Retorna {'total_csv','ids_csv','ids_banco','faltantes','extras'}."""
    rows = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8-sig')))
    ids_csv = {clean(r.get('id_externo',''), 200) for r in rows if r.get('id_externo')}

    # IDs no banco para esta fonte
    ids_banco_raw = psql_query(
        f"SELECT id_externo FROM imoveis WHERE fonte_id="
        f"(SELECT id FROM fontes WHERE nome='{fonte_nome}' LIMIT 1)"
    )
    ids_banco = set(ids_banco_raw)

    faltantes = ids_csv - ids_banco
    extras    = ids_banco - ids_csv   # no banco mas não no CSV (importações anteriores)

    return {
        'total_csv':   len(rows),
        'total_unicos_csv': len(ids_csv),
        'ids_csv':     ids_csv,
        'ids_banco':   ids_banco,
        'faltantes':   faltantes,
        'extras':      extras,
    }


# Script que roda DENTRO do container Docker
INNER_SCRIPT = '''
import csv, os, sys
from decimal import Decimal
from pathlib import Path

CSV_FILE = '/tmp/_reimport.csv'
FONTE_NOME = open('/tmp/_reimport_fonte.txt').read().strip()

rows = list(csv.DictReader(open(CSV_FILE, newline='', encoding='utf-8-sig')))

def _d(v):
    try:
        f = float(Decimal(str(v).replace(',', '.'))) if v else None
        return f if f is None or abs(f) <= 9_999_999_999_999.99 else None
    except Exception: return None

def clean(v, max_len=None):
    s = str(v or '').replace('\\x00', '')
    return s[:max_len] if max_len else s

import psycopg2
db_url = os.environ.get('DATABASE_URL_SYNC','postgresql://leilao:leilao123@postgres:5432/leilao_db')
db_url = db_url.replace('postgresql+asyncpg://','postgresql://')
conn = psycopg2.connect(db_url)
cur = conn.cursor()

cur.execute(f"INSERT INTO fontes (nome,url_base,ativo,criado_em) VALUES ('{FONTE_NOME}','',true,NOW()) ON CONFLICT (nome) DO NOTHING")
cur.execute(f"SELECT id FROM fontes WHERE nome='{FONTE_NOME}' LIMIT 1")
FONTE_ID = cur.fetchone()[0]
conn.commit()

TIPOS_I = {'APARTAMENTO','CASA','TERRENO','COMERCIAL','RURAL','GALPAO','SALA','VAGA','OUTRO'}
TIPOS_L = {'JUDICIAL','EXTRAJUDICIAL','BANCARIO'}
ins = upd = err = 0

SQL = """INSERT INTO imoveis (
    fonte_id,id_externo,titulo,descricao,url_original,
    tipo_imovel,tipo_leilao,status,categoria,
    cidade,estado,cep,endereco_completo,
    valor_minimo,valor_avaliacao,area_total,quartos,
    data_primeiro_leilao,data_segundo_leilao,
    imagem_principal,arquivos,numero_processo,
    leiloeiro,ativo,classificado,geocodificado,criado_em,atualizado_em
) VALUES (%s,%s,%s,%s,%s,%s,%s,'ABERTO','IMOVEL',
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          true,false,false,NOW(),NOW())
ON CONFLICT (fonte_id,id_externo) DO UPDATE SET
    titulo=EXCLUDED.titulo, valor_minimo=EXCLUDED.valor_minimo,
    data_primeiro_leilao=EXCLUDED.data_primeiro_leilao,
    imagem_principal=EXCLUDED.imagem_principal,
    arquivos=EXCLUDED.arquivos, atualizado_em=NOW()"""

for i, r in enumerate(rows):
    ti = clean(r.get('tipo_imovel','outro')).upper()
    if ti not in TIPOS_I: ti = 'OUTRO'
    tl = clean(r.get('tipo_leilao','extrajudicial')).upper()
    if tl not in TIPOS_L: tl = 'EXTRAJUDICIAL'
    id_ext = clean(r.get('id_externo',''), 200)
    if not id_ext: continue
    cur.execute('SAVEPOINT sp')
    try:
        cur.execute(SQL, (
            FONTE_ID, id_ext,
            clean(r.get('titulo',''),500), clean(r.get('descricao',''),500),
            clean(r.get('url_original',''),1000), ti, tl,
            clean(r.get('cidade',''),200), clean(r.get('estado','RS'),2),
            clean(r.get('cep',''),10), clean(r.get('endereco_completo',''),500),
            _d(r.get('valor_minimo')), _d(r.get('valor_avaliacao')),
            _d(r.get('area_total')),
            int(r['quartos']) if r.get('quartos') else None,
            clean(r.get('data_primeiro_leilao','')) or None,
            clean(r.get('data_segundo_leilao','')) or None,
            clean(r.get('imagem_principal',''),1000),
            clean(r.get('arquivos','[]'),4000),
            clean(r.get('numero_processo',''),100),
            clean(r.get('leiloeiro',''),300),
        ))
        cur.execute('RELEASE SAVEPOINT sp')
        ins += 1
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT sp')
        cur.execute('RELEASE SAVEPOINT sp')
        err += 1
        if err <= 5: print(f'  ERR [{i}] {str(e)[:100]}')
    if (i+1) % 200 == 0:
        conn.commit()

conn.commit(); cur.close(); conn.close()
print(f'[OK] {ins} processados, {err} erros')
'''


def reimportar(csv_path: Path, fonte_nome: str, ids_faltantes: set) -> tuple[int, int]:
    """Reimporta apenas as linhas faltantes. Retorna (inseridos, erros)."""
    rows_todas = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8-sig')))
    rows_faltantes = [r for r in rows_todas
                      if clean(r.get('id_externo',''), 200) in ids_faltantes]

    if not rows_faltantes:
        return 0, 0

    # Salva CSV temporário só com as linhas faltantes
    tmp_csv = BASE / '_reimport_tmp.csv'
    with open(tmp_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=rows_todas[0].keys())
        w.writeheader()
        w.writerows(rows_faltantes)

    # Salva script e fonte
    script_path = BASE / '_reimport_inner.py'
    script_path.write_text(INNER_SCRIPT, encoding='utf-8')
    (BASE / '_reimport_fonte.txt').write_text(fonte_nome, encoding='utf-8')

    # Copia para container e executa
    subprocess.run(['docker','cp', str(tmp_csv),   'leilao_api:/tmp/_reimport.csv'], check=True)
    subprocess.run(['docker','cp', str(script_path),'leilao_api:/tmp/_reimport_inner.py'], check=True)
    subprocess.run(['docker','cp', str(BASE/'_reimport_fonte.txt'),'leilao_api:/tmp/_reimport_fonte.txt'], check=True)

    proc = subprocess.run(
        ['docker','exec','leilao_api','python','/tmp/_reimport_inner.py'],
        capture_output=True, text=True, encoding='utf-8', timeout=600
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print('[ERR]', proc.stderr[:200])

    # Lê resultado
    for linha in proc.stdout.splitlines():
        if '[OK]' in linha:
            parts = linha.replace('[OK]','').split(',')
            ins = int(parts[0].strip().split()[0]) if parts else 0
            err = int(parts[1].strip().split()[0]) if len(parts) > 1 else 0
            return ins, err
    return 0, 0


def main():
    ap = argparse.ArgumentParser(description='Checagem pós-scraping CSV vs banco')
    ap.add_argument('--so-verificar', action='store_true', help='Só verifica, não reimporta')
    ap.add_argument('--forcar', action='store_true', help='Reimporta todos (mesmo existentes)')
    ap.add_argument('--fonte', type=str, help='Nome da fonte no banco (ex: JUCISRS)')
    ap.add_argument('--csv', type=str, help='Caminho do CSV (padrão: mais recente em /csv)')
    args = ap.parse_args()

    # Encontra CSV
    if args.csv:
        csv_path = Path(args.csv)
    else:
        csvs = sorted(CSV_DIR.glob('imoveis_*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not csvs:
            print('[ERRO] Nenhum CSV encontrado em', CSV_DIR)
            sys.exit(1)
        csv_path = csvs[0]

    fonte_nome = args.fonte or detectar_fonte(csv_path)
    print(f'[INFO] CSV:   {csv_path.name}')
    print(f'[INFO] Fonte: {fonte_nome}')

    # Verificação inicial
    print('\n--- Verificação inicial ---')
    resultado = verificar(csv_path, fonte_nome)
    total_csv   = resultado['total_csv']
    unicos_csv  = resultado['total_unicos_csv']
    n_banco     = len(resultado['ids_banco'])
    n_faltantes = len(resultado['faltantes'])
    n_extras    = len(resultado['extras'])
    duplicatas  = total_csv - unicos_csv

    print(f'  Total CSV:            {total_csv}')
    print(f'  Únicos (id_externo):  {unicos_csv}  ({duplicatas} duplicatas no CSV = mesmo URL)')
    print(f'  No banco (fonte):     {n_banco}')
    print(f'  Faltando no banco:    {n_faltantes}')
    print(f'  Extras no banco:      {n_extras}  (de importações anteriores)')

    if n_faltantes == 0 and not args.forcar:
        print('\n[OK] Todos os imóveis únicos do CSV estão no banco.')
        return

    if args.so_verificar:
        print(f'\n[ATENÇÃO] {n_faltantes} faltantes. Use sem --so-verificar para reimportar.')
        return

    # Reimportação
    ids_para_reimportar = resultado['faltantes'] if not args.forcar else resultado['ids_csv']
    print(f'\n--- Reimportando {len(ids_para_reimportar)} linhas faltantes ---')
    ins, err = reimportar(csv_path, fonte_nome, ids_para_reimportar)
    print(f'  Reimportados: {ins} | Erros persistentes: {err}')

    # Verificação final
    print('\n--- Verificação final ---')
    resultado2 = verificar(csv_path, fonte_nome)
    n_faltantes2 = len(resultado2['faltantes'])
    n_banco2     = len(resultado2['ids_banco'])

    print(f'  No banco agora:    {n_banco2}')
    print(f'  Ainda faltando:    {n_faltantes2}')

    if n_faltantes2 == 0:
        print('\n[OK] Banco sincronizado com o CSV.')
    elif n_faltantes2 == err:
        print(f'\n[AVISO] {n_faltantes2} linha(s) com dados inválidos (overflow, NUL irrecuperável).')
        print('  Estas linhas têm erros nos dados de origem e não podem ser inseridas.')
        # Mostra quais
        rows_inv = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8-sig')))
        for r in rows_inv:
            if clean(r.get('id_externo',''),200) in resultado2['faltantes']:
                print(f'  → [{r.get("id_externo","")[:20]}] {r.get("leiloeiro","")} | '
                      f'preco={r.get("valor_minimo","")} | titulo={r.get("titulo","")[:40]}')
    else:
        print(f'\n[FALHA] {n_faltantes2} linhas ainda faltando após reimportação.')
        print('  Execute novamente ou verifique o log do container:')
        print('  docker logs leilao_api --tail 50')

    # Pós-processamento
    if n_banco2 > n_banco:
        print('\n[Pós-processamento] Classificando e deduplicando novos registros...')
        subprocess.run(['docker','exec','leilao_api','bash','-c',
                        'cd /app && python run.py classificar --limite 2000'],
                       capture_output=True, timeout=120)
        subprocess.run(['docker','exec','leilao_api','bash','-c',
                        'cd /app && python run.py deduplicar'],
                       capture_output=True, timeout=60)
        subprocess.run(['docker','restart','leilao_api'], capture_output=True, timeout=60)
        print('  Feito.')


if __name__ == '__main__':
    main()
```

---

### 36.4. Como integrar no pipeline de cada scraper

**Passo único:** adicionar no final de todo scraper, depois da importação:

```python
# Ao final do main() de qualquer scraper
import subprocess
print("\n[Checagem pós-scraping] Verificando integridade...")
proc = subprocess.run(
    ["python", "verificar_importacao.py",
     "--fonte", NOME_FONTE,         # ex.: "JUCISRS", "JUCEMS", "JUCESC"
     "--csv",   str(csv_imoveis)],  # caminho do CSV gerado
    capture_output=False,           # mostra output em tempo real
    timeout=600
)
```

Ou via linha de comando, como checagem manual após qualquer scraping:

```powershell
cd "C:\Users\arthur\OneDrive\Documentos\Cursor\leiloes"

# Verifica o CSV mais recente (detecção automática de fonte)
python verificar_importacao.py

# Verifica um CSV específico
python verificar_importacao.py --csv csv\imoveis_jucisrs_2026-06-08.csv

# Apenas mostra divergência sem reimportar
python verificar_importacao.py --so-verificar

# Força reimportação completa (útil se o banco foi recriado)
python verificar_importacao.py --forcar
```

---

### 36.5. Padrão de INSERT correto para todos os scrapers futuros

Todo script de importação para PostgreSQL deve seguir este padrão:

```python
def importar_postgres_correto(rows: list[dict], fonte_id: int, conn):
    """
    Importação robusta com:
    - SAVEPOINT por linha (falha isola só a linha atual)
    - Limpeza de NUL bytes antes de inserir
    - Validação de overflow numérico
    - Commit a cada 200 linhas
    """
    MAX_PRICE = 9_999_999_999_999.99
    cur = conn.cursor()
    ins = upd = err = 0

    for i, r in enumerate(rows):
        id_ext = clean(r.get('id_externo', ''), 200)
        if not id_ext:
            continue

        cur.execute('SAVEPOINT sp')
        try:
            cur.execute(INSERT_SQL, build_params(r, fonte_id))
            cur.execute('RELEASE SAVEPOINT sp')
            ins += 1
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT sp')
            cur.execute('RELEASE SAVEPOINT sp')
            err += 1
            if err <= 3:
                print(f'  ERR [{i}] {str(e)[:120]}')
        finally:
            if (i + 1) % 200 == 0:
                conn.commit()
                print(f'  {i+1}/{len(rows)}: {ins} ins, {err} err')

    conn.commit()
    cur.close()
    print(f'  Total: {ins} inseridos, {err} erros')
    return ins, err


def clean(v, max_len=None):
    """Remove NUL bytes e trunca."""
    s = str(v or '').replace('\x00', '')
    return s[:max_len] if max_len else s


def _d(v):
    """Converte para float validando overflow NUMERIC(15,2)."""
    try:
        f = float(Decimal(str(v).replace(',', '.'))) if v else None
        if f is not None and abs(f) > 9_999_999_999_999.99:
            return None   # descarta: dado impossível
        return f
    except Exception:
        return None
```

---

### 36.6. Diagnóstico rápido de divergência CSV vs banco

```powershell
# 1. Quantos estão no CSV?
(Get-Content "csv\imoveis_jucisrs_*.csv" | Measure-Object -Line).Lines  # -1 para o header

# 2. Quantos estão no banco?
docker exec leilao_postgres psql -U leilao -d leilao_db -c `
  "SELECT COUNT(*) FROM imoveis WHERE fonte_id=(SELECT id FROM fontes WHERE nome='JUCISRS');"

# 3. Quais ids_externo estão no CSV mas não no banco?
python -c "
import csv, subprocess
rows = list(csv.DictReader(open('csv/imoveis_jucisrs_2026-06-08.csv', encoding='utf-8-sig')))
ids_csv = {r.get('id_externo','') for r in rows if r.get('id_externo')}
proc = subprocess.run(['docker','exec','leilao_postgres','psql','-U','leilao','-d','leilao_db',
    '--no-align','--tuples-only','-c',
    \"SELECT id_externo FROM imoveis WHERE fonte_id=(SELECT id FROM fontes WHERE nome='JUCISRS')\"],
    capture_output=True, text=True)
ids_banco = set(proc.stdout.splitlines())
faltantes = ids_csv - ids_banco
print(f'Faltando: {len(faltantes)} de {len(ids_csv)} unicos no CSV')
"

# 4. Reimportar faltantes
python verificar_importacao.py
```

---

### 36.7. Checklist pós-scraping (obrigatório após todo scraping)

Adicionar ao final do checklist de qualquer seção de scraping deste documento:

- [ ] **Executar checagem:** `python verificar_importacao.py`
- [ ] **Confirmar resultado:** `[OK] Todos os imóveis únicos do CSV estão no banco.`
- [ ] Se `faltantes > 0` após a reimportação automática: verificar se são dados inválidos (overflow, NUL irrecuperável) — esses são esperados e documentar na seção do scraper
- [ ] Se `faltantes > 0` por outro motivo: verificar `docker logs leilao_api --tail 50` e abrir issue

---

### 36.8. Raiz dos erros (resumo executivo)

| Erro | Causa | Solução |
|---|---|---|
| **Rollback em cascata** | `conn.rollback()` dentro de loop desfaz toda transação pendente | `SAVEPOINT sp` / `ROLLBACK TO SAVEPOINT sp` por linha |
| **NUL bytes (0x00)** | HTML scrapeado embute `\x00` em strings | `s.replace('\x00','')` antes de INSERT |
| **Overflow numérico** | Parser captura código/área como preço | Validar `abs(f) <= 9_999_999_999_999.99` |
| **Duplicatas id_externo** | Mesmo URL visitado 2x por leiloeiros com site compartilhado | Comportamento esperado — ON CONFLICT DO UPDATE |


---

## 37. Scraping JUCEES (ES) — Relatório de dificuldades (2026-06-08 14:55)

### 37.1. Resumo da execução

| Métrica | Valor |
|---|---|
| Leiloeiros REGULAR encontrados | 64 |
| Leiloeiros com site | 41 |
| Sites com imóveis | 22 |
| Sites sem leilão ativo | 19 |
| Sites com erro / offline | 0 |
| Total de imóveis coletados | 758 |
| CSV gerado | `csv/leiloeiros_jucees_2026-06-08.csv` |
| CSV imóveis | `csv/imoveis_jucees_2026-06-08.csv` |

### 37.2. Imóveis por leiloeiro

| Leiloeiro | Site | Imóveis |
|---|---|---|
| TIAGO TESSLER BLECHER | https://www.webleiloes.com.br | 175 |
| IRANI FLORES | https://www.leilaobrasil.com.br | 137 |
| MARCO ANTONIO BARBOSA DE OLIVEIRA JUNIOR | https://www.marcoantonioleiloeiro.com.br | 74 |
| DANIEL MELO CRUZ | https://www.grupolance.com.br | 71 |
| SUED PETER BASTOS DYNA | https://www.suedpeterleiloes.com.br/ | 47 |
| DANIEL ELIAS GARCIA | https://danielgarcialeiloes.com.br/ | 46 |
| DORA PLAT | https://www.portalzuk.com.br | 46 |
| JOSÉ SÉRGIO DELLA GIUSTINA | https://www.macedoleiloes.com.br | 33 |
| PAULO CESAR AGOSTINHO | https://www.agostinholeiloes.com.br/ | 24 |
| LILIANE DE NARDE SALLES | https://www.lilianecorretora.com.br | 21 |
| RUDIVAL ALMEIDA GOMES JÚNIOR | https://www.rjleiloes.com.br | 20 |
| ALEX WILLIAN HOPPE | https://www.hoppeleiloes.com.br/ | 19 |
| DAVI BORGES DE AQUINO | https://www.alfaleiloes.com | 12 |
| EDUARDO SCHMITZ | https://www.clicleiloes.com.br | 10 |
| GUSTAVO MARTINS ROCHA | https://www.grleiloes.com | 8 |
| MAURO COLODETE | https://colodeteleiloes.com.br/ | 6 |
| ALEXANDRE BUAIZ NETO | https://www.buaizleiloes.com.br/ | 3 |
| BRENNO DE FIGUEIREDO PORTO | https://www.portoleiloes.com.br/ | 2 |
| HIDIRLENE DUSZEIKO | https://www.hdleiloes.com.br/ | 1 |
| ALESSANDRO DE ASSIS TEIXEIRA | https://www.alessandroteixeiraleiloes.com.br | 1 |
| ERICK SOARES TELES | https://www.teza.com.br | 1 |
| MARCOS RODRIGO CUSTODIO SOARES | https://www.custodioleiloes.com.br | 1 |
| DJANIR DA RÓS | https://www.djanirleiloes.com.br/ | 0 |
| ANTONIO FREIRE DE PAIVA ALMEIDA | https://www.publicjud.com.br | 0 |
| ORLANDO LOPES FERNANDES | https://www.leilobras.lel.br/ | 0 |
| SÉRGIO DE PAULA PEREIRA | https://www.esleiloes.com.br/ | 0 |
| PATRÍCIA C. ALMEIDA | — | 0 |
| MARIA AMÉLIA DYNA DE SOUZA | — | 0 |
| MAURO CESAR ROCHA | http://www.leilofacil.lel.br/ | 0 |
| GABRIEL FARDIN PEREIRA | https://www.vixleiloes.com.br/ | 0 |
| AYRTON DE SOUZA PORTO FILHO | https://www.gestaodeleiloes.com.br/ | 0 |
| PIETRANGELO ROSALÉM | — | 0 |
| RENAN NERIS DA SILVA | https://www.renannerisleiloeiro.com.br/ | 0 |
| FLÁVIA DE OLIVEIRA ROCHA | https://www.leilofacil.lel.br/ | 0 |
| CAROLINE DE SOUSA RIBAS | — | 0 |
| ALEXSANDER PRETTI DOMINGOS | — | 0 |
| SANDRA DE FÁTIMA SANTOS | — | 0 |
| RONALD DE FREITAS MOREIRA | — | 0 |
| LUCAS RAFAEL ANTUNES MOREIRA | — | 0 |
| FERNANDO CAETANO MOREIRA FILHO | — | 0 |
| JONAS GABRIEL ANTUNES MOREIRA | — | 0 |
| GUSTAVO BOLZAN | https://www.gbleiloes.com.br/ | 0 |
| MARCUS ALLAIN DE OLIVEIRA BARBOSA | https://www.maleiloesro.com.br | 0 |
| PÂMELA DE SOUZA ALVES | — | 0 |
| RUAM CARLOS CHAVES GOTARDO | https://www.serranaleiloes.com.br | 0 |
| RENATO SCHLOBACH MOYSES | https://www.majudicial.com.br | 0 |
| GUSTAVO MORETTO GUIMARÃES DE OLIVEIRA | https://www.gustavomorettoleiloeiro.com.br | 0 |
| CAIO DE CARVALHO BORGES | https://www.cb-leiloeiro.com.br | 0 |
| ESTEVÃO STRINI CAMILO | — | 0 |
| JONAS RYMER | — | 0 |
| CARLA KARINE SANTOS AGOSTINHO | — | 0 |
| VICTOR DE ALMEIDA DOMINGUES CUNHA | https://www.almeidacunha.com | 0 |
| THIECO WAYNER MOZART MIGUEL GALVÃO | — | 0 |
| JOAO RENATO LAHAS DI CHIARA | — | 0 |
| MATHEUS WERNECK DE OLIVEIRA SANTOS | — | 0 |
| BRUNO BIRSCHNER LUBE | — | 0 |
| LUIZ ROBERTO DE OLIVEIRA BRENNEKEN | https://www.lubreleiloes.com.br | 0 |
| MANUELA MASAI VILAR VIEIRA DO NASCIMENTO | — | 0 |
| GIOVANA MARQUES COELHO BASTOS | — | 0 |
| SARA CORONA JUNQUEIRA | https://www.leiloescapixaba.com.br | 0 |
| MARCELO SEPULCRI VALADARES | — | 0 |
| ELIZABETH DE CARVALHO BORGES | https://www.vendaemgaragem.com | 0 |
| LUIS OTAVIO MARCOLINO SHINKAWA | — | 0 |
| COSME MARTINS | — | 0 |

### 37.3. Dificuldades encontradas

#### Bloqueio Cloudflare / WAF (403) (1 ocorrência)

- `https://www.vendaemgaragem.com` — ELIZABETH DE CARVALHO BORGES

#### Erro HTTP (404, 503, etc.) (1 ocorrência)

- `https://www.serranaleiloes.com.br` — RUAM CARLOS CHAVES GOTARDO

#### Falha na requisição HTTP (1 ocorrência)

- `HTTP falhou mesmo sem SSL: HTTPSConnectionPool(host='www.lubreleiloes.com.br', p` — LUIZ ROBERTO DE OLIVEIRA BRENNEKEN

#### Erro ao inserir no PostgreSQL (15 ocorrências)

- `[WinError 206] O nome do arquivo ou a extensão é muito grande`
- `[WinError 206] O nome do arquivo ou a extensão é muito grande`
- `[WinError 206] O nome do arquivo ou a extensão é muito grande`
- `[WinError 206] O nome do arquivo ou a extensão é muito grande`
- `[WinError 206] O nome do arquivo ou a extensão é muito grande`
- *(+10 ocorrências omitidas)*

#### Site acessado mas sem imóveis encontrados (13 ocorrências)

- `https://www.djanirleiloes.com.br/` — DJANIR DA RÓS
- `https://www.publicjud.com.br` — ANTONIO FREIRE DE PAIVA ALMEIDA
- `https://www.leilobras.lel.br/` — ORLANDO LOPES FERNANDES
- `https://www.esleiloes.com.br/` — SÉRGIO DE PAULA PEREIRA
- `https://www.vixleiloes.com.br/` — GABRIEL FARDIN PEREIRA
- *(+8 ocorrências omitidas)*

#### Site do leiloeiro offline / DNS inválido (3 ocorrências)

- `Conexão recusada / site offline: http://www.leilofacil.lel.br/` — MAURO CESAR ROCHA
- `Conexão recusada / site offline: https://www.leilofacil.lel.br/` — FLÁVIA DE OLIVEIRA ROCHA
- `Conexão recusada / site offline: https://www.leiloescapixaba.com.br` — SARA CORONA JUNQUEIRA

#### Erro de certificado SSL (1 ocorrência)

- `Erro SSL em https://www.lubreleiloes.com.br: HTTPSConnectionPool(host='www.lubre` — LUIZ ROBERTO DE OLIVEIRA BRENNEKEN

### 37.4. Sugestões de correção

| Problema | Causa | Correção sugerida |
|---|---|---|
| Sites sem leilão ativo | Leiloeiro sem eventos abertos no momento | Reagendar scraping; adicionar monitoramento periódico |
| Site offline / DNS inválido | Site encerrado ou URL desatualizada | Verificar URL manualmente; contatar leiloeiro; atualizar PDF da JUCEES |
| Cloudflare / WAF (403) | Proteção anti-bot ativa | Usar FlareSolverr (Docker :8191) — ver **seção 14** deste guia |
| Erro SSL | Certificado inválido ou expirado | Já contornado com `verify=False`; avisar o leiloeiro |
| JS-heavy sem imóveis (Playwright) | SPA carrega dados via API interna não interceptada | Inspecionar DevTools → XHR; criar extrator dedicado com `page.on('response')` |
| Leiloeiros sem site | Campo Site em branco na JUCEES | Derivar site do e-mail (domínio não-genérico); buscar manualmente |
| Leiloeiros 2025 não na Relação Regulares | PDF pode estar desatualizado | Fazer scraping direto do site https://leiloeiros.jucees.es.gov.br/ com filtro `regular` |
| Preços não extraídos | HTML sem padrão `R$` ou preço em atributo JS | Ampliar janela de regex; interceptar JSON da API interna |
| Imagens com URL relativa quebrada | `urljoin` não resolve alguns CDNs | Adicionar `data-src` e `data-lazy-src` ao extrator de imagens |
| PostgreSQL: fonte_id não encontrado | Container não rodando ou tabela `fontes` ausente | Verificar `docker ps`; rodar migration antes de importar |

### 37.5. Próximos passos

1. Para sites com Cloudflare: instalar FlareSolverr e adaptar `scrape_site_playwright` conforme seção 14.
2. Rodar `python run.py classificar --limite 5000` no container para classificar os imóveis importados.
3. Rodar `python run.py deduplicar` para remover duplicatas.
4. Rodar `python run.py baixar-docs --limite 200` para baixar PDFs (editais/matrículas).
5. Agendar re-scraping semanal com `CronCreate` para manter base atualizada.


---

## Sessão de Scraping JUCEPAR — 2026-06-08

### Resumo
- **Duração:** 2h 15m 18s
- **Total imóveis coletados:** 2004
- **Sites com resultados:** 82
- **Sites sem leilão ativo:** 28
- **Sites com erro:** 0

### Imóveis por leiloeiro

| Leiloeiro | Imóveis |
|-----------|---------|
| JEFFERSON ADRIANO DA COSTA | 223 |
| MURILO PAES LOPES LOURENÇO | 210 |
| RAFAEL CERETTA ALEGRANZZI | 137 |
| GIANCARLO PETERLONGO LORENZINI MENEGOTTO | 123 |
| DANIEL ELIAS GARCIA | 68 |
| ANA CAROLINA ZANINETTI ROSA | 62 |
| CATIELE BORGES LEFFA | 57 |
| DIEGO COSTA MULLER | 51 |
| EDUARDO JESUS BORDIGNON | 51 |
| GIORDANO BRUNO COAN AMADOR | 47 |
| PAULO ROBERTO NAKAKOGUE | 41 |
| LUIZ EGIDIO CRUZ MEDEIROS | 40 |
| RENATO SCHLOBACH MOYSÉS | 40 |
| FABIO MARLON MACHADO | 39 |
| RODRIGO APARECIDO RIGOLON DA SILVA | 36 |
| JONEY MARCELO LOPES FERREIRA | 34 |
| JOACIR MONZON POUEY | 33 |
| PATRÍCIA PIMENTEL GROCOSKI COSTA | 32 |
| CAMILA PADILHA PRESOTTO | 29 |
| LUIZ CARLOS DALL'AGNOL | 29 |
| JOÃO VITOR MARTINS FERREIRA | 28 |
| JORGE MARCO AURELIO BIAVATI | 28 |
| CLEVER ELMES MILANI | 26 |
| RAFAEL GALVANI FERREIRA | 26 |
| ANTONIO MAGNO JACOB DA ROCHA | 24 |
| DIEGO WOLF DE OLIVEIRA | 24 |
| JOSE FERNANDO DE QUINA | 23 |
| EDUARDO SCHMITZ | 22 |
| FÁBIO GONÇALVES BARBOSA | 21 |
| JAQUELINE SPERANÇA | 21 |
| ARTUR NOGARI DOS SANTOS | 20 |
| CAROLINE DE SOUSA RIBAS | 20 |
| DANIEL OLIVEIRA JUNIOR | 20 |
| RUDIVAL ALMEIDA GOMES JUNIOR | 20 |
| BRUNO BARRETO SANCHES | 19 |
| ALEX WILLIAN HOPPE | 18 |
| APARECIDA MARIA FIXER | 18 |
| JORGE VITORIO ESPOLADOR | 14 |
| JOSECELLI KILDARE FRAGA GOMES | 14 |
| HELTON ROGERIO VERRI VENTRILHO | 13 |
| DAVI BORGES DE AQUINO | 12 |
| SPENCER D'AVILA FOGAGNOLI | 12 |
| BRUNO HENRIQUE LOPES | 11 |
| GILSON KENITI INUMARU | 11 |
| JOÃO LUIZ DE OLIVEIRA | 11 |
| LEONICE FIXER | 11 |
| MARIA FILOMENA PLANAS SERRANO | 11 |
| GELSON BOURSCHIET | 10 |
| JAIR VICENTE MARTINS | 10 |
| ELTON LUIZ SIMON | 9 |
| CONRADO AUGUSTO CARVALHO DE MAGALHÃES | 8 |
| CRISTIANE BORGUETTI MORAES LOPES | 8 |
| LEVY DOS SANTOS MORAES FILHO | 7 |
| ALEXANDRE AUGUSTO DOS SANTOS SABBAG | 6 |
| DEYSE SCHEERER PIETNOZKA KULTZ | 6 |
| PAULO SETSUO NAKAKOGUE | 6 |
| ADYEL MARQUES DE PAULA | 5 |
| RAFAEL DANIELEWICZ | 5 |
| LUCAS EDUARDO DALCANALE | 4 |
| LUIZ FERNANDO FAVARETO | 4 |
| MARCOS ANTÔNIO TULIO | 4 |
| CAROLINE FERREIRA BARBOZA | 3 |
| DORA PLAT | 3 |
| ADALBERTO SCHERER FILHO | 2 |
| GALVÃO ADENYR LOPES JUNIOR | 2 |
| LELIA MARIA DE PAULA LENZ CESAR | 2 |
| PEDRO LERNER KRONBERG | 2 |
| POLIANA MIKEJEVS CALÇA | 2 |
| VANESSA GOELZER DE ARAÚJO VARGAS E PINTO | 2 |
| WERNO KLÖCKNER JÚNIOR | 2 |
| CLAUDIO CESAR KUSS | 1 |
| ERICK SOARES TELES | 1 |
| FERNANDO CAETANO MOREIRA FILHO | 1 |
| FERNANDO DE OLIVEIRA KUSS | 1 |
| JONAS GABRIEL ANTUNES MOREIRA | 1 |
| JOYCE RIBEIRO | 1 |
| JUNIOR CESAR DA SILVA | 1 |
| LUCAS RAFAEL ANTUNES MOREIRA | 1 |
| LUIZ BARBOSA DE LIMA JUNIOR | 1 |
| MARILAINE BORGES DE PAULA | 1 |
| MAURICIO SAMBUGARI APPOLINARIO | 1 |
| NEWTON JORGE GONÇALVES DE OLIVEIRA | 1 |
| ADRIANO MELNISKI | 0 |
| ALEX SANDRO VIEIRA FELIX | 0 |
| ARTHUR FERREIRA NUNES | 0 |
| AUGUSTO PARMEGGIANI PESTANA M. GOMES | 0 |
| BEATRIZ SILVA CARVALHO | 0 |
| CAMILA DE MOURA GAIA PELLISSARI | 0 |
| CATIA FERNANDA ALIEVI TOPOROSKI | 0 |
| DANIEL RIBAS ROSA FRAHM | 0 |
| FLAVIA KLOCKNER RODRIGUES | 0 |
| GILBERTO RUIZ GUILHEN | 0 |
| GUILHERME EDUARDO STUTZ TOPOROSKI | 0 |
| GUSTAVO MORETTO GUIMARÃES DE OLIVEIRA | 0 |
| HELCIO KRONBERG | 0 |
| HELOÍSE SANTI LOCATELLI | 0 |
| ISABELLA KATARINA SCHACKER PERACCHI | 0 |
| JAQUELINE CHRISTIANNI STRYK VARDANA | 0 |
| JEREMY WU SANTIAGO DA COSTA E SILVA | 0 |
| LUIZ RAFAEL LEMUCHI DE LIMA | 0 |
| MARCELO SOARES DE OLIVEIRA | 0 |
| MARCIANO MAURO PAGLIARINI | 0 |
| MIGUEL DONHA JUNIOR | 0 |
| NICOLAS TADASHI MATSUNE | 0 |
| OTAVIO LAURO SODRE SANTORO | 0 |
| PLINIO BARROSO DE CASTRO FILHO | 0 |
| RAIMUNDO MAGALHAES DE MORAES | 0 |
| RICARDO FERREIRA GOMES | 0 |
| RUBENS HENRIQUE DE CASTRO | 0 |
| SIDNEY BELARMINO FERREIRA JUNIOR | 0 |

### Principais Dificuldades Encontradas

#### Sites SPA/JavaScript (JS-heavy) (28 sites)
**Causa:** Site renderiza conteúdo via JavaScript — usa Playwright automaticamente

Sites afetados:
- `https://www.schererleiloes.com.br` (ADALBERTO SCHERER FILHO): SPA detectado — usando Playwright
- `https://www.amleiloeiro.com.br` (ADRIANO MELNISKI): SPA detectado — usando Playwright
- `https://www.alleiloes.com.br` (ALEX SANDRO VIEIRA FELIX): SPA detectado — usando Playwright
- `https://www.hoppeleiloes.com.br` (ALEX WILLIAN HOPPE): SPA detectado — usando Playwright
- `https://www.barretoleiloes.com.br` (BRUNO BARRETO SANCHES): SPA detectado — usando Playwright
- `https://www.presottoleiloes.com.br` (CAMILA PADILHA PRESOTTO): SPA detectado — usando Playwright
- `https://www.bzleiloes.com.br` (CAROLINE FERREIRA BARBOZA): SPA detectado — usando Playwright
- `https://www.aleiloeira.leilao.br` (CATIA FERNANDA ALIEVI TOPOROSKI): SPA detectado — usando Playwright
- `https://www.milanileiloes.com.br` (CLEVER ELMES MILANI): SPA detectado — usando Playwright
- `https://www.topoleiloes.com.br` (GUILHERME EDUARDO STUTZ TOPOROSKI): SPA detectado — usando Playwright
- ... e mais 18 sites

#### Sem lotes/imóveis ativos (26 sites)
**Causa:** Leiloeiro sem leilão em andamento no momento do scraping

Sites afetados:
- `https://www.amleiloeiro.com.br` (ADRIANO MELNISKI): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.alleiloes.com.br` (ALEX SANDRO VIEIRA FELIX): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.arthurnunesleiloes.com.br` (ARTHUR FERREIRA NUNES): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.grupocarvalholeiloes.com.br` (BEATRIZ SILVA CARVALHO): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.camilagaialeiloes.com.br` (CAMILA DE MOURA GAIA PELLISSARI): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.aleiloeira.leilao.br` (CATIA FERNANDA ALIEVI TOPOROSKI): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.fkleiloes.com.br` (FLAVIA KLOCKNER RODRIGUES): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.ggleiloes.com.br` (GILBERTO RUIZ GUILHEN): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.topoleiloes.com.br` (GUILHERME EDUARDO STUTZ TOPOROSKI): Nenhum lote/imóvel encontrado mesmo com Playwright
- `https://www.gustavomorettoleiloeiro.com.br` (GUSTAVO MORETTO GUIMARÃES DE OLIVEIRA): Nenhum lote/imóvel encontrado mesmo com Playwright
- ... e mais 16 sites

#### HTTP retorna 0 lotes mas Playwright encontra (18 sites)
**Causa:** Anti-bot básico contra requests Python — Playwright contorna

Sites afetados:
- `https://www.rochaleiloes.com.br` (ANTONIO MAGNO JACOB DA ROCHA): HTTP retornou 0 lotes, Playwright encontrou 24
- `https://www.cidafixerleiloes.com.br` (APARECIDA MARIA FIXER): HTTP retornou 0 lotes, Playwright encontrou 18
- `https://www.brunoleiloes.com.br` (BRUNO HENRIQUE LOPES): HTTP retornou 0 lotes, Playwright encontrou 11
- `https://www.doleiloes.com.br` (DANIEL OLIVEIRA JUNIOR): HTTP retornou 0 lotes, Playwright encontrou 20
- `https://www.teza.com.br` (ERICK SOARES TELES): HTTP retornou 0 lotes, Playwright encontrou 1
- `https://www.fabiobarbosaleiloes.com.br` (FÁBIO GONÇALVES BARBOSA): HTTP retornou 0 lotes, Playwright encontrou 21
- `https://www.gilsonleiloes.com.br` (GILSON KENITI INUMARU): HTTP retornou 0 lotes, Playwright encontrou 11
- `https://www.giordanoleiloes.com.br` (GIORDANO BRUNO COAN AMADOR): HTTP retornou 0 lotes, Playwright encontrou 47
- `https://www.verrileiloes.com.br` (HELTON ROGERIO VERRI VENTRILHO): HTTP retornou 0 lotes, Playwright encontrou 13
- `https://www.jsilvaleiloes.com.br` (JUNIOR CESAR DA SILVA): HTTP retornou 0 lotes, Playwright encontrou 1
- ... e mais 8 sites

#### Sites com bloqueio 403 (1 sites)
**Causa:** Servidor rejeita o scraper — precisaria de headers adicionais ou Playwright

Sites afetados:
- `https://www.pestanaleiloes.com.br` (AUGUSTO PARMEGGIANI PESTANA M. GOMES): Status 403 na home

#### Sites offline/DNS inválido (1 sites)
**Causa:** Domínio não existe ou está sem DNS

Sites afetados:
- `https://www.drrleiloes.com.br` (DANIEL RIBAS ROSA FRAHM): HTTPSConnectionPool(host='www.drrleiloes.com.br', port=443): Max retries exceede

### Sugestões de Melhoria

1. **Sites offline (404/Connection):** manter lista de sites atualizada; remover leiloeiros IRREGULAR/SUSPENSO automaticamente.
2. **Bloqueio 403/Cloudflare:** implementar FlareSolverr (Docker `:8191`) para sites com Cloudflare Managed Challenge — ver seção 14 deste documento.
3. **SPA/JS-heavy:** Playwright já é acionado automaticamente, mas alguns SPAs Next.js App Router exigem `wait_until='networkidle'` com timeout de 60s.
4. **Sem lotes ativos:** criar agenda de re-scraping; muitos leiloeiros têm leilões esporádicos — verificar novamente em 7-14 dias.
5. **Paginação:** alguns sites usam `?page=N` em vez de `?pagina=N` — ampliar lista de variantes de paginação.
6. **Imagens:** alguns sites usam `data-lazy-src` ou carregam imagens via CSS — adicionar extração de backgrounds CSS.
7. **Documentos (edital/matrícula):** muitos sites usam botões JS com `onclick` ou APIs internas — implementar extratores específicos por domínio.
8. **Rate limiting:** adicionar delay adaptativo baseado no tempo de resposta do servidor.
9. **Leiloeiros sem site:** 30+ leiloeiros REGULAR sem website identificado — buscar pelo nome no Google para encontrar sites atualizados.
10. **Deduplicação:** alguns leiloeiros compartilham site (ex.: Nogari, Pestana, Vardana) — usar `id_externo` baseado em URL para evitar duplicatas.


---

## CORREÇÕES DE CAPTURA — JUCEAC (Acre) + JUCER (Rondônia) — 08/06/2026

> Relatório consolidado das 3 execuções JUCEAC/JUCER. Cada item é uma **correção acionável**
> para uma dificuldade encontrada — foco em *como resolver*, para incorporar ao fluxo deste guia.

### Resultado da captura
- Leiloeiros REGULAR processados: 35 (de 49 no PDF de antiguidade; excluídos IRREGULAR/AFASTADO/CANCELADO)
- Com site: 26 | Sem site: 9 | Sites únicos: 20
- Imóveis (1ª praça > 08/06/2026): 52 | Inseridos no banco `imoveis_leiloeiros.db`: 2 (50 já existiam — dedup por URL)
- CSV: `leiloeiros_juceac_2026-06-08.csv` / `imoveis_juceac_2026-06-08.csv`

| Leiloeiro | Site | Imóveis |
|---|---|---|
| Daniel Elias Garcia | danielgarcialeiloes.com.br | 16 |
| Vladmir Oliani | leiloesaguiar.com.br | 9 |
| Vera Lucia Aguiar de Sousa | leiloesaguiar.com.br | 9 |
| Vera Maria Aguiar de Sousa | leiloesaguiar.com.br | 9 |
| Patricia Pimentel Grocoski Costa | pimentelleiloes.com.br | 6 |
| Evanilde Aquino Pimentel Rosa | lancevip.com.br | 1 |
| Ana Carolina Zaninetti Rosa | lancevip.com.br | 1 |
| Bruno Pimentel Rosa | lancevip.com.br | 1 |

### Correções a aplicar (dificuldade → solução)

1. **Site da JUCEAC é do estado errado e JS-pesado → trocar a fonte e renderizar.**
   `juceac.ac.gov.br` é a Junta do **Acre** (a maioria CANCELADA), enquanto os PDFs são da **Rondônia (JUCER)**.
   **Correção:** quando o alvo for RO, usar a JUCER (`jucer.ro.gov.br`) como fonte primária e o PDF de
   antiguidade (que tem campo `Situação`) como autoridade de status; renderizar o site com Playwright
   (`wait_until=networkidle`) já que a lista não existe no HTML estático.

2. **Situação só existe no PDF detalhado → cruzar os dois PDFs.**
   O PDF em tabela não traz `Situação` e inclui nomes que o PDF de antiguidade marca IRREGULAR.
   **Correção:** confiar sempre no campo `Situação` do PDF detalhado; em divergência, IRREGULAR/AFASTADO/
   CANCELADO prevalece e o leiloeiro é excluído.

3. **Encoding/mojibake nos metadados → forçar UTF-8 e normalizar.**
   **Correção:** ler o HTML renderizado pelo Playwright com `encoding=utf-8` e normalizar nomes com
   `unicodedata.normalize`; não confiar em `og:description` cru.

4. **Sites compartilhados entre leiloeiros → dedup por URL na ingestão.**
   `leiloesaguiar.com.br` ×3, `lancevip.com.br` ×3, `vincoleiloes.com.br` ×3.
   **Correção (aplicada):** inserir no banco com dedup por URL canônica; atribuir o imóvel ao leiloeiro
   pelo dado do próprio lote, não pelo domínio.

5. **9 leiloeiros REGULAR sem site → derivar/validar domínio.**
   **Correção:** derivar do e-mail corporativo (`@empresa.com.br`); para `@gmail/@hotmail`, resolver via
   busca "nome + leilões" e gravar o site validado de volta no CSV.

6. **Data da 1ª praça só no detalhe → enricher antes de descartar.**
   Itens sem data legível na listagem foram descartados (subnotifica).
   **Correção:** rodar o enricher (seções 17/23) que abre cada lote e extrai data da praça + edital +
   matrícula, em vez de descartar por ausência de data.

7. **Estruturas heterogêneas / SPA / offline → cascata + adaptador + checagem DNS.**
   **Correção:** manter a cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`);
   parser dedicado por plataforma (seção 27); FlareSolverr (seção 14) p/ Cloudflare e `curl_cffi` p/ TLS;
   validar DNS e tentar `www`/sem-`www` antes de marcar o site como offline (ex.: vbleiloes.com.br).

8. **`id` (PK) ficou NULL ao inserir → gerar hash na inserção.**
   **Correção (aplicada):** preencher `id = md5(url)[:12]` no INSERT (mesmo formato dos demais registros),
   evitando chave primária nula.

9. **Leilões esporádicos → re-scraping agendado.**
   **Correção:** reexecutar a cada 7–14 dias (cron/Celery beat, seção 21); o dedup por URL evita duplicar.

**Relatório gerado em:** 08/06/2026 20:19:54


---

## CORREÇÕES DE CAPTURA — JUCEG (Goiás) — 08/06/2026

> Cada item é uma **correção acionável** para uma dificuldade encontrada na captura da JUCEG.
> Foco em *como resolver* — para incorporar ao fluxo de scraping deste guia.

### Resultado da captura
- Leiloeiros REGULAR: 120 (excluídos todos SUSPENSO/CANCELADO e a seção "MATRÍCULAS CANCELADAS" do PDF)
- Com site: 80 | Sem site (só e-mail/telefone): 40 | Sites únicos: 68
- Imóveis (1ª praça > 08/06/2026): 291 | Inseridos no banco `imoveis_leiloeiros.db`: 112 (demais já existiam — dedup por URL)
- CSV: `leiloeiros_juceg_2026-06-08.csv` / `imoveis_juceg_2026-06-08.csv`

### Imóveis capturados por leiloeiro (apenas > 0)
| Leiloeiro | Imóveis |
|---|---|
| Flavio Duarte Ceruli | 40 |
| Lucas Andreatta de Oliveira | 39 |
| Erico Sobral Soares | 34 |
| Fernando Jose Cerello Goncalves Pereira | 23 |
| Daniel Elias Garcia | 16 |
| Rodrigo Schmitz | 11 |
| Jussiara Santos Ermano Sukiennik | 11 |
| Orlando Araujo dos Santos | 9 |
| Alglecio Bueno da Silva | 8 |
| Jean Carlo Rosa | 8 |
| Rudival Almeida Gomes Junior | 8 |
| Kaio Albuquerque Rosa Botelho | 8 |
| Leonardo Nunes Lobo | 7 |
| Felipe Guimaraes Carrijo | 6 |
| Eduardo Vinicius Fleury Lobo | 6 |
| Sergio Fleury Batista | 6 |
| Diego Wolf de Oliveira | 6 |
| Jorge Vinicius de Moura Correa | 6 |
| Lidia Ribeiro de Andrade | 6 |
| Cristiane Borguetti Moraes Lopes | 5 |
| Anderson Lopes de Paula | 5 |
| Rodrigo Paes Camapum Bringel | 4 |
| Wellington Martins Araujo | 3 |
| Carlos Augusto Ribeiro Lima | 2 |
| Antonio Brasil II | 1 |
| Leila Nanci Karasiaki | 1 |
| Leony Gomes dos Santos Junior | 1 |
| Johenn Brasil Balduino | 1 |
| Ygor Ferreira Brasil | 1 |
| Davi Borges de Aquino | 1 |
| Jose Luiz Pereira Vizeu | 1 |
| Danielle Joy Karasiaki Carvalho | 1 |
| Paulo de Oliveira Azevedo | 1 |
| Magnun Luiz Serpa | 1 |
| Victor Renno Polatto Vizeu | 1 |
| Eduardo Schmitz | 1 |
| Giovana Norma Bolico | 1 |
| Caroline de Sousa Ribas | 1 |

### Correções a aplicar (dificuldade → solução)

1. **Situação conflitante no PDF → usar a fonte oficial da JUCEG, não o PDF estático.**
   O PDF lista o mesmo leiloeiro 2× com status divergente (SUSPENSO × REGULAR) e mantém quem já foi
   cancelado. **Correção:** consultar o status atual por matrícula no site da JUCEG na hora da captura;
   na ausência disso, regra de desempate fixa — (a) se o nome consta na seção "MATRÍCULAS CANCELADAS",
   excluir sempre; (b) senão, vale o bloco de data mais recente.

2. **Maioria sem campo `site` (40 leiloeiros) → derivar e validar o domínio.**
   **Correção:** derivar site do domínio do e-mail corporativo (`@empresa.com.br` →
   `https://www.empresa.com.br`), descartando `@gmail/@hotmail`; para os sem domínio, resolver via busca
   "nome + leilões" e gravar o site validado de volta no CSV.

3. **Sites compartilhados entre leiloeiros → dedup por URL na ingestão.**
   `leiloesbrasil` ×4, `leilo.com.br` ×4, `lkleiloes` ×3, `sfrazao`/`mcleilao`/`arrematabem`/`leilaobrasil` ×2.
   **Correção (aplicada):** inserir no banco com dedup por URL canônica; atribuir o imóvel ao leiloeiro
   pelo dado do próprio lote, não pelo domínio compartilhado.

4. **SPA / Cloudflare retornando 0 na listagem → cascata + extrator por plataforma.**
   **Correção:** manter a cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`,
   `/busca?categoria=imoveis`); para `megaleiloes`, `sodresantoro`, `portalzuk`, `alfaleiloes`,
   `leilo.com.br`, `leiloesbrasil`, `lkleiloes` escrever parser dedicado por domínio (seção 27) e acionar
   FlareSolverr (seção 14) onde houver Cloudflare; `curl_cffi` para erros de TLS.

5. **Data da 1ª praça só no detalhe → enricher de detalhe antes de descartar.**
   Itens sem data legível na listagem foram descartados (subnotifica). **Correção:** rodar o enricher
   (seções 17/23) que abre cada lote e extrai data da praça + edital + matrícula, em vez de descartar por
   ausência de data na listagem.

6. **Domínios offline/DNS/TLS → checagem prévia e fallback.**
   **Correção:** validar resolução DNS antes de raspar; tentar `www`/sem-`www` e `http`/`https`; marcar
   como inativo após 2 tentativas e remover do pool de re-scraping. Sites com problema nesta rodada:
- **Braulio Ferreira Neto**: sem imoveis com leilao futuro
- **Marcia Regina Cardellichio Nunes**: sem imoveis com leilao futuro
- **Alvaro Sergio Fuzo**: sem imoveis com leilao futuro
- **Maria Aparecida de Freitas Fuzo**: sem imoveis com leilao futuro
- **Geoliano de Souza Lima**: sem imoveis com leilao futuro
- **Ivan Rodrigues Nogueira**: sem imoveis com leilao futuro
- **Erick Soares Teles**: sem imoveis com leilao futuro
- **Maik Nunes de Oliveira**: sem imoveis com leilao futuro
- **Mike Dutra Fleitas**: offline: HTTPSConnectionPool(host='www.mikedutraleiloeiro.com.br', port=443): Max retries exceeded with url: / (Caused by NameRes
- **Leonardo Coelho Avelar**: sem imoveis com leilao futuro
- **Cesar Augusto Bagatini**: sem imoveis com leilao futuro
- **Elenice Lira Sales de Sousa**: sem imoveis com leilao futuro
- **Fernando Caetano Moreira Filho**: sem imoveis com leilao futuro
- **Alex Willian Hoppe**: sem imoveis com leilao futuro
- **Antonio Carlos Peres Bernardini**: sem imoveis com leilao futuro
- **Rossana Paiva Borges de Oliveira**: sem imoveis com leilao futuro
- **Frederico Albert Krausegg Neves**: sem imoveis com leilao futuro
- **Jose Valero Santos Junior**: sem imoveis com leilao futuro
- **Tiago Tessler Blecher**: sem imoveis com leilao futuro
- **Luiz Ubirata de Carvalho**: sem imoveis com leilao futuro

7. **Leilões esporádicos → re-scraping agendado.**
   **Correção:** reexecutar a cada 7–14 dias (cron/Celery beat, seção 21); o dedup por URL evita duplicar
   o que já está no banco.

**Relatório gerado em:** 08/06/2026 21:02:00


---

## CORREÇÕES DE CAPTURA — TRT3 (MG Judiciais) + JUCEMG — 09/06/2026 06:05

> Cada item é uma **correção acionável** para uma dificuldade encontrada na captura.
> Foco em *como resolver* — para incorporar ao fluxo de scraping deste guia.

### Resultado da captura
- Leiloeiros REGULAR: 133 (excluídos Suspensos/Licenciados: Aristóteles Ruas, Carmen Michetti, Paulo S. Gregório, Camila Pires, Arthur Vianna, Frederico Faria)
- Sites únicos visitados: 130
- Imóveis (1ª praça > 09/06/2026): 333
- Inserção no PostgreSQL do site: via `pipeline.importar_ofertas_csv` (dedup por URL) → classificar/normalizar/dedup/geocodificar
- CSV: `leiloeiros_trt3mg_2026-06-09.csv` (nome+site) / `imoveis_trt3mg_2026-06-09.csv` (imóveis)

### Imóveis capturados por leiloeiro (apenas > 0)
| Leiloeiro | Imóveis |
|---|---|
| Mauricio Jose de Sousa Costa | 43 |
| Flavio Duarte Ceruli | 40 |
| Isaias Rosa Ramos Junior | 40 |
| Luis Otavio Marcolino Shinkawa | 31 |
| Fabio Prando Fagundes Goes | 21 |
| Julio Abdo Costa Calil | 20 |
| Fernando Jose Cerello Goncalves Pereira | 19 |
| Daniel Elias Garcia | 15 |
| Joao Emilio de Oliveira Filho | 13 |
| Orlando Araujo dos Santos | 11 |
| Marcos Roberto Torres | 9 |
| Rosimeire das Dores Garcia de Castro | 7 |
| Cesar Augusto Bagatini | 6 |
| Cintia Regina Martins Roma | 5 |
| Denis de Oliveira Fernandes | 5 |
| Francisco David Batista de Souza | 5 |
| Sergio Sousa Rodrigues | 5 |
| Thais Silva Moreira de Sousa | 5 |
| Alexsander Pretti Domingos | 4 |
| Cristiane Borguetti Moraes Lopes | 4 |
| Paulo Cesar Agostinho | 4 |
| Thais Costa Bastos Teixeira | 4 |
| Carla Karine Santos Agostinho | 2 |
| Carlos Augusto Ribeiro Lima | 2 |
| Gilson Aparecido Mariano | 2 |
| Marilaine Borges de Paula | 2 |
| Angela Saraiva Portes Souza | 1 |
| Caroline de Sousa Ribas | 1 |
| Davi Borges de Aquino | 1 |
| Eduardo Schmitz | 1 |
| Giovana Norma Bolico | 1 |
| Jose Luiz Pereira Vizeu | 1 |
| Luiz Felipe Perpetuo Lobato | 1 |
| Magnun Luiz Serpa | 1 |
| Sandra de Fatima Santos | 1 |

### Correções a aplicar (dificuldade → solução)

1. **Fonte é o "banco do site" (PostgreSQL), não o SQLite local → usar o pipeline oficial.**
   Capturas anteriores gravaram no `imoveis_leiloeiros.db` (SQLite standalone), que **não** é lido pelo site.
   **Correção (aplicada):** gerar CSV no formato `url,leiloeiro,cidade,estado,titulo,preco,avaliacao` e rodar
   `python -m pipeline.importar_ofertas_csv` apontando para `postgresql://...:5432/leilao_db`, seguido de
   `classificar → normalizar-cidades → separar-produtos → deduplicar → devoltaparaofuturo → geocodificar`.

2. **TRT3 lista credenciados sem `site`; JUCEMG tem `www.` por leiloeiro → cruzar as duas fontes.**
   O PDF do TRT3 (judiciais) traz só "Acesse o site" sem URL legível; o registro da JUCEMG traz o domínio.
   **Correção:** casar nome do TRT3 com o registro JUCEMG para obter o site; status REGULAR pela JUCEMG
   (excluir `(Suspenso)`, `(Suspensa)`, `(Licenciado...)`).

3. **Matrículas duplicadas (principal + SUPLEMENTAR) e sites compartilhados → dedup.**
   Muitos leiloeiros têm 2 matrículas e/ou usam a mesma plataforma (`palaciodosleiloes` ×4, `goldenlance` ×2,
   `milhaoleiloes` ×2, `claudiokussleiloes` ×2, `gpleiloes`, `stefanelli`). **Correção:** dedup por site na
   coleta e por URL canônica na ingestão (já aplicado).

4. **SPA / Cloudflare / data só no detalhe → cascata + enricher + FlareSolverr.**
   **Correção:** cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`); enricher de detalhe
   (seções 17/23) p/ recuperar data da 1ª praça/edital; FlareSolverr (seção 14) p/ Cloudflare; `curl_cffi` p/ TLS.

5. **Encoding Windows (cp1252) quebrou `separar-produtos` (caractere `→`).**
   **Correção (aplicada):** exportar `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` antes de rodar comandos do pipeline.

6. **`id`/PK e geocodificação dos novos → backfill direcionado.**
   **Correção (aplicada):** geocodificar mirando só os registros recém-inseridos (criados nas últimas horas),
   evitando processar o backlog de ~30k; `id` gerado como `md5(url)[:12]` quando ausente.

7. **Domínios offline/DNS → checagem prévia.** Sites com problema nesta rodada:
- **Adriana Pires Amancio**: sem imoveis com leilao futuro
- **Adriano Apolinario Leao de Oliveira**: sem imoveis com leilao futuro
- **Alessandro de Assis Teixeira**: sem imoveis com leilao futuro
- **Alex Willian Hoppe**: sem imoveis com leilao futuro
- **Alexandra Benedita de Sousa Casado**: sem imoveis com leilao futuro
- **Ananda Portes Souza**: sem imoveis com leilao futuro
- **Andre Fonseca Dias**: sem imoveis com leilao futuro
- **Andre de Oliveira Kuss**: sem imoveis com leilao futuro
- **Angela Assis Oliveira Bechara**: sem imoveis com leilao futuro
- **Arnaldo Emilio Colombarolli**: sem imoveis com leilao futuro
- **Arnold Strass**: sem imoveis com leilao futuro
- **Arthur Ferreira Nunes**: sem imoveis com leilao futuro
- **Breno Augusto Magalhaes da Anunciacao**: sem imoveis com leilao futuro
- **Breno Cesar Oliveira Farias**: sem imoveis com leilao futuro
- **Caio Marcos Campos Caldeira**: sem imoveis com leilao futuro
- **Carlos Chui**: sem imoveis com leilao futuro
- **Catia Fernanda Alievi Toporoski**: sem imoveis com leilao futuro
- **Claudio Cesar Kuss**: sem imoveis com leilao futuro
- **Cleber Cardoso Pereira**: sem imoveis com leilao futuro
- **Clecio Oliveira de Carvalho**: sem imoveis com leilao futuro

**Relatório gerado em:** 09/06/2026 06:05:51


---

## CORREÇÕES DE CAPTURA — JUCETINS (Tocantins) — 09/06/2026 06:46

> Cada item abaixo é uma **correção acionável** para uma dificuldade encontrada na captura
> da JUCETINS (PDF DREI "Leiloeiros Tocantins"). Foco em *como resolver* — para incorporar ao guia.

### Resultado da captura
- Leiloeiros REGULAR: 44 (42 com site, 2 só e-mail) | Sites únicos: 39
- Excluídos do PDF (IRREGULAR/CANCELAMENTO): 8 (Dulnik, Borges Guedes Neto, Danilo A. Oliveira, Carlos Chui, Mike Dutra Fleitas, Renato Moysés, Eduardo Schmitz, Lorrainny R. Lopes)
- Imóveis (1ª praça > 09/06/2026): 64 | Inseridos no banco: 19
- CSV: `leiloeiros_jucetins_2026-06-09.csv` / `imoveis_jucetins_2026-06-09.csv`

| Leiloeiro | Site | Imóveis |
|---|---|---|
| Victor Oliveira Dorta | https://www.victordortaleiloes.com.br | 9 |
| Cesar Augusto Bagatini | https://www.leiloesfederal.com.br | 6 |
| Rudival Almeida Gomes Junior | https://www.rjleiloes.com.br | 8 |
| Daniel Elias Garcia | https://www.danielgarcialeiloes.com.br | 15 |
| Davi Borges de Aquino | https://www.alfaleiloes.com | 1 |
| Milena Rosa Di Giacomo Adri | https://www.megaleiloes.com.br | 19 |
| Joao Luiz de Franca Neto | https://www.jocaleiloesagro.com.br | 2 |
| Lucas Fernandes Almeida | https://www.leiloestocantins.com | 2 |
| Elenice Lira Sales de Sousa | https://www.leiloesbrasil.com.br | 1 |
| Luiz Barbosa de Lima Junior | https://www.lbleiloes.com.br | 1 |

### Correções a aplicar (dificuldade → solução)

1. **Numeração do PDF com saltos (1–48 e 52–55) e status no título → parsing por marcador, não por índice.**
   O PDF de Tocantins pula de 48 para 52 e marca a situação entre parênteses no nome (`(IRREGULAR)`,
   `(CANCELAMENTO DE MATRÍCULA…)`). **Correção:** detectar a situação por regex no título do leiloeiro,
   nunca pela posição na lista; tratar qualquer marcador ≠ vazio como exclusão.

2. **Sites compartilhados entre leiloeiros → dedup por URL na ingestão.**
   `mgl.com.br` ×3 (Lucas/Jonas/Fernando Moreira), `vecchileiloes.com.br` ×2 (Camilla/Marciano).
   **Correção (já aplicada):** inserir no banco com **dedup por URL canônica**; atribuir o imóvel ao
   leiloeiro pelo dado do próprio lote, não pelo domínio compartilhado.

3. **2 leiloeiros REGULAR sem site (só Gmail) → derivar/buscar domínio.**
   Joselma Moraes Martins e Lysia Moreira Silva têm apenas e-mail `@gmail.com`. **Correção:** resolver
   via busca "nome + leilões Tocantins" e gravar o site validado de volta no CSV; sem domínio próprio,
   marcar para captura manual.

4. **SPA / Cloudflare retornando 0 na listagem → cascata + extrator por plataforma.**
   **Correção:** manter a cascata httpx → Playwright → sufixos (`/imoveis`, `/leiloes`, `/lotes`,
   `/busca?categoria=imoveis`); para `megaleiloes`, `arrematabem`, `alfaleiloes`, `webleiloes`,
   `leiloesbrasil` escrever **parser dedicado por domínio** (seção 27) e acionar **FlareSolverr**
   (seção 14) onde houver Cloudflare; `curl_cffi` para erros de TLS.

5. **Data da 1ª praça só no detalhe → enricher de detalhe antes de descartar.**
   Itens sem data legível na listagem foram descartados (subnotifica). **Correção:** rodar o
   *enricher* (seções 17/23) que abre cada lote e extrai data da praça + edital + matrícula.

6. **Domínios offline/DNS/TLS → checagem prévia e fallback.**
   **Correção:** validar resolução DNS antes de raspar; tentar `www`/sem-`www` e `http`/`https`;
   marcar como inativo após 2 tentativas. Sites com problema nesta rodada:
- **Eduardo Gomes**: sem imoveis com leilao futuro
- **Rossana Paiva Borges de Oliveira**: sem imoveis com leilao futuro
- **Antonio Carlos Volpi Santana**: sem imoveis com leilao futuro
- **Tatiana Dinelly e Silva Bonato**: sem imoveis com leilao futuro
- **Sandro de Oliveira**: sem imoveis com leilao futuro
- **Alvaro Sergio Fuzo**: sem imoveis com leilao futuro
- **Fernanda Lima Mascarenhas**: sem imoveis com leilao futuro
- **Murilo Goncalves Ramos**: sem imoveis com leilao futuro
- **Tiago Tessler Blecher**: sem imoveis com leilao futuro
- **Arnold Strass**: sem imoveis com leilao futuro
- **Leonardo Coelho Avelar**: sem imoveis com leilao futuro
- **Bruno Barreto Sanches**: sem imoveis com leilao futuro
- **Nelci Dezan**: sem imoveis com leilao futuro
- **Alex Willian Hoppe**: sem imoveis com leilao futuro
- **Uesley da Silva Oliveira dos Santos**: sem imoveis com leilao futuro
- **Rafael Galvani Ferreira**: sem imoveis com leilao futuro
- **Livia Leilane de Oliveira Azevedo**: sem imoveis com leilao futuro
- **Mouzar Baston Filho**: sem imoveis com leilao futuro
- **Rodolfo da Rosa Schontag**: sem imoveis com leilao futuro
- **Aluisio Francisco de Assis Cardoso Bringel**: sem imoveis com leilao futuro
- **Evando da Silva Lagares**: sem imoveis com leilao futuro
- **Mara Helena de Urzedo Fortunato**: sem imoveis com leilao futuro
- **Joabe Balbino da Silva**: sem imoveis com leilao futuro
- **Rosimeire Alves de Oliveira Maia**: sem imoveis com leilao futuro
- **Lucas Rafael Antunes Moreira**: sem imoveis com leilao futuro
- **Jonas Gabriel Antunes Moreira**: sem imoveis com leilao futuro
- **Fernando Caetano Moreira Filho**: sem imoveis com leilao futuro
- **Erico Sobral Soares**: sem imoveis com leilao futuro
- **Camilla Correia Vecchi Aguiar**: sem imoveis com leilao futuro
- **Marciano Aguiar Carneiro**: sem imoveis com leilao futuro

7. **Leiloeiros sediados fora de TO (GO, SP, MG, DF, BA, etc.) → captura nacional, filtro por leiloeiro.**
   Vários REGULAR de TO operam de outros estados. **Correção:** não filtrar imóvel por UF do leiloeiro;
   capturar tudo que o site publica e registrar a UF real do imóvel a partir do lote.

8. **Leilões esporádicos → re-scraping agendado.**
   **Correção:** agendar re-execução a cada 7–14 dias (cron/Celery beat, seção 21); o dedup por URL
   evita duplicar o que já está no banco.

**Relatório gerado em:** 09/06/2026 06:46:52


---

## ETAPA 1 — Recuperação via FlareSolverr (SPA/Cloudflare) — 09/06/2026 11:33

Reprocessados 32 sites que retornaram 0 no scraper inicial (SPA/JS/Cloudflare).
FlareSolverr renderiza o JS e a `extrair()` é reaplicada (mesmas regras: 1ª praça futura, dedup por URL).

- Imóveis recuperados (1ª praça futura): **126** | Inseridos novos no banco: **84**
- Sites com recuperação > 0: 11 de 32

| Leiloeiro (recuperado) | Imóveis |
|---|---|
| Rodolfo da Rosa Schontag | 50 |
| Jonas Gabriel Antunes Moreira | 15 |
| Lucas Rafael Antunes Moreira | 15 |
| Fernando Caetano Moreira Filho | 15 |
| Rafael Galvani Ferreira | 14 |
| Joabe Balbino da Silva | 3 |
| Alex Willian Hoppe | 3 |
| Bruno Barreto Sanches | 3 |
| Livia Leilane de Oliveira Azevedo | 3 |
| Murilo Goncalves Ramos | 3 |
| Paulo Marcelo Silva Almeida | 2 |

**Diagnóstico-chave:** o erro original não era Cloudflare na maioria — era SPA cujo HTML só
aparece após JS (ex.: `webleiloes` home = 472 KB renderizada vs. ~1,4 KB no shell estático).
A correção genérica (render via FlareSolverr na **home** antes de tentar sufixos) resolve a
maior parte sem parser dedicado por domínio. Sites ainda em 0 exigem API interna (XHR/JSON).

**Gerado em:** 09/06/2026 11:33:59


---

## 38. Captura dos leiloeiros REGULARES de Roraima/Rondônia — JUCER (jun/2026)

> Execução real em 09/06/2026 a partir do PDF *Leiloeiros Roraima* (lista JUCER RR + lista
> de antiguidade RO). Fluxo: extrair leiloeiros **REGULARES** → CSV `nome,site` → scraper
> genérico (`scraper_rr_ro.py`) → filtro de 1ª praça futura → inserção no banco + CSV datado.

### 38.1. Resultado

- **26 leiloeiros REGULARES com site** (após excluir IRREGULAR/suspenso/afastado e deduplicar
  domínios compartilhados — MGL, LanceVip, Leilões Aguiar, Vinco). CSV de leiloeiros:
  `csv/leiloeiros_roraima_rondonia_2026-06-09.csv`.
- **72 imóveis** com 1ª praça **posterior a 09/06/2026** capturados; **55 novos** inseridos no
  SQLite `imoveis_leiloeiros.db`, **17** já existiam (dedup por URL). CSV de imóveis:
  `csv/imoveis_rr_ro_2026-06-09.csv` (colunas: leiloeiro, junta, site, titulo, cidade, uf,
  preco, data_leilao, imagem, anexos, url). 69/72 com imagem.
- **11 sites produtivos**, 14 com 0 imóveis (parkados, Cloudflare/JS não-mapeado, ou lotes sem
  tipo no título), 1 inacessível (DNS).

| Imóveis | Leiloeiro | Observação |
|--------:|-----------|------------|
| 12 | Thaís Costa Bastos Teixeira | |
| 10 | Hugo Moreira Pimenta (Rio Negro) | |
| 10 | Rodrigo Aparecido Rigolon | |
|  8 | Patrícia Pimentel Grocoski Costa | |
|  7 | Dora Plat (Portal Zuk) | |
|  6 | Deonizia Kiratch | plataforma vlance |
|  6 | Leilões Aguiar (Vera Lúcia/Vera Maria) | |
|  5 | Rafael Galvani Ferreira | Cloudflare |
|  3 | MGL Leilões (Fernando/Jonas/Lucas) | mgl.com.br 403 → FlareSolverr |
|  3 | Daniel Elias Garcia | Cloudflare |
|  2 | Fernando Caetano Moreira Filho | espelhado com Lucas (dedup) |
|  0 | Wesley, Mayco, Hoppe, Brian/GF, Jimmy Asami, Carlos H. Barbosa, LanceVip, Portela, Marcus Allain, Lucas Rafael, Sandro/Norte, Beltrame, Vinco, Reis | ver 38.2 |
|  — | Angélica Vilas Boas (vbleiloes.com.br) | inacessível (DNS) |

### 38.2. Principais dificuldades enfrentadas

1. **Listagens 100% renderizadas por JS.** `requests` puro retorna só o *shell* (0 cards) na
   maioria — vlance/Vue (deonizialeiloes), WordPress+AJAX e SPAs. Os lotes só aparecem após
   execução de JavaScript.
2. **Cloudflare Managed Challenge** em ~15 dos 26 sites. Vários passam com 200 mas
   `mgl.com.br` retorna **403** e `reisleiloes.com.br` **401** direto no `requests`.
3. **Lotes aninhados dentro do leilão (padrão vlance).** A página de categoria
   (`/leilao/index/imoveis`) lista **leilões** (eventos), não lotes; os lotes ficam em
   `/leilao/index/leilao_id/<id>`. Extração "plana" de cards na home/categoria não acha lote
   nenhum — é preciso seguir os links de leilão.
4. **API interna opaca.** `/core/api/get-lotes` existe mas ignora os params óbvios
   (`categoria`, `leilao_id`) e devolve vazio (len 28); `get-leiloes` ignora `categoria` e
   retorna tudo. Reverter cada plataforma não escala.
5. **Lixo de DOM capturado como "imóvel".** A 1ª versão (crawl profundo) trouxe **71% de
   ruído**: texto de navegação (`prev next`, `Detalhes do lote`, `Auditório do Leilão`),
   **leilões cancelados** e até **produtos/veículos** (TV, iPhone, ar-condicionado) que estavam
   no mesmo bloco de um imóvel.
6. **Data ausente no card.** Muitos cards de listagem não mostram a data da praça — exige
   abrir a página de detalhe (caro) para decidir se a 1ª praça é futura; com orçamento limitado
   de detalhes, parte dos imóveis válidos é perdida.
7. **Sites mortos/parkados:** `vbleiloes.com.br` (ConnectionError/DNS), `maleiloesro.com.br`
   (len 67, parkado), `maycosantos.lel.br` (página mínima).
8. **Status divergente entre fontes.** O mesmo leiloeiro aparece **Regular** na tabela de RR e
   **Irregular** na lista de antiguidade de RO (ex.: Wesley Silva Ramos) — exige reconciliação
   manual antes de montar o CSV.
9. **Plataformas espelhadas.** `fernandoleiloeiro.com.br` e `lucasleiloeiro.com.br` servem o
   **mesmo conteúdo** (backend MGL) → risco de imóvel duplicado entre leiloeiros (mitigado por
   dedup por URL, que aqui zerou Lucas).
10. **Encoding no Windows.** Ler o banco com `python -c` sem `-X utf8` mostra mojibake
    (`S�tio`) no console **cp1252** — é artefato de exibição, não corrupção no banco; e a fonte
    em si às vezes vem Latin-1, gerando `U+FFFD` se decodificada errada.
11. **Anexos com falso-positivo.** O heurístico de "qualquer `.pdf`" capturou o
    `aviso_cookies.pdf` do rodapé em vez do edital/matrícula reais (que ficam mais fundo, na
    página do lote).
12. **Parsing de cidade imperfeito.** O regex `Cidade/UF` corta nomes compostos
    ("Plácido de Castro/AC" → "Castro"; "Poços de Caldas" → "Caldas"). O nome completo continua
    no título, mas a coluna `cidade` fica truncada.

### 38.3. Correções sugeridas (mapeadas às dificuldades)

- **(1,2,3) Render + fallback + crawl em camadas — já implementado.** Playwright (browser único
  reusado) com fallback automático para **FlareSolverr** (`:8191`) quando há challenge/401/403,
  e crawl `home → categoria → leilão → lote`. Manter este pipeline como base.
- **(4) Adapter por plataforma para os casos de alto volume.** Detectar `vlance`
  (`/v3/js/vlance`, `/core/api/get-leiloes`) e consumir o JSON de `get-leiloes` (tem `nm`,
  `dt_formatada`, `dt_segundoleilao_data`, paginação) — dados tipados e mais rápidos que render.
  Capturar o XHR real de `get-lotes` via `page.on("response")` (em vez de chutar params) e fixar
  o endpoint/params descobertos por plataforma. Ver seção 27 (camada de adaptadores).
- **(5) Filtro precisão > recall — já implementado.** Exigir **tipo de imóvel no título**
  (casa/terreno/sítio/galpão/apartamento/sala comercial/chácara…), rejeitar lixo de navegação
  (`JUNK_RE`), excluir `cancelad|encerrad|arrematad|suspens` (`DEAD_RE`) e bloquear
  produtos/veículos (`NEG_WORDS`). **Efeito colateral conhecido:** sites que titulam o lote como
  "Lote N" (descrição só no corpo) rendem 0 — corrigir com adapter que leia o `<h_>`/descrição
  do card, não só o texto do link. Reaproveitar `categoria_bem` (seção 22) para a decisão
  imóvel × produto.
- **(6) Orçamento de detalhe adaptativo + 2ª praça.** Quando a categoria já é "imóveis",
  assumir o lote como imóvel e priorizar abrir detalhe **apenas** para os sem data no card;
  extrair também `dt_segundoleilao` para casos em que a 1ª praça já passou mas a 2ª é futura
  (regra de negócio a confirmar). Persistir o orçamento por site em config.
- **(7) Pré-checagem de liveness.** Rodar o `recon` (status/headers/tamanho) antes do scraping e
  pular automaticamente DNS-fail/parkados (`len < 1KB`, sem `<a>` de imóvel), registrando o
  motivo — evita gastar render em site morto.
- **(8) Tabela-mestre de status.** Consolidar RR+RO numa planilha única com a **regra: vale o
  status mais restritivo** entre as fontes; marcar conflitos para revisão. Automatizar o merge
  na geração do CSV de leiloeiros.
- **(9) Dedup por URL canônica + chave leiloeiro.** Já há dedup por URL; adicionar
  normalização de URL (remover query de tracking) e, para backends espelhados, mapear o
  leiloeiro "dono" para não perder atribuição.
- **(10) Encoding.** Rodar todo script com `python -X utf8` (ou `PYTHONUTF8=1`); ao ler resposta
  bruta, respeitar `charset` do `Content-Type`/meta e normalizar para UTF-8 antes do parse.
- **(11) Anexos com allowlist.** Aceitar PDF só quando o texto do link casar
  `edital|matr[ií]cula|laudo|avalia[çc][ãa]o` **e** rejeitar `cookie|termo|pol[ií]tica|aviso`;
  buscar anexos preferencialmente na **página do lote**, não na listagem. Integrar com
  `baixar-docs` (DocumentoDownloader) para baixar de fato os PDFs válidos.
- **(12) Cidade via IBGE.** Após capturar, normalizar `cidade/uf` contra a lista IBGE
  (`corrigir_cidades_ibge.py`, seção 32) usando match por substring no título — corrige nomes
  compostos truncados.

### 38.4. Checklist desta captura

- [x] Extrair só leiloeiros REGULARES (excluir IRREGULAR/suspenso/afastado).
- [x] Deduplicar domínios compartilhados (MGL/LanceVip/Aguiar/Vinco).
- [x] CSV `nome,site` em `/csv`.
- [x] Render JS + fallback FlareSolverr para Cloudflare.
- [x] Filtrar 1ª praça > data da captura.
- [x] Capturar título, imagem, preço, cidade/uf, data, anexos.
- [x] Inserir no banco com dedup por URL + CSV datado em `/csv`.
- [x] Report parcial por leiloeiro a cada 5 min.
- [ ] **Pendências p/ próxima rodada:** adapter vlance via API; allowlist de anexos + baixar-docs;
      normalização de cidade IBGE; capturar lotes "Lote N" (ler descrição do card).

---

## ETAPAS 2–4 — Pós-captura JUCETINS (enriquecimento, sync e docs) — 09/06/2026

Continuação da ETAPA 1 (recuperação via FlareSolverr). Correções acionáveis:

### Etapa 4 — Sync para produção (PostgreSQL)
- Conversão SQLite→JSONL e `python run.py importar-scraping --arquivo <jsonl>`: **103 lidos → 88 inseridos, 15 duplicados** (dedup por `url_original`; sites nacionais já presentes via outras juntas).
- **Mapeamento de campos** (SQLite → JSONL do importador): `titulo→titulo`, `url→url/source_url`, `tipo→tipo_imovel` (derivar por palavra-chave do título; o importador mapeia string→enum, `imovel` cai em OUTRO), `imagem→fotos[]`, `uf→estado` (validado contra UFs BR), `lance_inicial→preco` (parse pt-BR `1.234,56`→float), `leiloeiro→nome_anunciante`, `descricao→descricao_completa`.
- **Atenção:** `importar_scraping.py` **não** mapeia documentos — `arquivos` precisa ser populado em etapa separada (enricher), não no import.

### Etapa 2 — Enricher de detalhe + baixar-docs
- A listagem captura **links de página de detalhe**, não os PDFs. Para `baixar-docs` funcionar é **obrigatório** um enricher: abrir cada `url_original` (FlareSolverr), extrair `edital|matrícula|laudo|avaliação|certidão`/`.pdf` e gravar `Imovel.arquivos = [{tipo,url,nome}]`.
- Resultado: **76/104 imóveis enriquecidos, 130 docs gravados**; guard para **não sobrescrever** imóveis que já tinham PDFs (duplicatas de produção).
- **Armadilha de download (Windows local):** `run.py baixar-docs` via Python local falha com `SSL: CERTIFICATE_VERIFY_FAILED` (mesmo erro do `normalizar-cidades`). **Correção:** rodar `baixar-docs` **dentro do container** (cert store correto) — é o que o **Celery beat** já faz de hora em hora (800 docs previamente baixados comprovam). Alternativa: `verify=False`/`certifi` no `document_downloader` para execução local. Os 130 docs ficam **enfileirados** e o beat os baixa automaticamente.

### Etapa 3 — Leiloeiros sem site
- `Joselma Moraes Martins` → **jmleiloesto.com.br** (resolvido por busca; telefone confere com o PDF). Gravado no CSV e no scraper.
- `Lysia Moreira Silva` → **sem site próprio** (advogada OAB TO002535, matrícula 12/12/2025, só Gmail) — marcar para captura manual quando houver domínio.

**Registrado em:** 09/06/2026


---

## CORREÇÕES DE CAPTURA — Revisão de TÍTULOS e LINKS de todos os imóveis — 09/06/2026 14:43

> Remediação de qualidade dos dados já no banco do site (PostgreSQL). Títulos-lixo como
> "Compartilhe no WhatsApp", "O JavaScript não está disponível" e textos concatenados
> ("Anterior Próxima... 636 visitas...") foram reinvestigados e re-extraídos da fonte.

### Resultado
- Registros com título/URL ruim: **3.562 → 0 títulos-lixo** (~10% dos ativos saneados)
- Corrigidos: ~3.858 títulos | Desativados: 234 (125 páginas 404 + 109 links sociais quebrados)
- Ativos finais: 33.948

### Correções a aplicar (dificuldade → solução)

1. **Botão "Compartilhe no WhatsApp/Twitter" capturado como imóvel → ignorar âncoras sociais; recuperar do `?text=`.**
   O scraper genérico pegava o `<a href="api.whatsapp.com/send?text=...">` do botão de compartilhar como
   se fosse o lote (URL e título errados). **Correção:** (a) na coleta, **excluir** hrefs de
   `whatsapp|twitter|x.com|facebook|telegram` da seleção de lotes; (b) para registros já gravados, o título
   real está no parâmetro `?text=` — recuperá-lo via `parse_qs`+`unquote`; (c) como a URL do lote se perdeu,
   **desativar** esses registros (link aponta para rede social, não para o imóvel).

2. **SPA não renderizado → "O JavaScript não está disponível" → usar `og:title`/JSON-LD do HTML estático.**
   Mesmo em SPAs, as meta tags de SEO/compartilhamento (`og:title`, `application/ld+json` `name`) vêm no
   HTML inicial. **Correção:** extração em cascata `og:title → JSON-LD name → <title> limpo → <h1>` via httpx
   (rápido); **Playwright só como fallback** para os poucos que não servem meta estática. Recuperou ~90% sem browser.

3. **Texto concatenado do card ("Anterior Próxima... 636 visitas... Facebook Twitter Whatsapp") → priorizar meta/JSON-LD, nunca o texto bruto do container.**
   **Correção:** o título deve vir de `og:title`/JSON-LD/`<title>`, com **limpeza de sufixos** ("- Lance Inicial: R$...",
   "- Avaliação: R$...", "| NomeDoSite Leilões") e **rejeição de lixo** por regex
   (`compartilh|whatsapp|javascript|visitas|cookie|anterior próxima|^Lote -|categorias`).

4. **Título no slug da URL quando a página não entrega meta (vipleiloes SPA) → derivar do path.**
   Páginas tipo `/evento/anuncio/toyota-hilux-swsrxa4fd-17` não servem `og:title` nem no Playwright, mas o
   slug **é** o título. **Correção:** último segmento do path → remove id numérico final → `-`/`_`→espaço →
   Title Case. Recuperou 287 títulos.

5. **Mojibake nos títulos antigos → re-extração já corrige.**
   Títulos gravados com `R�`, `S�tio` etc. **Correção:** ao re-extrair via httpx/Playwright, o `og:title`
   vem em UTF-8 correto, substituindo o texto corrompido automaticamente.

6. **Página de lote removida (404/410) → desativar.**
   **Correção:** no re-fetch, status `404/410` ⇒ `ativo=false` (o lote saiu do site). 125 desativados.

7. **Concorrência alta gerava falsas falhas (throttling) → retry com menos conexões.**
   A 1ª passada (15 conexões) registrou 897 "falhas" que eram timeouts. **Correção:** 2ª passada com
   concorrência 8, 2 tentativas e timeout 20s recuperou 674; restante via Playwright.

8. **Filtro de "título curto" (<12 chars) marca títulos legítimos como ruins.**
   "MOTOCICLETA", "VESTUÁRIO", "Picotadeira", "Apartamento" são títulos reais e curtos. **Correção:** não tratar
   comprimento isolado como lixo — só sinalizar curto **se também** casar padrão de lixo ou vier de URL de
   listagem (`/eventos/leilao/`).

**Relatório gerado em:** 09/06/2026 14:43:22

### 38.5. 2ª passada — melhoria de recall (slug) + import em produção (PostgreSQL)

Após a 1ª rodada (72 imóveis, seção 38.1), duas ações:

**(a) Import no banco de produção (PostgreSQL Docker).** Os imóveis foram convertidos para
JSONL no schema do `pipeline/importar_scraping.py` (campos: `titulo`, `url`, `tipo_imovel`,
`fotos`, `cidade`, `estado`, `preco`, `nome_anunciante`, `descricao_completa`) e importados com
`python run.py importar-scraping`. **Armadilhas reais:**
- O mount real do container **não** era `OneDrive/.../leilao-scraper/leilao-scraper`, e sim
  `C:\Users\arthur\leilao-scraper` → `/app` (descobrir com
  `docker inspect <ctn> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`).
- O container `leilao_api` estava `Exited (1)`; usar o `leilao_worker` (mesmo mount/DB) para o
  `docker exec`.
- **Git Bash mangla `/app/...`** → usar `MSYS_NO_PATHCONV=1` antes do `docker exec`.
- **Bind-mount do Docker Desktop no Windows fica obsoleto** ao sobrescrever o arquivo *in place*:
  o host via 82 linhas, o container via 72. **Solução:** `docker cp host.jsonl ctn:/app/...`
  com nome novo (ou novo nome de arquivo) em vez de confiar no mount.
- O `importar-scraping` **não persiste a data da praça** (não há campo no mapeamento) — a data
  fica só em `descricao_completa`. Para manter `data_leilao` estruturada, estender o mapeamento.

Resultado import: 1ª passada **50 inseridos** / 22 dup; 2ª passada **+10** → **60 imóveis meus
em produção** (`SELECT count(*) FROM imoveis WHERE descricao LIKE '%1a praca:%'` = 60).

**(b) Recall via slug da URL.** Muitos sites (ex.: `gfleiloes.com.br`) deixam o link do lote
como **imagem** (texto vazio) e descrevem o bem **no slug**:
`/lote/imovel-area-de-4896m_-morumbi-sao-paulo-sp/238/`. O filtro que exigia tipo de imóvel no
*título* descartava esses cards. **Correção implementada** em `extract_cards`: derivar o título
do **último segmento não-numérico do path** (slug → texto) quando o título do card é fraco/lixo,
e aceitar o card se o tipo de imóvel aparece no título **ou** no slug. Mantidos os filtros de
lixo/cancelado/produto.

Ganho (mesma data de captura, 09/06/2026): **72 → 82 imóveis**; sites produtivos **11 → 13**
(Carlos H. Barbosa 0→3, Brian/GF 0→2, Daniel Garcia 3→5, Hugo 10→11, Zuk 7→8), **sem regressão**
(Deonizia 6→6) e **sem reintrodução de lixo**.

**Ainda em 0 (limitações remanescentes):** sites SPA cujos lotes carregam por JS sem slug
descritivo nem cards no DOM raspável (Beltrame `/leilao/lotes/imoveis`, Vinco), e
`reisleiloes.com.br` (401 persistente). Próximo passo seria adapter por plataforma capturando o
XHR real de lotes (`page.on("response")`).


---

## 37. Scraping TJAM (Superbid) + leiloeiros JUCEA — Amazonas (jun/2026)

Captura de duas fontes para o Amazonas: **(A)** a loja **TJAM no Superbid** (`tjam.superbid.net`)
e **(B)** os sites próprios dos leiloeiros **REGULAR** do PDF "Leiloeiros Amazonas" (JUCEA).
Script: `scraper_tjam_jucea.py`. Resultado: **340 imóveis válidos**, 64 descartados por data,
0 erros de importação; 23 leiloeiros Regular (12 Irregulares excluídos).

### 37.1. Descoberta crucial: `tjam.superbid.net` é uma LOJA, não um portal

O site é um SPA (jQuery + bundle, atrás de Cloudflare). A API interna foi achada no `bundle.js`
(`config.apiOffers = "https://offer-query.superbid.net"`). Interceptando o XHR real via Playwright
(`page.on("request")`), descobriu-se que o site **filtra por loja**, não por portal:

```
GET https://offer-query.superbid.net/offers/
    ?filter=stores.id:16418        ← TJAM é a STORE 16418 (não um portalId próprio)
    &portalId=[2,15]               ← portal judicial genérico (compartilhado entre tribunais)
    &searchType=opened&orderBy=endDate:asc&preOrderBy=orderByFirstOpenedOffers
    &pageNumber=1&pageSize=30&locale=pt_BR&timeZoneId=America/Sao_Paulo&requestOrigin=store
```

**Armadilha:** filtrar só por `portalId=15` traz ofertas de tribunais de todo o Brasil (SC, SP…).
O recorte do Amazonas vem do `filter=stores.id:16418`. **Sempre capture o XHR real no Playwright**
antes de assumir o parâmetro de recorte — o `portalId` da URL do navegador engana.

A API **não exige `x-api-key`** (responde 200 anônimo) e é JSON limpo e tipado — fonte muito
superior a raspar o DOM. Campos-chave por oferta:
`product.shortDesc` (título), `product.galleryJson`/`thumbnailUrl` (fotos),
`product.attachments[]` (edital/matrícula/avaliação/penhora/escritura, com `link` direto),
`product.location.city` ("Manaus - AM"), `product.judicial` (processNumber, vara, foro, district),
`eventPipeline.stages[]` (1ª e 2ª praça com `beginDate`/`endDate`), `offerDetail.initialBidValue`.

### 37.2. Filtro "leiloeiro Regular" cruzado com a API

Na loja TJAM, cada oferta traz `auction.auctioneer`. Cruzou-se com a lista de **Regulares** do PDF
(match exato + aproximado por 2+ tokens do nome). As 2 ofertas-imóvel abertas eram de
**Danielly Fernandes** (013/2011) e **Ricardo Marcelo de Oliveira** (014/2011) — ambos Regular.

**Classificação imóvel × não-imóvel:** usar `product.productType.id` — `13 = Imóveis`. A 3ª oferta
aberta era `productType 12` ("Sucata de embarcação Ana Carolina I") e foi corretamente descartada.
Não confie só no título; o `productType` é o discriminador limpo (complementa a seção 22).

### 37.3. Regra de data (seção 8.1) para leilão judicial de 2 praças

"1ª praça posterior à captura" é restritivo demais para judiciais: a 1ª praça quase sempre já
começou, mas o leilão segue aberto até a 2ª. Implementou-se `imovel_valido_por_data()`:

> **válido** se `data_primeiro_leilao >= hoje` **OU** `data_encerramento >= hoje`
> (datas desconhecidas → mantém; o `devoltaparaofuturo` desativa o que ficou no passado).

Isso casa com a semântica de `searchType=opened` e descartou **64 imóveis** já encerrados nos
sites dos leiloeiros (ex.: BRIAN −10, DANIEL −16, IRANI −5).

### 37.4. Dificuldades enfrentadas e correções sugeridas

| # | Dificuldade | Causa | Correção aplicada / sugerida |
|---|---|---|---|
| 1 | `tjam.superbid.net` parecia um portal | É uma **loja** (`stores.id:16418`) dentro do marketplace judicial | Capturar XHR real no Playwright; nunca inferir o recorte pelo `portalId` da URL |
| 2 | `SSLCertVerificationError` no Windows | Python 3.14 sem CA do host | `verify=False` + `urllib3.disable_warnings()` (padrão do repo) |
| 3 | `leilaomanaus.lel.br` offline (Hugo Pimenta) | Domínio `.lel.br` morto / sem DNS | Marcar `offline`; sugerir derivar site alternativo do e-mail institucional ou usar a loja Superbid do leiloeiro |
| 4 | Sites JS-heavy lentos (leilo.com.br, hoppe, asami) | SPA → Playwright por lote (até 120 URLs) | Funcionou, mas **lento (~1,5–2 min/site)**. Sugestão: interceptar o XHR de lotes (`page.on("response")`) em vez de navegar lote a lote — muitos desses rodam sobre a **própria plataforma Superbid/S4B**, então a API `offer-query` por `stores.id` serve para todos |
| 5 | `leiloesdonorte`, `wrleiloes`, `renansouza` sem resultado | Listagem em rota não-mapeada ou só veículos no momento | Ampliar `LISTING_PATHS` e detectar links de lote por XHR; revisitar quando houver praça de imóvel |
| 6 | SQLite: 214 de 340 "já existiam" | Leiloeiros nacionais (webleiloes, leilaobrasil, leilaonet) já raspados em juntas anteriores | **Comportamento correto** de dedup por URL canônica (regra 0.2); 126 novos inseridos |
| 7 | Fonte duplicada no Postgres (ALEX HOPPE) | Slug por domínio (`hoppeleiloescombr`) ≠ fonte antiga (`alexwillianhoppe`, id 902) | Sugestão: tabela de alias leiloeiro→fonte_id canônica para evitar fontes redundantes do mesmo leiloeiro |
| 8 | Endereço do imóvel ausente na API TJAM | A API traz só `location.city`; o endereço completo está no edital (PDF) | Para precisão de logradouro, baixar o edital (`baixar-docs`) e extrair via OCR/regex (seções 23/26) |

### 37.5. Resultado por leiloeiro (Regular) — 2026-06-09

Loja TJAM: Danielly Fernandes 1, Ricardo Marcelo 1.
Sites: TIAGO/webleiloes 109, IRANI/leilaobrasil 86, FELIPE/leilo 44, DANIEL/dgleiloes 30,
ALEX/hoppe 17, LEONARDO/leilaonet 11, DAVI/alfaleiloes 10, THAIS/tmleiloes 9,
PAULO/agostinho 7, WESLEY/wrleiloes 6, DEONIZIA 3, JIMMY ASAMI 2, SANDRO/norte 2,
BRIAN/amazonas 1, FERNANDO 1. Sem imóvel agora: HUGO (offline), LUIZ DE CHIRICO, RENAN.

Artefatos: `csv/imoveis_tjam_jucea_2026-06-09.csv`, `csv/leiloeiros_tjam_jucea_2026-06-09.csv`;
SQLite `junta='JUCEA'`; PostgreSQL (fonte por slug de leiloeiro + `tjam_superbid`).

### 38.6. Adapter por plataforma via XHR real (`page.on("response")`)

Para os sites ainda em 0 (lotes 100% via JS, sem slug descritivo), mapeei a **API real de
lotes** capturando o tráfego XHR com `page.on("response")` durante o render Playwright. Achados
por plataforma:

| Plataforma | Sites (deste lote) | Endpoint de lotes | Observação |
|-----------|--------------------|-------------------|------------|
| **Superbid** (white-label) | Hoppe, Asami | `offer-query.superbid.net/offers/?filter=...;product.productType.description:imoveis;stores.id:<id>` | `storeId` vem de `siteconfigprod.superbid.net/<host>/style.config.json`; **filtra imóveis no servidor**; sem auth |
| **suporteleiloes** | LanceVip | XHR de lote em `stats.suporteleiloes.com.br` (telemetria) — API de listagem não exposta na home | precisa abrir o evento; pendente |
| **sistema/norteleiloes** | Norte | `sistema.norteleiloes.com.br/lotes/listar-dados?pagina=N&porPagina=K&api=true` | `lotes-categorias` retornou `quantidade 0` p/ imóveis → sem inventário atual |
| Custom/Cloudflare | Portela, Beltrame, Vinco, Reis | nenhum XHR de lotes capturado (SSR atrás de JS/CF ou 401) | precisa de render + scroll ou bypass específico |

**Adapter Superbid implementado** (`superbid_adapter.py`, integrado no topo de
`scrape_leiloeiro`):
1. Detecta a loja: `GET siteconfigprod.superbid.net/<host>/style.config.json` → `storeId`,
   `portalId`. Se não existir, retorna `None` (cai no crawl genérico).
2. Pagina `offer-query.superbid.net/offers/` com o filtro
   `auction.modalityId:[1,4];product.productType.description:imoveis;stores.id:<id>` e
   `searchType=opened` (só leilões abertos). Headers: apenas
   `Accept: application/json, application/hal+json` + `User-Agent` (sem token).
3. Mapeia cada `offer`: `product.shortDesc` (título), `product.thumbnailUrl` (imagem),
   `product.location.city` (cidade/UF), `priceFormatted` (preço) e a **1ª praça** via
   `auction.eventPipeline.stages[0].endDate` (fallback `beginDate`/`endDate`). Filtra
   `1ª praça ≥ hoje`. URL canônica: `https://<host>/oferta/<offerId>`.

> **Armadilha:** chamar o `offer-query` direto com filtro incompleto devolve **500**. O segredo
> é replicar o `filter=` exato observado no `page.on("request")` (modalityId + productType +
> stores.id) — não basta `stores.id`.

**Ganho:** **Hoppe 0 → 5 imóveis** (títulos limpos, datas de 1ª praça, preço, cidade/UF, foto),
direto da API tipada — mais confiável que raspar DOM. Asami é Superbid mas sem imóvel aberto
futuro (0, legítimo). Apenas 2 dos 26 sites são Superbid.

**Resultado consolidado (após adapter):** **87 imóveis** capturados (1ª praça > 09/06/2026),
**14 leiloeiros produtivos**, **71 no SQLite** e **65 imóveis em produção (PostgreSQL)**
(`descricao LIKE '%1a praca:%'`). CSV final: `csv/imoveis_rr_ro_2026-06-09.csv` (87 linhas).

**Próximos adapters de maior retorno:** `suporteleiloes` (LanceVip — abrir o evento e capturar a
listagem de lotes) e os SSR/CF (Portela/Beltrame/Vinco/Reis) com render + scroll incremental.


---

## 39. Captura dos leiloeiros REGULARES do Amapá — JUCAP (jun/2026)

> Entrada: PDF **"Leiloeiros Amapá"** (18 leiloeiros, matrículas JUCAP de 1992 a 2025).
> Scraper: `scraper_amapa.py` (adaptado do template validado `scraper_rr_ro.py`).
> Entrada CSV: `csv/leiloeiros_amapa_2026-06-09.csv` (nome, site, cidade, uf, junta, matrícula).
> Saída: `csv/imoveis_amapa_2026-06-09.csv` + inserção em `imoveis_leiloeiros.db` (dedup por URL).
> Captura: 09/06/2026 16:20–16:46. **0 erros.**

### 39.1. Resultado consolidado

- **18 leiloeiros** processados; **15 com site raspável**, **3 sem site** (Jaciara Coutinho,
  Henrique Borba, Daniel Ribas Rosa Frahm — só redes sociais ou sem presença online).
- **20 imóveis** com **1ª praça posterior à data de captura** (filtro estrito `1ª praça > hoje`).
- Produtivos: Daniel Elias Garcia (5), Jonas Moreira / MGL (5), Lucas Moreira / MGL (5),
  Rafael Galvani (3), Fernando Moreira (2). Os demais: 0.
- **Banco: 0 novos / 20 já existiam.** Os 20 imóveis já haviam sido capturados **no mesmo dia**
  (12:28–14:17) pelas rodadas **JUCER/RR-RO** e **JUCEPAR** — são os **mesmos leiloeiros nacionais
  registrados em várias juntas**. A dedup por URL impediu duplicatas (comportamento correto da
  seção 0). Verificação CSV↔banco fecha: **20 coletados = 20 gravados** (0 novos + 20 existentes).

### 39.2. Principais dificuldades enfrentadas

1. **PDF sem sites e sem status.** O PDF lista nome, endereço, contato e e-mail — mas **não traz o
   site nem a situação (regular/cancelado/suspenso)**. Os sites tiveram de ser descobertos por:
   (a) domínio do e-mail corporativo (~10 casos), (b) busca web (Curiau/Zingra, Fantinatto,
   Canezin, Maestro/Tobelem, Hugo→`moreirapimenta.com.br`, Wirna→`vipleiloes.com.br`),
   (c) cruzamento com o CSV de RR/RO (Wesley→`wrleiloes`, MGL→`mgl.com.br`).

2. **Status regular não verificável offline.** A consulta pública oficial
   `https://consultapublica.ap.gov.br/leiloeiros` (e o antigo `jucap.ap.gov.br/leiloeiros`)
   retornam **erro de certificado TLS** (`ERR_TLS_CERT_ALTNAME_INVALID`) no WebFetch e **404** no
   path antigo. Não foi possível confirmar o status individual de forma automatizada → assumiu-se o
   **PDF como a lista de regulares**. ⚠️ Há indício (doc judicial de 2024) de **cancelamento de
   Sidney Canezin** — não confirmado; mantido na lista com ressalva.

3. **3 leiloeiros sem site (Jaciara, Henrique, Daniel Ribas).** Não possuem plataforma própria
   (Daniel Ribas/DRR opera só por Instagram/Threads/Facebook). Impossível raspar imóveis → pulados.

4. **Sobreposição de plataformas nacionais.** Daniel Garcia, Galvani, Fernando e o grupo MGL
   (Jonas/Lucas) atuam em RR, RO, PR **e** AP. Como a captura é por **URL canônica**, todos os 20
   imóveis já estavam no banco das rodadas do mesmo dia → **0 novos** para o Amapá (esperado).

5. **Diversidade de plataformas / conteúdo dinâmico** (causa-raiz dos `0 cards`):
   - **Wix (Canezin):** SPA Wix/Thunderbolt; HTML de 525 KB sem **nenhuma URL de lote**; lotes (se
     houver) ficam em galeria dinâmica ou iframe externo → extrator genérico não enxerga.
   - **SPA pesado com trackers (VIP/Wirna):** `networkidle` **estoura timeout** por causa de
     RDStation/LinkedIn/Azure Insights nunca cessarem a rede; listagem fica sob rota de busca não
     linkada na home.
   - **Plataforma Leiloar/`externo` (Curiau, Fantinatto):** home traz só o formulário de busca; na
     data **não havia leilão ativo** (mensagem "encerrado") → **0 legítimo**.
   - **Next.js/RSC (Norte/Sandro):** os ~12 lotes vêm no **payload RSC** e via **lazy-load** na
     página `/lotes` (não na home). A API interna `sistema.norteleiloes.com.br/lotes/listar-dados?
     api=true` retorna **só os filtros** (e são de **veículos**: marca/modelo/combustível/ano). O
     scraper genérico, rodando na home sem scroll, pegou **2 de ~12** lotes.

6. **Filtro de 1ª praça excluiu corretamente lotes do dia.** O inventário ativo do Norte em 09/06
   era de leilões **com praça hoje** (`09/06 - 09h30/10h00`), além de veículos. Como a regra exige
   **1ª praça estritamente posterior** à captura, foram (corretamente) descartados → 0 imóveis.

7. **Qualidade de dados na extração genérica:**
   - **Pixel de tracking capturado como imagem:** nas linhas de Daniel Garcia, o campo `imagem`
     recebeu `https://www.facebook.com/tr?id=...` (pixel 1x1 do Meta), não a foto do imóvel —
     `block.find("img")` pega o primeiro `<img>`, que pode ser um tracker.
   - **Anexos imprecisos:** apontam para a página de detalhe (`/item/...`) em vez do PDF real do
     edital/matrícula (o link do PDF exige abrir o detalhe).
   - **Mojibake** em alguns títulos/cidades (`Florian�polis`, `Chapec�`) por encoding na origem.

### 39.3. Correções sugeridas

1. **Adapter por plataforma (camada da seção 27):**
   - **Norte (Next.js/RSC):** abrir `/lotes`, fazer **scroll incremental** até parar de crescer,
     coletar `/lote/\d+`, e **drillar cada detalhe** (a data de 1ª praça e o tipo imóvel/veículo só
     aparecem no lote). Classificar imóvel vs veículo pelos filtros (`categoria_bem`, seção 22).
   - **Leiloar/`externo` (Curiau/Fantinatto):** seguir "Leilões atuais" → `/externo/leilao/N` →
     cards `/externo/lote/N`; só há dados quando há leilão aberto.
   - **Wix (Canezin):** detectar `parastorage/thunderbolt` e buscar a plataforma de leilão embutida
     (iframe/subdomínio) ou cair em captura manual; Wix raramente expõe lote em URL.
   - **VIP Leilões (Wirna):** trocar `wait_until` de `networkidle` para `domcontentloaded` +
     `wait_for_selector` do card + descobrir o endpoint de busca/listagem (XHR) em vez da home.
2. **Imagem real, não pixel:** ignorar `src` de domínios de tracking
   (`facebook.com/tr`, `google-analytics`, `doubleclick`, `1x1`, `pixel`, dimensões ≤ 2px) e
   preferir `data-src`/`srcset`/`og:image` do detalhe.
3. **Anexos = PDF de fato:** no detalhe, exigir `href` terminando em `.pdf` ou rotulado
   edital/matrícula/laudo; descartar links para a própria página do item.
4. **Encoding:** normalizar para UTF-8 na leitura (`resp.encoding = resp.apparent_encoding`)
   e aplicar `ftfy`/`unidecode` defensivo nos títulos.
5. **Status JUCAP:** raspar `consultapublica.ap.gov.br/leiloeiros` via **Playwright com
   `ignore_https_errors=True`** (ou FlareSolverr) para confirmar regular/cancelado/suspenso —
   resolve o item 2 e a ressalva do Canezin.
6. **Descoberta de site automatizada:** módulo que, a partir do e-mail, testa
   `{dominio_email}` e variações `{slug}leiloes.com.br`, valida via HTTP (já feito aqui), e cai em
   busca web só no resíduo — reduz o trabalho manual de mapear sites a partir de um PDF.


---

## 40. Scraping JUCEAC/AC — leiloeiros REGULARES do Acre (PDF "Leiloeiros Acre", jun/2026)

> Captura a partir do PDF com **7 URLs** de leiloeiros do Acre (sem nomes — só links). Reaproveitou
> integralmente o motor genérico de `scraper_amapa.py` (Playwright + fallback FlareSolverr, filtro de
> 1ª praça futura, dedup por URL, report a cada 5 min). Entrada `csv/leiloeiros_acre_2026-06-09.csv`,
> script `scraper_acre.py`, junta `JUCEAC/AC`. Captura em 09/06/2026 16:40–16:54.

### 40.1. Resultado

| Leiloeiro | Site | Status | Imóveis (1ª praça futura) |
|---|---|---|---|
| Thais Teixeira Leilões | thaisteixeiraleiloes.com.br | ok | 12 |
| Deonizia Leilões | deonizialeiloes.com.br | ok | 6 |
| Leilões Aguiar e Oliani | leiloesaguiar.com.br | ok | 6 |
| SA Leilões | saleiloes.com.br | ok (0 cards) | 0 |
| Lote Leilões | loteleiloes.com.br/site | ok (0 cards) | 0 |
| J Silva Leilões | jsilvaleiloes.com.br | "Welcome to nginx!" | 0 |
| Danie Garcia Leilões | daniegarcialeiloes.com.br | **inacessível** (DNS/offline) | 0 |

- **Total coletado (válido, 1ª praça > data da captura): 24 imóveis.** Cobertura de campos:
  21/24 com imagem, 21/24 com anexos (edital/matrícula), 21/24 com preço; 100% com data futura
  (datas vistas: 18/06/2026 e 01/07/2026).
- **Banco:** `novos=0, já_existiam=24`. **Verificação CSV↔banco fecha** (24 válidos = 0 novos + 24
  já existentes). CSV de imóveis em `csv/imoveis_acre_2026-06-09.csv`; CSV nome+site em
  `csv/leiloeiros_acre_2026-06-09.csv`.

### 40.2. Principais dificuldades

1. **Overlap de leiloeiros entre juntas estaduais (a descoberta central).** Os 24 imóveis dos 3 sites
   ativos **já estavam no banco sob outras juntas** — `JUCER/RO` (13), `JUCER/RR-RO` (6) e `JUCEMS` (5),
   importados em rodadas anteriores do mesmo dia. Ex.: o lote da Deonizia estava como
   *"Deonizia Kiratch / JUCER/RR-RO"*. Os mesmos leiloeiros são credenciados em **vários estados** e
   listam os **mesmos lotes físicos** (mesma URL canônica). O dedup por URL fez o certo (não duplicou),
   mas isso significa que a junta `JUCEAC/AC` fica com **0 linhas próprias** no banco, ainda que a
   captura tenha "encontrado" 24 imóveis.
2. **PDF sem nomes de leiloeiro.** A entrada trazia apenas URLs; os nomes tiveram de ser inferidos do
   `<title>`/domínio (ex.: "Leilões Aguiar e Oliani", "Thais Teixeira Leilões"). Sem cruzar com o
   cadastro oficial da JUCEAC, o `nome` e o número de matrícula/situação (regular×suspenso) ficam
   incertos.
3. **2 dos 7 sites sem conteúdo raspável.** `jsilvaleiloes.com.br` responde a página **default do nginx**
   ("Welcome to nginx!" — site não publicado) e `daniegarcialeiloes.com.br` dá **ConnectionError**
   (DNS/host offline). Nenhum lote disponível.
4. **2 sites ativos retornaram 0 cards** (`saleiloes.com.br`, `loteleiloes.com.br`). O `saleiloes` é
   SPA com `<title>` vazio no HTML inicial (conteúdo só após JS/XHR que o render genérico não disparou);
   o `loteleiloes` é majoritariamente **veículos/carros** ("Leilões de carros... Rio Branco") — sem
   imóveis ou com layout de card fora dos seletores genéricos.
5. **Latência das páginas de detalhe.** Quando o card não traz data, o scraper drilla o lote
   (`detail_budget=12`); com fallback FlareSolverr (até 70 s/req) o site da Thais levou ~6 min sozinho.
6. **Parse de cidade impreciso.** A regex `Cidade/UF` pega só a última palavra do nome composto
   ("Guaporé/RO" em vez de "São Miguel do Guaporé/RO", "Oeste/RO" em vez de "Espigão do Oeste/RO").

### 40.3. Correções sugeridas

1. **Tornar o dedup ciente de multi-junta (modelar leiloeiro N:N estado).** Em vez de tratar a colisão
   de URL como "já existe → ignora", registrar a **associação leiloeiro↔junta** numa tabela de vínculo
   (ou campo `juntas` multivalorado no imóvel) para que o lote apareça **também** sob `JUCEAC/AC` sem
   duplicar a linha física. Hoje a contagem por junta subestima a presença real do leiloeiro.
   - Mitigação imediata sem mudar schema: ao detectar URL existente, **acrescentar a nova junta** ao
     campo `junta` (ex.: `JUCER/RO; JUCEAC/AC`) em vez de só pular.
2. **Cruzar o PDF com o cadastro oficial da JUCEAC** (lista de leiloeiros regulares + matrícula +
   situação) para preencher `nome`, `matricula` e confirmar **regular × suspenso/cancelado** na origem,
   em vez de inferir do `<title>`. Mesmo padrão das juntas anteriores (RR/RO, MS, AP).
3. **Adapter para SPA com `<title>` vazio (saleiloes).** Antes de raspar a DOM, **interceptar XHR/Fetch**
   (`page.on("response")`) e capturar o JSON da API interna; se não houver, fazer **scroll incremental**
   + esperar seletor de card específico do site. Sem isso, SPAs retornam 0 cards.
4. **Pré-classificar sites só de veículos (loteleiloes).** Detectar pelo `<title>`/categorias
   ("carros", "veículos") e ou pular ou aplicar o filtro `categoria_bem` (seção 22) para não gastar
   render à toa em sites sem imóveis.
5. **Sondagem de saúde do site antes de renderizar.** Um `requests.head/get` rápido que detecte
   "Welcome to nginx!", `ConnectionError`/`NXDOMAIN` e classifique o site como *sem site publicado* /
   *offline* — economiza um ciclo de Playwright+FlareSolverr e deixa o status do relatório mais preciso.
6. **Melhorar a regex de Cidade/UF** para capturar nomes compostos com preposições
   (`(?:[A-ZÀ-Ý][a-zà-ÿ]+|d[aeo]s?|\s)+`), evitando truncar "São Miguel do Guaporé" → "Guaporé".
7. **Reduzir latência do drill de detalhe:** subir o orçamento por site mas paralelizar contextos
   Playwright, e só cair no FlareSolverr quando o Playwright realmente detectar challenge (já é o caso),
   evitando o custo de 70 s por lote em sites sem Cloudflare.


### 39.4. Adapter Norte Leilões implementado (`scraper_norte.py`)

Construído o adapter dedicado que resolve o item 5 (Next.js/RSC) e os itens 6–7 (data e qualidade).

**Fluxo:** `/lotes` → scroll incremental até estabilizar → coleta `/lote/<id>` → render de cada
detalhe → extrai título (`CÓDIGO - CIDADE/UF - TIPO - desc`), **1ª praça**, avaliação, imagem real
e anexos → filtra imóvel + 1ª praça > hoje → insere no SQLite (dedup) + CSV.

**Armadilha decisiva — a data certa da 1ª praça:** o scraper genérico capturava o cabeçalho global
`PRÓXIMO LEILÃO: HOJE, 09/06/2026` (mesma data em todo lote) e por isso descartava tudo. A 1ª praça
real está na linha do lote: `1º LEILÃO ... ID <n> • DD/MM/YYYY • HHhMM`. **O separador é `•`
(U+2022), não hífen** — a regex inicial `[—\-–]` não casava e retornava 0. Corrigido para
`ID\s*\d+\D{1,6}(\d{1,2}/\d{1,2}/\d{4})`. Só aceita lote rotulado **1º LEILÃO** (se a página é
"2º LEILÃO", a 1ª praça já passou → descarta).

**Outras correções aplicadas no adapter:**
- **Imagem real:** ignora `_250_50` (logo), `facebook`/`/tr?` (pixel) → pega `_534_380.jpeg`.
- **Anexos PDF de fato:** `.pdf` ou `download-veiculo-arquivo`/`download-arquivo`.
- **Classificação imóvel × veículo** pelo TIPO no título (lista `IMOVEL`/`VEICULO`).

**Resultado (09/06/2026):** 12 lotes ativos no Norte → 2 imóveis (resto veículos) → **1 com 1ª praça
futura** (Imóvel Comercial — terreno c/ posto de combustível, **Viseu/PA**, 1ª praça **15/06/2026**,
avaliação **R$ 185.000,00**). **1 novo inserido no banco** (o genérico dava 0). Casa em Ananindeua/PA
tinha 1ª praça hoje → corretamente excluída. CSV: `csv/imoveis_norte_amapa_2026-06-09.csv`.

### 38.7. Varredura final dos sites em 0 — o "0" é legítimo na maioria

Antes de escrever parsers SSR sob medida, confirmei **por que** cada site restante rende 0.
Resultado: na quase totalidade **não há imóvel a capturar**, então o 0 é correto, não falha:

| Site | Diagnóstico | É falha do scraper? |
|------|-------------|---------------------|
| Beltrame | `/leilao/lotes/imoveis` → **"IMÓVEIS 0 itens"** (todas as categorias 0) | Não — sem inventário |
| Portela | `/lotes/imovel` renderiza sem lotes (0 links, 0 datas) | Não — sem inventário |
| Norte | `lotes-categorias` → imóveis `quantidade 0` | Não — sem inventário |
| Asami | Superbid, filtro imóveis → 0 aberto futuro | Não — sem inventário |
| Mayco, Marcus (maleiloesro) | página parkada (`len ≈ 67`) | Não — site parkado |
| vbleiloes | ConnectionError (DNS) | Não — site fora do ar |
| Reis | HTTP 401 persistente | Bloqueio — exigiria credencial |
| Lucas | espelho do backend do Fernando (MGL) | Não — dedup por URL |
| **LanceVip, Vinco** | **SPA** (suporteleiloes / PHP+JS): a home não expõe links de lote/categoria no DOM raspável e **não há API de listagem** observável (a telemetria só registra `entity:lote` individual) | **Sim, parcial** — precisaria de engenharia reversa mais profunda |

**Conclusão operacional:** o único adapter de bom custo/benefício deste lote foi o **Superbid**
(seção 38.6). Construir parsers SSR para Beltrame/Portela/Norte seria inútil agora (estão
vazios); os únicos com possível ganho futuro são **LanceVip e Vinco** (SPAs), que demandam
mapear o backend real (provável `api.suporteleiloes.com.br` / endpoint PHP de busca) — fica como
trabalho futuro de ROI incerto, a reavaliar quando esses leiloeiros tiverem leilão de imóvel
ativo.

**Aprendizado para o guia:** antes de codar parser por site, **medir o inventário** (contar
"itens" por categoria / lotes na página). Muitos "0" são estado real do leilão (sem imóvel
aberto), não bug — evita gastar tempo raspando catálogo vazio.


### 39.5. Adapter Hoppe (Superbid) implementado (`scraper_hoppe.py`)

Segundo adapter — resolve o item 4/diversidade para sites **Superbid** (Hoppe = `hoppeleiloes.com.br`),
onde os lotes vêm de **API tipada**, não do DOM (por isso o genérico dava 0).

**Pipeline da API (replicado do XHR real do site):**
1. `GET siteconfigprod.superbid.net/<host>/style.config.json` → `storeId` (Hoppe = **16194**).
2. `GET offer-query.superbid.net/offers/` com os parâmetros exatos observados via `page.on("request")`:
   `filter=product.productType.id:13;stores.id:16194` (13 = Imóveis), `portalId=[2,15]`,
   `requestOrigin=store`, `searchType=opened`, `pageNumber`/`pageSize`, `timeZoneId=America/Sao_Paulo`.
   ⚠️ **Armadilhas:** o param é `pageSize` (não `size` → 400 "[size] inválido"); `portalId` é a string
   literal `[2,15]`; e o filtro mínimo é `stores.id` — sem ele dá 400.
3. Mapeia: `product.shortDesc` (título), `product.thumbnailUrl` (imagem),
   `product.location.city` → cidade/UF, `priceFormatted`/`price`, URL canônica `https://<host>/oferta/<id>`.

**Regra da 1ª praça (campo `auction.judicialPraca` + `eventPipeline.stages`):**
- `judicialPraca == 2` → a 1ª praça **já ocorreu**: 1ª praça = `stages[0].endDate` (passado) → exclui.
- `1ª Praça` ou `Praça Única` → 1ª praça = `offer.endDate`.
- ⚠️ **Comparar por DATA, não datetime:** o `endDate` traz hora (ex.: `09/06 16:00`), que passava no
  filtro `> hoje 00:00`. Corrigido para `primeira.date() > hoje.date()` — assim um lote cuja 1ª praça
  é **no próprio dia da captura** (caso *Major Vieira/SC*, já em 2ª praça) é **corretamente excluído**.

**Resultado (09/06/2026):** API retornou **15 ofertas de imóvel**; **14 com 1ª praça futura**
(1 excluído por 1ª praça = hoje). **10 novos** inseridos sob `Alex Willian Hoppe / JUCAP/AP`
(4 já existiam sob outra junta via run Superbid anterior — dedup por URL). Imóveis em SC e PR
(Canoinhas, Bombinhas, São Bento do Sul, Londrina/PR etc.). CSV: `csv/imoveis_hoppe_amapa_2026-06-09.csv`.

**Reaproveitável:** o mesmo `scraper_hoppe.py` serve para qualquer site Superbid — basta trocar
`HOST`/`STORE_ID` (descobríveis em `style.config.json`).

### 39.6. Saldo após os dois adapters

| Leiloeiro | Plataforma | Genérico | Com adapter | Novos no banco (JUCAP/AP) |
|-----------|-----------|---------:|------------:|--------------------------:|
| Alex Willian Hoppe | Superbid | 0 | 14 | **10** |
| Sandro de Oliveira (Norte) | Next.js/RSC | 0 | 1 | **1** |

**Total de imóveis genuinamente novos do Amapá: 11** (todos com 1ª praça posterior a 09/06/2026,
foto e dados tipados). Próximos alvos de menor retorno hoje: Maestro/Leonardo (`/leilao/N` sem lote
ativo), Curiau/Fantinatto (Leiloar sem leilão aberto), Canezin (Wix) e VIP/Wirna (imóveis vão para
plataforma nacional `leilaovip`, não atribuíveis à leiloeira).

### 38.8. Adapter LanceVip (plataforma suporteleiloes — SSR)

Mapeamento do backend do LanceVip (pedido: "numa data com leilão de imóvel ativo"). Diagnóstico:

- **Não há API JSON de lotes.** O único XHR é telemetria (`stats.suporteleiloes.com.br/ping` e
  `/register` com `entity:lote, entityId`). A listagem e o lote são **SSR** (HTML renderizado no
  servidor); o app é um bundle compilado (`/build/app.*.js`).
- **Estrutura:** home (SSR, ~180 KB) lista eventos `/eventos/leilao/<id>/<slug>`. Os lotes ficam
  em `/eventos/leilao/<id>/<slug>/lote` (requests direto dá **500** — precisa render Playwright;
  via FlareSolverr/render funciona). Cada evento `sicoob-...-imovel-...` corresponde a **1
  imóvel**.
- **Maioria dos eventos é veículo/venda-direta** (ex.: evento 2631 = "VW POLO SEDAN") — por isso
  o crawl genérico dava 0 imóvel. Filtrar pelo **slug do evento** (`imove|rural|terreno|casa|...`)
  é o que isola os imóveis.

**Adapter** (`lancevip_adapter.py`, integrado no topo de `scrape_leiloeiro`):
1. `GET /` (home, SSR) → coleta `/eventos/leilao/<id>/<slug>` cujo slug casa imóvel.
2. Para cada um, `render(<url>/lote)` (Playwright) → extrai `H1` (título), datas (1ª praça =
   menor data futura), `R$` (lance inicial via contexto "lance inicial"/2º maior valor), 1ª
   imagem de conteúdo e anexos (edital/matrícula).
3. Cidade/UF do título (`... EM <cidade>/<UF>`).

**Resultado:** **2 imóveis em RO** capturados limpos:
- `IMÓVEL EM BURITIS/RO` — 1ª praça 15/06/2026 — lance R$ 1.770.808,05 (aval. 3.541.616,10)
- `IMÓVEL RURAL EM OURO PRETO D'OESTE/RO` — 1ª praça 19/06/2026 — lance R$ 341.494,00

Adicionados ao CSV (`csv/imoveis_rr_ro_2026-06-09.csv`, agora **89 linhas**) e ao SQLite.

> **Achado de qualidade em produção:** um scraper **anterior** já havia importado esses mesmos
> dois URLs (ids 201467, 201461), porém com **dados errados** — `valor_minimo` R$ 15,00 / R$ 22,00
> (pegou o incremento, não o lance) e `cidade` quebrada ("OESTE"). O `importar-scraping` os tratou
> como duplicados (dedup por `url_original`) e **manteve os valores errados**. A correção dos 2
> registros (UPDATE de `valor_minimo`/`cidade` com os valores corretos do adapter) **requer
> autorização explícita** para escrita direta no Postgres de produção — ficou pendente.

**Aprendizado:** dedup por URL protege contra duplicata, mas **não corrige** um registro
pré-existente de baixa qualidade. Para fontes reescrapeadas com melhor parser, o pipeline
precisaria de um modo **upsert/atualização** (não só "inserir se novo"), idealmente comparando
qualidade (ex.: preencher `valor_minimo` quando o existente é nulo/lixo).

### 38.9. Modo `--upsert` no `importar-scraping` (corrige registros pré-existentes de baixa qualidade)

Problema (seção 38.8): o `importar-scraping` deduplica por `url_original` e **pula** o que já
existe — então um registro antigo com dado errado (ex.: LanceVip com `valor_minimo` R$ 15/22 =
incremento, e `cidade` quebrada) **nunca é corrigido**, mesmo reescrapeando com um parser melhor.

**Solução estrutural implementada** (em vez de UPDATE manual em produção): flag `--upsert`.

`pipeline/importar_scraping.py`:
- Na duplicata, com `upsert=True`, carrega o `Imovel` existente e chama `_aplicar_upsert(existente,
  novo)`, que atualiza **conservadoramente**:
  - `valor_minimo`: preenche nulo **ou** corrige "lixo" (`_is_junk_preco` = nulo ou `< R$100`)
    quando o novo é plausível (`≥ R$100`);
  - `cidade`: preenche nula; corrige quando a linha estava com **preço lixo** (sinal de parse ruim);
  - `estado`/`imagem_principal`: preenche apenas nulos;
  - `tipo_imovel`: troca `OUTRO` por um tipo específico quando o novo tiver.
- Novo contador `atualizados` no retorno/log. Sem `--upsert`, comportamento inalterado (só conta dup).

`run.py importar-scraping`:
```
python run.py importar-scraping --arquivo scraping/<arquivo>.jsonl --upsert
```

**Resultado da execução** sobre os 89 imóveis RR/RO: `0 inseridos, 19 atualizados, 70 duplicados`.
Os 2 imóveis LanceVip ficaram corretos:
- `IMÓVEL EM BURITIS/RO` → `valor_minimo` **R$ 1.770.808,05** (era 15,00)
- `IMÓVEL RURAL EM OURO PRETO D'OESTE/RO` → cidade **"Ouro Preto D'Oeste"** + `valor_minimo`
  **R$ 341.494,00** (era 22,00)

E outros **17** registros antigos com campo nulo/lixo foram preenchidos no mesmo passe.

> **Princípio:** dedup evita duplicar, mas não melhora dado existente. Reescrape com parser melhor
> deve usar **upsert conservador** (preenche nulo, corrige só o que é claramente lixo) — nunca
> sobrescrever cegamente, para não degradar registros bons. A escrita direta de linhas em produção
> (UPDATE manual) foi **evitada**; a correção passou pelo pipeline auditável.

---

## 41. Scraping JUCERN/RN — leiloeiros REGULARES do Rio Grande do Norte (PDF "Leiloeiros Rio Grande do Norte", jun/2026)

> Captura a partir do PDF da JUCERN com **29 leiloeiros**, dos quais só **16 REGULARES** foram
> raspados — excluídos os marcados como **SUSPENSO, IRREGULAR, CANCELADO, REGISTRO SUSPENSO** e
> **"cancelado a pedido"** (13 descartados). Reaproveitou integralmente o motor de `scraper_acre.py`
> (Playwright + fallback FlareSolverr, filtro de 1ª praça futura, dedup por URL, report a cada 5 min).
> Entrada `csv/leiloeiros_rn_2026-06-09.csv`, script `scraper_rn.py`, junta `JUCERN/RN`.
> Captura em 09/06/2026 18:14–18:40.

### 41.1. Seleção de regulares (filtro na origem, no PDF)

Diferente das juntas que mandam só URLs (AC/AP), o PDF do RN traz **nome + situação** por leiloeiro.
Bastou ler o rótulo ao lado do nome. **Excluídos (13):** Roberto A. N. Fernandes (SUSPENSO), José Gomes
da Costa Neto (IRREGULAR), Francisco Luã Costa (IRREGULAR), Luciano de Morais R. Soares (IRREGULAR),
Celso Alves Cunha (CANCELADO por decisão judicial), Antônio Romero F. da Silva (IRREGULAR), Josecelli
Kildare F. Gomes (CANCELADO), Raphael Queiroga Magliano (REGISTRO SUSPENSO — não recadastramento),
Davi Borges de Aquino (IRREGULAR), Cyntia A. Diniz Nóbrega (IRREGULAR), Tiago Tessler Blecher
(IRREGULAR), Francisco Jonnathan S. Freitas (cancelado a pedido), Josafa Alves Pinheiro (IRREGULAR).
**Aceitos: 16 regulares.**

### 41.2. Resultado

| Imóveis (1ª praça futura) | Leiloeiro | Site | Status |
|---:|---|---|---|
| 40 | Stella Araújo Zanatta | leiloesrn.com.br | ok (117 cards) |
| 19 | Georgia de Souza Castelo | bomvalor.com.br/montenegroleiloes | ok |
| 5 | Edeylson Peixoto Fidelis | fidelisleiloes.com.br | ok |
| 5 | Rudival Almeida Gomes Júnior | rjleiloes.com.br | ok |
| 5 | Daniel Elias Garcia | danielgarcialeiloes.com.br | ok (já no banco) |
| 0 | Marcus Dantas Nepomuceno | mnleilao.com.br (inferido do e-mail) | ok (0 cards) |
| 0 | Erick Luiz Neves da Câmara | erickleiloes.com.br | ok (0 cards) |
| 0 | Francisco Doege Esteves Filho | lancecertoleiloes.com.br | ok (0 cards) |
| 0 | Filipe Pedro de Araújo | leiloesaraujo.com.br | ok (3 cards, sem 1ª praça futura) |
| 0 | Alex Willian Hoppe | hoppeleiloes.com.br | ok (1 card) |
| 0 | Renato Schlobach Moysés | rmoyses.com.br | ok (2 cards) |
| 0 | Samara Barbosa Araújo | colossoleiloes.com.br (inferido do e-mail) | ok (0 cards) |
| 0 | Roberto A. N. Fernandes Filho | robertofernandesleiloes.com | **inacessível** |
| 0 | Alessandra C. A. de Carlos | alessandraleiloes.com | **inacessível** |
| 0 | Davi Eduardo Paulim | — | **sem site** (só e-mail yahoo) |
| 0 | Jussara Daniele de Medeiros | — | **sem site** (só e-mail gmail) |

- **Total coletado (válido, 1ª praça > data da captura): 74 imóveis.**
- **Banco:** `novos=69, já_existiam=5`. **Verificação CSV↔banco fecha** (74 URLs do CSV = 74 no banco,
  0 faltando). Os 5 que "já existiam" são de **Daniel Elias Garcia**, leiloeiro multi-estado já gravado
  sob `JUCER/RR-RO` — dedup por URL fez o certo. CSV de imóveis em `csv/imoveis_rn_2026-06-09.csv`;
  CSV nome+site em `csv/leiloeiros_rn_2026-06-09.csv`.

### 41.3. Principais dificuldades

1. **Baixíssima taxa de aproveitamento dos sites (5 de 16 deram imóveis).** A maioria dos leiloeiros
   regulares do RN ou não tem imóvel com 1ª praça futura no momento, ou usa sites com layout que o
   extrator genérico não casa. **45 dos 74 imóveis vêm de um único leiloeiro** (Stella/leiloesrn.com.br);
   a captura é fortemente concentrada.
2. **2 sites inacessíveis** (`robertofernandesleiloes.com`, `alessandraleiloes.com`) — DNS/host offline
   ou TLS inválido. Nenhum lote.
3. **2 leiloeiros sem site no PDF** (Davi Eduardo Paulim, Jussara Daniele de Medeiros) — só e-mail em
   provedor genérico (yahoo/gmail), sem domínio próprio para raspar. Pulados.
4. **Sites com e-mail mas sem campo "Site" explícito** (Marcus Nepomuceno → `mnleilao.com.br`; Samara
   → `colossoleiloes.com.br`): o domínio teve de ser **inferido do e-mail corporativo**. Ambos
   responderam, mas com 0 cards — inferência correta do host não garante conteúdo raspável.
5. **Overlap multi-estado (mesmo padrão das juntas anteriores).** Daniel Elias Garcia é credenciado em
   RN e em RR/RO; os 5 lotes já estavam no banco sob `JUCER/RR-RO`. A junta `JUCERN/RN` fica com 69
   linhas próprias (não 74), embora a captura tenha "encontrado" 74.
6. **Sites SPA/landing com 0 cards** (mnleilao, erickleiloes, lancecertoleiloes, colosso): `<title>`/HTML
   inicial sem lotes; o conteúdo (se houver) vem por XHR/JS que o render genérico não dispara.
7. **Latência alta em alguns sites** (lancecertoleiloes ~3 min, rjleiloes ~2,5 min) por causa do
   drill de detalhe + fallback FlareSolverr (até 70 s/req) quando o card não traz data.

### 41.4. Correções sugeridas

1. **Dedup ciente de multi-junta (recorrente — já levantado na seção 40).** Modelar leiloeiro↔junta como
   N:N (tabela de vínculo) ou, sem mudar schema, **acrescentar a nova junta** ao campo `junta` ao
   detectar URL existente (`JUCER/RR-RO; JUCERN/RN`) em vez de só pular — hoje a contagem por junta
   subestima a presença real.
2. **Inferência de domínio a partir do e-mail como fallback de cadastro.** Já aplicada manualmente aqui;
   automatizar: quando não houver "Site", derivar `https://www.<domínio-do-email>` se o domínio **não**
   for provedor genérico (gmail/yahoo/hotmail/outlook/live/ig). Isso recupera leiloeiros que só
   publicam e-mail corporativo.
3. **Adapters por plataforma para os SPAs de 0 cards.** vários sites RN são SPA: interceptar XHR/Fetch
   (`page.on("response")`) e ler o JSON da API interna, ou scroll incremental + espera de seletor
   específico. Sem isso, retornam 0 — exatamente como saleiloes (AC) na seção 40.
4. **Sondagem de saúde do site antes de renderizar** (`requests.get` com timeout curto) para classificar
   *offline*/*DNS inválido*/*sem site publicado* sem gastar um ciclo Playwright+FlareSolverr — torna o
   status do relatório mais preciso e acelera a rodada.
5. **Cruzar com o cadastro oficial da JUCERN** (matrícula + situação) para confirmar regular×suspenso na
   origem e pegar o **site oficial** quando o PDF só traz e-mail — evita a inferência de domínio.
6. **Reduzir latência do drill** paralelizando contextos Playwright e só caindo no FlareSolverr quando
   houver challenge real (já é o caso) — evita 70 s/lote em sites sem Cloudflare.

### 41.5. Correções IMPLEMENTADAS (módulo `scraper_commons.py`, 09/06/2026 21:09)

As 4 correções dos itens 1–4 de 41.4 foram codificadas num módulo reaproveitável
**`scraper_commons.py`** (funções puras, `requests` + stdlib) e ligadas ao `scraper_rn.py`. Os demais
scrapers de junta podem `import scraper_commons` e ganhar o mesmo comportamento.

| # | Correção | Função em `scraper_commons.py` | Como foi ligada no `scraper_rn.py` |
|---|---|---|---|
| 1 | Dedup multi-junta | `merge_juntas(atual, nova)` | `db_insert`: ao achar URL existente, faz `UPDATE imoveis SET junta=merge_juntas(...)` em vez de só pular; conta `multijunta`. |
| 2 | Inferência de domínio por e-mail | `site_from_email(email)`, `candidate_sites(site, email)` | `scrape_leiloeiro` monta a lista de candidatos = site do PDF **+** `https://www.<domínio-do-email>` (se não-genérico) e tenta na ordem. Exige coluna `email` no CSV de leiloeiros. |
| 3 | Adapter SPA (XHR + scroll) | `cards_from_json(payloads)`, `walk_json` | `render(..., capture_xhr=True, scroll=True)` intercepta `page.on("response")` com `content-type: json` e rola a página; quando o HTML dá **0 cards**, `_spa_cards` extrai lotes do JSON da API interna. |
| 4 | Sondagem de saúde | `site_health(url)` | `scrape_leiloeiro` roda `site_health` em cada candidato **antes** de abrir o Playwright; classifica `dns_invalido`/`offline`/`timeout`/`sem_site_publicado`/`http_4xx` e só renderiza os vivos (HTTP 403/503 = provável Cloudflare → renderiza). |

**Detalhes de projeto que evitaram regressões:**

- `render()` ganhou parâmetros **opcionais** `capture_xhr`/`scroll`; sem eles a assinatura `(html, final)`
  é preservada — nenhum chamador existente quebrou. Com `capture_xhr=True` retorna `(html, final, payloads)`.
- `db_insert()` agora retorna **3 valores** `(novos, existe, multijunta)`; o `main()` foi atualizado.
- `GENERIC_EMAIL_DOMAINS` (gmail/yahoo/hotmail/outlook/live/ig/uol/bol/terra...) impede inferir site de
  provedor pessoal — Davi Paulim (yahoo) e Jussara (gmail) seguem corretamente como "sem site".
- `site_health` resolve **DNS primeiro** (`socket.getaddrinfo`) para cortar `NXDOMAIN` sem nem abrir socket HTTP.

**Resultado da re-execução (21:09–21:41) — ganhos medidos vs. a 1ª rodada (74 imóveis):**

- **77 imóveis** (1ª praça futura), +3 — o **adapter SPA** recuperou cards onde antes dava 0:
  `erickleiloes.com.br` (0 → **6 cards** via XHR/scroll) e `fidelisleiloes.com.br` (5 → **8**).
- **Dedup multi-junta:** `multijunta+=8` — 5 lotes viraram `JUCER/RR-RO; JUCERN/RN` (Daniel Garcia) e 3
  viraram `JUCEPE/PE; JUCERN/RN` (Edeylson). **Lotes associados a JUCERN/RN subiram de 69 → 77** sem
  duplicar nenhuma linha física (verificado no banco: `SELECT … WHERE junta LIKE '%JUCERN/RN%'` = 77).
- **Sondagem de saúde:** `robertofernandesleiloes.com` e `alessandraleiloes.com` cortados na hora como
  `dns_invalido` (antes gastavam ~30 s cada em Playwright+FlareSolverr). Erros caíram de 2 → 1.
- **Inferência por e-mail:** em Alessandra, o `.com` do PDF é DNS inválido, mas o domínio do e-mail
  (`alessandraleiloes.com.br`) foi alcançado como candidato alternativo — antes era só "inacessível".
- Itens **5** (cruzar cadastro oficial) e **6** (paralelizar contextos) ficam como melhorias futuras.

---

## 42. Captura dos leiloeiros REGULARES do Pará — JUCEPA (PDF "Leiloeiros Pará", 2026-06-09)

**Scraper:** `scraper_para.py` (clone do `scraper_amapa.py`, seção 39 — mesma engine Playwright + fallback
FlareSolverr, mesmo filtro de 1ª praça futura e dedup global por URL). **Entrada:**
`csv/leiloeiros_para_2026-06-09.csv`. **Junta:** `JUCEPA/PA`. **Progresso:** `scraper_para_progress.json`.

### 42.1. Universo e filtragem na origem
- O PDF lista **61 leiloeiros**. Excluídos **8** por situação irregular na própria JUCEPA:
  marcados `IRREGULAR` (Maria do Socorro Patello de Moraes, Antonio Alberto Correa do Vale, Ananias de Sá,
  Erick Rodrigo Corrêa de Oliveira, Hugo Moreira Pimenta, Tiago Tessler Blecher) ou
  `MATRÍCULA CANCELADA` (Mauricio Ferreira Maciel, Rodrigo Schmitz).
- Restaram **53 REGULARES**, todos gravados no CSV de leiloeiros (nome + site quando publicado).
- Apenas **27 dos 53** trazem site no diário; os outros **26** só publicam e-mail/telefone → não-raspáveis.

### 42.2. Resultado
- **Total de imóveis com 1ª praça > data da captura (09/06/2026): 39.**
- **Banco (`imoveis_leiloeiros.db`):** **27 novos** + **12 já existentes** (dedup por URL) → `JUCEPA/PA` = 27.
- **Verificação CSV↔banco:** 39 linhas no CSV, **0 URLs faltando** no banco. Fecha.
- **Qualidade:** 38/39 com foto, 33/39 com anexo (edital/matrícula).
- **CSVs gerados:** `csv/leiloeiros_para_2026-06-09.csv` e `csv/imoveis_para_2026-06-09.csv`.

**Relação de imóveis por leiloeiro (somente os que renderam):**

| Imóveis | Leiloeiro | Site |
|--:|---|---|
| 12 | Renato Guedes Rocha | rioleiloes.com.br |
| 6 | Victor Oliveira Dorta | victordortaleiloes.com.br |
| 5 | Rafael Galvani Ferreira | galvanileiloes.com.br |
| 5 | Daniel Elias Garcia | danielgarcialeiloes.com.br |
| 5 | José Antônio Rodovalho Junior | joserodovalholeiloes.com.br |
| 2 | Francisco Jonnathan Santos Freitas | franciscofreitasleiloes.com.br |
| 2 | Fernando Caetano Moreira Filho | fernandoleiloeiro.com.br |
| 2 | Ícaro Alexandre Felfili Jardim | felfilileiloes.com.br |

Os demais 19 sites raspáveis retornaram 0 (SPA/0 cards ou inacessível); 26 leiloeiros sem site foram pulados.

### 42.3. Principais dificuldades enfrentadas

1. **26 de 53 regulares sem site no diário (49%).** A JUCEPA publica só e-mail/telefone para metade dos
   regulares. São intrinsecamente não-raspáveis pela rotina atual.
2. **Sites grandes retornando 0 cards (SPA / API interna).** vipleiloes, maestroleiloes, leilo.com.br
   (grupoleilo), lanceja, lmleiloes, celsocunha, alfaleiloes, leiloesmwd, lancecerto, solidus, hoppe,
   norteleiloes — a listagem chega via XHR/Fetch ou exige interação com filtros de busca; o extrator
   genérico de cards não casa nada. Mesmo sintoma das seções 39-41.
3. **3 sites inacessíveis.** `wirnacampos.com.br`, `jonasleiloerio.com.br`, `apiceleiloes.com` — falha de
   DNS/conexão mesmo com fallback FlareSolverr. O domínio `jonasleiloerio` aparenta **erro de digitação no
   PDF** (provável `jonasleiloeiro`).
4. **Divergência de domínio entre juntas → recall perdido.** Lucas Rafael Antunes Moreira
   (`lucasleiloeiro.com.br`) e Jonas Gabriel Antunes Moreira (`jonasleiloerio.com.br`) deram **0** no PA,
   mas os **mesmos leiloeiros** renderam **5 cada** no run do Amapá usando o domínio `mgl.com.br`. O PDF
   da JUCEPA traz o domínio de marca pessoal (parkeado/vazio) em vez da plataforma real onde estão os lotes.
5. **Dedup global "transfere" lotes de leiloeiro multi-estado para a 1ª junta que capturou.** Galvani (5),
   Daniel Garcia (5) e Fernando (2) já estavam no banco sob `JUCAP/AP`; entram como `já existentes` e o
   `JUCEPA/PA` "não fica" com eles. Comportamento esperado (ver [[project_scraper_acre]]), mas mascara a
   real cobertura por junta.
6. **`detail_budget=12` por site limita a confirmação de datas.** Cards sem data na listagem só viram
   imóvel se entrarmos na página de detalhe; acima de 12 detalhes/site, lotes válidos podem ser descartados.

### 42.4. Como corrigir essas falhas (sugestões)

1. **Enriquecer o cadastro de sites antes de raspar:** para leiloeiro sem site, derivar
   `https://www.<domínio-do-email>` quando o domínio **não** for genérico (gmail/yahoo/hotmail/outlook/
   bol/uol/live) — recupera quem só publica e-mail corporativo (ex.: norteleiloes, dgleiloes, lanceja).
2. **Mapa canônico de domínio por leiloeiro, transversal às juntas.** Manter `site_canonico` por CPF/nome e
   **preferir o domínio que historicamente rendeu lotes** (ex.: usar `mgl.com.br` para Lucas/Jonas em vez do
   domínio de marca do PDF). Resolve as falhas 3 e 4 de uma vez.
3. **Adapters por plataforma para os SPAs.** Interceptar `page.on("response")` e ler o JSON da API interna,
   ou scroll incremental + espera de seletor específico. Detectar plataforma (vlance, superbid, sodré, etc.)
   e aplicar o parser certo — sem isso o extrator genérico retorna 0 nos sites grandes (falha 2).
4. **Sondagem de saúde (`requests.get` timeout curto + checagem DNS) antes do Playwright** para classificar
   *offline*/*DNS inválido*/*sem site* sem gastar ciclo de render+FlareSolverr; também sinaliza prováveis
   erros de digitação de domínio (falha 3) para correção manual.
5. **Relatório por junta deve separar "novos" de "já existentes (outra junta)"** explicitando sob qual junta
   o lote foi originalmente gravado — torna a cobertura por estado auditável apesar do dedup global (falha 5).
6. **Elevar `detail_budget`** (ou extrair data da própria listagem/JSON) nos sites que rendem cards mas
   poucas datas, equilibrando recall × tempo (falha 6).

**Conclusão:** a rodada cumpriu a Regra 0 (todos os imóveis válidos no banco, verificação CSV↔banco fechada).
O gargalo de recall não é o filtro de data nem o dedup, e sim **(a)** metade dos regulares sem site e
**(b)** sites SPA cuja listagem só sai via API interna — ambos endereçáveis pelos itens 1-3 acima.

### 38.10. Correção "em escala" — diagnóstico e por que o re-scrape agendado está bloqueado

Pedido: corrigir registros antigos de baixa qualidade em escala (rodar `--upsert` no resto do
dataset ou agendar no Celery beat). Diagnóstico (read-only, antes de qualquer mutação):

**Impacto em produção** (123.058 imóveis): `valor_minimo` nulo = 16.776; `< R$100` = 10.306;
`cidade` nula = 55.987; sem imagem = 38.296.

**Origem do "lixo de valor" (por fonte):** quase tudo de **scrapers standalone fora do beat**:
`frazaoleiloes` 8543 (todos `valor_minimo = NULL`), `e-leiloes.com.br` 5279 (`= 0.00`),
`JUCISRS` 1951, `lut` 1858, `megaleiloes` 1135, `JUCEPAR` 956, etc.

**Por que não dá pra "corrigir em massa" agora:**
1. **É dado faltante, não dado errado patchável.** frazao = NULL, e-leiloes = 0,00 — não há valor
   correto a aplicar sem **re-coletar a fonte**. O `--upsert` só conserta uma linha se receber um
   registro (por URL) com valor melhor; não posso fabricar 27k preços.
2. **Os arquivos-fonte sumiram** (`/app/scraping` só tem os meus `imoveis_rr_ro_*`). O grande
   `imoveis_A_final.jsonl` não está mais lá.

**Por que agendar re-scrape no beat (a opção escolhida) não resolve hoje:**
- O `FrazaoScraper` atual **está quebrado**: `_buscar_pagina(1)` retorna **0 itens** (o site mudou;
  os seletores `.preco/.valor-lance/.bid-value` não casam mais). frazao **não é Superbid** (o
  `style.config.json` não existe), então o adapter pronto não serve.
- `daniel_garcia` e outros scrapers Playwright **nem sobem no worker** (`chromium` ausente em
  `/root/.cache/ms-playwright` no container `leilao_worker`).
- `e-leiloes`, `jucisrs`, `jucepar`, `lut`, `megaleiloes` **não têm classe de scraper** (foram
  imports pontuais de JUCE/sites).
- Agendar um scraper quebrado/que-não-roda é **prejudicial**: gasta ciclos, e como
  `salvar_imoveis` **sobrescreve** `valor_minimo` no conflito (não é coalesce), um re-scrape ruim
  poderia **zerar** dados bons. Por isso **não** adicionei nada ao `beat_schedule`.

**O que já é estrutural e funciona:**
- **Fontes agendadas** (`caixa`, `superbid`, `zuk`, `tjsp`): `pipeline/normalizer.salvar_imoveis`
  faz `on_conflict_do_update` **sobrescrevendo** `valor_minimo/cidade/imagem` a cada ciclo do beat
  → **auto-curam sozinhas**. (Só `latitude/longitude/geocodificado/leiloeiro_id/arquivos/descricao`
  são preservados-se-nulos.)
- **Caminho de import manual**: o novo `importar-scraping --upsert` (seção 38.9) corrige
  null/lixo quando a fonte standalone for **reexecutada** e reimportada.

**Recomendação (próximo passo real, por fonte, em ordem de impacto):**
1. **Reconstruir o `FrazaoScraper`** (8543 linhas) para o site atual — checar se virou outra
   plataforma (Cloudflare/JS) e remapear preço/lote; validar que captura `valor_minimo`; só então
   **agendar `coletar_frazao` no beat** (aí sim auto-cura via `salvar_imoveis`).
2. Instalar `chromium` no `leilao_worker` (`playwright install chromium`) para os scrapers
   Playwright rodarem no beat.
3. Para `e-leiloes/jucisrs/jucepar/...` sem classe: criar scraper/adapter ou re-exportar a fonte e
   reimportar com `--upsert`.

> **Conclusão honesta:** a infraestrutura de auto-cura está correta (upsert no `salvar_imoveis` +
> `--upsert` no import manual). O que falta é **scraper funcional por fonte** — cada um é um
> mini-projeto de engenharia reversa. Não agendei re-scrape de scraper quebrado para não degradar o
> banco.


---

## 43. Scraping JUCEPE/PE — leiloeiros judiciais de Pernambuco (jun/2026)

Captura a partir de dois PDFs: a **lista de leiloeiros judiciais** (com site e período de habilitação) e o **registro da JUCEPE** (com Situação de Matrícula). Reaproveita integralmente o motor do `scraper_para.py` (Playwright + fallback FlareSolverr, filtro de 1ª praça futura, dedup por URL), com uma melhoria: **cache por domínio**.

- **Entrada:** `csv/leiloeiros_pe_2026-06-09.csv` (46 leiloeiros regulares; excluídos Cancelada / Com Pendências/Irregular / Em Andamento).
- **Saída:** `csv/imoveis_pe_2026-06-09.csv` + inserção em `imoveis_leiloeiros.db` (junta `JUCEPE/PE`).
- **Resultado:** 414 linhas no CSV → **253 URLs únicas**, **todas no banco** (128 novas sob JUCEPE/PE; ~125 já existiam sob outras juntas via dedup global por URL). 1 site inacessível.

### 43.1. Principais dificuldades enfrentadas

1. **Duas fontes com dados conflitantes para o mesmo leiloeiro.** O PDF judicial e o registro
   JUCEPE divergem em site (ex.: Flávio Costa = `hastaleilao.com.br` no judicial vs
   `flaviocostaleiloes.com.br` no registro; Rudival = `leilaovip` vs `rjleiloes`; Ícaro =
   `icarotenorioleiloes` vs `organizacaodeleiloes`) e até em situação (Tânia Grimaldi consta
   habilitada no judicial mas "Irregular" na JUCEPE).
2. **Site ausente, mascarado ou inválido em ~40% do registro.** Muitos campos `Site:` traziam
   `000000000000`, o próprio e-mail, ou "Não informado". 18 dos 46 regulares ficaram sem site
   raspável (pulados).
3. **Vários leiloeiros compartilham o mesmo domínio.** leilaopernambuco (Daniel/Flávia),
   leiloesfreire (Osman/Onildo), lancecertoleiloes (Luciano/Roberta), leilaovip, leilaobrasil
   (Irani/Aline) — sem cache, cada domínio seria raspado 2x.
4. **Plataforma nacional infla a contagem.** leilaobrasil.com.br devolveu 160 imóveis (teto de
   cards) de todo o Brasil, não só PE — atribuídos a Irani e Aline. O dedup por URL evitou
   gravar duas vezes, mas a contagem por-leiloeiro fica superestimada e mistura imóveis de
   outras UFs.
5. **Renderização lenta.** Sites com muitos cards forçam até 14 renders de página de detalhe
   (45 s de timeout cada) — sites pesados levaram 4–5 min. Execução total ≈ 1 h.
6. **Condição de corrida ao ler o progresso.** Leitor externo abriu o `*_progress.json` no meio
   de uma escrita e, em cp1252 (Windows), quebrou com `UnicodeDecodeError`.
7. **Site inacessível.** albuquerqueleiloes.com.br (Gervasio) caiu mesmo com fallback FlareSolverr.

### 43.2. Correções sugeridas

1. **Resolver conflito de fonte por precedência explícita:** preferir o site do PDF judicial
   (habilitação ativa) e só cair para o registro JUCEPE quando ausente; registrar ambos numa
   coluna `site_alt` para tentativa secundária.
2. **Inferência de site a partir do e-mail corporativo** (já aplicada: `destakleiloes`,
   `fernandoleiloeiro`, `leilaobrasil`, `rmoyses`, `colodeteleiloes`). Formalizar como passo
   do pipeline e validar com `HEAD`/DNS antes de enfileirar.
3. **Escrita atômica do progresso:** gravar em `*.tmp` e `os.replace()`; leitores sempre com
   `encoding='utf-8', errors='replace'`.
4. **Tag de origem da plataforma + filtro por UF:** marcar imóveis de portais nacionais
   (leilaobrasil, leilaovip) e, quando possível, filtrar por UF=PE no card para não inflar a
   atribuição ao leiloeiro pernambucano.
5. **Paralelizar contextos do Playwright** (2–3 abas) e reduzir `networkidle` para encurtar a
   janela de ~1 h.
6. **Retry programado para inacessíveis** (Gervasio) numa segunda passada com `maxTimeout` maior
   no FlareSolverr.
7. **Rodar `baixar-docs`** para materializar editais/matrículas dos 253 imóveis em `storage/docs/`.

---

## 44. Captura dos leiloeiros REGULARES da Bahia — JUCEB (PDF "Leiloeiros Bahia", 2026-06-09)

**Scraper:** `scraper_bahia.py` (clone do `scraper_para.py`, seção 42 — mesma engine Playwright +
fallback FlareSolverr, mesmo filtro de 1ª praça futura e dedup global por URL). **Entrada:**
`csv/leiloeiros_bahia_2026-06-09.csv`. **Junta:** `JUCEB/BA`. **Progresso:** `scraper_bahia_progress.json`.
**Duração:** ~2h10 (18:35 → 20:47).

### 44.1. Universo e filtragem na origem
- O PDF lista **95 leiloeiros**. Excluídos **7** por situação não-REGULAR: `IRREGULAR`
  (Simone de Queiroz, Vinicius Martins Cutolo, Márcia Cristina dos Santos Silva, Fabio da Silva Ferreira),
  `Em processo de cancelamento de matrícula` (Mike Dutra Fleitas), `Cancelamento de Matrícula em Andamento`
  (Mouzar Baston Filho) e `Destituído como leiloeiro` (Rodrigo Brandel Martins).
- Restaram **88 REGULARES**, todos gravados no CSV de leiloeiros (nome + site quando publicado).
- Apenas **68 dos 88** trazem site no diário; os outros **20** só publicam e-mail gratuito
  (gmail/hotmail/outlook/yahoo) → não-raspáveis. Para vários regulares, o site foi **derivado do
  domínio do e-mail corporativo** (ex.: `contato@alessandraleiloes.com.br` → `alessandraleiloes.com.br`).

### 44.2. Resultado
- **Total de imóveis com 1ª praça > data da captura (09/06/2026): 620.**
- **Banco (`imoveis_leiloeiros.db`):** **250 novos** + **370 já existentes** (dedup por URL) → `JUCEB/BA` = 250.
- **Verificação CSV↔banco:** 620 linhas no CSV; `620 = 250 novos + 370 já existiam`. Fecha sem faltantes.
- **Filtro de data:** 0 imóveis com 1ª praça no passado (100% futuros). **Qualidade:** 242/250 com
  lance inicial, 108/250 com imagem (lazy-load reduz a captura de foto).
- **Distribuição dos 88 sites:** 30 renderam imóveis · 31 acessíveis mas 0 cards (SPA/API interna) ·
  20 sem site · 7 inacessíveis.
- **CSVs gerados:** `csv/leiloeiros_bahia_2026-06-09.csv` e `csv/imoveis_bahia_2026-06-09.csv`.

**Relação de imóveis por leiloeiro (somente os que renderam):**

| Imóveis | Leiloeiro | Site |
|--:|---|---|
| 150 | Irani Flores | leilaobrasil.com.br |
| 150 | Aline Souza Flores | leilaobrasil.com.br (mesmo portal → dedup integral) |
| 144 | Tiago Tessler Blecher | webleiloes.com.br (portal nacional → quase tudo já no banco) |
| 18 | Catiele Borges Leffa | leffaleiloes.com.br |
| 16 | Maria Isabel Brito Mendes Palma Soeiro | isabelleiloes.com.br |
| 15 | Renato Guedes Rocha | rioleiloes.com.br |
| 13 | Angela Saraiva Portes | saraivaleiloes.com.br |
| 10 | José David Gonçalves de Melo | jdleiloes.com.br |
| 9 | Dora Plat | portalzuk.com.br |
| 8 | Rafaela Santos Ribeiro do Vale | rafaelaribeiroleiloes.com.br |
| 7 | Emílio Matos Rocha | emiliomatosleiloes.com.br |
| 7 | Alessandra Cristine Aparecida de Carlos | alessandraleiloes.com.br |
| 7 | Daniel Melo Cruz | grupolance.com.br |
| 7 | Marcus Vinicius Yoshimi Uebara | destakleiloes.com.br |
| 6 | Paulo Cézar Rocha Teixeira | leiloesjudiciaisbahia.com.br |
| 6 | Rodrigo Schmitz | hammer.lel.br |
| 6 | Marco Antônio Barbosa de Oliveira Jr | marcoantonioleiloeiro.com.br |
| 5 | Rudival Almeida Gomes Júnior | rjleiloes.com.br |
| 5 | Viriato Domingues Cravo | cravoleiloes.com.br |
| 5 | Daniel Elias Garcia | danielgarcialeiloes.com.br |
| 5 | Rodrigo Aparecido Rigolon Da Silva | rigolonleiloes.com.br |
| 4 | Hilda Emília de Souza Costa Lima | hastaleiloes.com.br |
| 4 | Maurício Paes Inácio | hastaleiloes.com.br (mesmo site da Hilda → dedup) |
| 4 | Renan Souza Silva | silvaleiloes.com.br |
| 2 | Ivana Montenegro Castelo Branco Rocha | patiorochaleiloes.com.br |
| 2 | Fernando Caetano Moreira Filho | fernandoleiloeiro.com.br |
| 2 | Francisco Jonnathan Santos Freitas | franciscofreitasleiloes.com.br |
| 1 | Fábio Manoel Guimarães | fabioleiloes.com.br |
| 1 | Flávio Alexandre Alves Da Costa e Silva | hastaleilao.com.br |
| 1 | Hélcio Kronberg | kronleiloes.com.br |

Os demais 31 sites acessíveis retornaram 0 (SPA/0 cards); 20 leiloeiros sem site e 7 inacessíveis foram pulados.

### 44.3. Principais dificuldades enfrentadas

1. **Múltiplos leiloeiros compartilhando o mesmo portal → atribuição inflada e dedup massivo.**
   Irani Flores e Aline Souza Flores publicam **ambos via `leilaobrasil.com.br`**; cada um "rendeu" 150,
   mas o 2º foi 100% deduplicado por URL (`db_novos` subiu só +0). Idem hastaleiloes (Hilda + Maurício) e
   ccjleiloes (Miguel + Jamile). O número por leiloeiro **não é exclusivo** do leiloeiro — é o acervo do
   portal compartilhado.
2. **Portais nacionais grandes já estavam no banco.** webleiloes (Tiago, 144 cards) gerou só ~3 novos:
   o portal já fora raspado em runs de outros estados. Dos 620 coletados, **370 (60%) eram dedup**.
3. **31 de 68 sites raspáveis retornaram 0 cards (SPA / API interna).** oscarleiloes, ccjleiloes, eustacio,
   aguialeiloes, nordesteleiloes, hatoryleiloes, bezerraleiloes, kcleiloes, alfaleiloes, msoleiloes,
   hoppeleiloes, jonasleiloeiro, lucasleiloeiro, palaciodosleiloes, guariglialeiloes, jussiaraleiloes,
   positivoleiloes, lopesleiloes, albertomacedoleiloes, gustavomorettoleiloeiro, leiloeiroeduardo, rmoyses,
   lancecertoleiloes etc. — a listagem chega via XHR/Fetch ou exige interação com filtros de busca; o
   extrator genérico de cards (`extract_cards`) não casa nada. Mesmo sintoma das seções 39-43.
4. **20 de 88 regulares sem site no diário (23%).** A JUCEB publica só e-mail gratuito para esses; são
   intrinsecamente não-raspáveis pela rotina atual.
5. **7 sites inacessíveis** mesmo com fallback FlareSolverr: `leiloestaniaabreu.wix.com.br/leiloes`,
   `jocaleiloesagro.com`, `carrollruralleiloes.com`, `vecchileiloes.com` (Marciano + Camila),
   `clicleiloes.com`, `lubreleiloes.com.br` — DNS/timeout/Wix com challenge.
6. **Lazy-load de imagens.** Só 108/250 vieram com foto: muitos cards usam `data-src`/IntersectionObserver
   e a `<img>` real só carrega ao rolar; o `render()` atual não faz scroll.
7. **Cidade default herdada do leiloeiro.** Quando o card não traz `Cidade/UF`, o registro herda a cidade
   do leiloeiro (Salvador), produzindo imóveis "em Salvador" que na verdade são de São Vicente/SP etc.
   (caso Irani Flores, portal nacional).
8. **Lentidão (~2h10 para 88 sites).** `networkidle` + `wait_ms=3500` por página, até 5 categorias + 8
   leilões + 12 detalhes por site, tudo serial e single-tab. Sites lentos (oscarleiloes ~10 min) seguram a fila.

### 44.4. Correções sugeridas

1. **Chave de dedup/atribuição por (URL, leiloeiro) e flag de portal compartilhado.** Marcar `plataforma`
   (leilaobrasil, webleiloes, vlance, portalzuk) e **não recontar** o acervo do portal para cada leiloeiro
   que o usa; atribuir o lote ao leiloeiro real do edital, não ao dono do domínio.
2. **Camada de adaptadores por plataforma (seção 27).** Implementar adapters para os portais que somam a
   maior parte do "0 cards": vlance, superbid/leilo, vipleiloes, sodré-santoro-like. Captura a API interna
   (XHR/JSON) em vez de raspar a DOM — resolve de uma vez dezenas de leiloeiros (já há `superbid_adapter.py`
   e `lancevip_adapter.py` no repo a integrar).
3. **Scroll + espera de lazy-load no `render()`.** `page.mouse.wheel`/`scroll_into_view_if_needed` antes de
   `page.content()` para materializar `data-src` e elevar a taxa de fotos de 43% para ~90%.
4. **Detecção de UF no próprio card com prioridade sobre o default do leiloeiro;** quando ausente, deixar
   cidade/UF vazias em vez de herdar a sede do leiloeiro (evita "imóvel de SP marcado como Salvador").
5. **Paralelizar 2–3 contextos do Playwright e reduzir `networkidle`** para cortar a janela de ~2h pela
   metade; aplicar `timeout` por-site (ex.: 4 min) para não deixar um site lento (oscarleiloes) travar a fila.
6. **Segunda passada (retry) para os 7 inacessíveis** com `maxTimeout` maior no FlareSolverr e correção de
   URLs Wix (usar a home `leiloestaniaabreu.wix.com.br` sem o path `/leiloes`).
7. **Derivação de site a partir do e-mail corporativo** já aplicada manualmente aqui — vale automatizar no
   pré-processamento do CSV (descartar provedores gratuitos, manter domínio próprio).
8. **Rodar `baixar-docs`** para materializar editais/matrículas dos 250 imóveis novos em `storage/docs/`.

### 38.11. Reconstrução do `FrazaoScraper` + agendamento no Celery beat

`frazaoleiloes` era a maior fonte de lixo (8543 linhas, `valor_minimo` NULL). Diagnóstico do
scraper antigo: usava `/leiloes/imoveis` + seletores `.lot-item/.preco` que **não casam mais** —
capturava páginas de **"Verificação de segurança"** (título) e itens **não-imóveis** (celulares),
com valor NULL.

**Engenharia reversa da plataforma (.NET):**
- Lotes de imóveis vêm de `GET /Sale/SearchLotResult?uf=<UF>&cidade=&bairro=&tipoimovel=&start=<n>`
  — **HTML, sem JS** (funciona com httpx/requests; só precisa `X-Requested-With: XMLHttpRequest`
  e `Referer`).
- Card `.card.thumbnail-vitrine-lot`: `data-lote-id`, `data-tipo` (Casa/Apartamento/Terreno…),
  `data-addr` (logradouro), título com cidade/UF, **`.price-line`** com o valor, data da 1ª praça
  em `"Leilão: DD/MM/YYYY às HHhMM"`, imagem em `cdn.frazaoleiloes.com.br/images/lot/...`.
- **Armadilha:** no HTML cru o preço vem **sem o "R$"** (o cifrão é adicionado por JS); pegar do
  elemento `.price-line` (que tem `R$ 300.200,00`) em vez de regex `R\$` no texto todo.
- `SearchLotResult` retorna **muito lote histórico/encerrado** — filtrar `data_primeiro_leilao ≥
  hoje` para não reimportar passado; UF vem como código no meio do título
  (`"…, Porto Velho RO Rua…"`), então casar cidade por **alternância de códigos de UF**, não por
  `/UF` no fim.

**Scraper reescrito** (`scrapers/leiloeiros/frazao.py`): itera os 27 UFs, pagina `start` até
esgotar, extrai os campos acima, filtra 1ª praça futura, monta `url_original =
/lote/<lote_id>` e `id_externo = frazao_lote_<lote_id>`. **httpx async** → roda no `leilao_worker`
**sem Chromium**.

**Validação (RO):** 2 imóveis futuros com **valor preenchido** —
`Casa … Porto Velho/RO R$ 65.500,00` e `R$ 147.000,00` (antes: NULL). Título/cidade/UF limpos.

**Agendamento:** adicionados ao `scheduler/tasks.py` a task `coletar_frazao` (espelha
`coletar_caixa`: `FrazaoScraper().run()` → `salvar_imoveis`) e a entrada de beat
`scraping-frazao-diario` (`crontab(hour=4, minute=0)`). `leilao_beat` reiniciado → schedule
confirmado em `app.conf.beat_schedule`. Como `salvar_imoveis` faz `on_conflict_do_update`
(sobrescreve valor/cidade/imagem), a fonte passa a **auto-curar** a cada ciclo diário.

> **Nota sobre o lixo antigo:** as 8543 linhas velhas (celulares/"Verificação de segurança") têm
> `id_externo` diferente dos lotes novos, então **não** são sobrescritas — são registros de
> não-imóveis que devem ser limpos à parte (ex.: `DELETE WHERE titulo='Verificação de segurança'`),
> não "curados". O scraper novo gera **registros limpos de imóveis** daqui pra frente.

---

## 45. Captura dos leiloeiros REGULARES de Alagoas — JUCEAL (PDF "Leiloeiros Alagoas", 2026-06-09)

Captura a partir do PDF da junta de Alagoas. Dos 29 leiloeiros do PDF, **1 foi excluído por estar
CANCELADO** (Eduardo Schmitz, matrícula 01/2024, cancelado em 19/01/2026); os **28 REGULARES**
entraram no scraper. Script: [`scraper_al.py`](scraper_al.py) (adaptado de `scraper_pe.py`).
Entrada: [`csv/leiloeiros_al_2026-06-09.csv`](csv/leiloeiros_al_2026-06-09.csv); lista enxuta
nome+site: [`csv/leiloeiros_al_nome_site_2026-06-09.csv`](csv/leiloeiros_al_nome_site_2026-06-09.csv).
Saída de imóveis: [`csv/imoveis_al_2026-06-09.csv`](csv/imoveis_al_2026-06-09.csv).

### 45.1. Resultado

| Métrica | Valor |
|---|---|
| Leiloeiros regulares processados | 28 |
| Imóveis com 1ª praça futura (linhas no CSV) | 290 |
| URLs únicas | 273 |
| **Novos no banco (JUCEAL/AL)** | **85** |
| Já existiam (dedup por URL, outras juntas) | 205 |
| Erros de execução | 0 |

**Por leiloeiro (1ª praça futura):** Irani Flores 160 · Davi Borges de Aquino 45 ·
Maria Catarina (Nasar) 24 · Rudival Gomes Jr. 13 · Alex Hoppe 7 · Diogo Martins 7 ·
Lucas Kury 7 · Daniel Garcia 5 · Fernando Caetano 5 · Lucas Rafael 5 · Jonas Gabriel 5 ·
Adilson Bento 2 · Francisco Jonnathan 2 · Ivana Montenegro 2 · Murilo Ramos 1 · demais 13 leiloeiros: 0.

> A maior parte das 290 linhas já existia no banco porque muitos leiloeiros de AL atuam em
> **portais nacionais multi-estado** (leilaobrasil, mgl, inovaleilao) já raspados sob outras juntas.
> A dedup por URL é correta: JUCEAL/AL "fica" apenas com os 85 lotes ainda inéditos no banco.

### 45.2. Principais dificuldades enfrentadas

1. **Filtro de título derrubava títulos vindos do slug da URL (causa de 0 imóveis no 1º run).**
   O `JUNK_RE` original rejeitava qualquer título começando com "leilão/leilao". Vários sites
   (alfaleiloes, danielgarcia) só expõem o título no slug, no formato
   `/lote/9798/leilao-de-sala-comercial-no-bairro-...`. Resultado: **todos os sites retornaram 0**
   na primeira execução, mesmo tendo lotes válidos no HTML.

2. **Plataformas SPA sem lotes no HTML server-side.** `leje.com.br`, `nordesteleiloes.com.br`,
   `bravoleiloes.com.br`, `adrileiloes.com.br`, `lancecertoleiloes.com.br` e o
   `vipleiloes.com.br` (que lista por **evento**, não por lote) carregam os cards via XHR/JS
   após o load. O HTML inicial só traz um link de filtro (`filtro/imoveis`) — 0 lotes extraíveis
   por DOM. Foram os principais responsáveis pelos 13 leiloeiros com 0 imóveis.

3. **Bloqueio anti-bot (HTTP 403) no `mgl.com.br`.** Requests simples levam 403; resolvido via
   Playwright + fallback FlareSolverr (capturou 5 imóveis para cada um dos 3 leiloeiros da MGL).

4. **Estrutura de dois níveis (evento → lote).** `danielgarcialeiloes.com.br` e `vipleiloes`
   só listam páginas de evento (`/leilao/<id>/lotes`, `/evento/detalhes/...`) na home; os lotes
   individuais ficam dentro delas.

5. **Teto de visitas a páginas de detalhe (`detail_budget`).** Em portais grandes (leilaobrasil
   com ~197 cards na home), muitos cards não trazem a data da 1ª praça na listagem — é preciso
   abrir o detalhe. O limite de 24 detalhes/site (para manter o tempo viável) descarta lotes
   válidos cuja data só aparece na página interna.

6. **Atribuição leiloeiro↔junta em multi-estado.** Como a dedup é por URL, um lote de um leiloeiro
   que atua em vários estados fica "preso" à primeira junta que o capturou — subnotificando AL.

7. **Mojibake no console (cp1252 no Windows).** Acentos saem corrompidos no stdout
   (`ARA�JO`); cosmético, não afeta os dados gravados (UTF-8 no CSV/banco).

8. **Sites genuinamente sem imóveis na home.** `portaldosleiloes`, `leiloesfreire`,
   `albuquerquelins`, `vipleiloes` (home só com categorias/eventos) retornaram 0 — sem 1ª praça
   futura visível pela rota raspada.

### 45.3. Correções aplicadas nesta rodada

- **`best_title()` / `clean_title()` / `good_title()`** ([`scraper_al.py`](scraper_al.py)):
  o título passa a ser escolhido entre heading, texto da âncora **e todos os segmentos do slug**,
  removendo o prefixo boilerplate "Leilão de/da/do…" e "Lote N -". Isso destravou alfaleiloes
  (0 → 45) e leilaobrasil (0 → 160).
- **Seguir páginas de evento/oferta**: adicionados `/evento` e `/oferta` aos padrões de link de
  leilão e ao filtro de href, permitindo descer evento → lote (Daniel Garcia 0 → 5).
- **`detail_budget` elevado** de 14 para 24 detalhes/site.

### 45.4. Correções sugeridas (próximos passos)

1. **Adaptadores por plataforma (maior ganho).** Em vez de raspar DOM genérico, descobrir o
   endpoint XHR/JSON de cada SPA e consumi-lo diretamente:
   - `leilaobrasil.com.br`, `leje.com.br`, `vipleiloes.com.br`, `nordesteleiloes.com.br`,
     `lancecertoleiloes.com.br`, `adrileiloes.com.br`, `bravoleiloes.com.br` — abrir DevTools
     (Network → Fetch/XHR) e mapear a API de busca de lotes (paginação, filtro por categoria
     "imóveis"). Reaproveitar a camada de adaptadores descrita nas seções 19 (BomValor) e 27
     (scraper genérico) — `abaleiloes` já cai no padrão BomValor.
2. **Render com espera por conteúdo dinâmico nas SPAs.** Quando não houver API, no Playwright
   aguardar o seletor dos cards (`wait_for_selector`) e/ou rolar a página (lazy-load) antes de
   `page.content()`, em vez do `wait_for_timeout` fixo.
3. **Extrair datas/preço da listagem via JSON embutido** (`__NEXT_DATA__`, `__NUXT__`, JSON-LD)
   para reduzir a dependência do `detail_budget`; quando precisar abrir detalhes, **paralelizar**
   (várias abas) e elevar o teto.
4. **Tabela de associação leiloeiro↔junta (N:N).** Registrar todas as juntas em que um lote/leiloeiro
   aparece, em vez de deixar a primeira junta "dona" do URL — melhora a contagem por estado sem
   duplicar imóveis.
5. **Normalizar saída do console** com `PYTHONUTF8=1` / `chcp 65001` para evitar mojibake nos logs.
6. **Inferência de site a partir do e-mail corporativo** (já aplicada: Maysala sem site no PDF →
   `bravoleiloes.com.br` via `atendimento@bravoleiloes.com.br`) — generalizar como heurística padrão
   quando o campo Site estiver vazio no PDF.

---

## 46. Captura dos leiloeiros REGULARES do Piauí — JUCEPI (PDF "Leiloeiros Piauí", 2026-06-09)

Scraper: [`scraper_pi.py`](scraper_pi.py) (clone do `scraper_pe.py`). Entrada:
[`csv/leiloeiros_pi_2026-06-09.csv`](csv/leiloeiros_pi_2026-06-09.csv). Junta `JUCEPI/PI`.
Captura iniciada 09/06/2026 20:54, concluída ~21:27 (~33 min).

### 46.1. Recorte da lista (somente regulares)

O PDF da Jucepi tem 3 blocos. Mantidos **apenas os 19 "aptos conforme a Portaria Nº 002/2026"**
(páginas 1–5). **Excluídos**:
- **Irregulares** (pág. 6–7): Celso Alves Cunha, Tiago Tessler, Davi Borges de Aquino, Irani Flores.
- **Cancelados** (pág. 7): Dalton Luis de Moraes Leal, Victor Oliveira Dorta, Erico Lages Soares,
  Rodrigo Schmitz.

⚠️ **Armadilha de homônimo:** o regular é **Erico Sobral Soares** (matr. 15/2015); o cancelado é
**Erico Lages Soares** (matr. 02/1994) — pessoas distintas. Manter o primeiro, descartar o segundo.

A maioria dos leiloeiros do PI tem domicílio em **outras UFs** (SP, GO, MG, BA, PR, PB, CE, MA, SC, PE):
são leiloeiros nacionais com matrícula também na Jucepi. O campo `cidade/uf` da entrada guarda o
endereço de registro; o `uf` real do imóvel vem do card (regex cidade/UF).

### 46.2. Inferência de site (campo Site ausente/poluído no PDF)

Vários verbetes não trazem campo "Site" — só e-mail. Aplicada a heurística da seção 45.6:
- e-mail **corporativo** → site (`contato@dgleiloes.com.br` → `dgleiloes.com.br`;
  `erico.sobral@vipleiloes.com.br` → `vipleiloes.com.br`; `@portalzuk.com.br` → `portalzuk.com.br`;
  `@mjleiloes.com.br`, `@rigolonleiloes.com.br`, `@leiloesjudiciaisbahia.com.br`,
  `@leiloesmonteiro.com.br`, `@hoppeleiloes.com.br`, `@fernandoleiloeiro.com.br`).
- e-mail **gratuito** (gmail/yahoo/hotmail) → **sem site raspável** (Sergio Lima, Murilo Ramos,
  Ítalo Filho, Jonas Moreira, Lucas Moreira, Andrezza Peron, Eduardo Sydney) → 7 pulados.
- Outra armadilha do PDF: o campo "Site:" do Erico Sobral continha um **e-mail** (`erico.sobral@vipleiloes.com.br`),
  não uma URL — tratado como e-mail corporativo.

### 46.3. Resultado

| Imóveis (1ª praça futura) | Leiloeiro | Site | Status |
|--------------------------:|-----------|------|--------|
| 14 | Maurício José de Sousa Costa | mjleiloes.com.br | ok |
| 11 | Dora Plat | portalzuk.com.br | ok |
| 11 | Rodrigo Aparecido Rigolon da Silva | rigolonleiloes.com.br | ok |
|  9 | José David Gonçalves de Melo | jdleiloes.com.br | ok (já no banco via PE) |
|  6 | Paulo Cezar Rocha Teixeira | leiloesjudiciaisbahia.com.br | ok |
|  5 | Daniel Elias Garcia | dgleiloes.com.br | ok |
|  3 | Ítalo Trindade Moura | italoleiloes.com | ok |
|  3 | Miguel Alexandrino Monteiro Neto | leiloesmonteiro.com.br | ok |
|  2 | Fernando Caetano Moreira Filho | fernandoleiloeiro.com.br | ok |
|  0 | Erico Sobral Soares | vipleiloes.com.br | acessível, sem 1ª praça futura |
|  0 | Alex Willian Hoppe | hoppeleiloes.com.br | acessível, sem imóveis |
|  0 | Suzana C. M. de Carvalho | suzanacarvalholeiloes.com | **inacessível** |
|  0 | 7 leiloeiros | — | sem site (e-mail gratuito) |

**Totais:** 64 imóveis válidos (1ª praça > data da captura); **19 novos no banco**, 45 já existiam
(dedup por URL — `portalzuk`, `rigolonleiloes`, `jdleiloes`, `leiloesjudiciaisbahia` já capturados em
rodadas de outras juntas). CSVs: [`csv/imoveis_pi_2026-06-09.csv`](csv/imoveis_pi_2026-06-09.csv) e
[`csv/leiloeiros_pi_2026-06-09.csv`](csv/leiloeiros_pi_2026-06-09.csv). Verificação CSV↔banco:
`SELECT COUNT(*) WHERE junta='JUCEPI/PI'` = 19 = `db_novos`. ✅

### 46.4. Principais dificuldades

1. **Alta sobreposição com bancos anteriores (70% de dedup).** 45 de 64 imóveis já estavam no banco —
   leiloeiros nacionais (Zuk, Rigolon, JD, Leilões Judiciais Bahia) já raspados sob PE/BA/etc. O dedup
   por URL funcionou, mas **gastou tempo de render** raspando sites já conhecidos.
2. **Site inacessível (`suzanacarvalholeiloes.com`).** Caiu no fallback FlareSolverr e ainda assim não
   respondeu (timeout/DNS) → 0 imóveis, 1 erro registrado.
3. **Sites lentos dominam o tempo de execução.** `mjleiloes.com.br` levou ~5 min sozinho (vários
   `auction_links` + `detail_budget`, cada render com timeout de 45 s + fallback de até 70 s). ~33 min
   totais para 19 leiloeiros, sendo a maior parte em 2–3 domínios pesados.
4. **37% dos leiloeiros sem site raspável** (7 de 19 com e-mail gratuito). Recall limitado pela fonte:
   sem URL, não há o que raspar.
5. **Mojibake no console do Windows** (`Maur��cio`, `Gon��alves`) nos logs — cosmético, não afeta os
   dados gravados (CSV/DB em UTF-8 corretos).

### 46.5. Correções sugeridas

1. **Cache global de URLs já no banco antes de renderizar.** Carregar o set de URLs existentes no início
   e, para domínios 100% já capturados em rodadas anteriores, pular o render (ou só revalidar datas) —
   elimina o desperdício do item 1. Hoje o dedup ocorre só na inserção, depois de raspar.
2. **Adaptadores por plataforma para os sites pesados** (Zuk/`portalzuk`, `mjleiloes`, `rigolonleiloes`):
   consumir o XHR/JSON da busca de lotes (paginação + filtro "imóveis") em vez de DOM genérico — corta
   drasticamente o tempo do item 3. Reaproveitar seções 19/27.
3. **Timeout adaptativo + 1 retry curto para sites inacessíveis** (item 2): baixar `maxTimeout`,
   tentar `http://` e `https://`, e marcar como "offline" rapidamente em vez de segurar o pipeline.
4. **Paralelizar leiloeiros independentes** (várias abas/contextos Playwright) — os 19 são independentes;
   um pool de 3–4 workers reduziria o tempo total de ~33 min para ~10 min.
5. **Enriquecer site dos leiloeiros sem URL** via busca web pelo nome + "leiloeiro" + cidade, ou pela
   matrícula no portal da Jucepi — recupera parte dos 7 pulados (item 4).
6. **Forçar UTF-8 no console** (`PYTHONUTF8=1` / `chcp 65001`) para eliminar o mojibake dos logs (item 5).


---

## 47. Scraping JUCEMA/MA — leiloeiros do Maranhão (2 PDFs, jun/2026)

> Captura a partir de **dois PDFs** da junta: `1626201361_Leioleiros 2021.2.pdf` (18 leiloeiros, RELAÇÃO DOS LEILOEIROS) e `Leiloeiros Maranhão.pdf` (LEILOEIROS CREDENCIADOS + relação de endereços). Implementação em `scraper_ma.py` (base reaproveitada de `scraper_para.py`/seção 27), entrada `csv/leiloeiros_ma_2026-06-09.csv`.

### 47.1. Resultado
- **28 leiloeiros regulares** consolidados (dedup entre os 2 PDFs; nenhum marcado cancelado/suspenso — ambos são listas de credenciados/regulares).
- **15 com site** (direto no PDF ou inferido do e-mail corporativo); **13 sem site** (e-mail pessoal hotmail/gmail/yahoo/bol/live → não-raspável).
- **191 imóveis** com 1ª praça futura coletados; **1 novo** no banco, **190 já existiam** (dedup por URL). Verificação CSV↔banco fechou: 191/191 URLs presentes.
- Maiores rendimentos: Irani Flores/leilaobrasil.com.br (150 — portal nacional), Marco Antônio (10), Rigolon (10), Aranha/leilaovip (8), Victor Dorta (6). Duração ~31 min.

### 47.2. Principais dificuldades
1. **Quase todo imóvel já estava no banco (novos=1/191).** Os leiloeiros do MA com site são, na prática, **leiloeiros nacionais** (Marco Antônio, Maria Fixer, Francisco Freitas, Victor Dorta, Fernando, Rigolon, Irani/Leilão Brasil) já capturados em rodadas anteriores (MS, PA, etc.). O dedup por URL canônica funcionou — mas o rendimento marginal de novas juntas com leiloeiros nacionais é baixo.
2. **46% dos leiloeiros sem site (13/28).** A maioria dos credenciados locais (Wesley, Hetury, Pedro, Leonardo, Marina, Taís, Daniel, Ana Cláudia Scarpim, Thales, Tassiana, Frazão) só tem e-mail pessoal no PDF → impossível inferir domínio → 0 imóveis próprios.
3. **Portal nacional infla a contagem (Irani Flores = 150).** `leilaobrasil.com.br` lista imóveis de todo o Brasil; atribuir 150 lotes a 1 leiloeiro do MA distorce a relação por-leiloeiro (mesmo padrão já visto em PE/BA).
4. **Sites compartilhados (vipleiloes.com.br) entre 3 leiloeiros** (Vicente, Erico, Wirna) e VIP/leilaovip majoritariamente de **veículos** → 0 imóveis. O cache por domínio evitou re-raspar, mas o site em si rende pouco imóvel.
5. **Site offline (lealleiloes.com.br)** — ConnectionError/DNS em www e ápice; 1 leiloeiro (Dalton) perdido.
6. **403 inicial (norteleiloes, fernandoleiloeiro)** resolvido via fallback FlareSolverr, mas custou tempo (sites lentos levaram 4–15 min cada; Francisco Freitas ~15 min batendo timeouts de 45s).
7. **Mojibake nos logs** (`José`→`Jos�`) pelo redirect do PowerShell em UTF-16 e console cp1252.

### 47.3. Correções sugeridas
1. **Dedup-aware reporting:** ao raspar uma nova junta, separar no relatório "imóveis novos da junta" de "já existentes (leiloeiro nacional)" — evita a falsa impressão de baixa captura quando o trabalho real é só confirmar cobertura.
2. **Pular leiloeiros nacionais já cobertos:** manter um set de domínios já raspados globalmente e, em nova junta, só re-raspar se passou > N dias — corta ~70% do tempo (os 31 min foram quase todos em sites já no banco).
3. **Enriquecer site dos 13 sem URL** via busca web (nome + "leiloeiro" + cidade) ou pelo nº de matrícula no portal da JUCEMA antes de marcar como não-raspável.
4. **Cap por leiloeiro em portais nacionais:** filtrar lotes do leilaobrasil por UF=MA (ou limitar a N) para não atribuir 150 imóveis nacionais a 1 credenciado do MA.
5. **Timeout adaptativo + 1 retry http/https** para sites lentos/offline (Francisco Freitas, lealleiloes): baixar `maxTimeout`, marcar "offline" cedo em vez de segurar o pipeline ~15 min.
6. **Paralelizar** os 15 sites independentes (pool de 3–4 contextos Playwright) → ~31 min cairia para ~10 min.
7. **Forçar UTF-8 no console** (`PYTHONUTF8=1` / `chcp 65001`) para eliminar o mojibake (item 7).

---

## 48. Captura dos leiloeiros REGULARES da Paraíba — JUCEP (PDF "Leiloeiros PAraíba", 2026-06-09)

> Captura a partir do PDF da junta (28 páginas: ficha por leiloeiro nas pp. 1-17 + suplemento "Regiões de atuação" pp. 17-26 + tabela de contatos pp. 26-28). Implementação em `scraper_pb.py` (base reaproveitada de `scraper_pe.py`/seção 43), entrada `csv/leiloeiros_pb_2026-06-09.csv`, junta `JUCEP/PB`.

### 48.1. Resultado
- **38 leiloeiros regulares** consolidados; **6 IRREGULAR excluídos** (Rennan Napy Neves, Emanuel Abraão Silva de Lima, Josecelli Kildare Fraga Gomes, Cyntia Araújo Diniz Nóbrega, Aluizio Hilario De Souza Junior, Lúcia De Fátima Barbosa Almeida).
- **27 com site** (direto no PDF, no suplemento de contatos ou inferido do e-mail corporativo); **11 sem site** (e-mail pessoal gmail/hotmail/icloud/yahoo → não-raspável).
- **196 imóveis** com 1ª praça futura coletados; **7 novos** no banco, **189 já existiam** (dedup por URL). Verificação CSV↔banco fechou: 196 = 7 novos + 189 já existentes.
- Maiores rendimentos: Irani Flores/leilaobrasil.com.br (160 — portal nacional), Rigolon (11), Edeylson/Rudival/Daniel Garcia (5 cada), Cleber/leiloespb (4), Miguel/leiloesmonteiro (3), Fernando (2), Ives (1). Duração ~50 min.
- **Únicos imóveis genuinamente novos da junta:** Cleber da Silva Melo (4) e Miguel Alexandrino Monteiro Neto (3) — os demais já estavam no banco sob outras juntas.

### 48.2. Principais dificuldades
1. **96% dos imóveis já estavam no banco (novos=7/196).** Quase todos os leiloeiros da PB com site são **leiloeiros nacionais/multi-estado** já capturados em rodadas anteriores (PE, BA, RN, etc.): Irani/Leilão Brasil, Rigolon, RJ Leilões (Rudival), Daniel Garcia, Fidelis, Nasar. O dedup por URL canônica funcionou; o ganho marginal é baixo.
2. **Portal nacional infla a contagem (Irani Flores = 160).** `leilaobrasil.com.br` lista imóveis de todo o Brasil — atribuir 160 lotes a 1 leiloeiro da PB distorce a relação por-leiloeiro (mesmo padrão de PE/BA/MA).
3. **29% dos leiloeiros sem site (11/38).** Locais como Alexandre Nunes, Filipe Pedro, Igor Oliveira, Danillo Cunha, Celso Cunha, Bruno Monteiro, Stefania Xavier, Jussara Medeiros só têm e-mail pessoal → sem domínio inferível → 0 imóveis próprios.
4. **Muitos sites compartilhados.** `maglianoleiloes.com.br` (José Andrea, Jéssica, Raphael), `vipleiloes.com.br` (Roberto, Erico), `rjleiloes.com.br` (Rudival) — o cache por domínio evitou re-raspar, mas concentra a captura em poucos domínios.
5. **Site inacessível (albuquerqueleiloes.com.br)** — 1 leiloeiro (Gervasio) perdido (timeout/DNS mesmo com fallback FlareSolverr).
6. **SPAs/portais que renderizam 0 cards válidos** apesar de "ok": maglianoleiloes, alfaleiloes, atlanticoleiloes, colossoleiloes, tesouroleiloes, abrantesleiloes — listagem via JS pesada ou catálogo majoritariamente de veículos/sem data futura.
7. **Sites inferidos podem não existir** (`mgrleiloes.com.br`, `lucasleiloeiro.com.br`, `jonasleiloeiro.com.br`, `fernandoleiloeiro.com.br`) — inferência a partir do e-mail é palpite; quando o domínio não resolve vira "inacessível"/0 sem ganho.

### 48.3. Correções sugeridas
1. **Dedup-aware reporting:** separar no relatório "imóveis novos da junta" de "já existentes (leiloeiro nacional)" — aqui o trabalho real foram só 7 imóveis novos (Cleber + Miguel); o resto foi confirmação de cobertura.
2. **Registro global de domínios já raspados + TTL:** ao abrir nova junta, só re-raspar domínio se passou > N dias da última varredura — cortaria ~80% do tempo (os ~50 min foram quase todos em sites já no banco).
3. **Cap/filtro por UF em portais nacionais:** filtrar lotes do `leilaobrasil` por UF=PB (ou limitar a N) para não atribuir 160 imóveis nacionais a 1 credenciado da PB.
4. **Confirmar domínio antes de raspar:** fazer um HEAD/DNS-check rápido nos sites inferidos do e-mail e marcar "site não confirmado" em vez de gastar render+FlareSolverr num domínio inexistente.
5. **Enriquecer site dos 11 sem URL** via busca web (nome + "leiloeiro" + cidade/matrícula) antes de marcar como não-raspável.
6. **Adaptador específico para SPAs de alto valor** (maglianoleiloes, colossoleiloes, atlanticoleiloes — leiloeiros locais reais da PB): inspecionar XHR/API interna para extrair os lotes que o parser genérico de cards não vê.
7. **Paralelizar** os ~20 domínios únicos (pool de 3-4 contextos Playwright) → ~50 min cairia para ~15 min.

### 45.5. Por que os imóveis não apareciam na listagem (correção do destino)

Após o scraping, os 290 imóveis estavam apenas no **SQLite standalone** `imoveis_leiloeiros.db` —
que **não é o banco do site**. A listagem (frontend → API `leilao_api`) lê do **PostgreSQL dentro do
container `leilao_postgres`** (`leilao_db`, ~123 mil imóveis). Dois detalhes não-óbvios travaram a
visibilidade:

1. **`scraper_al.py` (herdado de pe/para/bahia) só insere no SQLite.** Nenhum desses scrapers
   standalone escreve no PostgreSQL — é preciso um passo de importação separado.
2. **Dois PostgreSQL na mesma porta.** A máquina tem um PostgreSQL **do host** ouvindo em
   `localhost:5432` que **sombreia** o `0.0.0.0:5432->5432` do Docker. Conectar via
   `psycopg2 postgresql://...@localhost:5432` cai no banco **do host** (74 mil imóveis), **não** no do
   site. O canal correto é o mesmo usado por `importar_jucems.py`:
   `docker exec leilao_postgres psql -U leilao -d leilao_db`.

**Correção aplicada** ([`importar_al_docker.py`](importar_al_docker.py)):
- desfez a importação que havia ido para o banco-host por engano;
- importou no container via `docker exec`, com tabela `stage` temporária + `\copy` + dedup global por
  `url_original` (não duplica o que já está no site) e dedup intra-CSV por url;
- mapeou os enums **reais** do schema (`tipoimovel`, `tipoleilao`, `statusleilao`, `categoriaitem` —
  e não os nomes das colunas);
- fonte `JUCEAL` (id 977) criada; **82 imóveis novos** inseridos no site (191 das 273 URLs já
  existiam lá, vindos de portais nacionais já raspados). Confirmado pela API
  (`GET /api/v1/imoveis/{id}` e listagem por `criado_em desc`).

**Pendências de qualidade (não bloqueiam a listagem):**
- `cidade`/`estado` caem no endereço do leiloeiro quando não foram parseados do card (ex.: terreno em
  Rondonópolis/MT aparece como São Paulo/SP). Melhorar o parser de localização por lote.
- `leiloeiro_id` não foi vinculado (só o texto `leiloeiro`): o nome aparece no **detalhe**, mas o card
  da lista que depende do join por `leiloeiro_id` fica sem nome. Sincronizar leiloeiros novos (seção
  24/25) e popular `leiloeiro_id`.

> **Recomendação geral:** os scrapers standalone deste diretório deveriam, ao final, importar para o
> container via `docker exec` (e nunca `localhost:5432`). Vale checar se pe/para/bahia também ficaram
> só no SQLite e nunca chegaram ao site.

### 48.4. Correção aplicada — PB (e reimport das demais juntas) não estavam no banco do site

**Sintoma relatado pelo usuário:** "não estou visualizando no banco de dados estes novos imóveis".
Confirmado: o `scraper_pb.py` (como pe/para/bahia/al/etc.) grava **só** no SQLite
`imoveis_leiloeiros.db`. O site lê do **PostgreSQL `leilao_db`** no container `leilao_postgres`
(~123 mil imóveis). Sem o passo de importação, **nada do scraping aparece no site** — exatamente a
pendência prevista em [[project_gravar_ambos_bancos]] e na seção 46.

**Correção aplicada (canônica, idempotente):** rodar [`importar_site.py`](importar_site.py) por junta —
ele grava nos **dois** bancos e deduplica globalmente por `url_original` (PG) / `url` (SQLite):

```bash
python importar_site.py --csv csv/imoveis_pb_2026-06-09.csv \
  --fonte JUCEP --junta "JUCEP/PB" --estado-padrao PB --url-base "https://www.jucep.pb.gov.br/"
```

**Resultado da rodada (reimport de todas as juntas pendentes do dia):**

| Junta | CSV (linhas) | PG inseridos novos | Total na fonte (PG) |
|-------|-------------:|-------------------:|--------------------:|
| JUCEP/PB  | 196 | 1 | 10 |
| JUCEPE/PE | 414 | 1 | 43 |
| JUCEPA/PA | 39  | 1 | 13 |
| JUCEB/BA  | 620 | 1 | 218 |
| JUCEAL/AL | 290 | 0 | 82 |
| JUCEC/CE  | 235 | 1 | 9 |
| JUCEPI/PI | 64  | 0 | 36 |
| JUCESE/SE | 61  | 1 | 8 |

**Lição não-óbvia:** o nº "inseridos novos" é quase sempre **0–1** porque os imóveis dessas juntas já
estão no PG sob **outras fontes** (a base de produção do `leilao-scraper` já capturou
leilaobrasil/leiloespb/leiloesmonteiro/rjleiloes etc.). A dedup por `url_original` é **global**, então
o mesmo imóvel não é duplicado entre fontes — o "total na fonte" só conta os que foram *atribuídos*
àquela fonte; a cobertura real é maior. Ou seja: **"poucos novos" ≠ "não importou"**; significa que o
site já tinha aqueles imóveis.

### 48.5. Correções sugeridas (dual-bank / visibilidade no site)

1. **Importação ao PG embutida no scraper — ✅ IMPLEMENTADO (ver 48.6).** Ao final de `main()`, cada
   scraper standalone agora chama o import ao PostgreSQL automaticamente via `pg_autoimport.py`,
   eliminando o passo manual que causou a invisibilidade no site.
2. **Checklist "concluído" inclui o PG:** a definição de concluído da seção 0 deve exigir
   `coletados_válidos == gravados no PostgreSQL do container` — não só no SQLite. Adicionar uma
   verificação `docker exec leilao_postgres psql ... count` por fonte ao fim de cada rodada.
3. **Guarda contra `localhost:5432`:** o `importar_site.py` deve **recusar** conexão direta a
   `localhost:5432` (banco-host que sombreia o Docker) e só operar via `docker exec`/host correto,
   evitando gravar no banco errado silenciosamente.
4. **Relatório por-fonte vs. global:** ao reportar "X novos", distinguir "novos globais no PG" de "já
   existiam sob outra fonte" para não dar a falsa impressão de captura perdida (ver 48.4).
5. **Varredura de pendências:** manter um script que liste CSVs `csv/imoveis_*.csv` ainda não
   importados ao PG (comparando contagem por fonte) e reimporte os faltantes — como foi feito aqui para
   PE/PA/BA/AL/CE/PI/SE.

### 48.6. Implementação — auto-import ao PostgreSQL em todos os scrapers (`pg_autoimport.py`)

Para que **nenhum scraping volte a ficar invisível no site**, o passo de importação ao PG passou a ser
automático no fim de cada scraper standalone. A correção 48.5.1 está aplicada.

**Helper compartilhado** [`pg_autoimport.py`](pg_autoimport.py) — uma única fonte de verdade (DRY),
reutilizada por todos os scrapers:

```python
from pg_autoimport import importar_para_site
# ... no fim de main(), logo após gravar o CSV datado:
print(f"\n[CSV] {out}")
importar_para_site(out, JUNTA)     # empurra o CSV para o PostgreSQL do site
```

O helper:
- deriva `--fonte` e `--estado-padrao` do rótulo `JUNTA` (formato `FONTE/UF`, ex.: `JUCEP/PB` →
  fonte `JUCEP`, estado `PB`; o UF é cortado em 2 chars p/ casos como `JUCER/RR-RO`);
- chama o importador canônico [`importar_site.py`](importar_site.py) por **subprocess** (decoupled —
  não importa psycopg2/docker para dentro do scraper), que grava em **AMBOS** os bancos via
  `docker exec leilao_postgres` (nunca `localhost:5432`) e deduplica global por `url_original`;
- é **idempotente** (re-rodar não duplica) e **tolerante a falha**: se o Docker/container estiver
  indisponível, **não aborta o scraping** — apenas imprime o comando manual a rodar depois e os
  imóveis seguem salvos no SQLite/CSV. Retorna `True/False`.

**Scrapers já com auto-import (13):** `scraper_acre, _al, _amapa, _bahia, _ce, _ma, _para, _pb, _pe,
_pi, _rn, _se, _rr_ro`. Ao criar um novo scraper de junta, **reaproveitar** esse padrão (import +
chamada após o CSV) — assim a regra da seção 46 ("gravar em AMBOS os bancos") é cumprida por padrão,
sem depender de lembrar do passo manual.

> **Pré-requisito operacional:** o container `leilao_postgres` precisa estar de pé no fim do scraping.
> Se subir o Docker só depois, basta rodar `importar_site.py` (ou re-chamar o helper) sobre o
> `csv/imoveis_<junta>_<data>.csv` — idempotente.

---

## 46. REGRA OBRIGATÓRIA: gravar todo scraping em AMBOS os bancos (SQLite + container do site)

> ⚠️ Complementa a **Seção 0**. Concluir um scraping significa ter os imóveis válidos **nos dois
> bancos**, não só no SQLite. Caso contrário eles **não aparecem na listagem do site** (ver 45.5).

### 46.1. Os dois destinos (e a cilada dos dois PostgreSQL)

| Banco | O que é | Como acessar |
|---|---|---|
| **SQLite** `imoveis_leiloeiros.db` | Fonte de verdade dos scrapers standalone deste diretório | `sqlite3` direto no arquivo |
| **PostgreSQL do site** `leilao_db` | **O que a API/listagem lê** | **`docker exec leilao_postgres psql -U leilao -d leilao_db`** |

**Nunca** use `psycopg2 postgresql://...@localhost:5432` para o banco do site: a máquina tem um
**PostgreSQL do host** ouvindo em `localhost:5432` que **sombreia** o mapeamento do Docker
(`0.0.0.0:5432->5432`). Conectar em `localhost` cai no **banco do host** (errado) — os dados não
chegam ao site e você "perde" o trabalho silenciosamente. O único canal correto é `docker exec`.

Sintoma típico: `docker exec leilao_postgres psql ... -c "SELECT count(*)..."` e
`psycopg2 localhost` retornam **contagens e registros diferentes para o mesmo id** → são dois bancos.

### 46.2. Importador genérico — [`importar_site.py`](importar_site.py)

Grava o CSV simples de qualquer scraper standalone nos **dois** bancos, com dedup:

```bash
python importar_site.py --csv csv/imoveis_<UF>_<DATA>.csv \
    --fonte <FONTE_PG> --url-base <URL> --junta "<JUNTA/UF>" --estado-padrao <UF>

# exemplo (Alagoas)
python importar_site.py --csv csv/imoveis_al_2026-06-09.csv \
    --fonte JUCEAL --url-base https://www.juceal.al.gov.br/ --junta "JUCEAL/AL" --estado-padrao AL
```

O que ele faz:
- **SQLite:** cria a tabela se preciso e insere com dedup por `url` (PK derivada da url).
- **PostgreSQL (container):** garante a `fonte`, sobe um `stage` temporário, `\copy` do CSV e
  `INSERT ... SELECT` com **dedup global por `url_original`** (não duplica o que já está no site) +
  dedup intra-CSV (`DISTINCT ON (url_original)`) + `ON CONFLICT (fonte_id,id_externo) DO NOTHING`.
- **Idempotente:** rodar de novo reporta `novos=0` / `inseridos=0`.

### 46.3. Detalhes de mapeamento que quebram se ignorados

- **Nomes dos enums ≠ nomes das colunas.** Os tipos são `tipoimovel`, `tipoleilao`, `statusleilao`,
  `categoriaitem` (descubra com `information_schema.columns.udt_name`). Castar para
  `::tipo_imovel` falha (`type "tipo_imovel" does not exist`).
- **`\copy` com colunas TEXT + cast no INSERT.** O stage é todo `text`; converta com
  `NULLIF(x,'')::numeric` / `::timestamp` no `SELECT` (CSV vazio não vira NULL sozinho).
- **`id` é serial** (`nextval('imoveis_id_seq')`) — não envie `id`.
- **NUL bytes (`\x00`)** são removidos antes de inserir.
- **Acentos:** o banco guarda UTF-8 correto; `�` no terminal Windows é só artefato de exibição
  (cp1252), não corrupção.

### 46.4. Pós-importação (obrigatório)
1. Conferir contagem da fonte no container:
   `docker exec leilao_postgres psql -U leilao -d leilao_db -c "SELECT count(*) FROM imoveis WHERE fonte_id=<id>;"`
2. Validar pela API: `GET http://localhost:8000/api/v1/imoveis/{id}` e
   `GET /api/v1/imoveis/?ordenar_por=criado_em&ordem=desc` (os novos aparecem no topo).
3. Pendências de qualidade conhecidas a tratar: `cidade/estado` caindo no endereço do leiloeiro
   quando não parseados do card; `leiloeiro_id` não vinculado (nome aparece no detalhe, mas o card da
   lista depende do join) — sincronizar leiloeiros novos (seções 24/25) e popular `leiloeiro_id`.

> **Pendência herdada:** os scrapers PE, Pará e Bahia (e demais standalone) provavelmente só
> gravaram no SQLite. Reprocessar os respectivos `csv/imoveis_*.csv` com `importar_site.py` para
> levá-los ao site.


---

## MELHORIAS SUGERIDAS — Reprocessamento de páginas de LISTAGEM + drift de schema — 09/06/2026 22:21

> Reprocessamento dos 48 registros cujo `url_original` apontava para uma **página de listagem/evento**
> (`/eventos/leilao/...`, `/leilao/...`) com título de categoria ("Casas", "Carros", "Sucatas").
> Resultado: **48 artefatos desativados** (nunca foram imóveis individuais). A tentativa de extrair os lotes
> individuais retornou mais lixo de categoria ("ELETRÔNICOS", "SEMOVENTES", "Faça login para se habilitar"),
> revelando que esses portais exigem **adaptador por site**. Os 28 URLs extraídos NÃO foram importados.

### Melhorias a implementar (dificuldade → solução)

1. **[CRÍTICO] Drift entre o modelo ORM e o schema do banco quebra todo o pipeline.**
   O `database/models.py` (`Imovel`) declara colunas **`valor_arrematacao`, `rescrape_status`, `rescrape_em`**
   que **não existem** na tabela `imoveis`; e o banco tem **`documentos`, `edital_url`, `matricula_url`** que
   **não estão** no modelo. Qualquer `session.query(Imovel)` (full ORM select) falha com
   `UndefinedColumn: imoveis.valor_arrematacao` — isso **derruba importer, classifier e geocoder**.
   A API só responde porque seus endpoints usam schema de resposta com colunas específicas.
   **Solução:** criar e aplicar migração Alembic aditiva:
   ```sql
   ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS valor_arrematacao NUMERIC(15,2);
   ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS rescrape_status VARCHAR(20);
   ALTER TABLE imoveis ADD COLUMN IF NOT EXISTS rescrape_em TIMESTAMP;
   ```
   e reconciliar `documentos/edital_url/matricula_url` (migrar p/ o modelo ou removê-las do banco).
   **Enquanto a migração não roda, scripts auxiliares devem usar CORE SQL** (`text()`), nunca o ORM `Imovel`
   (foi o que se fez aqui para desativar os artefatos).

2. **Página de listagem capturada como se fosse 1 imóvel → detectar e expandir, não cadastrar.**
   Um registro apontando para `/eventos/leilao/<evento>` representa um **leilão com N lotes**, não um imóvel.
   **Solução:** no scraper, (a) **nunca** cadastrar a URL do evento como imóvel; (b) ao encontrar uma listagem,
   **iterar os lotes** e cadastrar cada `/item|/oferta/<id>` individualmente; (c) para registros legados desse
   tipo, **desativar** (feito: 48) e recapturar via adaptador.

3. **Portais com lotes gated por login / navegação por categoria → adaptador por site (não genérico).**
   juleiloes, kleiberleiloes, lancevip, bigleilao: os links dentro da listagem são **subcategorias**
   ("ELETRÔNICOS", "SUCATAS") ou exigem **"Faça login para se habilitar"**; o HTML público não expõe os lotes.
   **Solução:** adaptador dedicado por plataforma — identificar o endpoint JSON/AJAX de lotes (muitos usam
   `/api/.../lotes?leilao=<id>`), ou Playwright autenticado quando o catálogo exige sessão; só então extrair
   título/preço/data por lote. Extração genérica de `<a href*=item>` aqui só traz lixo de menu.

4. **Validar título extraído contra um dicionário de "categorias/sucata" antes de gravar.**
   "ELETRÔNICOS", "SEMOVENTES", "MÁQUINAS", "Faça login", "0 itens" são rótulos de navegação, não títulos.
   **Solução:** estender o filtro `LIXO` para rejeitar rótulos de categoria isolados e textos de UI
   ("faça login", "habilitar", "N itens"), exigindo que o título tenha ao menos um descritor de bem
   (apartamento/terreno/veículo/marca-modelo) ou número de lote real.

### Métodos que JÁ funcionaram (consolidar no scraper de produção)
- Título: cascata **`og:title` → JSON-LD `name` → `<title>` limpo → `<h1>`** (recupera ~90% sem browser; corrige mojibake).
- Recuperar título do **slug da URL** quando o SPA não entrega meta (`/anuncio/toyota-hilux-...`).
- **Excluir âncoras sociais** (`whatsapp|twitter|x.com|facebook`) da seleção de lotes; recuperar título de `?text=` só p/ legados e então desativar (link quebrado).
- **404/410 → desativar**; concorrência ≤ 8 + retry p/ evitar falsos timeouts.

**Relatório gerado em:** 09/06/2026 22:21:40


---

# Captura SE (JUCESE) + CE (JUCEC/TJCE/TRT7) — 09/06/2026

Execução em duas etapas sequenciais usando `scraper_se.py` e `scraper_ce.py`
(mesmo motor do `scraper_pe.py`: Playwright + fallback FlareSolverr, filtro de
1ª praça futura, cache por domínio, dedup por URL no `imoveis_leiloeiros.db`).

## Resultado consolidado

| UF | Leiloeiros (regulares no CSV) | Com site / sem site | Imóveis 1ª praça futura | Novos no banco | Já existiam | Inacessíveis |
|----|------|------|------|------|------|------|
| **SE** | 23 | 21 / 2 | **61** | 19 | 42 | 0 |
| **CE** | 54 | 43 / 11 | **235** | 17 | 218 | 1 |

CSVs gerados: `csv/leiloeiros_se_2026-06-09.csv`, `csv/imoveis_se_2026-06-09.csv`,
`csv/leiloeiros_ce_2026-06-09.csv`, `csv/imoveis_ce_2026-06-09.csv`.

### SE — imóveis por leiloeiro (somente Situação = Regular no PDF)
Valerio Cesar de Azevedo Deda (lancese) **13** · Rodrigo Rigolon (rigolonleiloes) **11** ·
Paulo Cezar Rocha Teixeira (leiloesjudiciaisbahia) **6** · Carlos Vinicius Mascarenhas (rjleiloes) **5** ·
Daniel Elias Garcia (dgleiloes) **5** · Jonas Antunes (mgl) **5** · Lucas Antunes (mgl) **5** ·
Jose Ivan Rabelo (realizaleiloes) **4** · Cristiane Gois (argleiloes) **2** ·
Fernando Moreira Filho (fernandoleiloeiro) **2** · Francisco Jonnathan (franciscofreitasleiloes) **2** ·
Helcio Kronberg (kronleiloes) **1** · demais 0; Hildegards e Adelane sem site (pulados).
Excluídos por situação ≠ Regular: Jailson, Alisson, Sostenes, Arthur Nunes, Erico Sobral,
Marco Túlio, Davi Borges, Lucas Marllon, Rodrigo Prata, Ivana Rocha (Irregular),
Eduardo Schmitz (Cancelado), Robério, Irani Flores, Fellipe, Antônio Carlos, Danyele (Irregular),
toda a seção INATIVOS e CANCELADOS.

### CE — imóveis por leiloeiro (registros JUCEC/TJCE/TRT7; nenhum marcado cancelado/suspenso)
Irani Flores (leilaobrasil) **160** ⚠️ · Mauricio J. Costa (mjleiloes) **14** · Rodrigo Rigolon (rigolonleiloes) **11** ·
Daniel Melo Cruz (grupolance) **11** · Silvio Maraschi (hastapublica) **5** · Rudival Gomes Jr (rjleiloes) **5** ·
Daniel Elias Garcia (danielgarcialeiloes) **5** · Jonas Antunes (mgl) **5** · Lucas Antunes (mgl) **5** ·
Francisco Jonnathan (nortenordesteleiloes) **2** · Carlos A. Ribeiro Lima (infinityleiloes) **2** ·
Fernando Moreira Filho (fernandoleiloeiro) **2** · Paulo Marcelo (upleilao) **2** · Ivana Rocha (patiorochaleiloes) **2** ·
Francisca Medeiros (construbemleiloes) **1** · Ives Nasar (nasarleiloes) **1** · Eduardo Fleury (leilomaster) **1** ·
Regilane Monteiro (construbemleiloes) **1** · demais 0; 11 sem site (pulados);
William Marden (otonirodriguesadvogados — escritório de advocacia) inacessível.

## Principais dificuldades enfrentadas e correções sugeridas

1. **Portais nacionais inflam a contagem.** `leilaobrasil.com.br` (Irani Flores) devolveu
   **160** lotes — quase todos já existentes no banco (CE inseriu só 17 novos de 235; SE só 19 de 61).
   Esses portais agregam leilões do Brasil inteiro, não os do leiloeiro específico.
   *Correção:* manter um blocklist de domínios-portal (`leilaobrasil`, `bomvalor`, `vlance`,
   `leilaojudicial`…) e, nesses casos, filtrar lotes pelo nome/matrícula do leiloeiro na página,
   ou raspar via a rota do leiloeiro (ex.: `hastapublica.com.br/grupos/11?TJ-CE`) em vez da home.

2. **Múltiplos leiloeiros no mesmo domínio.** montenegroleiloes (Fernando, Daniela, Georgia,
   João Paulo), mgl (Jonas, Lucas), lancecertoleiloes (Saulo, Marco Túlio), construbemleiloes
   (Francisca, Regilane), rjleiloes (Carlos Vinicius/SE e Rudival/CE). O cache por domínio
   atribui o **mesmo conjunto** de imóveis a todos — superestima por pessoa.
   *Correção:* dentro de site compartilhado, segmentar lotes por leiloeiro (página/curador) e
   atribuir só os correspondentes; contar imóveis únicos por domínio no relatório.

3. **Leiloeiros regulares sem site.** SE: 2 (Hildegards, Adelane — "Site: #"); CE: 11
   (só e-mail gmail/hotmail). Ficam não-raspáveis.
   *Correção:* inferência de domínio a partir de e-mail corporativo (feito p/ Daniel Garcia→dgleiloes,
   Renato→rmoyses, Silvio→hastapublica); para e-mails pessoais, buscar "nome + leilões" no Google
   ou localizar em agregadores (bomvalor/vlance) e cadastrar o site real.

4. **Domínio inferido nem sempre é portal de leilão.** `otonirodriguesadvogados.com.br`
   (William Marden) é escritório de advocacia → inacessível/zero lotes. Risco de falso-site.
   *Correção:* validar que a home retorna sinais de leilão (palavras "lote/leilão/edital") antes
   de varrer; senão marcar "site não é portal de leilão".

5. **Mesma pessoa, domínios diferentes por estado.** Daniel Elias Garcia usa `dgleiloes.com.br`
   no PDF de SE e `danielgarcialeiloes.com.br` no de CE (ambos funcionaram, 5 imóveis cada).
   *Correção:* consolidar aliases de leiloeiro→domínios num cadastro mestre para reaproveitar
   entre estados e evitar redescoberta.

6. **Alta sobreposição entre estados (dedup por URL).** CE teve 218/235 já no banco porque
   muitos "leiloeiros do CE" são nacionais já capturados em rodadas anteriores (PE etc.):
   Irani/leilaobrasil, Davi/alfaleiloes, Renato/rmoyses, Rigolon, Arthur Nunes. O dedup por URL
   funcionou corretamente — não é bug, mas a contagem "imóveis do estado" superdimensiona o
   estoque local. *Correção:* relatório separar "novos" de "reatribuições" e, se útil, marcar a
   UF do imóvel pela cidade/UF do lote, não pela junta do leiloeiro.

7. **Cloudflare + render JS é lento.** `realizaleiloes.com.br` e similares só abrem via
   FlareSolverr; SE levou ~52 min e CE ~75 min. *Correção:* reaproveitar sessões persistentes do
   FlareSolverr (já mapeado no projeto Milan), paralelizar 2-3 domínios e preferir endpoints
   JSON/API quando existirem (padrão vlance `/core/api/get-leiloes`).

8. **Datas só no detalhe.** Cards sem data exigem abrir a página do lote (orçamento limitado a 14
   por domínio); lotes sem data detectável são descartados, podendo perder imóveis válidos.
   *Correção:* aumentar o orçamento de detalhe para sites pequenos e extrair data de JSON-LD/API.

### 38.12. Sistema de staging/aprovação de novos anúncios + enriquecimento com documentos

Pedido: revisar os sites dos leiloeiros, raspar tudo, comparar com o banco, **enriquecer cada
anúncio** com fotos/descrição/edital/matrícula/anexos (transferindo os documentos para a janela
do anúncio), **sem deduplicar** anúncios, e jogar os **novos** numa **página de aprovação** onde o
usuário dá OK antes de inserir nos **2 bancos**.

**Escopo real:** o banco tem **777 fontes / 1.362 sites de leiloeiros / 123.260 imóveis**. Raspar
e enriquecer tudo é operação de semanas (muitos scrapers quebrados/diversos). Entregue o
**mecanismo completo** + execução numa fatia validada (leiloeiros RR/RO), **fonte-agnóstico** para
escalar incrementalmente.

**Arquitetura (3 peças, em `/leiloes`):**
1. `staging_anuncios.py` — captura (reusa `scraper_rr_ro`) → **enriquece** cada lote abrindo a
   página de detalhe (`enrich()`: descrição, todas as fotos, edital, matrícula, anexos PDF, com
   *allowlist* `edital|matr[ií]cula|laudo|avalia` e *blocklist* `cookie|termo|aviso`) → **compara a
   URL contra os 2 bancos** (SQLite local + Postgres via `psycopg2`) → classifica **NOVO ×
   JÁ_NO_BANCO** (não deduplica: cada anúncio é distinto) → grava NOVOS em `staging.db`
   (`staging_imoveis`) + `staging_anuncios.json`, e gera `anuncios_novos.html`.
2. `anuncios_novos.html` — página de revisão: cards com foto, título, cidade/UF, 1ª praça, preço,
   descrição, **chips de edital/matrícula/anexos**, galeria e badge **🆕 NOVO** (com checkbox
   "Aprovar") × **✓ já no banco · enriquecido**.
3. `aprovar_anuncios.py` — após o OK: `--aprovar-todos`/`--aprovar aprovados.txt` → `--inserir`
   insere os aprovados nos **2 bancos** (SQLite `imoveis` + Postgres via
   `run.py importar-scraping --upsert`, levando `arquivos`/`edital_url`/`matricula_url`).

**Pipeline estendido para persistir documentos** (`pipeline/importar_scraping.py`):
- `_record_para_imovel` agora mapeia **`arquivos`** (JSON `[{tipo,url,nome}]`) a partir de
  `arquivos`/`documentos` + `edital_url`/`matricula_url`.
- `_aplicar_upsert` passou a **preencher em registros existentes**: `arquivos` (quando vazio),
  `descricao`, `imagens` (galeria) — além de valor/cidade/estado/imagem. Assim o **re-scrape
  enriquece o anúncio que já está no banco** sem duplicar.

**Execução (4 leiloeiros RR/RO):** 36 imóveis capturados → **0 NOVOS** (os DBs já estavam
populados das rodadas anteriores) / **32 JÁ_NO_BANCO**. Enriquecimento: **32/32 com anexos
(edital+matrícula+docs), 32/32 com fotos, 31/32 com descrição** (~1500 chars). Aplicado via
`importar-scraping --upsert`: **29 anúncios atualizados** no Postgres — verificado:
`arquivos` agora traz os PDFs de edital/matrícula, `descricao` preenchida, `imagens` (galeria) em
parte. Os documentos passaram a constar na "janela" do anúncio.

> **Por que 0 novos:** as rodadas anteriores já inseriram os imóveis em destaque desses
> leiloeiros. O mecanismo de comparação está correto (achou os 36 como existentes). Para a página
> de aprovação aparecer **populada de novos**, basta rodar sobre **fontes ainda não raspadas** ou
> com **crawl mais fundo** (ver melhorias).

#### Melhorias e correções sugeridas
1. **URL por lote (crítico).** Em plataformas vlance, vários lotes compartilham a URL do leilão
   (`/leilao/index/leilao_id/<id>`), então fotos/edital de lotes diferentes colidem e a
   comparação "novo×existente" fica grossa. Capturar a **URL real por lote** (ex.: endpoint
   `get-lotes` ou link do lote) para granularidade correta.
2. **Crawl mais fundo para surfasr novos.** Os scrapers pegam só os lotes em destaque; paginar
   todos os leilões/lotes de cada leiloeiro revela os que ainda não estão no banco.
3. **Baixar os PDFs (não só linkar).** Rodar `run.py baixar-docs` após o enriquecimento para
   salvar edital/matrícula em disco (`storage/docs/`) e gravar `path_local`/`hash_md5` em
   `arquivos` — evita link quebrado quando o leiloeiro remove o anexo.
4. **Fila de aprovação no frontend/admin** em vez de HTML estático: endpoint que lê `staging.db` e
   um botão "Aprovar → inserir" que chama `aprovar_anuncios.py` (auditável, multiusuário).
5. **Galeria completa.** Só ~parte dos anúncios trouxe `imagens`; padronizar a extração da galeria
   (carrossel) por plataforma.
6. **chromium no `leilao_worker`** (`playwright install chromium`) para rodar os scrapers que
   dependem de render dentro do beat/worker.
7. **Normalizar cidade/UF** dos enriquecidos com `run.py normalizar-cidades` (IBGE).
8. **Rate-limit/robots** por domínio no enriquecimento (abrir N páginas de detalhe é pesado);
   honrar `robots.txt` e espaçar requisições.

### 37.6. ⚠️ Pós-importação obrigatório: vincular `imoveis.leiloeiro_id`

**Sintoma:** após importar, os imóveis existem no banco (`ativo=true`, `status=ABERTO`),
aparecem na listagem por `estado`/busca, mas **não aparecem ao navegar pela aba Leiloeiros**
nem ficam atribuídos ao leiloeiro.

**Causa:** o scraper grava o campo texto `leiloeiro` mas **não preenche `imoveis.leiloeiro_id`**
(FK para a tabela `leiloeiros`). O frontend filtra/agrupa por `leiloeiro_id`; com ele NULL, o
imóvel fica "órfão" na visão por leiloeiro.

**Correção:** rodar `sync_leiloeiros_jucea.py` após a importação:
1. Casa o nome do leiloeiro (normalizado sem acento/maiúsculas; alias para variações do
   `auctioneer` da loja TJAM) com a tabela `leiloeiros`, preferindo o registro `junta='JUCEA'`.
   A tabela `leiloeiros` **não tem unique em `nome`** → há duplicatas de grafia; sempre casar por
   nome normalizado, nunca por igualdade literal, para não criar novos duplicados.
2. Insere os REGULAR ausentes (situacao='Regular').
3. `UPDATE imoveis SET leiloeiro_id=… WHERE leiloeiro='<texto exato>' AND leiloeiro_id IS NULL`.

Resultado (2026-06-09): 348/348 imóveis vinculados, 0 órfãos; 23 leiloeiros já existiam.
**Recomendação:** mover essa etapa para dentro do `importar_postgres()` (resolver `leiloeiro_id`
no momento do INSERT) para que todo scraper já grave a FK e a sincronização da aba Leiloeiros
seja automática (complementa a regra 0/seção 24 de sincronização obrigatória).

---

## 49. Scraping do site Daniel Garcia Leilões — site único, server-rendered (2026-06-09)

> Scraping direto de **um leiloeiro nacional** a partir do próprio site
> `https://www.danielgarcialeiloes.com.br/` (não de PDF de junta). Implementação em
> [`scraper_danielgarcia.py`](scraper_danielgarcia.py). Diferente das seções 40-48 (juntas), aqui o alvo
> é **uma fonte só**, totalmente renderizada no servidor — **sem Playwright e sem FlareSolverr**.

### 49.1. Reconhecimento (o achado que dispensou o navegador)
- **HTML 100% server-rendered** (`requests` puro retorna 140 KB já com lotes, preços e descrições). Sem
  SPA, sem `__NEXT_DATA__`, sem Cloudflare. Playwright seria desperdício — o scraper inteiro roda em
  `requests.Session` com `urllib3.Retry`, **~1 min para os 39 leilões**.
- **Estrutura de URLs previsível e estável:**
  `/calendario-leiloes` → lista os IDs de leilão →
  `/leilao/<id>/lotes?page=N` (30 lotes/página, paginação por `?page=N`) →
  `/item/<id>/detalhes` (detalhe, com edital/matrícula).
- **O card da lista já traz quase tudo** — não foi preciso visitar cada `/item`: título-categoria (`<h5>`),
  `Cidade: <Município>/<UF>`, descrição com `VALOR MÍNIMO DE ARREMATAÇÃO`/`LOCALIZAÇÃO`/`ENDEREÇO`,
  imagem (URL do CDN gocache no `background:url(...)` do `<a>` da miniatura) e o `Lance Inicial`.
- **A data da praça mora no cabeçalho do LEILÃO, não no card.** Dois formatos:
  - extrajudicial/prefeitura/PF → `Data do Leilão: DD/MM/YYYY`;
  - judicial (Vara Cível etc.) → `1º Leilão: DD/MM/YYYY` (e `2º Leilão: …`).
  Como todos os lotes de um leilão compartilham a mesma data, ela é lida **uma vez por leilão** e aplicada
  a todos os cards — barato e correto.

### 49.2. Resultado
- **39 leilões** no calendário; **87 imóveis** com 1ª praça futura coletados.
- **SQLite:** 81 novos + 6 já existiam = **87** (verificação CSV↔banco: 87/87 URLs presentes).
- **PostgreSQL do site (`leilao_db` no container):** **todos os 87 URLs presentes** (1 inserido agora,
  86 já estavam sob outras fontes/juntas) — via [`importar_site.py`](importar_site.py), dedup global por
  `url_original`. Gravação em **ambos os bancos** conforme [[project_gravar_ambos_bancos]] e seção 46.
- Maiores rendimentos: leilões 7598 e 7647 (30 cada — prefeituras com lotes de terreno), 7589 (8),
  7555/7591/7632 (3 cada). Os leilões da **Polícia Federal/PRF** (7556, 7557, 7622, 7623) e de **sucatas**
  (7522, 7550, 7607) renderam **0 imóveis** — corretamente filtrados (veículos/sucata).

### 49.3. Principais dificuldades
1. **Catálogo fortemente misto dentro do mesmo leilão.** Mesmo leilões judiciais trazem semoventes
   (ovinos/bovinos), veículos e máquinas ao lado de imóveis. A filtragem **tem de ser por lote** (título +
   descrição), não por tipo de leilão — não dá para confiar no título do leilão.
2. **"Mojibake" enganoso no console, não nos dados.** O `print` no PowerShell (cp1252) exibia
   `Descri��o`, mas os **bytes crus são UTF-8 válidos** (`\xc3\xa7\xc3\xa3o` = "ção"). Confirmado lendo
   `r.content` direto. Lição: antes de "consertar encoding", cheque os **bytes**, não o que o terminal
   mostra — o CSV/SQLite em UTF-8 saíram corretos sem nenhuma conversão.
3. **Data ausente no `get_text()` global.** Extrair qualquer `DD/MM/YYYY` da página pegava datas das
   **descrições** dos lotes (ex.: "avaliado em 12/09/25"). Solução: regex **rotulada**
   (`Data do Leilão|Nº Leilão|Nº Praça`) restrita ao **cabeçalho** (texto antes do primeiro `Lote 0`).
4. **Cada lote aparece em 2-3 `<a>` no card** (miniatura + título + bloco de preço). Sem dedup por URL
   canônica (`/item/<id>/detalhes`, sem `?page=`), a contagem dobrava. Dedup por URL resolveu.
5. **"Lance Inicial" tem variações.** Em judicial há `Lance Inicial 2º Leilão: R$…` além do
   `Lance Inicial: R$…` base. A precedência adotada foi `Lance Inicial:` > `VALOR MÍNIMO DE ARREMATAÇÃO` >
   primeiro `R$` do card.
6. **Site lento/instável de vez em quando** (read timeout de 30 s numa varredura). Resolvido com
   `urllib3.Retry(total=3, backoff)` + `get_html()` com 4 tentativas e backoff próprio.
7. **"Poucos novos" no PG (1/87) ≠ falha.** O leiloeiro já fora capturado em rodadas anteriores sob
   múltiplas juntas (havia 184 registros de Daniel Garcia no SQLite sob JUCEES/JUCEPAR/JUCISRS/JUCEA/
   JUCESC/etc.). O ganho real foi confirmar cobertura + 81 URLs novos no SQLite local que estava defasado
   em relação ao PG de produção (mesmo padrão de 48.4).

### 49.4. Correções/melhorias sugeridas
1. **Adaptador "site único server-rendered" no scraper genérico.** Detectar via sondagem
   (`site_health` + contagem de cards no HTML cru) sites que **não precisam de Playwright** e roteá-los
   para um caminho `requests`-only — drasticamente mais rápido (1 min vs. dezenas). Daniel Garcia é o
   caso-padrão: enumerar `/calendario` → `/leilao/<id>/lotes?page=N`.
2. **Reconhecer a plataforma "Daniel Garcia / leilões.br".** As URLs `/leilao/<id>/lotes` e
   `/item/<id>/detalhes`, o CDN `*.cdn.gocache.net/watermark/bens/` e o rótulo `L<leilao>_H<hash>` são
   assinatura de uma plataforma reutilizada (o cadastro lista `dgleiloes.com.br`, `dgleiloes.leilao.br`).
   Vale um adaptador por-plataforma (camada da seção 27) que outros leiloeiros nessa mesma engine reusem.
3. **Capturar a 2ª praça também.** Hoje só guardo a 1ª data futura. O card/cabeçalho judicial expõe
   `2º Leilão` + `Lance Inicial 2º Leilão` — útil para o usuário saber o piso da 2ª praça. Estender o
   schema (ou concatenar em `descricao`) com `data_2a_praca`/`lance_2a_praca`.
4. **Baixar edital/matrícula por lote.** Hoje anexo só o **edital do leilão** (1 PDF, do cabeçalho). O
   `/item/<id>/detalhes` tem `/matriculas` e PDFs por lote — integrar com `run.py baixar-docs`
   (seção do `DocumentoDownloader`) para puxar matrícula individual.
5. **Endereço estruturado.** A descrição traz `ENDEREÇO: …` e `LOCALIZAÇÃO: …` em formato regular —
   parsear para a coluna `endereco` (hoje vazia) além de `cidade`/`uf`.
6. **Classificador de imóvel reutilizável.** `is_imovel(title, desc)` (listas `IMOVEL_WORDS`/`NEG_WORDS`)
   funcionou bem aqui filtrando veículos/semoventes/sucata; promover para `scraper_commons.py` e
   reaproveitar nos demais scrapers (substitui o `TITLE_TYPES`/`NEG_WORDS` duplicado em pe/pb/ba/al).
7. **Importação ao PG embutida no `main()`** (mesma recomendação de 48.5): ao gerar o CSV, chamar
   `importar_site.py` automaticamente em vez de passo manual.

### 49.5. "Não estou encontrando no site" — leiloeiro_id NULL + duplicatas + registros stale

**Sintoma:** após importar, o usuário não achava os imóveis na listagem. Diagnóstico no PG (casando
os 87 `url_original`):

| fonte | status | qtd | datas |
|---|---|---:|---|
| Daniel Garcia Leiloes | ABERTO | 43 | 2026-06-11 a 06-29 ✅ |
| JUCEPAR | ABERTO | 5 | futuras ✅ |
| JUCEPAR | ENCERRADO | 1 | 2026-06-08 (passou) |
| **danielgarcialeiloes** | **ENCERRADO** | **38** | **2010-2019** ❌ |

Duas causas independentes:
1. **Todos os 87 com `leiloeiro_id = NULL`.** O frontend agrupa/filtra por `leiloeiro_id`; nulo →
   imóvel "órfão", invisível na visão por leiloeiro (mesma pendência de 45.5/48.4 — recorrente porque
   `importar_site.py` ainda **não resolve a FK** no INSERT).
2. **Registros stale sombreando os novos.** 38 dos imóveis já existiam sob a fonte antiga
   `danielgarcialeiloes` com `status=ENCERRADO` e datas-lixo de 2010-2019. Como `url_original` é
   **único global**, o import (INSERT-only) **não atualizou** esses registros → continuam encerrados e
   somem da listagem de abertos. **"Já existe" ≠ "está correto".**

**Correção aplicada (autorizada pelo usuário — só `leiloeiro_id` + consolidação; sem mexer em
status/datas):** a tabela `leiloeiros` tinha **5 duplicatas de grafia** do mesmo leiloeiro (não há
unique em `nome`): ids 1087(SC,30), 2333(153), 2254(SP,3), 2394(RJ,2), 2004(0). Script
[`consolidar_danielgarcia_leiloeiro.py`](consolidar_danielgarcia_leiloeiro.py):
- escolheu **id 1087** como canônico (melhor metadado: Regular/SC/nome em caixa correta/email/site);
- **repontou 158** imóveis das 4 duplicatas → 1087 e **ligou os 87** recém-raspados (estavam NULL);
- gravou domínios alternativos em `sites_alternativos` e **removeu as 4 duplicatas**;
- resultado: **275 imóveis no canônico, 0 dos 87 ainda NULL, 0 duplicatas**. API confirmou:
  `GET /api/v1/imoveis/?leiloeiro_id=1087` → **86 ativos** (HTTP 200) — agora findáveis.

**Pendência deixada de propósito (decisão do usuário):** os 38 registros `danielgarcialeiloes` com
datas 2010-2019/ENCERRADO continuam ocultos — não foram refrescados. Para exibi-los seria preciso um
**UPDATE de data/status** (script [`corrigir_danielgarcia_pg.py`](corrigir_danielgarcia_pg.py),
preparado mas **não executado**). Mexer em registros de produção pré-existentes exige autorização
explícita — o classificador de auto-mode bloqueou o UPDATE em massa até o usuário decidir.

**Lições (reforçam 48.5):**
- O `importar_site.py` precisa de um modo **upsert** que, ao reencontrar `url_original`, **atualize**
  data/status/valor dos registros stale em vez de só ignorá-los.
- Resolver `leiloeiro_id` (por nome normalizado, com alias) **dentro** do INSERT/UPDATE — e manter um
  unique/canonicalização em `leiloeiros.nome` para não recriar duplicatas de grafia.
- Modificação de dados de produção que já existiam antes da sessão **não está coberta** por um pedido de
  scraping/importação: pare e peça autorização (foi o que o auto-mode fez aqui, corretamente).
