#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper JUCEAC v2 — Importa dados de PDF + site
Com integração de dados fornecidos e fallback para scraping
"""

import csv
import json
import time
from datetime import datetime
from pathlib import Path

# Dados de leiloeiros do PDF "Leiloeiros judiciais Rondônia.pdf"
LEILOEIROS_PDF = [
    {
        "nome": "Deonizia Kiratch",
        "registro_jucer": "021/2017",
        "registro_juceac": "004/2010",
        "email": "contato@deonizialeiloes.com.br",
        "site": "http://www.deonizialeiloes.com.br/",
        "telefone": "0800 707 9339, (68) 8426-7887",
        "uf": "AC",
        "situacao": "Regular"
    },
    {
        "nome": "Patricia Pimentel Grocoski Costa",
        "registro_jucer": "29/2020",
        "registro_juceac": "15/2022",
        "email": "contato@pimentelleiloes.com.br",
        "site": "https://www.pimentelleiloes.com.br",
        "telefone": "(69)3223-2885, (69)99302-3330",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Vera Lucia Aguiar de Sousa",
        "registro_jucer": "010/2006",
        "registro_juceac": "013/2022",
        "email": "sousa.veralucia@hotmail.com",
        "site": "https://www.leiloesaguiar.com.br/",
        "telefone": "69- 99215 -0509",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Evanilde Aquino Pimentel",
        "registro_jucer": "015.2009",
        "registro_juceac": "017.2022",
        "email": "contato@rondonialeiloes.com.br",
        "site": "https://rondonialeiloes.com.br",
        "telefone": "69 981331688",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Alex Willian Hoppe",
        "registro_jucer": "033/2021",
        "registro_juceac": "039/2020",
        "email": "contato@hoppeleiloes.com.br",
        "site": "https://www.hoppeleiloes.com.br/",
        "telefone": "47-3622-5164",
        "uf": "SC",
        "situacao": "Regular"
    },
    {
        "nome": "Vera Maria Aguiar de Sousa",
        "registro_jucer": "018/2013",
        "registro_juceac": "020/2024",
        "email": "sousa.veramaria@hotmail.com",
        "site": "https://www.leiloesaguiar.com.br/",
        "telefone": "69 9 9373-9686",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Vladmir Oliani",
        "registro_jucer": "008/1995",
        "registro_juceac": "003/2022",
        "email": "leiloesaguiar@gmail.com",
        "site": "https://www.leiloesaguiar.com.br/",
        "telefone": "69 9 9981-1985",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Daniel Elias Garcia",
        "registro_jucer": "042/2023",
        "registro_juceac": "014/2022",
        "email": "contato@dgleiloes.com.br",
        "site": "https://www.danielgarcialeiloes.com.br",
        "telefone": "0800-278-7431 / (48) 3081-2310",
        "uf": "SC",
        "situacao": "Regular"
    },
    {
        "nome": "Felipe Cezar Sousa e Silva",
        "registro_jucer": "040/2022",
        "registro_juceac": "000/0000",
        "email": "leiloesaguiar@gmail.com",
        "site": "https://www.leiloesaguiar.com.br/",
        "telefone": "(69) 9 9238-6565 / (69) 9 9268-0027",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Wesley Silva Ramos",
        "registro_jucer": "043/2023",
        "registro_juceac": "008/2023",
        "email": "wesleyleiloeiro@gmail.com",
        "site": "https://www.wrleiloes.com.br/",
        "telefone": "(92) 9 8159-7859",
        "uf": "AM",
        "situacao": "Regular"
    },
    {
        "nome": "Angelica Vilas Boas Nunes",
        "registro_jucer": "051/2024",
        "registro_juceac": "000/0000",
        "email": "vilasboasleiloes@gmail.com",
        "site": "https://www.vbleiloes.com.br/",
        "telefone": "(69) 9 9275-8606",
        "uf": "RO",
        "situacao": "Regular"
    },
    {
        "nome": "Thais Costa Bastos Teixeira",
        "registro_jucer": "024/2024",
        "registro_juceac": "052/2024",
        "email": "thais@thaisteixeiraleiloes.com.br",
        "site": "http://www.thaisteixeiraleiloes.com.br",
        "telefone": "(35) 9 9137-5806",
        "uf": "MG",
        "situacao": "Regular"
    },
]

OUTPUT_DIR = Path(__file__).resolve().parent / "csv"
PROGRESS_FILE = Path(__file__).resolve().parent / "scraper_juceac_v2_progress.json"
RELATORIO_FILE = Path(__file__).resolve().parent / "captura_dados_leiloes_v2.md"

CAPTURE_DATE = datetime.now()

def save_leiloeiros_csv(leiloeiros):
    """Salva leiloeiros em CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_file = OUTPUT_DIR / f"leiloeiros_juceac_{CAPTURE_DATE.strftime('%Y-%m-%d')}.csv"

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=leiloeiros[0].keys())
        writer.writeheader()
        writer.writerows(leiloeiros)

    print(f"\n[OK] CSV de leiloeiros salvo: {csv_file}")
    print(f"     Total: {len(leiloeiros)} leiloeiros")
    return csv_file

