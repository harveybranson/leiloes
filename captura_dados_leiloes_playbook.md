# Playbook de Captura de Dados — Agregador de Leilões

Guia operacional do pipeline de scraping. A ideia central é uma **escada de robustez**:
sempre use o método mais estável e barato que funciona, e só desça para os mais frágeis
quando for bloqueado de fato. "Scraper perfeito" não é o que nunca quebra; é o sistema
modular que quebra de forma previsível, avisa na hora e é barato de consertar.

> **Regra de ouro:** fique o mais alto possível na escada. Só desça um degrau quando o
> anterior continuar bloqueado. E antes de cada degrau pra baixo, pergunte *"estou
> apanhando porque estou rápido demais?"* — a maioria dos bloqueios é frequência, não
> fingerprint. Educação (rate limit + cache + respeitar `429`) evita o bloqueio antes dele.

---

## 1. A escada de robustez

| Tier | O que é | Ferramenta | Característica |
|------|---------|-----------|----------------|
| 1 | API oficial / endpoint JSON | `json_api_harvester.py` | mais **estável, rápido, barato** |
| 2 | Intercepção de API (XHR/Fetch) via Playwright | `pw_intercept.py` | |
| 3 | JSON embutido no HTML (`__NEXT_DATA__`, JSON-LD, globais) | `html_json_extractor.py` | |
| 4 | Parse de HTML renderizado (CSS selectors) | `html_parser.py` | |
| 5 | Navegador headless + execução de JS | `pw_intercept.py` / `camoufox_stealth.py` | |
| 6 | Evasão pesada (TLS, proxies, stealth, CAPTCHA) | `fetch_evasive.py` + `camoufox_stealth.py` | mais **frágil, lento, caro** |

Quanto mais alto, mais perto do JSON que alimenta a página → menos depende do HTML → quebra
menos. A maioria dos leiloeiros (WordPress, Next/Nuxt, server-rendered) resolve em **tiers 1–4**.
Tiers 5–6 são bisturi para um punhado de fontes com anti-bot agressivo.

---

## 2. Setup (uma vez)

```bash
pip install httpx selectolax chompjs curl_cffi playwright "camoufox[geoip]"
playwright install chromium        # p/ pw_intercept.py
python -m camoufox fetch           # baixa o Firefox patcheado p/ camoufox_stealth.py
# no VPS headless, p/ o modo stealth --virtual:
sudo apt-get install -y xvfb
```

Mapa de arquivos: cada ferramenta é independente e compartilha o **mesmo formato de
sessão** (`sessao_httpx.json`), então elas compõem sem reescrever nada.

---

## 3. Onboarding de uma nova fonte (o fluxo)

Para cada leiloeiro novo, siga esta ordem:

### 3.1 Reconhecimento (antes de escrever qualquer scraper)
- **DevTools → aba Network → filtro Fetch/XHR.** Dispare a ação que carrega dados (buscar,
  paginar, scroll, abrir um lote) e veja as respostas JSON. Identifique o endpoint de
  **listagem** e o de **detalhe**.
- **`view-source`** atrás de JSON embutido: `__NEXT_DATA__`, `window.__NUXT__`,
  `window.__INITIAL_STATE__`, `<script type="application/ld+json">`.
- **`sitemap.xml` + `robots.txt`** — estrutura de URLs e modelo de paginação de graça.
- Identifique: **framework** (Next/Nuxt/WordPress/server-rendered), **anti-bot**
  (Cloudflare/DataDome/Akamai) e **paginação** (página numérica / offset / cursor / scroll).

### 3.2 Escolha o tier (árvore de decisão)
1. O dado aparece em alguma resposta **XHR/Fetch**? → **Tier 1/2** (intercepte e replique).
2. Está no HTML como **JSON embutido**? → **Tier 3**.
3. Está no **markup renderizado** da resposta inicial? → **Tier 4** (httpx + parse).
4. Só materializa **depois do JS rodar**, OU você não consegue resposta válida sem executar
   JS (anti-bot/assinatura)? → **Tier 5**, e se ainda bloquear → **Tier 6**.

### 3.3 Rode a ferramenta do tier (seções 4–9).
### 3.4 Normalize + valide + dedupe (seção 11).
### 3.5 Agende incremental + monitore (seção 12).

