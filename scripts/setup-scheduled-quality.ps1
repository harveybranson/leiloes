# Registra tarefa agendada no Windows: roda o gate de qualidade 1x/dia.
# Alimenta o histórico de cobertura (cobertura_historico.jsonl) p/ a detecção de
# regressão e regenera o dashboard. Execute uma vez:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-scheduled-quality.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$qualityScript = Join-Path $repoRoot "scripts\run-quality.ps1"
$taskName = "Leiloes-Qualidade-Diaria"

if (-not (Test-Path $qualityScript)) {
    throw "Script nao encontrado: $qualityScript"
}

$taskCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$qualityScript`""

schtasks /Delete /TN $taskName /F *> $null

# /sc daily /st 23:30 = todo dia às 23:30 (após as coletas do dia)
$result = schtasks /Create `
    /TN $taskName `
    /TR $taskCmd `
    /SC DAILY `
    /ST 23:30 `
    /F 2>&1

if ($LASTEXITCODE -ne 0) {
    throw "Falha ao criar tarefa agendada: $result"
}

Write-Host ""
Write-Host "Tarefa agendada criada: $taskName" -ForegroundColor Green
Write-Host "  Intervalo: diario as 23:30" -ForegroundColor Gray
Write-Host "  Script:    $qualityScript" -ForegroundColor Gray
Write-Host ""
Write-Host "Testar agora:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$qualityScript`"" -ForegroundColor Gray
