# Captura de Dados de Leilões — Guia Mestre + Prompt Operacional

> **Documento único e definitivo** para coletar, de qualquer site de leilão, **todos** os
> detalhes de cada lote (foto, descrição, localização, datas, valores, anexos) com **cobertura
> máxima** e **mínima perda/erro**. Consolida o guia de captura (v2), o playbook operacional
> (escada de robustez) e o prompt acionável num só lugar.
>
> Tem duas camadas de leitura:
> - **Parte I–IX — Guia/Referência:** como pensar e implementar a captura.
> - **Parte X — Prompt:** cole direto para o Claude executar uma coleta. Substitua os `{{...}}`.

---

## Como usar este documento

1. Site **novo**? Leia a **Parte II** (escada) e **III** (reconhecimento), escolha o tier na
   **árvore de decisão (III.4)** e implemente pelo método correspondente (**Parte IV**).
2. Quer **rodar uma coleta** agora? Vá direto à **Parte X (Prompt)** e preencha os campos.
3. **Regra de ouro:** fique o mais **alto possível na escada**. Só desça um degrau quando o
   anterior estiver bloqueado de fato — e antes de descer, pergunte *"estou apanhando porque
   estou rápido demais?"*. A maioria dos bloqueios é **frequência, não fingerprint**; educação
   (rate limit + cache + respeitar `429`) evita o bloqueio antes dele.

---

# PARTE I — Princípios obrigatórios

1. **Fonte mais limpa primeiro.** API > JSON embutido > HTML renderizado. APIs retornam dados
   exatos e tipados; o HTML é frágil e muda. Sempre mire na fonte mais próxima do JSON.
2. **Caminho mais barato primeiro** (escada da Parte II). Só escale a complexidade quando o
   site exigir de fato.
3. **Nunca engula erro.** Toda exceção/timeout/seletor ausente vai para log estruturado com a
   URL e o campo que falhou. **"Sem dado" e "erro ao buscar" são estados diferentes.**
4. **Idempotência e retomada.** Persista progresso em `*_progress.json`, faça **upsert** (não
   duplique) e permita reexecutar de onde parou (`--reset` reinicia do zero).
5. **Robustez, não força.** Retries com backoff, detecção de mudança de layout, fallbacks por
   campo e logs cobrem mais casos de forma sustentável do que tentar burlar proteções.
6. **Guarde o cru.** Salve o JSON/HTML bruto além dos campos normalizados — quando o produto
   pedir um campo novo, ele já está no banco, sem recoletar.
7. **Respeite o site.** `robots.txt`/ToS, rate limit, concorrência baixa por domínio, sem
   contornar CAPTCHA ativo. A maioria dos dados de leilão é pública por obrigação legal.

---

# PARTE II — A escada de robustez (espinha dorsal)

Use o método mais estável e barato que funciona; desça só quando bloqueado.

| Tier | O que é | Ferramenta típica | Característica |
|------|---------|-------------------|----------------|
| **1** | API oficial / endpoint JSON interno | `requests`/`httpx` (`json_api_harvester`) | mais **estável, rápido, barato** (10–50× navegador) |
| **2** | Intercepção de API (XHR/Fetch) via Playwright | `pw_intercept` (`page.on("response")`) | descobre e promove ao Tier 1 |
| **3** | JSON embutido no HTML (`__NEXT_DATA__`, JSON-LD, globais) | `html_json_extractor` + `chompjs` | robusto, não executa JS; blob por-página |
| **4** | Parse de HTML renderizado (CSS/XPath) | `selectolax`/`BeautifulSoup`/`lxml` | camada mais frágil; contrato é o DOM |
| **5** | Navegador headless + execução de JS | Playwright / `camoufox` | só quando o dado não existe sem rodar JS |
| **6** | Evasão pesada (TLS, proxies, stealth, CAPTCHA) | `curl_cffi`, `camoufox`/`patchright` | mais **frágil, lento, caro**; último recurso |

Quanto mais alto, mais perto do JSON que alimenta a página → menos depende do HTML → quebra
menos. A maioria dos leiloeiros (WordPress, Next/Nuxt, server-rendered) resolve em **tiers 1–4**.
Tiers 5–6 são bisturi para poucas fontes com anti-bot agressivo.

**Reaproveite o que já existe no repositório:** `scraper_commons.py`
(`site_health`, `cards_from_json`, `candidate_sites`, `upsert_multijunta`) e os adapters por
plataforma (`superbid_adapter.py`, `lancevip_adapter.py`, `scraper_milan.py`, etc.).

---

