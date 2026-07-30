@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FAIL] Python environment not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "preflight.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%