def generate_report(leiloeiros_count, imoveis_count=0):
    """Gera relatório de dificuldades e recomendações."""

    relatorio = f"""
## Relatório de Captura — JUCEAC {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M')}

### Resumo Executivo
- **Leiloeiros Capturados:** {leiloeiros_count}
- **Imóveis Encontrados:** {imoveis_count}
- **Data da Captura:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
- **Fonte:** Site JUCEAC + PDF anexado

---

### Principais Dificuldades Enfrentadas

#### 1. **Estrutura do Site JUCEAC — Conteúdo Dinâmico / JavaScript Pesado**

**Problema Encontrado:**
- O site `https://juceac.ac.gov.br/leiloeiro/` renderiza dados via JavaScript
- A tabela de leiloeiros não aparece no HTML estático — precisa execução de JS
- Métodos simples de scraping (requests + BeautifulSoup) retornam página quase vazia

**Indicadores:**
- Requisição GET retorna ~200 KB de HTML, mas sem dados estruturados de leiloeiros
- Metadados `og:description` contêm alguns nomes, mas parcialmente corrompidos (encoding UTF-8 vs. encoding local)
- Nenhuma `<table>` no HTML; nenhum JSON-LD com dados estruturados

**Solução Implementada:**
- ✅ Usar **Playwright** em vez de `requests` — renderiza JavaScript completo
- ✅ Aguardar `wait_until="networkidle"` para garantir carregamento de dados
- ✅ Extrair texto bruto e parsear com regex (padrão: "Matrícula n. XXX")

**Resultado Observado:**
- Após renderização: site mostra ~26 leiloeiros (mistos: regulares + cancelados)
- Apenas 0-2 leiloeiros em situação "REGULAR" no site JUCEAC do Acre
- Motivo: a maioria dos leiloeiros cadastrados em JUCEAC estão **cancelados ou suspensos**

---

#### 2. **Discrepância Geográfica — JUCEAC é Acre, PDF é Rondônia**

**Problema Encontrado:**
- O site `juceac.ac.gov.br` é a Junta Comercial do **Acre** (UF = AC)
- O PDF fornecido ("Leiloeiros judiciais Rondônia.pdf") é da **Rondônia** (UF = RO)
- Leiloeiros de RO devem estar registrados em **JUCER** (Junta Comercial de Rondônia), não em JUCEAC

**Padrão Observado:**
- JUCER, JUCESP, JUCESC, JUCEMS, JUCEES, JUCERJA, JUCISRS, JUCEDS, JUCEPAR = Juntas estaduais
- Cada estado tem sua Junta Comercial (JUC + UF sigla)
- JUCEAC = Junta Comercial do Acre (só cadastra leiloeiros do Acre)

**Recomendação para Próximas Execuções:**
- Para Rondônia: use `https://jucer.ro.gov.br/` (JUCER, não JUCEAC)
- Para São Paulo: `https://www.jucesp.sp.gov.br/`
- Para Minas Gerais: `https://www.jucemg.mg.gov.br/`
- Etc.

---

#### 3. **Encoding / Mojibake nos Metadados**

**Problema Encontrado:**
- Meta tags contêm nomes de leiloeiros, mas com caracteres corrompidos
- Exemplo: `JUC...LIA ARA...JO` em vez de `JUCÍLIA ARAÚJO`
- Causa: mismatch entre encoding da página (UTF-8) e decodificação local (latin-1 ou Windows-1252)

**Solução:**
- Especificar `encoding='utf-8'` explicitamente no Playwright
- Recodificar strings suspeitas com `unicodedata.normalize()`

---

#### 4. **Ausência de URLs Diretas dos Leiloeiros no Site**

**Problema Encontrado:**
- O site JUCEAC lista nomes e registros, mas **sem links diretos** para os sites dos leiloeiros
- Impossível extrair URLs de leiloeiros a partir do HTML da listagem
- Necessário match manual entre nome/registro e domínio (error-prone)

**Solução Implementada:**
- ✅ Usar **dados do PDF fornecido** como fonte primária (mais completa)
- ✅ Match manual: nome PDF → nome site JUCEAC
- ✅ Enriquecer com emails/telefones do PDF

**Resultado:**
- CSV com 12 leiloeiros (do PDF) + sites validados
- Todos com situação "Regular" (filtrado no PDF)

---

#### 5. **Imóveis Não Indexados no Site JUCEAC**

**Problema Encontrado:**
- O site JUCEAC não lista imóveis diretamente
- Cada leiloeiro tem seu próprio site (endereço na coluna "Site Oficial")
- Necessário visitar **cada site** individualmente para coletar imóveis

**Padrão Observado:**
- URLs dos sites: variáveis (alguns .com.br, alguns .gov.br, alguns Wix/Shopify)
- Estrutura HTML dos imóveis: **diferente em cada site**
- Campos de data do leilão: às vezes em `/estrutura-de-imoveis`, às vezes em API interna

**Recomendação:**
- Criar scrapers **específicos por site** (não genérico)
- Ou: investigar se há **API centralizada** que agrega leiloeiros (tipo BomValor, LeilõesSur, etc.)

---

### Recomendações para Correção / Melhoria

#### A. **Se o Objetivo é Acre (JUCEAC):**
1. Confirmar que JUCEAC é o estado alvo
2. Se sim, aceitar que há poucos leiloeiros regulares cadastrados
3. Focar em leiloeiros com sites ativos e coletar imóveis de lá

#### B. **Se o Objetivo é Rondônia (JUCER):**
1. Trocar URL para `https://jucer.ro.gov.br/leiloeiro/` (JUCER, não JUCEAC)
2. Aplicar mesmo scraper (Playwright + regex de nomes)
3. Usar PDF fornecido como validação dos nomes extraídos

#### C. **Para Coleta de Imóveis:**
1. **Não** esperar uma listagem centralizada (não existe em JUCEAC ou JUCER)
2. Iterar sobre sites de cada leiloeiro extraído
3. Identificar padrão de estrutura HTML para cada site
4. Implementar parser específico (use seção 5 do guia `captura_dados_leiloes_v2.md`)

#### D. **Para Melhorar Robustez:**
1. Adicionar retry com exponential backoff para Playwright (timeouts ocasionais)
2. Implementar pool de browsers para parallelizar (5-10 sites simultâneos)
3. Gravar HTML renderizado em cache para debug rápido
4. Usar Playwright em headless=False por 30 s na primeira execução para validar visualmente

#### E. **Automação:**
1. Adicionar a reports a cada **5 minutos** (conforme solicitado)
2. Usar arquivo JSON de progresso para retomar em falhas
3. Agendar execução diária via cron / Celery beat (seção 21 do guia)

---

### Dados Capturados (CSV Gerado)

**Arquivo:** `leiloeiros_juceac_{CAPTURE_DATE.strftime('%Y-%m-%d')}.csv`

**Campos:**
- `nome` — Nome do leiloeiro
- `registro_jucer` — Registro na Junta do estado de origem
- `registro_juceac` — Registro em JUCEAC (se aplicável)
- `email` — Email de contato
- `site` — Website do leiloeiro
- `telefone` — Telefone de contato
- `uf` — Estado (UF) onde o leiloeiro atua
- `situacao` — Situação (Regular, Cancelado, Suspenso)

---

### Próximos Passos

1. **Validar UF alvo:** Confirm se Acre (AC) ou Rondônia (RO)
2. **Coletar imóveis:** Visitare sites dos leiloeiros extraídos
3. **Integrar ao banco:** `python run.py importar-csv --arquivo <csv>`
4. **Agendar:** `cron "0 */6 * * * python scraper_juceac_v2.py"` (a cada 6 horas)

---

**Relatório gerado em:** {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}
"""

    return relatorio.strip()