# PARTE III — Reconhecimento do site (etapa obrigatória antes de codar)

### III.1 DevTools → aba Network (a mais importante)
- Filtre por **Fetch/XHR**. Dispare o que carrega dados (buscar, paginar, scroll, abrir lote).
- Identifique o endpoint de **listagem** e o de **detalhe** que retornam **JSON**.
- Anote: URL, método, params de query, payload e headers (`Authorization`, `X-CSRF-Token`,
  `Cookie`, `User-Agent`, `Referer`).
- Botão direito → **Copy as cURL** replica a chamada exata. Converta com `curlconverter`.
- Filtre por **WS** para ver WebSocket (lances ao vivo).

### III.2 Identifique a tecnologia
- **SSR/HTML estático:** dados já no HTML → scraping direto (Tier 4) ou JSON-LD (Tier 3).
- **SPA (React/Vue/Angular):** HTML inicial quase vazio → API interna (Tier 1/2).
- **Next.js:** `<script id="__NEXT_DATA__">` (ouro em `props.pageProps`); guarde o `buildId`
  e use o atalho de API `/_next/data/<buildId>/<rota>.json?page=N`.
- **Nuxt:** `window.__NUXT__`. Outros globais: `__INITIAL_STATE__`, `__APOLLO_STATE__`.
- **Wappalyzer** identifica o stack rápido.

### III.3 Fontes estruturadas e sinais
- `/robots.txt` e `/sitemap.xml` → mapa de URLs, modelo de paginação e `lastmod` (incremental).
- JSON-LD `<script type="application/ld+json">` → dados schema.org (`Product`/`Offer`/
  `RealEstateListing`); em WordPress (Yoast/RankMath) há `@graph`.
- Identifique **paginação** (página numérica / offset / cursor / scroll) e **anti-bot**
  (Cloudflare/DataDome/Akamai).

### III.4 Árvore de decisão de tier
1. O dado aparece em alguma resposta **XHR/Fetch**? → **Tier 1/2** (intercepte e replique).
2. Está no HTML como **JSON embutido**? → **Tier 3**.
3. Está no **markup renderizado** da resposta inicial? → **Tier 4** (httpx + parse).
4. Só materializa **depois do JS rodar**, ou não há resposta válida sem executar JS
   (anti-bot/assinatura)? → **Tier 5**, e se ainda bloquear → **Tier 6**.

> **Falsos gatilhos de browser** (NÃO precisam de Tier 5/6): "é SPA Next" (dado está no XHR ou
> `__NEXT_DATA__`); "carrega no scroll" (XHR paginável); "tem Cloudflare" (cookie cunhado uma
> vez + TLS); "precisa logar" (POST que devolve token); "view-source vazio" (shell manda dado
> por XHR).

---

# PARTE IV — Métodos por tier (com código)

### Tier 1 — API / JSON na fonte
A abordagem mais eficiente. Replica o que o front-end faz; paralelizável e estável.

```python
import requests
headers = {"User-Agent": "Mozilla/5.0 ...", "Accept": "application/json",
           "Referer": "https://leilao.com/lotes"}
params = {"categoria": "imoveis", "pagina": 1, "ordenar": "preco"}
r = requests.get("https://api.leilao.com/v1/lotes", headers=headers, params=params)
for lote in r.json()["resultados"]:
    print(lote["id"], lote["titulo"], lote["lance_atual"])
```
**Gotchas:** guarde o JSON **cru**; `403` súbito = sessão/cookie expirado (renove via Tier 2/5);
sem `total`, pare quando a página vier vazia; endpoint atrás de anti-bot → troque `httpx` por
`curl_cffi` (Tier 6). **Atalho Next.js:** bata em `/_next/data/<buildId>/<rota>.json`.

### Tier 2 — Intercepção XHR/Fetch (Playwright)
Quando o endpoint só aparece com JS rodando ou precisa de sessão. É também o instrumento de
**descoberta** para promover ao Tier 1.

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    capturado = []
    page.on("response", lambda r: capturado.append(r.json())
            if "/api/imoveis" in r.url and r.status == 200
            and "json" in r.headers.get("content-type", "") else None)
    page.goto("https://leilao.com/imoveis", wait_until="domcontentloaded")
    page.wait_for_selector(".lote-card")
    browser.close()
