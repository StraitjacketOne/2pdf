# 2pdf — Kontextmenü entfernen
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
python "$Root\2pdf.py" --uninstall
exit $LASTEXITCODE
