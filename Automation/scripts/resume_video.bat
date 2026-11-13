@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
set /p VIDEO_REFERENCE=Video name or number:
set /p CONFIRM=Type COST to permit required provider requests:
if not "%CONFIRM%"=="COST" (
    echo Cancelled before any provider request.
    pause
    exit /b 0
)
call "news-studio.bat" resume "%VIDEO_REFERENCE%" --confirm-api-costs
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
