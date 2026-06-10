# agendar_semanal.ps1
# Registra a tarefa "LeiloesScrapingSemanal" no Windows Task Scheduler.
# Executa rodar_semanal.py toda segunda-feira as 09:00 (horario local).
#
# Uso (PowerShell normal, NAO precisa Admin):
#   powershell -ExecutionPolicy Bypass -File .\agendar_semanal.ps1
#
# Para remover a tarefa:
#   Unregister-ScheduledTask -TaskName "LeiloesScrapingSemanal" -Confirm:$false

$ErrorActionPreference = "Stop"

$ProjDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script    = Join-Path $ProjDir "rodar_semanal.py"
$LogsDir   = Join-Path $ProjDir "logs"
$WrapLog   = Join-Path $LogsDir "task_scheduler.log"

if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

# Descobre o python.exe instalado (prioriza py launcher, depois python no PATH).
$Python = $null
try { $Python = (Get-Command py -ErrorAction Stop).Source } catch {}
if (-not $Python) {
    try { $Python = (Get-Command python -ErrorAction Stop).Source } catch {}
}
if (-not $Python) {
    throw "Nao encontrei python.exe nem py.exe no PATH. Instale o Python ou ajuste manualmente o caminho."
}

$TaskName  = "LeiloesScrapingSemanal"
$ArgsLine  = "`"$Script`""

# Trigger: toda segunda-feira as 09:00 local.
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9:00am

# Action: roda no diretorio do projeto, redireciona stdout/stderr para wrapper log.
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $ArgsLine `
    -WorkingDirectory $ProjDir

# Settings: rodar mesmo na bateria, parar apos 12h, nao rodar se ja em execucao,
# tentar mais tarde se o PC estava desligado.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew

# Principal: roda como o usuario atual, sem privilegios elevados.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Remove tarefa antiga se existir (idempotente).
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Roda todos os scrapers estaveis (AC, AL, BA, CE, MA, PA, PB, PE, PI, RN, RR-RO, SE), gera relatorio em relatorios/ e anexa sugestoes em captura_dados_leiloes_v2.md." | Out-Null

Write-Host ""
Write-Host "OK: tarefa '$TaskName' registrada."
Write-Host "  Python:    $Python"
Write-Host "  Script:    $Script"
Write-Host "  Quando:    Segunda-feira, 09:00 (horario local)"
Write-Host "  Logs:      $LogsDir\semanal_YYYY-MM-DD.log"
Write-Host "  Relatorio: $ProjDir\relatorios\relatorio_semanal_YYYY-MM-DD.md"
Write-Host ""
Write-Host "Para rodar agora manualmente:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Para ver historico/proxima execucao:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
