# Roda o gate de qualidade pós-coleta: snapshot + regressão + cobertura + dashboard.
# Chamado pela tarefa agendada (setup-scheduled-quality.ps1) ou manualmente:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run-quality.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

# Usa o python do venv se existir, senão o do PATH.
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# --desde hoje: avalia só o que foi importado hoje (gate não trava em dia sem coleta).
& $py "finalizar_coleta.py" --desde hoje
$code = $LASTEXITCODE

$log = Join-Path $repoRoot "qualidade.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content $log "$stamp  finalizar_coleta exit=$code"

if ($code -ne 0) {
    Write-Host "GATE REPROVADO (exit=$code) — banco NÃO deve ser publicado." -ForegroundColor Red
} else {
    Write-Host "Gate OK — dashboard_frescor.html atualizado." -ForegroundColor Green
}
exit $code