---

## 4. Tier 1 — API / JSON na fonte → `json_api_harvester.py`

**Quando:** existe um endpoint JSON (oficial ou interno) que você consegue reproduzir com
HTTP puro. É o topo: 10–50x mais rápido que navegador, paralelizável, estável.

**Como descobrir:** no DevTools, botão direito na requisição → **Copy as cURL** → converta
com `curlconverter`. Varie página/filtros pra mapear o contrato (paginação, params
obrigatórios, total).

```bash
# 1) confirme o caminho dos itens
python json_api_harvester.py --probe
# 2) colha tudo -> SQLite (grava o JSON cru, com upsert por id)
python json_api_harvester.py --db leiloes.db --delay 1.5
```
Configure o `Endpoint` no topo do arquivo (URL, params, headers do DevTools, modo de
paginação `page`/`offset`/`cursor`, `items_path`, `id_field`).

**Atalho Next.js:** se o site é Next, leia o `buildId` (ver Tier 3) e bata em
`/_next/data/<buildId>/<rota>.json` — vira uma API de fato.

**Gotchas:** guarde o **JSON cru** (não só os campos de hoje); `403` súbito = sessão/cookie
expirado (renove via Tier 2/5); sem `total`, para quando a página vier vazia; endpoint atrás
de anti-bot → troque `httpx` por `curl_cffi` (Tier 6).

---

## 5. Tier 2 — Intercepção XHR/Fetch (Playwright) → `pw_intercept.py`

**Quando:** o endpoint existe mas só aparece quando o JS roda, ou precisa de uma sessão
(token/cookie) que o navegador estabelece. A intercepção é também o **instrumento de
descoberta** pra promover ao Tier 1.

Primitivos: `page.on("response")` (colher passivamente), `page.on("request")` (aprender a
assinatura), `page.route()` (bloquear recurso pesado / modificar), `context.request` (chamar
a API herdando a sessão do browser, **sem renderizar**).

```bash
# descobre os endpoints de API que a página chama
python pw_intercept.py discover --url https://site/imoveis --match /api/ --scroll 4
# coleta o JSON das respostas que casam o filtro -> JSONL
python pw_intercept.py harvest  --url https://site/imoveis --match /api/imoveis --items-path data --out lotes.jsonl
# resolve Cloudflare (visível) e EXPORTA a sessão p/ httpx
python pw_intercept.py --headed session --url https://site --state state.json --export sessao_httpx.json
# chama a API via context.request (herda a sessão, sem renderizar)
python pw_intercept.py replay --api-url https://site/api/v1/imoveis --state state.json --pages 5
```

**Padrão de produção:** abra a página **uma vez** pra cunhar a sessão (`session --export`),
depois faça o loop no **Tier 1** (`json_api_harvester` com os cookies exportados) ou no
`replay`. Não crawleie milhares de páginas dentro do browser.

**Gotchas:** `response.json()` quebra em redirect/304/cache/não-JSON (filtre por status 200 +
`content-type` e use try/except); evite `wait_until="networkidle"` (instável); `context.request`
herda a sessão mas **não executa JS** — se a API exige token gerado em runtime, capture-o no
`discover` e repasse no header.

---

## 6. Tier 3 — JSON embutido no HTML → `html_json_extractor.py`

**Quando:** frameworks que renderizam no servidor já deixam o JSON dentro de uma `<script>`.
Mais robusto que navegador (não executa JS), abaixo só da API porque o blob é por-página.

- **`__NEXT_DATA__`** (Next.js) — JSON puro; o ouro está em `props.pageProps`. Guarda o
  `buildId` → use `--next-data-url` pra montar o atalho de API do Tier 1.
- **JSON-LD** (`application/ld+json`) — o mais padronizado (schema.org). Em WordPress
  (Yoast/RankMath) há `@graph` com `Organization`/`Product`/`Offer`/`RealEstateListing`.
- **Globais JS** (`__NUXT__`, `__INITIAL_STATE__`, `__APOLLO_STATE__`) — atribuição JS, não
  JSON; parseada com `chompjs` (aguenta aspas simples / vírgula sobrando).

