@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
title News Studio - Reset YouTube Upload Date

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

echo YouTube next-upload date manager
echo.
".venv\Scripts\python.exe" "src\reset_youtube_schedule.py"
set "RESULT=%ERRORLEVEL%"
echo.

if "%RESULT%"=="2" (
    echo [NOT CHANGED] The requested date was invalid or stale.
    pause
    exit /b %RESULT%
)

if not "%RESULT%"=="0" (
    echo [FAILED] The YouTube schedule state was not changed.
    pause
    exit /b %RESULT%
)

echo [DONE] The next upload date has been reset.
pause
exit /b 0