```
**Padrão de produção:** abra a página **uma vez** para cunhar a sessão e exportar
(`sessao_httpx.json`), depois faça o loop no **Tier 1**. Não crawleie milhares de páginas dentro
do browser. **Gotchas:** `response.json()` quebra em redirect/304/não-JSON (filtre status 200 +
content-type + try/except); evite `networkidle` (instável); `context.request` herda a sessão mas
**não executa JS**.

### Tier 3 — JSON embutido no HTML
```python
import json, re
from bs4 import BeautifulSoup
# Next.js
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
data = json.loads(m.group(1))                       # ouro em data["props"]["pageProps"]
# JSON-LD
for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
    item = json.loads(tag.string)                   # nome, preço, datas no padrão schema.org
```
Globais JS (`__NUXT__`, `__INITIAL_STATE__`) são atribuição, não JSON → parseie com `chompjs`.
**Gotchas:** blob por-página → ainda enumere as páginas; JSON escapado em atributo precisa
unescape; confira completude (alguns sites renderizam só um subconjunto e completam via API).

### Tier 4 — Parse de HTML renderizado
A fonte do HTML é indiferente: `httpx.get(url).text` ou `page.content()` — mesmo código.
```python
import requests
from bs4 import BeautifulSoup
soup = BeautifulSoup(requests.get(url, headers=headers).text, "lxml")
titulo = soup.select_one("h1.lote-titulo").get_text(strip=True)
```
**Regra de robustez:** ancore em sinal **estável** (`data-*`, `id`, `itemprop`) e, na página de
detalhe, **no texto do rótulo visível** ("Lance mínimo" → célula ao lado) — sobrevive a redesign.
Múltiplos **fallbacks por campo** (seletor A → B → regex), degradando para `None` em vez de
quebrar. **Nunca** selecione por classe hasheada (`css-1a2b3c`). Encoding de site de governo às
vezes é latin-1 → force `r.encoding = "latin-1"`. Tabela limpa → `pandas.read_html`.

### Tier 5 — Navegador headless + JS
**Quando realmente precisa:** anti-bot que só libera cookie após rodar JS (transiente — cunhe a
sessão uma vez e volte ao httpx); token/assinatura por requisição gerado por JS ofuscado; dado
montado no client (`<canvas>`/WASM). Custo: 50–200 MB RAM/instância, lento, mais detectável. Use
para render/solve **uma vez** → promova ao httpx.

### Tier 6 — Evasão pesada (último recurso)
Escale o mínimo, nesta ordem:
1. **TLS/HTTP fingerprint** — maior alavanca, mais leve. `curl_cffi` com `impersonate="chrome120"`
   casa o fingerprint sem subir browser.
2. **Proxies** — quando o bloqueio é por IP/volume/geo. Use **residencial geolocalizado no
   Brasil**; com `cf_clearance`, mantenha o **mesmo IP** pela sessão (sticky).
3. **Stealth de navegador** — `camoufox` (Firefox endurecido, melhor p/ Python) ou `patchright`
   (Chrome). `playwright-stealth` não basta contra anti-bot sério. **Coerência de TLS:** se
   cunhou no camoufox (Firefox), impersone **firefox** no httpx seguinte.
4. **CAPTCHA** — o último do último. Melhor é **não disparar** (TLS bom + IP residencial + ritmo
   lento). Resolver CAPTCHA de rotina é o sinal mais claro de que você opera contra a vontade do
   site (máxima exposição de ToS/legal).

---

# PARTE V — Autenticação (login/senha)

A área restrita tem os dados mais valiosos (histórico de lances, avaliação, documentos).

- **Login por formulário + sessão HTTP** (POST simples): pegue o CSRF da página de login,
  poste credenciais com `requests.Session()`; os cookies de auth ficam na sessão.
- **Reaproveitar cookies/token** (driblar CAPTCHA/2FA): logue manualmente no navegador e copie
  o cookie de sessão / `Authorization: Bearer ...`. Atenção à validade.
- **Persistência com Playwright (recomendado):** logue **uma vez** com `headless=False`
  (resolve CAPTCHA/2FA manual), salve `context.storage_state(path="auth.json")` e reutilize com
  `new_context(storage_state="auth.json")` nas execuções seguintes.
- **2FA/MFA:** TOTP via `pyotp.TOTP(seed).now()` se você tem o seed; SMS/e-mail exige
  `headless=False` manual; marque "lembrar-me" para cookies de longa duração.

> **Login só com credenciais próprias** que você cadastrou. Burlar autenticação de sistemas que
> você não tem direito de acessar pode configurar violação de ToS/lei.

---

# PARTE VI — Casos especiais

### VI.1 Tempo real (lances ao vivo)
Valores chegam por **WebSocket** (filtre **WS** na Network) ou **polling**/**SSE**. Conecte ao
WS replicando headers/cookies da sessão:
```python
import websocket, json
ws = websocket.WebSocketApp("wss://leilao.com/socket",
        on_message=lambda ws, m: print("Novo lance:", json.loads(m)),
        header=["Cookie: session_id=..."])
