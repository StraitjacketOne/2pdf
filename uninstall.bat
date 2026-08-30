@echo off
setlocal
cd /d "%~dp0"

echo === 2pdf Deinstallation ===
echo.
python "%~dp0\2pdf.py" --uninstall
if errorlevel 1 (
    echo FEHLER: Deinstallation fehlgeschlagen.
    pause
    exit /b 1
)

echo.
pause
endlocal
