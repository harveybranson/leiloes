import psycopg2
conn = psycopg2.connect("postgresql://leilao:leilao123@localhost:5432/leilao_db")
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=TRUE")
print("Total ativos:", cur.fetchone()[0])

cur.execute("SELECT COUNT(*) FROM imoveis WHERE criado_em >= NOW() - INTERVAL '1 hour'")
print("Inseridos ultima hora:", cur.fetchone()[0])

cur.execute("""
    SELECT estado, COUNT(*) as n FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '1 hour'
    GROUP BY estado ORDER BY n DESC LIMIT 8
""")
print("Por estado (ultima hora):", cur.fetchall())

cur.execute("""
    SELECT tipo_imovel, COUNT(*) as n FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '1 hour'
    GROUP BY tipo_imovel ORDER BY n DESC LIMIT 8
""")
print("Por tipo (ultima hora):", cur.fetchall())

cur.execute("""
    SELECT titulo, valor_minimo, cidade, estado, data_primeiro_leilao
    FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '5 minutes'
    ORDER BY id DESC LIMIT 5
""")
print("Mais recentes:")
for r in cur.fetchall():
    print(" ", r)

conn.close()