ws.run_forever()
```

### VI.2 Cloudflare Managed Challenge / Turnstile bloqueando paginação
Cenário real: primeiras páginas acessíveis, mas `?pag=2+` dispara o desafio. O que libera é o
cookie **`cf_clearance`**, emitido só após resolução por navegador real — e que vale para
**todas** as páginas até expirar. **Amarrado ao User-Agent exato e ao IP** que o geraram.

**Estratégia (recomendada):** resolver o desafio **uma vez** no Playwright visível, salvar
`storage_state` (inclui `cf_clearance`) e **permanecer no mesmo navegador** para paginar — sem
descasamento de fingerprint.
```python
def ensure_session():               # gera cf_auth.json (resolução manual única)
    with sync_playwright() as p:
        ctx = p.chromium.launch(headless=False).new_context()
        page = ctx.new_page(); page.goto(f"{BASE}/leiloes")
        input("Resolva o desafio Cloudflare e tecle ENTER...")
        ctx.storage_state(path="cf_auth.json")
# depois: new_context(storage_state="cf_auth.json"); detecte "challenge" in page.url
# ou status 403/503 = sessão caiu → reabrir ensure_session() e retomar.
```
**Antes disso:** com o desafio resolvido, cheque DevTools → Network se a paginação dispara uma
**API JSON** (`/api/imoveis?page=2`) — pagine por ela com o mesmo cookie (mais leve/confiável).
**Alternativas se só `?pag=` é bloqueado:** scroll infinito (aciona API), fatiar por
estado/cidade/categoria, ordenações diferentes. **`cf_clearance` expira** (~30 min a horas) →
implemente **detecção de sessão caída** para renovar.

### VI.3 Anexos / documentos (edital, matrícula, laudo)
Siga os links `a[href$=.pdf]`, baixe e guarde caminho/URL. Extraia texto com `pdfplumber` (ou
OCR `pytesseract` em PDFs escaneados). Dados de **lance mínimo, ônus e débitos** costumam estar
**só no edital** — fazem parte da captura "completa".

---

# PARTE VII — Schema unificado de campos

Normalize tudo para o schema do projeto. **Saída dupla:** linha por imóvel em CSV (header de
`ofertas_detalhadas.csv`) em `/csv` **e** upsert na tabela `imoveis` de `imoveis_leiloeiros.db`
(`id, leiloeiro, junta, site, titulo, descricao, endereco, cidade, uf, lance_inicial,
avaliacao, data_leilao, url, tipo, imagem, importado_em`).

| Campo | Origem típica | Regras de normalização |
|---|---|---|
| `id` | hash estável de `url` (ou id do lote) | determinístico: mesmo lote → mesmo id |
| `fonte`/`leiloeiro`/`junta` | contexto da execução | preencher **sempre** |
| `site` / `url` | URL do lote | absoluta e canônica |
| `titulo` | `h1`/título do lote | trim, sem espaços duplicados |
| `descricao` | bloco completo de descrição | **texto integral**, preservar quebras úteis |
| `endereco` / `bairro` | descrição/ficha | regex quando não houver campo dedicado |
| `cidade` / `uf` (`estado`) | ficha/endereço | **validar contra `_ibge_municipios.json`** |
| `lance_inicial`/`preco` | valor 1ª praça | `R$ 185.000,00`→`185000.0` (`.` milhar, `,` decimal) |
| `avaliacao` | valor de avaliação | float; calcular `desconto_pct` quando ambos existirem |
| `data_leilao` / `_1` / `_2` | datas das praças | `12/03/2026`→`2026-03-12` (ISO); ver filtro VIII |
| `tipo` / `tipo_imovel` / `tipo_leilao` | ficha/classificação | apartamento/casa/terreno…; judicial/extrajudicial |
| `area_m2`, `quartos`, `banheiros`, `vagas` | ficha | numérico; extrair da descrição se não estruturado |
| `imagem` / `imagem_url` | galeria | ver regra de fotos abaixo |
| `anexos` | edital/matrícula/laudo | baixar + guardar caminho/URL |
| `importado_em` | timestamp da captura | ISO datetime |
| *(cru)* | resposta JSON/HTML original | **guardar** para reprocessar campos futuros |

### Regras específicas para reduzir perda
- **Fotos:** capture **todas** as imagens da galeria, não só a 1ª. Resolva URLs relativas →
  absolutas, prefira **maior resolução** (atenção a `data-src`, `data-lazy`, `srcset`,
  thumbnails). Principal em `imagem`; lista completa em campo/JSON auxiliar. Ignore
  ícones/logos/placeholders.
- **Descrição:** o **bloco inteiro** (não o resumo do card). Se a listagem for pobre, **entre na
  página de detalhe** de cada lote para enriquecer.
- **Localização:** sem campo de cidade/UF, parseie endereço/descrição e **valide o município**
  contra `_ibge_municipios.json`. **Nunca** grave UF inválida.
- **Lazy-load / paginação infinita:** role / siga "próxima" / chame a API paginada até esgotar;
  **confirme que o nº coletado bate com o total exibido** no site.
- **Encoding:** force UTF-8 na leitura e na escrita (`sys.stdout.reconfigure(encoding="utf-8")`).

---

# PARTE VIII — Filtro de inserção por data da 1ª arrematação

**Um imóvel só entra se a data da 1ª praça/arrematação for igual ou posterior ao dia da
inserção.** Datas passadas = leilões vencidos → descartar. Aplicado **após extração, antes do
armazenamento**.
```python
from datetime import datetime, date
def deve_inserir(lote, hoje=None):
    hoje = hoje or date.today()
    d = lote.data_primeira_arrematacao
    d = d.date() if isinstance(d, datetime) else d
    return d >= hoje                       # hoje/futuro = inclui; passado = descarta
