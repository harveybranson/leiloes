from leiloeiros import (
    Perfilador, BuscaSitemap, ExtratorGenerico,
    ExtratorLLM, ScraperHeadless, Pipeline
)

# Perfila 200 domínios
p = Perfilador(workers=12)
p.rodar("dominios.txt", saida="resultado")

# Extrai um lote direto
e = ExtratorGenerico()
reg = e.extrair("https://site.com.br/lote/123")
print(reg["titulo"], reg["preco"], reg["cidade"])

# Roda o pipeline completo
pip = Pipeline("resultado.csv")
pip.rodar("imoveis", rodar_A=True, rodar_B=True)

# LLM para sites sem dados estruturados
llm = ExtratorLLM()  # requer ANTHROPIC_API_KEY no ambiente
llm.rodar("dominios_sem_dados.txt", modo="descobrir", saida="imoveis_llm.jsonl")
