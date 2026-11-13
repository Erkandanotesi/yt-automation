@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "FORCE_INSTALL=0"
set "NO_PAUSE=0"
if /I "%~1"=="--upgrade" set "FORCE_INSTALL=1"
if /I "%~1"=="--reinstall" set "FORCE_INSTALL=1"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~2"=="--no-pause" set "NO_PAUSE=1"
if "%NEWS_STUDIO_NO_PAUSE%"=="1" set "NO_PAUSE=1"

echo ==================================================
echo Tech News Studio - Portable Setup
echo ==================================================
echo.

REM Resolve workspace root (parent of Automation\)
for %%I in ("%~dp0..") do set "WORKSPACE_ROOT=%%~fI"

REM 0. Ensure Git trusts the moved workspace folder (if Git is installed)
where git >nul 2>nul
if %ERRORLEVEL%==0 (
    if exist "%WORKSPACE_ROOT%\.git" (
        git -C "%WORKSPACE_ROOT%" rev-parse --is-inside-work-tree >nul 2>nul
        if !ERRORLEVEL! NEQ 0 (
            git config --global --add safe.directory "%WORKSPACE_ROOT:\=/%" >nul 2>nul
        )
    )
)

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
        if not "%NO_PAUSE%"=="1" pause
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
        if not "%NO_PAUSE%"=="1" pause
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

set "REQ_MARKER=.venv\.requirements.sha256"
set "NEED_PIP=0"
if "%NEED_CREATE_VENV%"=="1" set "NEED_PIP=1"
if "%FORCE_INSTALL%"=="1" set "NEED_PIP=1"
if not exist "%REQ_MARKER%" set "NEED_PIP=1"
if "%NEED_PIP%"=="0" (
    ".venv\Scripts\python.exe" -c "import hashlib, pathlib; p = pathlib.Path('requirements.txt').read_bytes(); m = pathlib.Path(r'%REQ_MARKER%').read_text(encoding='utf-8').strip(); raise SystemExit(0 if hashlib.sha256(p).hexdigest() == m else 1)" >nul 2>nul
    if !ERRORLEVEL! NEQ 0 set "NEED_PIP=1"
)

if "%NEED_PIP%"=="1" (
    echo.
    echo Installing Python requirements...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to install Python dependencies.
        if not "%NO_PAUSE%"=="1" pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -c "import hashlib, pathlib; pathlib.Path(r'%REQ_MARKER%').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(), encoding='utf-8')"
) else (
    echo [OK] Python dependencies already match requirements.txt - use --upgrade to reinstall.
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

set "NEED_CHROMIUM=0"
".venv\Scripts\python.exe" -c "import sys; sys.path.insert(0, r'%WORKSPACE_ROOT%\motion_engine\vendor\python'); import playwright" >nul 2>nul
if %ERRORLEVEL%==0 (
    if "%FORCE_INSTALL%"=="1" (
        set "NEED_CHROMIUM=1"
    ) else (
        ".venv\Scripts\python.exe" -c "import os, pathlib, sys; sys.path.insert(0, r'%WORKSPACE_ROOT%\motion_engine\vendor\python'); os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', r'%WORKSPACE_ROOT%\motion_engine\cache\browsers'); from playwright.sync_api import sync_playwright; pw = sync_playwright().start(); exe = pathlib.Path(pw.chromium.executable_path); pw.stop(); raise SystemExit(0 if exe.exists() else 1)" >nul 2>nul
        if !ERRORLEVEL! NEQ 0 set "NEED_CHROMIUM=1"
    )
    if "!NEED_CHROMIUM!"=="1" (
        echo Ensuring Playwright Chromium browser is installed...
        ".venv\Scripts\python.exe" -c "import os, runpy, sys; sys.path.insert(0, r'%WORKSPACE_ROOT%\motion_engine\vendor\python'); os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', r'%WORKSPACE_ROOT%\motion_engine\cache\browsers'); sys.argv = ['playwright', 'install', 'chromium']; runpy.run_module('playwright', run_name='__main__')"
        if !ERRORLEVEL! NEQ 0 (
            echo [WARN] Playwright Chromium installation failed or skipped. Motion cards may require manual install.
        )
    ) else (
        echo [OK] Playwright Chromium browser is present.
    )
) else (
    echo [INFO] Playwright package not found; motion_engine will use local browser fallback if configured.
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
    echo [INIT] Created Automation\.env from .env.example - add your API keys.
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
    echo Building NewsStudio.App - Release...
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
if not "%NO_PAUSE%"=="1" pause
