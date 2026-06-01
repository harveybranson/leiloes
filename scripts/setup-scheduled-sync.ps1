# Registra tarefa agendada no Windows: upload para GitHub a cada 4 horas.
# Execute uma vez como administrador (opcional) ou no seu usuario:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-scheduled-sync.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$syncScript = Join-Path $repoRoot "scripts\sync-github.ps1"
$taskName = "Leiloes-GitHub-Sync-4h"

if (-not (Test-Path $syncScript)) {
    throw "Script nao encontrado: $syncScript"
}

$taskCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$syncScript`""

schtasks /Delete /TN $taskName /F *> $null

# /sc hourly /mo 4 = a cada 4 horas
$result = schtasks /Create `
    /TN $taskName `
    /TR $taskCmd `
    /SC HOURLY `
    /MO 4 `
    /F 2>&1

if ($LASTEXITCODE -ne 0) {
    throw "Falha ao criar tarefa agendada: $result"
}

Write-Host ""
Write-Host "Tarefa agendada criada: $taskName" -ForegroundColor Green
Write-Host "  Intervalo: a cada 4 horas" -ForegroundColor Gray
Write-Host "  Script:    $syncScript" -ForegroundColor Gray
Write-Host ""
Write-Host "Testar agora:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$syncScript`"" -ForegroundColor Gray
Write-Host ""
Write-Host "Ver status:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$taskName' | Get-ScheduledTaskInfo" -ForegroundColor Gray
