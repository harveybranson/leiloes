"""Executado DENTRO do container leilao_api via docker exec."""
import sys, os
sys.path.insert(0, '/tmp')

from pathlib import Path
from datetime import date
import importar_jucesp as ij

# Sobrescreve caminhos para dentro do container
ij.DB_URL       = "postgresql://leilao:leilao123@postgres:5432/leilao_db"
ij.COMPLETO_CSV = Path("/tmp/imoveis_completo.csv")
ij.EXTRA_CSV    = Path("/tmp/imoveis_jucesp_extra.csv")
ij.LEIS_CSV     = Path("/tmp/leiloeiros_regulares.csv")
ij.TODAY        = date.today()

# Executa
import argparse
sys.argv = ['importar_jucesp', '--skip-enrich']  # skip-enrich para ser rápido
ij.main()
