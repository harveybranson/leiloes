"""
sync_leiloeiros_jucea.py
========================
Sincroniza os leiloeiros REGULAR da JUCEA com a tabela `leiloeiros` do PostgreSQL e
vincula `imoveis.leiloeiro_id` aos imóveis capturados hoje pelo scraper_tjam_jucea.

- Casa por nome normalizado (sem acento/maiúsculas) com registros existentes; prefere
  o registro com junta_comercial='JUCEA'. Se não existir, insere novo (Regular).
- Atualiza imoveis.leiloeiro_id por correspondência exata do texto `leiloeiro`.
"""
import sys, io, re, unicodedata, subprocess
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
from datetime import date

from scraper_tjam_jucea import LEILOEIROS_JUCEA

# Strings exatas que aparecem em imoveis.leiloeiro vindas da loja TJAM (auctioneer),
# mapeadas para o nome canônico da JUCEA.
ALIASES_TJAM = {
    "Danielly Fernandes da Silva Nazareth": "DANIELLY FERNANDES DA SILVA NAZARETH",
    "Ricardo Marcelo Gomes de OLiveira":    "RICARDO MARCELO GOMES DE OLIVEIRA",
}

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()

def psql(sql, tuples=True, timeout=40):
    args = ["docker","exec","leilao_postgres","psql","-U","leilao","-d","leilao_db"]
    if tuples: args += ["--no-align","--tuples-only"]
    args += ["-c", sql]
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)

def esc(v, n=None):
    s = str(v if v is not None else "").replace("\x00","").replace("'","''")
    return s[:n] if n else s

# 1. Carrega leiloeiros existentes (id, nome, junta) — escolhe melhor match por nome
out = psql("SELECT id, nome, COALESCE(junta_comercial,'') FROM leiloeiros;").stdout
existentes = []
for line in out.splitlines():
    parts = line.split("|")
    if len(parts) >= 3 and parts[0].strip().isdigit():
        existentes.append((int(parts[0]), parts[1], parts[2]))

def achar_existente(nome_canon: str):
    n = norm(nome_canon)
    cands = [(i, j) for (i, nm, j) in existentes if norm(nm) == n]
    if not cands: return None
    # prefere JUCEA
    for i, j in cands:
        if "JUCEA" in j.upper(): return i
    return cands[0][0]

# 2. Para cada JUCEA leiloeiro: acha existente ou insere
nome_para_id = {}
inseridos = vinculados_existente = 0
for l in LEILOEIROS_JUCEA:
    nome = l["nome"]
    lid = achar_existente(nome)
    if lid is None:
        sql = (f"INSERT INTO leiloeiros (matricula,nome,uf,junta_comercial,situacao,cidade,site,criado_em,atualizado_em) "
               f"VALUES ('{esc(l.get('matricula'),20)}','{esc(nome,200)}','{esc(l.get('uf_leiloeiro','AM'),2)}',"
               f"'JUCEA','Regular','{esc(l.get('cidade_leiloeiro'),100)}','{esc(l.get('site'),200)}',NOW(),NOW()) RETURNING id;")
        r = psql(sql)
        novo = r.stdout.strip()
        if novo.isdigit():
            lid = int(novo); inseridos += 1
            print(f"  + inserido leiloeiro id={lid}: {nome}")
        else:
            print(f"  [ERRO insert] {nome}: {r.stderr[:120]}")
            continue
    else:
        vinculados_existente += 1
    nome_para_id[norm(nome)] = lid

print(f"\nLeiloeiros: {inseridos} inseridos, {vinculados_existente} já existiam")

# 3. Vincula imoveis.leiloeiro_id pelos imóveis criados hoje
FONTES = ('tjam_superbid','webleiloescombr','leilaobrasilcombr','leilocombr','dgleiloescombr',
          'hoppeleiloescombr','leilaonetcombr','alfaleiloescom','tmleiloescombr','agostinholeiloescombr',
          'wrleiloescombr','deonizialeiloescombr','norteleiloescombr','asamileiloescombr',
          'fernandoleiloeirocombr','amazonasleiloescombr','alexwillianhoppe')
fontes_sql = ",".join(f"'{f}'" for f in FONTES)

# distinct nomes de leiloeiro nos meus imóveis
out = psql(f"SELECT DISTINCT leiloeiro FROM imoveis WHERE criado_em::date='{date.today().isoformat()}' "
           f"AND fonte_id IN (SELECT id FROM fontes WHERE nome IN ({fontes_sql}));").stdout
nomes_imovel = [x for x in out.splitlines() if x.strip()]

total_upd = 0
for nm in nomes_imovel:
    canon = ALIASES_TJAM.get(nm, nm)
    lid = nome_para_id.get(norm(canon))
    if not lid:
        # tenta token-overlap com a lista JUCEA
        a = set(norm(nm).split())
        best = None
        for k, v in nome_para_id.items():
            if len(a & set(k.split())) >= 2: best = v; break
        lid = best
    if not lid:
        print(f"  [sem match] leiloeiro='{nm}'"); continue
    r = psql(f"UPDATE imoveis SET leiloeiro_id={lid} WHERE leiloeiro='{esc(nm)}' "
             f"AND criado_em::date='{date.today().isoformat()}' AND leiloeiro_id IS NULL;", tuples=False)
    m = re.search(r"UPDATE (\d+)", r.stdout)
    n = int(m.group(1)) if m else 0
    total_upd += n
    print(f"  vinculado '{nm[:40]}' → leiloeiro_id={lid} ({n} imóveis)")

print(f"\nTotal imóveis vinculados: {total_upd}")

# 4. Verificação
out = psql(f"SELECT COUNT(*) FROM imoveis WHERE criado_em::date='{date.today().isoformat()}' "
           f"AND fonte_id IN (SELECT id FROM fontes WHERE nome IN ({fontes_sql})) AND leiloeiro_id IS NULL;").stdout
print(f"Imóveis de hoje ainda sem leiloeiro_id: {out.strip()}")
