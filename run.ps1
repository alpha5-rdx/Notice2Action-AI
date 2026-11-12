# Launch Notice2Action AI. Usage:  .\run.ps1   (optionally: .\run.ps1 -Port 8502)
param([int]$Port = 8501)

Set-Location -Path $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    if (-not (Test-Path $python)) { Write-Error "Could not create .venv. Is Python installed and on PATH?"; exit 1 }
}

& $python -c "import streamlit, google.genai, pymupdf, pytesseract, PIL, pandas, psycopg, psycopg_pool" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Error "Dependency installation failed."; exit 1 }
}

Write-Host "Starting Notice2Action AI at http://localhost:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $python -m streamlit run app.py --server.port $Port
