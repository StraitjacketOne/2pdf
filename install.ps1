# 2pdf — Kontextmenü einrichten (aktueller Benutzer, kein Admin nötig)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "Installiere reportlab (falls nötig)..."
python -m pip install -r requirements.txt -q

Write-Host "Registriere Kontextmenü..."
python "$Root\2pdf.py" --install

if ($LASTEXITCODE -ne 0) {
    Write-Host "FEHLER bei der Installation." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Fertig. Rechtsklick auf .txt oder Bild (auch Mehrfachauswahl) → '2pdf'" -ForegroundColor Green
Write-Host "Log: $env:LOCALAPPDATA\2pdf\2pdf.log"