```bash
python html_json_extractor.py --url https://site/imoveis --probe              # o que existe?
python html_json_extractor.py --url https://site/imoveis --source next --out lotes.jsonl
python html_json_extractor.py --url https://site/lote/123 --source jsonld --type Product
python html_json_extractor.py --url https://site/imoveis --next-data-url       # imprime o atalho de API
```

**Gotchas:** o blob é por-página → ainda enumere as páginas (ou use `/_next/data` com
`?page=`); JSON escapado em atributo (Astro `<astro-island props="...">`, `data-*`) precisa
de unescape; confira completude — alguns sites renderizam só um subconjunto e completam via API.

---

## 7. Tier 4 — Parse de HTML renderizado → `html_parser.py`

**Quando:** o dado só existe como markup — sites server-rendered antigos (PHP/WordPress sem
JSON-LD, ASP.NET, Rails/Django sem API) e **portais jurídicos/editais** (tribunais,
prefeituras, Receita). É a camada mais frágil: o contrato é o DOM, cheio de classe hasheada.

A fonte do HTML é indiferente — `selectolax` parseia uma string: `httpx.get(url).text`
(server-rendered) **ou** `page.content()` do camoufox/pw_intercept (SPA). Mesmo código.

- **selectolax** (lexbor, em C): rápido, CSS selectors — o default pra volume.
- **BeautifulSoup** (com `lxml`): API ergonômica, navegação rica — para HTML legado bagunçado.
- **lxml + XPath**: `//th[contains(.,'Lance')]/following-sibling::td` — selectolax não tem XPath.

```bash
python html_parser.py list   --file listagem.html --normalize         # grade de cards
python html_parser.py list   --url https://site --table "table.resultados" --out lotes.jsonl
python html_parser.py detail --file lote.html --normalize             # detalhe por rótulo
```

**Regra de robustez:** ancore em sinal estável (`data-*`, `id`, `itemprop`) e, na página de
detalhe, **no texto do rótulo visível** ("Lance mínimo" → célula do lado) — sobrevive a
redesign. Múltiplos fallbacks por campo (seletor A → B → regex), degradando pra `None` em vez
de quebrar.

**Gotchas:** **nunca** selecione por classe hasheada (`css-1a2b3c`); encoding de site de
governo às vezes é latin-1 → force `r.encoding = "latin-1"`; tabela limpa → `pandas.read_html`
resolve na hora.

---

## 8. Tier 5 — Navegador headless + JS

**Quando você REALMENTE precisa** (definição estreita): o dado não existe em nenhuma resposta
que você consiga reproduzir, **ou** você não consegue uma resposta válida sem executar JS.
Casos reais:
- Anti-bot que só libera o cookie depois de executar JS (Cloudflare/DataDome) — **transiente**:
  cunhe a sessão uma vez e volte pro httpx.
- Token/assinatura por requisição gerado por JS ofuscado que você não consegue reproduzir.
- Dado montado/descriptografado no client, em `<canvas>` ou WASM — não está em resposta nenhuma.

**Falsos gatilhos** (não precisa de browser): "é SPA React/Next" (o dado está no XHR ou
`__NEXT_DATA__`); "carrega no scroll" (XHR paginável); "tem Cloudflare" (header + TLS +
cookie cunhado uma vez); "precisa logar" (POST que devolve token); "view-source vazio" (shell
ainda manda dado por XHR/blob).

**Custo:** ~50–200 MB de RAM por instância, lento, difícil de paralelizar, mais detectável.
Use pro mínimo: render/solve **uma vez** → promova pro httpx. Ferramentas: `pw_intercept.py`
(Chrome, descoberta + sessão) e `camoufox_stealth.py` (stealth, ver Tier 6).

---

## 9. Tier 6 — Evasão pesada → `fetch_evasive.py` + `camoufox_stealth.py`

Último recurso. Três verdades: é arms-race (manutenção eterna); a evasão mais eficaz é
educação (desacelerar); tem peso de LGPD (editais têm dado pessoal). **Escale o mínimo**, nesta
ordem:

### 9.1 Fingerprint TLS/HTTP — o mais leve, maior alavanca → `fetch_evasive.py`
Anti-bot lê o ClientHello (JA3/JA4) e o HTTP/2; `httpx` puro tem assinatura de não-navegador.
`curl_cffi` com `impersonate` casa o fingerprint de um navegador real **sem subir browser**.

