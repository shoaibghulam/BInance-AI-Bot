# The Trader — local launcher (Windows PowerShell)
# Usage:  ./run.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "[The Trader] Creating virtual environment..."
    python -m venv .venv
    ./.venv/Scripts/python.exe -m pip install --upgrade pip
    ./.venv/Scripts/python.exe -m pip install -r requirements.txt
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[The Trader] Created .env from template. Add your Binance TESTNET keys, then re-run." -ForegroundColor Yellow
}

$port = 8000
Write-Host "[The Trader] Dashboard -> http://127.0.0.1:$port   (Ctrl+C to stop)" -ForegroundColor Green
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $port