```
- Com duas praças, use a **1ª praça** como referência. Compare em **fuso de Brasília** para não
  descartar um "hoje" por timezone.
- **Sem data extraível:** não inserir + registrar para revisão (não assumir válido).
- **Reavaliar a cada coleta:** um item válido hoje pode virar passado amanhã.

---

# PARTE IX — Qualidade, armazenamento e operação

### IX.1 Validação e tipagem (falhe alto)
Valide cada lote com `pydantic`; schema inválido **dispara alerta** — é assim que você descobre
que o site mudou, em vez de gravar `null`. **Validação por linha** antes de persistir: descarte/
sinalize registros sem `titulo` **e** sem `url`; logue incompletos em vez de salvar lixo.

### IX.2 Robustez operacional
- **Retry com backoff exponencial** (2s, 4s, 8s, 16s) em falha de rede/timeout.
- **Timeouts explícitos**; no Playwright prefira `wait_for_selector` a `networkidle`.
- **`site_health()` antes de renderizar** — não gaste Playwright em site offline/nginx
  default/"em construção".
- **Detecção de mudança de layout:** se um seletor essencial sumir em N lotes seguidos, **pare e
  reporte** — não grave linhas vazias em massa.

### IX.3 Armazenamento, dedup, incremental
- **Upsert por `id`** (sem duplicatas); mantenha snapshots com timestamp para histórico de lances.
- **Resolução de entidade:** o mesmo imóvel em vários leiloeiros → case por **matrícula** +
  fuzzy match de endereço. É o que transforma "muitos dados" em "o maior banco sem duplicatas".
- **Incremental:** não re-raspar tudo todo dia — detecte o que mudou (`lastmod` do sitemap, hash,
  diff de IDs). O valor do banco é o frescor.
- **Modularidade por leiloeiro** atrás de interface comum: consertar uma fonte nunca derruba as
  outras.

### IX.4 Métricas e relatórios
- **Por fonte:** taxa de sucesso, frescor, contagem de itens, **% de campos `None`** (subiu →
  sinal de redesign).
- **Progresso a cada 5 min:** nº de imóveis por leiloeiro, total acumulado, % preenchido por
  coluna, erros por tipo.
- **Relatório final (markdown):** cobertura por campo, principais dificuldades, URLs que
  falharam e **sugestões de correção**.

### IX.5 Camada responsável (não-negociável)
- **Educação é técnica:** rate limit + jitter, concorrência limitada por domínio, respeitar
  `429`/`Retry-After`, cachear o que não mudou, crawlear fora de pico.
- **robots.txt / ToS:** raspar dado público é geralmente defensável no Brasil; violar ToS e
  burlar medidas técnicas aumenta o risco. *(Não é parecer jurídico.)*
- **LGPD:** editais carregam dado pessoal do executado (nome, às vezes CPF). Dado público não
  sai da alçada da LGPD — decida cedo o que guarda/anonimiza/descarta e a base legal.
- **Relacionamento:** proteção forte é sinal de que o operador não quer coleta em massa. Antes de
  investir em evasão, procure **rota oficial** (API de parceiro, export, contato comercial) — mais
  estável, mais barato e sem atrito legal.

### IX.6 Stack recomendada
| Necessidade | Ferramenta |
|---|---|
| HTTP | `requests`, `httpx` |
| Parse HTML | `selectolax`, `BeautifulSoup`+`lxml` (XPath) |
| Navegador | `Playwright` (preferido), `Selenium` |
| Anti-bloqueio TLS | `curl_cffi` |
| Stealth | `camoufox`, `patchright` |
| JSON em globais JS | `chompjs` |
| WebSocket | `websocket-client`, `websockets` |
| 2FA | `pyotp` |
| PDF | `pdfplumber`, `pytesseract` (OCR) |
| Validação | `pydantic` |
| Escala / agendamento | `Scrapy`, `APScheduler`/`cron`/Airflow |

---

# PARTE X — Prompt operacional (cole para executar)

> Substitua os `{{...}}`. Este prompt assume as Partes I–IX como contexto de arquitetura.

## Tarefa
Faça o scraping de **`{{URL_OU_LISTA_DE_URLS}}`** e extraia, para **cada lote/imóvel**, o schema
completo da **Parte VII**. Objetivo: **cobertura máxima** (nenhum campo disponível na página
fica vazio) e **mínima perda/erro** (toda falha registrada, nunca silenciada). Filtre por data
da 1ª arrematação (**Parte VIII**) usando hoje = `{{DATA_HOJE}}`.

## Como você (Claude) deve trabalhar
1. **Sonde primeiro (Parte III).** Abra a URL, identifique a plataforma (Superbid, Sodré
   Santoro, Mega Leilões, white-label…) e procure a **API JSON interna** antes de escrever
   qualquer parser. Escolha o tier pela árvore de decisão (III.4).
2. **Reaproveite o repositório:** `scraper_commons.py` e os adapters por plataforma já
   existentes; crie um novo extrator por plataforma só quando necessário.
3. **Prototipe em 1 lote**, confirme que **todos** os campos vêm corretos e **mostre-me a
   primeira linha extraída** antes de rodar em escala.
4. **Rode em escala** com retry/backoff, rate limit, progresso retomável e upsert.
5. Ao terminar, traga a **relação de imóveis por leiloeiro** e o **relatório de cobertura/erros**.

## Saídas
1. **CSV** com o header de `ofertas_detalhadas.csv` em `/csv`.
2. **Upsert** na tabela `imoveis` de `imoveis_leiloeiros.db`.
3. **Progresso a cada 5 min** (Parte IX.4).
4. **Relatório final** (markdown) ao fim deste arquivo: cobertura por campo, dificuldades, URLs
   que falharam e sugestões de correção.

## Critério de pronto (Definition of Done)
- [ ] Todo lote visível na origem foi coletado (contagem confere com o total do site).
- [ ] `titulo`, `descricao`, `cidade/uf`, ≥1 `imagem` e ≥1 `data` preenchidos sempre que
      existirem na página; vazios só quando comprovadamente ausentes (e logados).
- [ ] Todas as fotos da galeria capturadas (não só a 1ª), em maior resolução.
- [ ] Anexos (edital/matrícula/laudo) baixados/linkados; lance mínimo do edital extraído.
- [ ] Nenhuma 1ª praça com data anterior a `{{DATA_HOJE}}` gravada como ativa.
- [ ] Zero duplicatas no destino (upsert por `id`); JSON/HTML cru guardado.
- [ ] Logs e `*_progress.json` permitem retomar sem reprocessar.
- [ ] Relatório final com cobertura por campo + dificuldades + correções entregue.

---

## Referência rápida — cheat-sheet de escalada
```
API/JSON (Tier 1) → intercepta XHR (2) → JSON embutido (3) → markup (4)
   → (transiente) browser (5) → TLS curl_cffi → proxy residencial BR sticky
   → stealth camoufox/patchright → CAPTCHA (6)
