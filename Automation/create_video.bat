@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
call "news-studio.bat" interactive
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo [FAILED] Video production stopped with exit code %RESULT%.
    echo Review the red stage and error log above. This window will remain open.
    pause
)
exit /b %RESULT%
