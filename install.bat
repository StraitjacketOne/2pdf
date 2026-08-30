@echo off
setlocal
cd /d "%~dp0"

echo === 2pdf Installation ===
echo.

echo [1/2] Abhaengigkeit reportlab...
python -m pip install -r "%~dp0requirements.txt" -q
if errorlevel 1 (
    echo FEHLER: pip install fehlgeschlagen.
    echo Ist Python im PATH?  python --version
    pause
    exit /b 1
)

echo [2/2] Kontextmenue registrieren...
python "%~dp0\2pdf.py" --install
if errorlevel 1 (
    echo FEHLER: Kontextmenue-Installation fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo Fertig. Rechtsklick auf .txt oder Bild (auch Mehrfachauswahl) -^> "2pdf"
echo Log: %LOCALAPPDATA%\2pdf\2pdf.log
echo.
pause
endlocal
