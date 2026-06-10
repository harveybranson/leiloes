#!/usr/bin/env bash
# rodar_leiloeiros_db.sh
# =============================================================================
# Versao Linux/VPS do launcher. Chama o scraping dos leiloeiros cadastrados no
# banco do /admin (tabela `leiloeiros`, ~954 sites) -> Postgres do site.
# Segue a escada de robustez do playbook (JSON embutido -> API -> HTML -> Playwright).
#
# Pre-requisito: stack docker no ar (docker compose up -d).
#
# Uso:
#   ./rodar_leiloeiros_db.sh                         # todos os ~954 sites
#   ./rodar_leiloeiros_db.sh --uf SP --limite 50 --sem-playwright
#   ./rodar_leiloeiros_db.sh --fatia 2 --fatias 7    # so a fatia do dia
#   ./rodar_leiloeiros_db.sh --background            # nohup, log em logs/
# Qualquer outra flag de `run.py scrape-leiloeiros-db` e repassada direto.
# =============================================================================
set -euo pipefail

CONTAINER="${CONTAINER:-leilao_api}"
PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS_DIR="$PROJ_DIR/logs"
mkdir -p "$LOGS_DIR"
STAMP="$(date +%Y-%m-%d_%H%M)"
LOG="$LOGS_DIR/leiloeiros_db_$STAMP.log"

# Separa --background das flags que vao pro run.py
BACKGROUND=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--background" ]; then BACKGROUND=1; else ARGS+=("$a"); fi
done

if ! docker ps --filter "name=$CONTAINER" --format '{{.Names}}' | grep -q .; then
  echo "Container '$CONTAINER' nao esta rodando. Suba: docker compose up -d" >&2
  exit 1
fi

echo "== Scraping leiloeiros (banco -> Postgres) =="
echo "  Container: $CONTAINER"
echo "  Comando  : python run.py scrape-leiloeiros-db ${ARGS[*]:-}"
echo "  Log      : $LOG"

CMD=(docker exec "$CONTAINER" python run.py scrape-leiloeiros-db "${ARGS[@]:-}")

if [ "$BACKGROUND" = "1" ]; then
  nohup "${CMD[@]}" >"$LOG" 2>&1 &
  echo "OK: rodando em background (pid $!). Acompanhe: tail -f \"$LOG\""
else
  "${CMD[@]}" 2>&1 | tee "$LOG"
  echo "Concluido. Log em $LOG"
fi