```bash
python fetch_evasive.py --url https://site/api/v1/imoveis --impersonate chrome \
       --session sessao_httpx.json --proxy http://user:pass@host:porta
```
Defaults responsáveis embutidos: jitter, backoff que respeita `Retry-After`, IP fixo por sessão.

### 9.2 Proxies — quando o bloqueio é por IP/volume/geo
Hierarquia por custo/furtividade: **datacenter** (barato, fácil de barrar) → **ISP/residencial
estático** → **residencial rotativo** → **móvel 4G/5G** (mais difícil, mais caro). Para
leiloeiro use IP **residencial geolocalizado no Brasil**. Com `cf_clearance`, mantenha o
**mesmo IP** pela sessão (sticky) — rotacionar quebra o cookie. Escolha provedor reputável.

### 9.3 Stealth de navegador — só no Tier 5 e se o headless for detectado → `camoufox_stealth.py`
O `playwright-stealth` morreu contra anti-bot sério (não resolve o vazamento de **CDP** /
`Runtime.enable`, e o próprio remendo é detectável). Opções de hoje:
- **camoufox** — Firefox endurecido, fingerprint spoofado no C++ (não observável do JS),
  geo-match com o IP, escapa da detecção de CDP do Chrome. **Melhor escolha pra Python.**
- **patchright** — drop-in pro `playwright` em Python (`pip install patchright`), corrige o
  vazamento de CDP no Chromium. Use quando precisar de **Chrome**.

```bash
# no VPS headless: --virtual (Xvfb, Firefox real, menos detectável que headless puro)
python camoufox_stealth.py session --url https://site --proxy http://user:pass@host:porta --geoip --virtual
python camoufox_stealth.py content --url https://site/imoveis --geoip --virtual --out pagina.html
```
**Coerência de TLS ao promover:** se cunhou a sessão no camoufox (Firefox), a chamada httpx
seguinte tem que impersonar **firefox** (`fetch_evasive.py --impersonate firefox`), não chrome
— senão o cookie casa com um fingerprint que não bate. Com patchright (Chrome), impersone chrome.

Stealth ≠ só o navegador: precisa de **fingerprint coerente com o proxy** (locale `pt-BR`,
timezone `America/Sao_Paulo`, geo batendo), **proxy residencial**, **comportamento humano**
(`humanize`), **bloqueio de WebRTC** (senão vaza o IP real).

### 9.4 CAPTCHA — o último recurso do último recurso
O melhor é **não disparar** (TLS bom + IP residencial + ritmo lento e ele nem aparece). Se
aparecer: serviços (2Captcha, CapSolver, Anti-Captcha) recebem `sitekey` + URL e devolvem um
token que você injeta; custa por solve, é lento. Resolver CAPTCHA de forma rotineira é o sinal
mais claro de que você opera contra a vontade do site — máxima exposição de ToS/legal.

> **Ponto estratégico (M&A):** se uma fonte exige CAPTCHA constante e fazenda de proxy, recue
> e faça a conta. Um acordo de dados/parceria com o leiloeiro teimoso costuma sair mais barato
> — e menos arriscado — do que arms-race contra um futuro alvo de aquisição. Às vezes a
> resposta é um telefonema, não um proxy móvel.

---

## 10. A ponte entre tiers: `sessao_httpx.json`

Todas as ferramentas compartilham este formato, então elas se encaixam:

```json
{ "headers": { "User-Agent": "...", "Accept": "application/json" },
  "cookies": { "cf_clearance": "...", "session": "..." } }
```

**Fluxo típico para uma fonte com Cloudflare:**

```
pw_intercept discover            (acha o endpoint)
        |
pw_intercept/camoufox session --export sessao_httpx.json   (resolve Cloudflare, exporta)
        |
json_api_harvester  (cola cookies/headers no Endpoint)   →  colhe em httpx puro
   ou  fetch_evasive --session sessao_httpx.json          →  httpx com fingerprint
```

Para SPA sem API limpa: `camoufox content --out pagina.html` → `html_parser` / `html_json_extractor`.

---

