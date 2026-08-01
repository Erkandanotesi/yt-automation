@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
call "news-studio.bat" interactive
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
