# Sincroniza dados de scraping com o GitHub (commit + push).
# Uso: .\scripts\sync-github.ps1
# Agendado via Task Scheduler a cada 4 horas (setup-scheduled-sync.ps1).

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

# git escreve avisos (LF/CRLF) em stderr; nao tratar como erro fatal
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"

$logDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "sync-github.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

try {
    Write-Log "Iniciando sync para GitHub..."

    $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    if (-not $branch) { throw "Nao e um repositorio git: $repoRoot" }

    # Todos os arquivos do repositorio (respeita .gitignore)
    git add -A 2>&1 | Out-Null

    $ErrorActionPreference = $prevEap

    $status = git status --porcelain
    if (-not $status) {
        Write-Log "Nenhuma alteracao para enviar."
        exit 0
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    git commit -m "chore: sync automatico ($timestamp)"
    if ($LASTEXITCODE -ne 0) { throw "Falha no git commit" }

    git push origin $branch
    if ($LASTEXITCODE -ne 0) { throw "Falha no git push para origin/$branch" }

    Write-Log "Push concluido em origin/$branch."
    exit 0
}
catch {
    Write-Log "ERRO: $_"
    exit 1
}
