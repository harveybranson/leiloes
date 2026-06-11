# Prompt — Scraping de leilões com máxima cobertura e mínima perda de dados

> Cole este prompt para o Claude quando quiser capturar **todos** os detalhes de um
> site/leiloeiro (foto, descrição, localização, datas, valores, anexos) com o menor
> número possível de erros ou campos vazios. Substitua os trechos `{{...}}`.

---

## Tarefa

Faça o scraping de **`{{URL_OU_LISTA_DE_URLS}}`** e extraia, para **cada lote/imóvel**, o
conjunto completo de campos abaixo. O objetivo é **cobertura máxima** (nenhum campo
disponível na página pode ficar vazio) e **mínima perda/erro** (toda falha precisa ser
registrada, nunca silenciada). Use `captura_dados_leiloes_v2.md` e `scraper-leiloes.md`
como orientação de arquitetura e boas práticas.

## Princípios obrigatórios (nesta ordem)

1. **Caminho mais barato primeiro.** Tente nesta sequência e só escale quando o anterior
   falhar de fato: `requests`/`httpx` (HTML estático) → **API JSON interna** (DevTools →
   aba Network; muitos leiloeiros carregam os lotes via XHR já estruturado) → **Playwright**
   (SPA / JS pesado) → **FlareSolverr** (`http://localhost:8191/v1`) apenas para sites com
   Cloudflare/anti-bot, como Milan. Reaproveite `scraper_commons.py`
   (`site_health`, `cards_from_json`, `candidate_sites`, `upsert_multijunta`).
2. **Respeite o site.** Cheque `robots.txt`/Termos, aplique rate limiting (`time.sleep`
   entre requisições), `CONCURRENCY` baixo (≈5 páginas simultâneas no Playwright) e
   **não** contorne CAPTCHA ativo. A maioria dos dados de leilão é pública por lei.
3. **Nunca engula erro.** Toda exceção/timeout/seletor ausente vai para log estruturado
   com a URL e o campo que falhou. "Sem dado" e "erro ao buscar" são estados diferentes.
4. **Idempotência e retomada.** Persista progresso em `*_progress.json` e faça **upsert**
   no destino (não duplique). Permita reexecutar de onde parou (`--reset` reinicia do zero).

## Campos a capturar (schema unificado)

Normalize tudo para o schema do projeto. Saída: linha por imóvel em CSV **e** upsert na
tabela `imoveis` de `imoveis_leiloeiros.db`.

| Campo | Origem típica | Regras de normalização |
|---|---|---|
| `id` | hash estável de `url` (ou id do lote) | determinístico, mesmo lote → mesmo id |
| `fonte`/`leiloeiro`/`junta` | contexto da execução | preencher sempre |
| `site` / `url` | URL do lote | URL absoluta e canônica |
| `titulo` | `h1`/título do lote | trim, sem espaços duplicados |
| `descricao` | bloco de descrição completo | **texto integral**, preservar quebras úteis |
| `endereco`/`bairro` | descrição/ficha | extrair via regex quando não houver campo |
| `cidade` / `uf` (`estado`) | ficha/endereço | validar UF contra `_ibge_municipios.json` |
| `lance_inicial`/`preco` | valor 1ª praça | `parse_price` → float; `R$ 1.234,56`→`1234.56` |
| `avaliacao` | valor de avaliação | float; calcular `desconto_pct` quando ambos existirem |
| `data_leilao` / `data_leilao_1` / `data_leilao_2` | datas das praças | ISO `YYYY-MM-DD`; **descartar/sinalizar lotes cuja 1ª praça já passou** (`< {{DATA_HOJE}}`) |
| `tipo` / `tipo_imovel` / `tipo_leilao` | ficha/classificação | classificar (apartamento, casa, terreno…; judicial/extrajudicial) |
| `area_m2`, `quartos`, `banheiros`, `vagas` | ficha | numérico; extrair da descrição se não estruturado |
| `imagem` / `imagem_url` | galeria | ver regra de fotos abaixo |
| `anexos` | edital, matrícula, laudo (`a[href$=.pdf]`) | baixar e guardar caminho/URL |
| `importado_em` | timestamp da captura | ISO datetime |

