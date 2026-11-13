@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment is missing. Run setup.bat first.
    pause
    exit /b 1
)

if not exist ".youtube_credentials\client_secret.json" (
    echo [ERROR] Missing .youtube_credentials\client_secret.json
    echo Download a Google OAuth Desktop app client JSON and save it with that name.
    pause
    exit /b 1
)

echo Opening the one-time Google authorization flow. This does not upload a video.
".venv\Scripts\python.exe" "src\youtube_upload.py" --action authorize
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo [FAILED] YouTube authorization did not complete.
    pause
)
exit /b %RESULT%
