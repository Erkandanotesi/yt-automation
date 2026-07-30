@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"
title News Studio - Resume Manual Separators

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

for %%I in ("%~dp0..") do set "PROJECTS_ROOT=%%~fI"
set "LATEST_VIDEO_NUMBER="
for /f %%N in ('powershell.exe -NoProfile -Command "$max=0; foreach($d in (Get-ChildItem -LiteralPath '%PROJECTS_ROOT%' -Directory)){ if($d.Name -match '^Video ([0-9]+)$'){ $n=[int]$Matches[1]; if($n -gt $max){ $max=$n } } }; $max"') do set "LATEST_VIDEO_NUMBER=%%N"

if not defined LATEST_VIDEO_NUMBER set "LATEST_VIDEO_NUMBER=0"
if "%LATEST_VIDEO_NUMBER%"=="0" (
    echo [HATA] Devam ettirilebilecek Video N klasoru bulunamadi.
    pause
    exit /b 1
)

echo Video %LATEST_VIDEO_NUMBER% manuel separator checkpoint aciliyor...
".venv\Scripts\python.exe" "manual_checkpoint.py" --video %LATEST_VIDEO_NUMBER%
set "CHECKPOINT_EXIT=%ERRORLEVEL%"

if "%CHECKPOINT_EXIT%"=="0" (
    echo.
    echo Manuel separator asamasi READY.
) else if "%CHECKPOINT_EXIT%"=="2" (
    echo.
    echo Checkpoint beklemede veya kullanici tarafindan iptal edildi.
) else (
    echo.
    echo [HATA] Checkpoint tamamlanamadi.
)

pause
exit /b %CHECKPOINT_EXIT%