```
**A cada degrau, desacelere antes de escalar.** Pergunte sempre: *"estou apanhando porque estou
rápido demais?"* — educação resolve a maioria dos bloqueios antes que apareçam.

---

# PARTE XI — Ferramentas de apoio deste repositório

Implementam, na prática, as regras das partes anteriores.

| Ferramenta | Para quê | Uso |
|---|---|---|
| `plataformas.json` | Mapa plataforma → tier/adapter (Parte II/III.4). Pula o reconhecimento em fontes já conhecidas. | consultar `deteccao` por domínio/marcador HTML; achou → use `adapter`/`tier` |
| `migrar_imagens_anexos.py` | Cria as tabelas 1→N `imovel_imagens` e `imovel_anexos` (Parte VII: "todas as fotos" + anexos). Faz backfill de `imoveis.imagem`. | `python migrar_imagens_anexos.py` |
| `check_cobertura.py` | Gate de qualidade (Parte X DoD): % preenchido por campo; **exit ≠ 0** se abaixo do limite. **`--gate-leiloeiro`** também falha se UM leiloeiro despenca ≥`margem`pp abaixo da média num campo crítico (`n≥min-volume`) — pega redesign/extrator quebrado antes de afetar a média. | `python check_cobertura.py --por-leiloeiro` · `--gate-leiloeiro [--margem 40] [--min-volume 20]` · `--json` |
| `gerar_dashboard_frescor.py` | Dashboard HTML de cobertura por campo + frescor por data + tabela por leiloeiro (Parte IX.4). | `python gerar_dashboard_frescor.py` → `dashboard_frescor.html` |
| `snapshot_cobertura.py` | Histórico (`cobertura_historico.jsonl`) + **detecção de regressão** (queda de cobertura por leiloeiro = sinal de redesign). | `python snapshot_cobertura.py [--limite-queda 15] [--strict]` |
| `finalizar_coleta.py` | **Orquestra a pós-coleta:** snapshot+regressão → gate `check_cobertura` → regenera dashboard. **Exit ≠ 0 trava o commit do banco.** | `python finalizar_coleta.py --desde hoje` |
| `enrich_local.py` | Enriquecimento **offline** (sem rede): deduz `uf` via IBGE (`inferir_uf`) e `lance_inicial` do texto. **`--auditar`** revisa UFs já preenchidas vs. inferência, separando ALTA confiança (campo `cidade` contradiz a UF salva — `--corrigir` aplica só essas) de baixa (lotes ambíguos, só reporta). | `python enrich_local.py` · `--auditar [--corrigir]` |
| `scripts/run-quality.ps1` + `setup-scheduled-quality.ps1` | Agenda o gate **1×/dia** (Task Scheduler), alimentando o histórico p/ a detecção de regressão. | `powershell -File scripts\setup-scheduled-quality.ps1` |
| `gerar_viewer_galeria.py` | Viewer HTML com **carrossel** de fotos por imóvel (lê `imovel_imagens` 1→N) + links de anexos; filtro por UF/texto. | `python gerar_viewer_galeria.py` → `viewer_galeria.html` |
| `.claude/settings.json` + `scripts/session-setup.sh` | **SessionStart hook** (inclui Claude na web): instala deps + Playwright/Chromium e roda o smoke test. | automático ao abrir a sessão |

**Limpeza de cidade na ORIGEM:** `importar_site.py` (`cidade_limpa`) e `staging_anuncios.py`
(`_cidade_limpa_staging`) já normalizam a cidade na importação via `sc.extrair_municipio` (nome
canônico do IBGE quando o valor é bairro+cidade), evitando gravar ruído de origem — não só no
enrich a posteriori.

**Plug no `scraper_detalhe.py`:** o `_extract` já chama `extrair_galeria`/`extrair_anexos` e
infere `uf`; ao fim, `persistir_midia()` casa cada lote (por `url`) com `imoveis.id` e grava nas
tabelas 1→N. **`--reprocessar-sem-foto`** monta a lista de trabalho direto dos imóveis sem foto
(nem `imoveis.imagem` nem `imovel_imagens`) e revisita só esses — ataque cirúrgico à lacuna de
imagem. Piloto sugerido: `python scraper_detalhe.py --reprocessar-sem-foto --limite 20` (exige
navegador real; ver nota abaixo). **Dashboard:** painel de **tendência** (sparkline por campo) e de
**regressões** entre os dois últimos snapshots.

**Inferência de UF (`scraper_commons.inferir_uf`) — alta precisão** (melhor vazio que errado):
sinais aceitos, em ordem — (0) campo que é exatamente um município de UF única; (1) `[UF]` entre
colchetes ou `Cidade-UF`/`Cidade/UF`; (2) município após preposição de lugar (`em/de/no/na…`),
escolhendo o nome **mais longo** (`no Rio de Janeiro` vence `em Ipanema`). Não deduz de siglas
soltas (`AP`/`SP`) para não colidir com "apartamento" etc. Resultado no banco: `uf` 85,6% → 91,1%.

**Auditoria de `cidade` (`enrich_local.py --auditar-cidade`):** confere o campo contra o IBGE —
(a) cidades **inexistentes** (typo/ruído, ex.: "DPO LOGIN CADASTRE" ou "bairro+município"
concatenados como "BELA VISTA SÃO PAULO") e (b) cidade existente mas **fora da UF salva**. Aponta
volume e valores distintos para limpeza.

**Limpeza de cidade (`enrich_local.py --limpar-cidade [--remover-ruido]`):** extrai o município
real de valores inválidos (bairro+município concatenado) via `sc.extrair_municipio` (nome canônico
do IBGE, UF como desambiguador). **Guarda de consistência:** se a UF salva não contém o município
extraído (homônimo/grafia, ex.: "Mirassol" vs "Mirassol d'Oeste"), pula em vez de corromper.
`--remover-ruido` esvazia cidades sem município reconhecível (navegação: "LOGIN CADASTRE" etc.).
Resultado: `cidade` válida no IBGE 37% → **85,1%** das preenchidas. `--uf-de-cidade` preenche UF
vazia a partir da cidade canônica (idempotente; hoje 0 — o pipeline já captura via limpeza+enrich).

**Revisão de UF (`--auditar --csv csv/uf_revisao.csv` → `aplicar_revisao.py`):** exporta as
divergências com uma coluna `decisao` **auto-triada**: pré-marca `aplicar` quando a UF é confirmada
por **sinal forte inequívoco** (`sc.inferir_uf_forte` — `[UF]`/`Cidade-UF`, e *None* se houver
múltiplas UFs fortes = lote multi-localização); deixa em branco só as ambíguas (349 vs 190). Você
revisa, ajusta (`aplicar`/`manter`/`<UF>`) e o `aplicar_revisao.py` valida contra o IBGE e grava.

**FlareSolverr de fato (`docker-compose.flaresolverr.yml` + `probe_flaresolverr.py`):** sobe o
serviço (`docker compose -f docker-compose.flaresolverr.yml up -d`) e valida (`python
probe_flaresolverr.py`) antes de rodar o scraper — o fallback do `visit()` passa a destravar
Mega/Central Sul mesmo via headless.

**Resiliência de navegador (env vars, lidas por `scraper_detalhe.py`):** `PW_CHROMIUM_PATH` aponta
um Chromium já presente quando o download oficial falha (CDN bloqueado); `PW_IGNORE_HTTPS=1` ignora
cert inválido de proxy TLS. O `session-setup.sh` detecta e exporta esses valores automaticamente
(fallback para `/opt/pw-browsers/...` ou Chrome do sistema).

**Fallback FlareSolverr (fontes que bloqueiam headless):** `sc.fetch_flaresolverr(url)` +
`sc.parece_bloqueio(html, status)`. No `visit()` do `scraper_detalhe.py`, quando o headless recebe
403/challenge (Mega Leilões, Central Sul…), tenta resolver via FlareSolverr (`FLARESOLVERR_URL`,
default `http://localhost:8191/v1`) antes de desistir. Gracioso: sem o serviço, retorna `None` e o
fluxo segue. Suba o FlareSolverr (Docker) para destravar essas fontes mesmo via headless.

