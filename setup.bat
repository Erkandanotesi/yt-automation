@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python Launcher is missing. Install Python 3.11 or newer.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" py -3 -m venv ".venv"
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :failed

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\install_ffmpeg.ps1"
if errorlevel 1 goto :failed

if not exist ".env" copy /y ".env.example" ".env" >nul
if not exist "config\feeds.opml" copy /y "config\feeds_TEMPLATE.opml" "config\feeds.opml" >nul
if not exist "config\channel_profile.txt" copy /y "config\channel_profile_TEMPLATE.txt" "config\channel_profile.txt" >nul

echo Setup complete. Configure .env, then run create_video.bat.
pause
exit /b 0

:failed
echo [ERROR] Setup failed. Check the output above.
pause
exit /b 1
