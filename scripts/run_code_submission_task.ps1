$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"

$logDirectory = Join-Path $projectRoot ".orchestrator\submissions"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "submission-$timestamp.log"
$latestLogPath = Join-Path $logDirectory "submission-latest.log"

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$output = & $python "scripts/submit_code.py" `
    "--name" "nikitaa_r_r" `
    "--server" "https://play.theemailgame.com" 2>&1
$exitCode = $LASTEXITCODE
$output | Tee-Object -FilePath $logPath
$output | Set-Content -Path $latestLogPath
exit $exitCode
