@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
title News Studio - Clean News History
set "NONINTERACTIVE=0"
if /I "%~1"=="--yes" set "NONINTERACTIVE=1"
if /I "%~1"=="--dry-run" set "NONINTERACTIVE=1"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "src\clean_news_history.py" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
    echo [FAILED] News history was not cleaned.
    echo This window will remain open so you can review the error.
    if "%NONINTERACTIVE%"=="1" exit /b %RESULT%
    pause
    exit /b %RESULT%
)

echo [DONE] News-history cleanup finished.
if "%NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
