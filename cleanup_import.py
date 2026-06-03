import psycopg2
conn = psycopg2.connect("postgresql://leilao:leilao123@localhost:5432/leilao_db")
cur = conn.cursor()

# Diagnóstico: títulos inválidos inseridos na última hora
cur.execute("""
    SELECT titulo, COUNT(*) as n FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '2 hours'
    AND (
        titulo ILIKE '%login%'
        OR titulo ILIKE '%fa_a%seu%'
        OR titulo ILIKE '%acesso%negado%'
        OR titulo ILIKE '%entrar%'
        OR titulo ILIKE '%403%'
        OR titulo ILIKE '%404%'
        OR titulo ILIKE '%forbidden%'
        OR titulo ILIKE '%blocked%'
        OR titulo ILIKE '%compra por proposta%'
        OR LENGTH(TRIM(titulo)) < 8
    )
    GROUP BY titulo ORDER BY n DESC LIMIT 15
""")
bad_titles = cur.fetchall()
print("Titulos invalidos encontrados:")
for t, n in bad_titles:
    print(f"  [{n}] {t[:80]!r}")

total_bad_by_title = sum(n for _, n in bad_titles)
print(f"Total por titulo: {total_bad_by_title}")

# Desativa registros ruins (não exclui — mantém para auditoria)
cur.execute("""
    UPDATE imoveis SET ativo = FALSE
    WHERE criado_em >= NOW() - INTERVAL '2 hours'
    AND (
        titulo ILIKE '%login%'
        OR titulo ILIKE '%fa_a%seu%'
        OR titulo ILIKE '%acesso%negado%'
        OR titulo ILIKE '%entrar%'
        OR titulo ILIKE '%403%'
        OR titulo ILIKE '%404%'
        OR titulo ILIKE '%forbidden%'
        OR titulo ILIKE '%blocked%'
        OR LENGTH(TRIM(titulo)) < 8
    )
""")
print(f"\nDesativados: {cur.rowcount}")

# Também desativa itens com valor_minimo = 1.00 (placeholder)
cur.execute("""
    UPDATE imoveis SET ativo = FALSE
    WHERE criado_em >= NOW() - INTERVAL '2 hours'
    AND valor_minimo = 1.00
    AND titulo ILIKE '%login%'
""")
print(f"Desativados (val=1): {cur.rowcount}")

conn.commit()

# Resumo final
cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=TRUE")
print(f"\nTotal ativos no banco: {cur.fetchone()[0]}")

cur.execute("""
    SELECT COUNT(*) FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '2 hours' AND ativo=TRUE
""")
print(f"Inseridos nas últimas 2h (ativos): {cur.fetchone()[0]}")

cur.execute("""
    SELECT estado, COUNT(*) FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '2 hours' AND ativo=TRUE
    GROUP BY estado ORDER BY 2 DESC LIMIT 8
""")
print("Por estado:", cur.fetchall())

cur.execute("""
    SELECT tipo_imovel, COUNT(*) FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '2 hours' AND ativo=TRUE
    GROUP BY tipo_imovel ORDER BY 2 DESC
""")
print("Por tipo:", cur.fetchall())

# Exemplos de bons registros
cur.execute("""
    SELECT titulo, valor_minimo, cidade, estado, data_primeiro_leilao, leiloeiro
    FROM imoveis
    WHERE criado_em >= NOW() - INTERVAL '2 hours' AND ativo=TRUE
    AND titulo NOT ILIKE '%login%'
    AND valor_minimo > 1000
    ORDER BY RANDOM() LIMIT 5
""")
print("\nExemplos de registros válidos:")
for r in cur.fetchall():
    print(f"  {r[0][:60]} | R${r[1]} | {r[2]}/{r[3]} | {str(r[4])[:10]} | {r[5]}")

conn.close()
