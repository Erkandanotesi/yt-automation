@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==================================================
echo Tech News Studio - Portable Setup
echo ==================================================
echo.

REM Resolve workspace root (parent of Automation\)
for %%I in ("%~dp0..") do set "WORKSPACE_ROOT=%%~fI"

REM 1. Ensure expected workspace folders exist
for %%D in (
    "%WORKSPACE_ROOT%\Projects"
    "%WORKSPACE_ROOT%\Projects\_Archive"
    "%WORKSPACE_ROOT%\Assets\Media"
    "%WORKSPACE_ROOT%\Assets\Media (No-Copyright)"
    "%WORKSPACE_ROOT%\Assets\Audio"
    "%WORKSPACE_ROOT%\Assets\Branding"
    "%WORKSPACE_ROOT%\models"
    "%~dp0data"
    "%~dp0data\env_backups"
    "%~dp0tools"
) do (
    if not exist "%%~D" mkdir "%%~D"
)

REM 2. Find system Python 3 (for creating or repairing .venv)
set "SYS_PY="
py -3 -c "import sys; print(sys.executable)" >nul 2>nul
if %ERRORLEVEL%==0 (
    set "SYS_PY=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 set "SYS_PY=python"
)

REM 3. Validate, repair, or create .venv
set "NEED_CREATE_VENV=0"
if exist ".venv\Scripts\python.exe" (
    if defined SYS_PY (
        %SYS_PY% "scripts\repair_venv.py" --venv ".venv"
        if !ERRORLEVEL! NEQ 0 set "NEED_CREATE_VENV=1"
    ) else (
        ".venv\Scripts\python.exe" "scripts\repair_venv.py" --venv ".venv"
        if !ERRORLEVEL! NEQ 0 set "NEED_CREATE_VENV=1"
    )
) else (
    set "NEED_CREATE_VENV=1"
)

if "%NEED_CREATE_VENV%"=="1" (
    if not defined SYS_PY (
        echo [ERROR] Python 3 is required but was not found on PATH or via `py -3`.
        echo Install Python 3.11+ from python.org and re-run setup.bat.
        pause
        exit /b 1
    )
    if exist ".venv" (
        echo [RECREATE] Removing broken .venv directory...
        rmdir /s /q ".venv"
    )
    echo [CREATE] Creating Automation\.venv...
    %SYS_PY% -m venv ".venv"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to create .venv.
        pause
        exit /b 1
    )
)

REM Optional Qwen TTS venv repair check (if present)
if exist ".venv_qwen\Scripts\python.exe" (
    if defined SYS_PY (
        %SYS_PY% "scripts\repair_venv.py" --venv ".venv_qwen"
    ) else (
        ".venv_qwen\Scripts\python.exe" "scripts\repair_venv.py" --venv ".venv_qwen"
    )
    if !ERRORLEVEL! NEQ 0 (
        echo [WARN] Automation\.venv_qwen base Python is missing on this machine.
        echo        Re-create .venv_qwen manually if using local Qwen3-TTS.
    )
)

echo.
echo Installing Python requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to install Python dependencies.
    pause
    exit /b 1
)

REM 4. Motion engine JS vendor assets + Playwright Chromium
if exist "%WORKSPACE_ROOT%\motion_engine\setup_env.py" (
    if not exist "%WORKSPACE_ROOT%\motion_engine\vendor\gsap.min.js" (
        echo Downloading motion_engine JS assets...
        ".venv\Scripts\python.exe" "%WORKSPACE_ROOT%\motion_engine\setup_env.py"
    ) else if not exist "%WORKSPACE_ROOT%\motion_engine\vendor\lucide.min.js" (
        echo Downloading motion_engine JS assets...
        ".venv\Scripts\python.exe" "%WORKSPACE_ROOT%\motion_engine\setup_env.py"
    ) else (
        echo [OK] motion_engine JS vendor assets present.
    )
)

echo Ensuring Playwright Chromium browser is installed...
".venv\Scripts\python.exe" -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Playwright Chromium installation failed or skipped. Motion cards may require manual install.
)

REM 5. Local ffmpeg/ffprobe
echo.
echo Checking local ffmpeg / ffprobe...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install_ffmpeg.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Automatic ffmpeg check returned an error. Ensure ffmpeg.exe and ffprobe.exe exist in tools\ffmpeg\ or PATH.
)

REM 6. Initialize user configuration templates if missing
if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo [INIT] Created Automation\.env from .env.example (add your API keys).
)
if not exist "config\feeds.opml" (
    if exist "config\feeds_TEMPLATE.opml" (
        copy /Y "config\feeds_TEMPLATE.opml" "config\feeds.opml" >nul
        echo [INIT] Created Automation\config\feeds.opml from feeds_TEMPLATE.opml.
    )
)
if not exist "config\channel_profile.txt" (
    if exist "config\channel_profile_TEMPLATE.txt" (
        copy /Y "config\channel_profile_TEMPLATE.txt" "config\channel_profile.txt" >nul
        echo [INIT] Created Automation\config\channel_profile.txt from channel_profile_TEMPLATE.txt.
    )
)

REM 7. Build desktop UI if .NET SDK is available
where dotnet >nul 2>nul
if %ERRORLEVEL%==0 (
    echo.
    echo Building NewsStudio.App (Release)...
    dotnet build "NewsStudio.App\NewsStudio.App.csproj" --configuration Release --nologo -v minimal
    if !ERRORLEVEL! NEQ 0 (
        echo [WARN] NewsStudio.App build failed. CLI workflows remain usable.
    )
) else (
    echo [INFO] .NET SDK not found on PATH; skipping NewsStudio.App build.
)

REM 8. Run diagnostics
echo.
echo Running environment checks...
".venv\Scripts\python.exe" "src\env_doctor.py"
echo.
".venv\Scripts\python.exe" "src\preflight.py"

echo.
echo Setup complete! Launch the app via create_video_app.bat or CLI via create_video.bat.
pause
