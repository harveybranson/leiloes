import psycopg2, requests

conn = psycopg2.connect("postgresql://leilao:leilao123@localhost:5432/leilao_db")
cur = conn.cursor()

print("=" * 55)
print("  DIAGNÓSTICO DO BANCO")
print("=" * 55)

# 1. Contagens gerais
cur.execute("SELECT COUNT(*) FROM imoveis")
total_all = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=TRUE")
total_ativos = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=FALSE")
total_inativos = cur.fetchone()[0]

print(f"\nTotal ALL (incl. inativos): {total_all}")
print(f"Total ATIVOS              : {total_ativos}")
print(f"Total INATIVOS            : {total_inativos}")

# 2. Quando foram criados
cur.execute("""
    SELECT DATE(criado_em) as dia, COUNT(*) as n
    FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '7 days'
    GROUP BY dia ORDER BY dia DESC
""")
print("\nInserções por dia (últimos 7 dias):")
for dia, n in cur.fetchall():
    print(f"  {dia}: {n}")

# 3. Inseridos hoje, ativos vs inativos
cur.execute("""
    SELECT ativo, COUNT(*) FROM imoveis
    WHERE DATE(criado_em) = CURRENT_DATE
    GROUP BY ativo
""")
print("\nHoje - ativo vs inativo:")
for ativo, n in cur.fetchall():
    print(f"  ativo={ativo}: {n}")

# 4. Amostra dos inseridos hoje
cur.execute("""
    SELECT id, titulo, cidade, estado, valor_minimo, ativo, url_original
    FROM imoveis
    WHERE DATE(criado_em) = CURRENT_DATE AND ativo=TRUE
    ORDER BY id DESC LIMIT 5
""")
print("\nÚltimos 5 inseridos hoje (ativos):")
for r in cur.fetchall():
    t = (r[1] or "")[:50]
    print(f"  id={r[0]} | {t!r} | {r[2]}/{r[3]} | R${r[4]} | {r[6][:50] if r[6] else ''}")

# 5. Verifica a API do site
print("\n--- API STATUS ---")
try:
    r = requests.get("http://localhost:8000/api/v1/imoveis/estatisticas", timeout=5)
    data = r.json()
    print(f"total_imoveis (API): {data.get('total_imoveis')}")
    print(f"full response: {str(data)[:200]}")
except Exception as e:
    print(f"Erro API: {e}")

# 6. Verifica se tem fonte_id correto nos registros de hoje
cur.execute("""
    SELECT f.nome, COUNT(*) as n
    FROM imoveis i JOIN fontes f ON i.fonte_id = f.id
    WHERE DATE(i.criado_em) = CURRENT_DATE AND i.ativo=TRUE
    GROUP BY f.nome ORDER BY n DESC LIMIT 10
""")
print("\nFontes dos inseridos hoje:")
for nome, n in cur.fetchall():
    print(f"  {nome}: {n}")

# 7. IDs externo vs URL - verifica constraint
cur.execute("""
    SELECT COUNT(*) FROM imoveis
    WHERE DATE(criado_em) = CURRENT_DATE
    AND url_original IN (
        SELECT url_original FROM imoveis
        WHERE DATE(criado_em) < CURRENT_DATE
    )
""")
overlap = cur.fetchone()[0]
print(f"\nURLs de hoje que JÁ existiam antes: {overlap}")

conn.close()
