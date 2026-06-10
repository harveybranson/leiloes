# rodar_leiloeiros_db.ps1
# =============================================================================
# Chama o scraping dos leiloeiros CADASTRADOS NO BANCO do /admin (tabela
# `leiloeiros`, ~954 sites unicos) -> grava no Postgres do site (leilao_db).
#
# Segue a "escada de robustez" do playbook (captura_dados_leiloes_playbook.md /
# captura_dados_leiloes_v2.md): o scraper generico tenta, em ordem, JSON embutido
# -> API interna -> HTML renderizado -> Playwright (tiers 3->5). Ritmo gentil
# (--delay) e a evasao mais eficaz; so se sobe de tier quando bloqueado.
#
# Pre-requisito: o stack docker no ar (leilao_api, leilao_postgres). Suba com:
#   docker compose up -d
#
# Uso (PowerShell, NAO precisa Admin):
#   powershell -ExecutionPolicy Bypass -File .\rodar_leiloeiros_db.ps1
#
# Exemplos:
#   # todos os ~954 sites (varias horas), em foreground com log:
#   .\rodar_leiloeiros_db.ps1
#   # so SP, no maximo 50 sites, rapido (sem Playwright):
#   .\rodar_leiloeiros_db.ps1 -UF SP -Limite 50 -SemPlaywright
#   # so a fatia do dia (cobre tudo em 7 execucoes) — o que o agendador usa:
#   .\rodar_leiloeiros_db.ps1 -Fatia 2 -Fatias 7
#   # em background (volta o prompt na hora; acompanhe pelo log):
#   .\rodar_leiloeiros_db.ps1 -Background
#   # rodar e, ao terminar, normalizar/classificar (so foreground):
#   .\rodar_leiloeiros_db.ps1 -Pipeline
# =============================================================================

param(
    [string] $UF           = "",      # filtra por UF (ex: SP). Vazio = todas.
    [int]    $Limite       = 0,       # teto de sites. 0 = todos.
    [int]    $Fatia        = -1,      # indice da fatia (0..Fatias-1). -1 = sem fatiar.
    [int]    $Fatias       = 0,       # total de fatias. 0 = sem fatiar.
    [int]    $MaxPaginas   = 3,       # paginas de listagem por site.
    [double] $Delay        = 2.0,     # segundos entre sites (educacao > evasao).
    [switch] $SemPlaywright,          # desativa Playwright (mais rapido, perde JS-heavy).
    [switch] $Background,             # roda detached; volta o prompt na hora.
    [switch] $Pipeline,               # ao terminar, roda normalizar/classificar (so foreground).
    [string] $Container    = "leilao_api"
)

$ErrorActionPreference = "Stop"
$ProjDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogsDir = Join-Path $ProjDir "logs"
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }
$Stamp   = Get-Date -Format "yyyy-MM-dd_HHmm"
$LogFile = Join-Path $LogsDir "leiloeiros_db_$Stamp.log"
$ErrFile = Join-Path $LogsDir "leiloeiros_db_$Stamp.err.log"

# 1) Container no ar?
$running = (docker ps --filter "name=$Container" --format "{{.Names}}")
if (-not $running) {
    throw "Container '$Container' nao esta rodando. Suba o stack:  docker compose up -d"
}

# 2) Monta os argumentos do comando run.py
$cmd = @("run.py", "scrape-leiloeiros-db")
if ($UF)            { $cmd += @("--uf", $UF) }
if ($Limite -gt 0)  { $cmd += @("--limite", "$Limite") }
if ($Fatias -gt 0)  {
    $cmd += @("--fatias", "$Fatias")
    if ($Fatia -ge 0) { $cmd += @("--fatia", "$Fatia") }
}
$cmd += @("--max-paginas", "$MaxPaginas", "--delay", "$Delay")
if ($SemPlaywright) { $cmd += "--sem-playwright" }

$execArgs = @("exec", $Container, "python") + $cmd

Write-Host ""
Write-Host "== Scraping leiloeiros (banco -> Postgres) ==" -ForegroundColor Cyan
Write-Host "  Container : $Container"
Write-Host "  Comando   : python $($cmd -join ' ')"
Write-Host "  Log       : $LogFile"
Write-Host ""

# 3) Executa
if ($Background) {
    Start-Process -FilePath "docker" -ArgumentList $execArgs `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile -NoNewWindow | Out-Null
    Write-Host "OK: rodando em background. Acompanhe com:" -ForegroundColor Green
    Write-Host "  Get-Content `"$LogFile`" -Wait -Tail 20"
    Write-Host ""
    Write-Host "Resumo final (sites/inseridos/atualizados/sem_dados/erros) aparece no fim do log."
    return
}

# Foreground: mostra na tela e salva no log ao mesmo tempo.
& docker @execArgs 2>&1 | Tee-Object -FilePath $LogFile

# 4) Pos-pipeline opcional (normaliza cidades, separa produtos, classifica, expira encerrados)
if ($Pipeline) {
    Write-Host ""
    Write-Host "== Pos-pipeline ==" -ForegroundColor Cyan
    foreach ($step in @("normalizar-cidades", "separar-produtos", "classificar", "devoltaparaofuturo")) {
        Write-Host "-> run.py $step"
        & docker exec $Container python run.py $step
    }
}

Write-Host ""
Write-Host "Concluido. Log salvo em $LogFile" -ForegroundColor Green
