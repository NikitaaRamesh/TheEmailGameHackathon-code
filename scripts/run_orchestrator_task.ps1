$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$env:EMAIL_GAME_NO_BROWSER = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"

$logDirectory = Join-Path $projectRoot ".orchestrator\scheduler-logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ("orchestrator-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$arguments = @(
    "scripts/orchestrator.py",
    "nikitaa_r_r",
    "--module", "my_agent.py",
    "--server", "https://play.theemailgame.com",
    "--model", "gpt-4.1-mini",
    "--refactor-model", "gpt-4.1-mini",
    "--max-matches", "0",
    "--minimum-budget-left", "10",
    "--top-rank-hold-rank", "2",
    "--top-rank-hold-seconds", "480",
    "--top-rank-poll-seconds", "30"
)

& $python @arguments *>> $logPath
exit $LASTEXITCODE