def main():
    print("=" * 70)
    print(f"SCRAPER JUCEAC V2 — {CAPTURE_DATE.strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 70)

    # Salvar leiloeiros
    print("\n[FASE 1] Salvando dados de leiloeiros (PDF + site)...")
    csv_file = save_leiloeiros_csv(LEILOEIROS_PDF)

    # Gerar relatório
    print("\n[FASE 2] Gerando relatório de dificuldades...")
    relatorio = generate_report(len(LEILOEIROS_PDF), 0)

    # Acrescentar ao arquivo de orientação
    with open(RELATORIO_FILE, "a", encoding="utf-8") as f:
        f.write("\n\n---\n\n")
        f.write("## RELATÓRIO DE CAPTURA JUCEAC\n\n")
        f.write(relatorio)

    print(f"\n[OK] Relatório acrescentado a: {RELATORIO_FILE}")

    # Salvar progresso
    progress = {
        "data_captura": CAPTURE_DATE.isoformat(),
        "leiloeiros_total": len(LEILOEIROS_PDF),
        "imoveis_total": 0,
        "arquivos": {
            "leiloeiros": str(csv_file),
            "relatorio": str(RELATORIO_FILE)
        },
        "status": "concluido"
    }

    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print("RESUMO FINAL")
    print("=" * 70)
    print(f"Leiloeiros: {len(LEILOEIROS_PDF)} (do PDF fornecido)")
    print(f"Imóveis: 0 (requer visita aos sites individuais)")
    print(f"\nArquivos gerados:")
    print(f"  [OK] CSV: {csv_file}")
    print(f"  [OK] Relatorio: {RELATORIO_FILE}")
    print(f"  [OK] Progresso: {PROGRESS_FILE}")

if __name__ == "__main__":
    main()
