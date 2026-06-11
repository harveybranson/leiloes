#!/usr/bin/env bash
# SessionStart hook — prepara o ambiente (inclusive Claude Code na web) para rodar os
# scrapers e o gate de qualidade. Idempotente e tolerante a falha: nunca derruba a sessão.
# Referência: captura_dados_leiloes_master.md (Parte IX/X).
set +e
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}" || exit 0

echo "[session-setup] preparando ambiente de scraping de leilões..."

PY="${PYTHON:-python3}"

# 1) Dependências Python do projeto.
if [ -f requirements.txt ]; then
  $PY -m pip install -q -r requirements.txt 2>/dev/null \
    && echo "[session-setup] requirements.txt OK" \
    || echo "[session-setup] aviso: falha ao instalar requirements (sem rede?)"
fi

# 2) Playwright + Chromium (usados por scraper_detalhe.py e afins). Pesado: só se faltar.
if ! $PY -c "import playwright" 2>/dev/null; then
  $PY -m pip install -q playwright 2>/dev/null && echo "[session-setup] playwright instalado"
fi
if $PY -c "import playwright" 2>/dev/null; then
  if $PY -m playwright install chromium >/dev/null 2>&1; then
    echo "[session-setup] chromium pronto (download oficial)"
  else
    # Fallback: reaproveita um Chromium já presente (CDN bloqueado). Os scrapers leem
    # PW_CHROMIUM_PATH/PW_IGNORE_HTTPS (ver scraper_detalhe.py). Persiste no profile.
    CHROME=$(ls -1 /opt/pw-browsers/chromium-*/chrome-linux/chrome 2>/dev/null | head -1)
    [ -z "$CHROME" ] && CHROME=$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || command -v chromium-browser)
    if [ -n "$CHROME" ] && [ -x "$CHROME" ]; then
      echo "[session-setup] chromium oficial indisponível — usando fallback: $CHROME"
      { echo "export PW_CHROMIUM_PATH=\"$CHROME\""; echo "export PW_IGNORE_HTTPS=1"; } >> "$HOME/.bashrc" 2>/dev/null
      export PW_CHROMIUM_PATH="$CHROME"; export PW_IGNORE_HTTPS=1
    else
      echo "[session-setup] aviso: nenhum Chromium disponível — scrapers via Playwright não rodam aqui"
    fi
  fi
fi

# 3) Smoke test das funções puras (detecta regressão de ambiente cedo).
if $PY scraper_commons.py 2>/dev/null; then
  echo "[session-setup] smoke test de scraper_commons OK"
else
  echo "[session-setup] aviso: smoke test de scraper_commons falhou — verifique scraper_commons.py"
fi

echo "[session-setup] pronto. Gate de qualidade: python finalizar_coleta.py --desde hoje"
exit 0