### Regras específicas para reduzir perda

- **Fotos:** capture **todas** as imagens da galeria, não só a primeira. Resolva URLs
  relativas → absolutas, prefira a versão de **maior resolução** (atenção a `data-src`,
  `data-lazy`, `srcset` e thumbnails). Guarde a principal em `imagem` e a lista completa
  num campo/JSON auxiliar. Ignore ícones/logos/placeholders.
- **Descrição:** pegue o **bloco inteiro** (não o resumo do card). Se a página de listagem
  for pobre, **entre na página de detalhe** de cada lote para enriquecer.
- **Localização:** se não houver campo de cidade/UF, faça parsing do endereço/descrição
  e **valide o município** contra `_ibge_municipios.json`. Nunca grave UF inválida.
- **Lazy-load / paginação infinita:** role a página / siga "próxima página" / chame a API
  paginada até esgotar; confirme que o nº de lotes coletados bate com o total exibido.
- **Encoding:** force UTF-8 na leitura e na escrita (`reconfigure(encoding="utf-8")`).

## Robustez (mínimo de erros)

- **Retry com backoff exponencial** (2s, 4s, 8s, 16s) em falha de rede/timeout.
- **Timeouts explícitos** e `wait_until="networkidle"` no Playwright quando o conteúdo é JS.
- **Detecção de mudança de layout:** se um seletor essencial sumir em N lotes seguidos,
  pare e reporte — não grave linhas vazias em massa.
- **`site_health()` antes de renderizar:** evite gastar Playwright em site offline/nginx
  default/“em construção”.
- **Validação por linha** antes de persistir: descarte/sinalize registros sem `titulo` **e**
  sem `url`; logue os incompletos em vez de salvar lixo.

## Saídas e relatórios

1. **CSV** com o header já usado no projeto (ver `ofertas_detalhadas.csv`) em `/csv`.
2. **Upsert** na tabela `imoveis`.
3. **Relatório de progresso a cada 5 minutos:** nº de imóveis por leiloeiro, total
   acumulado, % de campos preenchidos por coluna, e contagem de erros por tipo.
4. **Relatório final** (markdown) com: cobertura por campo (quantos % vieram preenchidos),
   principais dificuldades, URLs que falharam e **sugestões de correção**. Acrescente essa
   seção ao final de `captura_dados_leiloes_v2.md`.

## Critério de pronto (Definition of Done)

- [ ] Todo lote visível na origem foi coletado (contagem confere com o total do site).
- [ ] `titulo`, `descricao`, `cidade/uf`, ≥1 `imagem` e ≥1 `data` preenchidos sempre que
      existirem na página; vazios só quando comprovadamente ausentes (e logados).
- [ ] Nenhuma 1ª praça com data anterior a hoje gravada como ativa.
- [ ] Zero duplicatas no destino (upsert por `id`).
- [ ] Logs e progresso permitem retomar sem reprocessar o que já foi feito.
- [ ] Relatório final com cobertura por campo + dificuldades + correções entregue.

---

### Como você (Claude) deve trabalhar

1. **Sonde primeiro:** abra a URL, identifique a plataforma (Superbid, Sodré Santoro, Mega
   Leilões, white-label…) e procure a API JSON interna antes de escrever qualquer parser.
2. **Prototipe em 1 lote**, confirme que todos os campos vêm corretos, **só então** rode em
   escala. Mostre-me a primeira linha extraída para validação antes do lote completo.
3. **Reaproveite os adapters/scrapers existentes** deste repositório quando a plataforma
   já estiver coberta; crie um novo extrator por plataforma quando necessário.
4. Ao terminar, traga a relação de imóveis por leiloeiro e o relatório de cobertura/erros.
