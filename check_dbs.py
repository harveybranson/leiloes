"""
Verifica se a API e o psycopg2 estão lendo o mesmo banco.
"""
import psycopg2, requests, subprocess, json

# 1. Conta via psycopg2 (localhost:5432)
conn = psycopg2.connect("postgresql://leilao:leilao123@localhost:5432/leilao_db")
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM imoveis WHERE ativo=TRUE")
db_ativos = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM imoveis")
db_total = cur.fetchone()[0]
cur.execute("SELECT MAX(id), MAX(criado_em) FROM imoveis")
max_id, max_dt = cur.fetchone()
conn.close()

print(f"psycopg2 (localhost:5432):")
print(f"  Total ALL : {db_total}")
print(f"  Ativos    : {db_ativos}")
print(f"  Max ID    : {max_id}")
print(f"  Mais recente: {max_dt}")

# 2. Conta via API
r = requests.get("http://localhost:8000/api/v1/imoveis/estatisticas")
api_data = r.json()
api_total = api_data.get("total_imoveis")
print(f"\nAPI (localhost:8000):")
print(f"  total_imoveis: {api_total}")

# 3. Pega o ID máximo via API
r2 = requests.get("http://localhost:8000/api/v1/imoveis/?limit=1&ordenar=id_desc")
if r2.status_code == 200:
    items = r2.json().get("items",[])
    if items:
        print(f"  Max ID na API: {items[0].get('id')}")

# 4. Diagnóstico
print("\n--- DIAGNÓSTICO ---")
if api_total == db_ativos:
    print("✓ API e banco estão sincronizados")
elif api_total > db_total:
    print("⚠ API mostra MAIS do que o total no banco — API pode estar usando DB diferente!")
    print("  Suspeita: API conecta a outro postgres (Docker interno vs host)")
elif api_total < db_ativos:
    print("⚠ API mostra MENOS do que o banco tem — pode haver filtros extras ou cache")
else:
    diff = api_total - db_ativos
    print(f"⚠ Diferença: API={api_total} vs DB ativo={db_ativos} (diff={diff:+d})")
    if diff > 0:
        print("  Possível causa: há inativos sendo contados, ou DB diferente")
    else:
        print("  Possível causa: registros novos não visíveis via API (filtro/status)")

# 5. Testa inserir um registro dummy e ver se API enxerga
print("\n--- TESTE DE VISIBILIDADE ---")
conn2 = psycopg2.connect("postgresql://leilao:leilao123@localhost:5432/leilao_db")
cur2 = conn2.cursor()
# Pega um fonte_id existente
cur2.execute("SELECT id FROM fontes LIMIT 1")
fonte_id = cur2.fetchone()[0]
cur2.execute("""
    INSERT INTO imoveis (id_externo, fonte_id, titulo, url_original,
        tipo_imovel, tipo_leilao, status, categoria,
        geocodificado, classificado, ativo, criado_em, atualizado_em)
    VALUES ('TEST_DIAGNOSTICO_9999', %s, 'TESTE DIAGNOSTICO 9999',
        'https://test-diagnostico-9999.example.com',
        'OUTRO', 'EXTRAJUDICIAL', 'ABERTO', 'IMOVEL',
        FALSE, FALSE, TRUE, NOW(), NOW())
    ON CONFLICT DO NOTHING
    RETURNING id
""", (fonte_id,))
result = cur2.fetchone()
conn2.commit()

if result:
    test_id = result[0]
    print(f"Registro de teste inserido: id={test_id}")

    # Verifica se a API enxerga
    import time; time.sleep(0.5)
    r3 = requests.get(f"http://localhost:8000/api/v1/imoveis/{test_id}")
    if r3.status_code == 200:
        print("✓ API ENXERGA o registro novo — mesmo banco!")
    else:
        print(f"✗ API NÃO enxerga (status {r3.status_code}) — banco DIFERENTE!")

    # Limpa o teste
    cur2.execute("DELETE FROM imoveis WHERE id = %s", (test_id,))
    conn2.commit()
    print("Registro de teste removido.")
else:
    print("Registro de teste já existia (ON CONFLICT).")

conn2.close()
