@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
call "news-studio.bat" interactive
exit /b %ERRORLEVEL%