## 11. Normalização BR + qualidade

- **Dinheiro:** `R$ 185.000,00` → `185000.0` (`.` milhar, `,` decimal). Em `html_parser.py`:
  `brl_to_float`.
- **Data:** `12/03/2026` → `2026-03-12` (ISO). `br_date`.
- Também: `m²`, matrícula, CEP. **Guarde o JSON/HTML cru** e normalize depois — quando o
  produto pedir um campo novo, ele já está no banco.
- **Dedupe / resolução de entidade:** o mesmo imóvel listado em vários leiloeiros. Case por
  **matrícula do imóvel** + fuzzy match de endereço. É o que transforma "muitos dados" em "o
  maior banco sem duplicatas".
- Dados nos **editais em PDF** (lance mínimo, ônus, débitos) fazem parte da captura "completa".

---

## 12. Resiliência e monitoramento

- **Valide com `pydantic`** e **falhe alto.** Como HTML/JSON mudam, schema inválido deve
  disparar alerta — é assim que você descobre que o site mudou, em vez de gravar `null`.
- **Métricas por fonte:** taxa de sucesso, frescor, contagem de itens. Suba a taxa de campos
  `None` → sinal de redesign.
- **Modularidade por leiloeiro** atrás de uma interface comum — consertar uma fonte nunca
  derruba as outras.
- **Incremental:** não re-raspar tudo todo dia; detecte o que mudou (`lastmod` do sitemap,
  hash, diff de IDs novos/removidos). O valor do banco é o frescor.

---

## 13. Camada responsável (não-negociável)

- **Educação é técnica:** rate limit + jitter, concorrência limitada por domínio, respeitar
  `429`/`Retry-After`, cachear o que não mudou, crawlear fora de pico. A maioria dos bloqueios
  vem de ser barulhento.
- **`robots.txt` / ToS:** zona cinzenta. Raspar dado público é geralmente defensável no Brasil,
  mas violar ToS e burlar medidas técnicas aumenta o risco. *(Não é parecer jurídico.)*
- **LGPD:** editais carregam dado pessoal do executado (nome, às vezes CPF). Dado ser público
  não tira da alçada da LGPD. Decida cedo o que guarda, anonimiza ou descarta, e qual a base
  legal — quanto mais agressiva a evasão, mais isso pesa.
- **Relacionamento:** postura adversarial contra quem você pode querer adquirir depois é
  contraproducente.

---

## 14. Referência rápida

| Preciso de... | Ferramenta | Comando base |
|---------------|-----------|--------------|
| Endpoint JSON conhecido | `json_api_harvester.py` | `--probe` → `--db leiloes.db` |
| Achar/usar API que precisa de JS | `pw_intercept.py` | `discover` / `harvest` / `session` / `replay` |
| JSON dentro do HTML | `html_json_extractor.py` | `--probe` / `--source next\|jsonld\|global` |
| Raspar markup | `html_parser.py` | `list` / `detail` `--normalize` |
| httpx bloqueado (TLS) | `fetch_evasive.py` | `--impersonate chrome --session ...` |
| Anti-bot que pega o browser | `camoufox_stealth.py` | `session --geoip --virtual` |

**Cheat-sheet de escalada:** API/JSON → embutido → markup → (transiente) browser → TLS
(`curl_cffi`) → proxy residencial BR sticky → stealth (`camoufox`/`patchright`) → CAPTCHA.
A cada degrau, desacelere antes de escalar.

---

## 15. Status de validação (deste pacote)

- **Testados de ponta a ponta** (contra API/HTML reais ou fixtures realistas):
  `json_api_harvester.py` (crates.io), `html_json_extractor.py`, `html_parser.py` (fixtures),
  `fetch_evasive.py` (pypi.org).
- **Validados em sintaxe + superfície de API** (browser/web real indisponível no ambiente de
  build): `pw_intercept.py`, `camoufox_stealth.py`. Confirme o comportamento (interceptação,
  stealth, fingerprint via `tls.peet.ws`/`browserleaks.com/tls`) num alvo real.
- Os blocos de `Endpoint`/`LISTING`/`DETAIL_LABELS` nos arquivos são **exemplos** — adapte ao
  HTML/JSON real de cada leiloeiro.
