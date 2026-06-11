# Inventário — Ferramentas de captura e qualidade de dados de leilões

Índice de tudo que foi construído nesta frente de trabalho (branch
`claude/cool-heisenberg-spln9c`). Agrupado por função, com **para que serve** e **como usar**.

> Visão geral da arquitetura e princípios: ver `captura_dados_leiloes_master.md` (Parte XI lista
> estas ferramentas no contexto do pipeline).

---

## 1. Documentação

| Arquivo | O que é |
|---|---|
| `captura_dados_leiloes_master.md` | Guia mestre: escada de robustez (tiers 1–6), reconhecimento, schema unificado, filtro por data, qualidade/operação, prompt operacional e **Parte XI** (estas ferramentas). |
| `INVENTARIO.md` | Este índice. |

---

## 2. Modelo de dados / migração

| Arquivo | Para que serve | Uso |
|---|---|---|
| `migrar_imagens_anexos.py` | Cria as tabelas **1→N** `imovel_imagens` e `imovel_anexos` e faz backfill de `imoveis.imagem`. | `python migrar_imagens_anexos.py` |
| `plataformas.json` | Mapa **plataforma → tier/adapter** (Superbid, Milan, Central Sul, Mega…) p/ pular o reconhecimento em fontes conhecidas. | lido por `scraper_commons.detectar_plataforma` |
| `cobertura_historico.jsonl` | Histórico de snapshots de cobertura (base p/ detecção de regressão). | alimentado por `snapshot_cobertura.py` / `finalizar_coleta.py` |
| `csv/uf_revisao.csv` | 539 divergências de UF; 349 auto-triadas (`decisao=aplicar`), 190 em branco p/ revisão. | editar `decisao` → `aplicar_revisao.py` |

---

## 3. Enriquecimento e correção de dados (offline)

| Arquivo | Para que serve | Uso |
|---|---|---|
| `enrich_local.py` | Enriquece/audita sem rede: preenche `uf` (IBGE) e `lance_inicial` (texto); audita e corrige UF; audita e **limpa cidade** (bairro+município, ruído). | `--auditar [--corrigir] [--csv ...]` · `--auditar-cidade` · `--limpar-cidade [--remover-ruido]` · `--uf-de-cidade` · `--dry-run` |
| `aplicar_revisao.py` | Aplica as decisões de triagem do `uf_revisao.csv` (`aplicar`/`manter`/`<UF>`), validando contra o IBGE. | `python aplicar_revisao.py --csv csv/uf_revisao.csv [--dry-run]` |

---

## 4. Gate de qualidade e observabilidade

| Arquivo | Para que serve | Uso |
|---|---|---|
| `check_cobertura.py` | Gate de cobertura por campo (**exit ≠ 0** se abaixo do limite) + `--gate-leiloeiro` (flagra leiloeiro que despenca vs. a média). | `--por-leiloeiro` · `--gate-leiloeiro [--margem 40] [--min-volume 20]` · `--desde AAAA-MM-DD` · `--json` |
| `snapshot_cobertura.py` | Grava snapshot diário + detecta **regressão** (queda por leiloeiro = redesign). | `python snapshot_cobertura.py [--limite-queda 15] [--strict]` |
| `finalizar_coleta.py` | **Pós-coleta:** snapshot+regressão → gate → regenera dashboard. Exit ≠ 0 trava o commit. | `python finalizar_coleta.py --desde hoje` |
| `gerar_dashboard_frescor.py` | Dashboard HTML: cobertura por campo, frescor por data, **tendência** (sparklines) e **regressões**. | `python gerar_dashboard_frescor.py` → `dashboard_frescor.html` |
| `gerar_viewer_galeria.py` | Viewer HTML com **carrossel** de fotos por imóvel (lê `imovel_imagens`) + anexos; filtro UF/texto. | `python gerar_viewer_galeria.py` → `viewer_galeria.html` |
| `dashboard_frescor.html` | Artefato gerado (dashboard). | abrir no navegador |
| `viewer_galeria.html` | Artefato gerado (galeria). | abrir no navegador |

---

## 5. Coleta (scrapers) — modificações

| Arquivo | O que mudou |
|---|---|
| `scraper_commons.py` | +`detectar_plataforma`, `extrair_galeria`/`extrair_anexos`, `salvar_galeria`/`salvar_anexos`, `inferir_uf`/`inferir_uf_forte`, `extrair_municipio`, `municipio_valido`, `carregar_municipios` (canônicos), `fetch_flaresolverr`/`parece_bloqueio`. |
| `scraper_detalhe.py` | `_extract` chama galeria/anexos + infere UF; `persistir_midia` (grava 1→N); `--reprocessar-sem-foto`; **fallback FlareSolverr** no `visit()`; resiliência de browser (`PW_CHROMIUM_PATH`/`PW_IGNORE_HTTPS`). |
| `importar_site.py` | `uf_backfill` + `cidade_limpa` (normaliza UF/cidade **na importação**). |
| `staging_anuncios.py` | backfill de UF + `_cidade_limpa_staging`. |
| `run_scraper.py` | `--finalize`/`--only-finalize`; import lazy do Playwright. |

---

## 6. Infraestrutura / ambiente

| Arquivo | Para que serve | Uso |
|---|---|---|
| `.claude/settings.json` | **SessionStart hook** (inclui Claude na web). | automático ao abrir a sessão |
| `scripts/session-setup.sh` | Instala deps + Playwright/Chromium (com **fallback** p/ Chromium existente / Chrome do sistema) e roda smoke test. | chamado pelo hook |
| `scripts/run-quality.ps1` | Roda o gate de qualidade (Windows). | `powershell -File scripts\run-quality.ps1` |
| `scripts/setup-scheduled-quality.ps1` | Agenda o gate **1×/dia** (Task Scheduler). | `powershell -File scripts\setup-scheduled-quality.ps1` |
| `docker-compose.flaresolverr.yml` | Sobe o **FlareSolverr** (resolve Cloudflare das fontes que bloqueiam headless). | `docker compose -f docker-compose.flaresolverr.yml up -d` |
| `probe_flaresolverr.py` | Verifica se o FlareSolverr está no ar antes de rodar o scraper. | `python probe_flaresolverr.py` |
| `imoveis_leiloeiros.db` | Banco SQLite: 9.924 imagens migradas p/ 1→N, UFs corrigidas (234+349), cidades normalizadas (~3.285) e ruído removido (503). | — |

---

## 7. Fluxo recomendado (ponta a ponta)

```bash
# 0. (1ª vez) tabelas de mídia
python migrar_imagens_anexos.py

# 1. coleta (na sua máquina, onde os sites são acessíveis)
docker compose -f docker-compose.flaresolverr.yml up -d   # destrava fontes com anti-bot
python probe_flaresolverr.py
python scraper_detalhe.py --reprocessar-sem-foto          # popula galeria/anexos/UF/lance

# 2. enriquecimento + correção offline
python enrich_local.py                       # uf/lance a partir do texto
python enrich_local.py --limpar-cidade --remover-ruido
python enrich_local.py --auditar --corrigir --csv csv/uf_revisao.csv
python aplicar_revisao.py --csv csv/uf_revisao.csv        # após revisar as ambíguas

# 3. gate + observabilidade
python finalizar_coleta.py --desde hoje      # snapshot + gate + dashboard
python check_cobertura.py --gate-leiloeiro   # flagra extratores quebrados
python gerar_viewer_galeria.py               # viewer com carrossel

# Estado atual do gate: tudo ✓ exceto lance_inicial (64,3%) — depende do re-scrape (passo 1).
```
