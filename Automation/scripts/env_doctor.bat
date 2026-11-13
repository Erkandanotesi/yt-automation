@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment is missing. Run setup.bat first.
    exit /b 1
)
".venv\Scripts\python.exe" "src\env_doctor.py" %*
exit /b %ERRORLEVEL%
