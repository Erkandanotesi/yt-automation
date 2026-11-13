@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "AUTOMATION_DIR=%CD%"
set "APP_PROJECT=%AUTOMATION_DIR%\NewsStudio.App\NewsStudio.App.csproj"
set "APP_EXE=%AUTOMATION_DIR%\NewsStudio.App\bin\Release\net10.0-windows\NewsStudio.App.exe"
set "BUILD_LOG=%AUTOMATION_DIR%\NewsStudio.App\build.log"

tasklist /FI "IMAGENAME eq NewsStudio.App.exe" 2>nul | find /I "NewsStudio.App.exe" >nul
if not errorlevel 1 (
    echo News Studio is already open.
    timeout /t 2 /nobreak >nul
    exit /b 0
)

dotnet build "%APP_PROJECT%" --configuration Release --nologo > "%BUILD_LOG%" 2>&1
if errorlevel 1 (
    if defined NEWS_STUDIO_HIDDEN_LAUNCH exit /b 1
    echo.
    echo [FAILED] The app could not be built. Review:
    echo %BUILD_LOG%
    pause
    exit /b 1
)

start "" "%APP_EXE%"
exit /b 0