> **Nota sobre o piloto de `scraper_detalhe.py`:** recuperar `lance_inicial` (64,3%) e popular as
> galerias 1→N **em volume** exige rodar com **navegador real**. A fiação está plugada e validada
> ponta a ponta (`--reprocessar-sem-foto --limite N` lança o browser, visita e persiste sem crashar),
> mas os leiloeiros **bloqueiam acesso headless** (HTTP 403) a partir deste sandbox. Rode na máquina
> onde os scrapers já passam pelo anti-bot — ou via FlareSolverr/stealth (Parte VI.2).

**Helpers em `scraper_commons.py`** (use nos extratores): `detectar_plataforma(site, html,
leiloeiro)` (roteia via `plataformas.json`); `extrair_galeria(html, base_url)` (todas as fotos,
maior resolução, sem logos); `extrair_anexos(html, base_url)` (PDFs tipados); `salvar_galeria()`
e `salvar_anexos()` (upsert nas tabelas 1→N).

**Modelo de dados de fotos/anexos:** grave a foto de capa também em `imoveis.imagem` (compat.) e
**todas** as imagens em `imovel_imagens` (`ordem`, `principal`, `largura`/`altura` para maior
resolução). PDFs em `imovel_anexos` (`tipo` = edital/matricula/laudo, `url`, `caminho_local`).

**No fim de cada coleta** (encadeado em `run_scraper.py --finalize` ou `python
finalizar_coleta.py --desde hoje`): grava snapshot e alerta regressões → roda o gate de
cobertura (trava se cair abaixo do limite) → regenera o `dashboard_frescor.html`. `% None`
subindo num leiloeiro = sinal de redesign — investigue o extrator antes que vire buraco no banco.
