# Migração: FlorianÃ³polis → Florianópolis

## Diagnóstico

**Causa raiz:** `rebuild_viewer.py` lê `ofertas_completo.csv` com `encoding="cp1252"`, mas o
arquivo é gravado por `scraper_completo.py` com `encoding="utf-8"`. Isso causa mojibake em
todos os caracteres acentuados:

| UTF-8 (correto) | Lido como cp1252 (errado) |
|---|---|
| `ó` (bytes `\xC3\xB3`) | `Ã³` |
| `Florianópolis` | `FlorianÃ³polis` |
| `São Paulo` | `SÃ£o Paulo` |

O efeito visível é que o filtro de cidade exibe `FlorianÃ³polis` como entrada separada de
`Florianópolis`, e a busca por texto não casa as duas.

---

## Passo 1 — Corrigir o leitor do CSV em `rebuild_viewer.py`

**Arquivo:** `rebuild_viewer.py`, linha 68.

**Antes:**
```python
with open(COMPLETO_CSV, encoding="cp1252", errors="replace") as f:
```

**Depois:**
```python
with open(COMPLETO_CSV, encoding="utf-8", errors="replace") as f:
```

> Se o arquivo tiver BOM (raro, pois `scraper_completo.py` grava sem BOM), use `utf-8-sig`.

Após essa alteração, basta regenerar o viewer:

```bash
python rebuild_viewer.py
```

---

## Passo 2 — Corrigir registros já gravados no banco SQLite

Qualquer registro importado enquanto o bug estava ativo pode ter a cidade gravada como
`FlorianÃ³polis` (ou variante com outros caracteres corrompidos).

### 2a. Verificar registros afetados

```python
import sqlite3

conn = sqlite3.connect("imoveis_leiloeiros.db")
cur = conn.cursor()

# Lista cidades que parecem double-encoded
cur.execute("""
    SELECT cidade, COUNT(*) as n
    FROM imoveis
    WHERE cidade LIKE '%Ã%'
       OR cidade LIKE '%Â%'
       OR cidade LIKE '%Ã³%'
    GROUP BY cidade
    ORDER BY n DESC
""")
for row in cur.fetchall():
    print(row)

conn.close()
```

### 2b. Corrigir automaticamente (desfaz o double-encoding)

```python
import sqlite3

def fix_mojibake(s: str) -> str:
    """Desfaz UTF-8 lido como Latin-1: re-encode como latin-1, decode como utf-8."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s  # já está correto ou não é recuperável

conn = sqlite3.connect("imoveis_leiloeiros.db")
cur = conn.cursor()

cur.execute("SELECT id, cidade FROM imoveis WHERE cidade LIKE '%Ã%' OR cidade LIKE '%Â%'")
rows = cur.fetchall()

updates = []
for iid, cidade in rows:
    fixed = fix_mojibake(cidade)
    if fixed != cidade:
        updates.append((fixed, iid))
        print(f"  {repr(cidade)} -> {repr(fixed)}")

if updates:
    cur.executemany("UPDATE imoveis SET cidade = ? WHERE id = ?", updates)
    conn.commit()
    print(f"\n{len(updates)} registro(s) corrigido(s).")
else:
    print("Nenhum registro com encoding corrompido encontrado.")

conn.close()
```

### 2c. Correção pontual só para Florianópolis (se preferir SQL direto)

```sql
-- Verifica antes
SELECT id, cidade FROM imoveis WHERE cidade = 'FlorianÃ³polis';

-- Corrige
UPDATE imoveis
SET cidade = 'Florianópolis'
WHERE cidade = 'FlorianÃ³polis';
```

---

## Passo 3 — Corrigir o CSV `ofertas_completo.csv` (se necessário)

Se `ofertas_completo.csv` foi gerado enquanto o bug existia **e** a origem já tinha o texto
corrompido, o CSV pode conter `FlorianÃ³polis` nas colunas. Nesse caso, regerar o CSV pelo
scraper é suficiente:

```bash
python scraper_completo.py
```

Para corrigir um CSV existente sem rodar o scraper:

```python
import csv, pathlib

SRC = pathlib.Path("ofertas_completo.csv")
TMP = pathlib.Path("ofertas_completo_fixed.csv")

def fix_mojibake(s):
    try:
        return s.encode("latin-1").decode("utf-8")
    except:
        return s

with open(SRC, encoding="utf-8", newline="") as fin, \
     open(TMP, "w", encoding="utf-8", newline="") as fout:
    reader = csv.DictReader(fin)
    writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in reader:
        row["cidade"] = fix_mojibake(row.get("cidade", ""))
        writer.writerow(row)

SRC.replace(SRC.with_suffix(".csv.bak"))
TMP.rename(SRC)
print("CSV corrigido.")
```

---

## Passo 4 — Adicionar normalização permanente no `rebuild_viewer.py`

Para evitar que qualquer futuro CSV com encoding errado volte a gerar entradas corrompidas
no filtro de cidade, adicione uma função de limpeza antes de inserir o campo `cid`:

```python
def fix_mojibake(s: str) -> str:
    try:
        return s.encode("latin-1").decode("utf-8")
    except Exception:
        return s
```

E aplique ao ler a cidade (linhas 93 e 121 de `rebuild_viewer.py`):

```python
# linha 93
"cid":  fix_mojibake(row.get("cidade", ""))[:50],

# linha 121
"cid":  fix_mojibake(row.get("cidade", ""))[:50],
```

---

## Resumo das alterações

| Arquivo | Linha | O que muda |
|---|---|---|
| `rebuild_viewer.py` | 68 | `cp1252` → `utf-8` na leitura do CSV |
| `rebuild_viewer.py` | 93, 121 | Adicionar `fix_mojibake()` no campo `cid` |
| `imoveis_leiloeiros.db` | — | UPDATE nas cidades com `Ã`/`Â` |
| `ofertas_completo.csv` | — | Regenerar via scraper ou corrigir in-place |

Após aplicar os passos 1 e 2, `FlorianÃ³polis` deixará de aparecer no filtro de cidade e
todos os imóveis associados serão exibidos corretamente sob `Florianópolis`.
