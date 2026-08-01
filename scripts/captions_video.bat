@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0.."
title News Studio - Basic SRT Captions

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

set "VIDEO_NUMBER=%~1"
if not defined VIDEO_NUMBER (
    for %%I in ("%~dp0..\..") do set "PROJECTS_ROOT=%%~fI"
    for /f %%N in ('powershell.exe -NoProfile -Command "$max=0; foreach($d in (Get-ChildItem -LiteralPath '%PROJECTS_ROOT%' -Directory)){ if($d.Name -match '^Video ([0-9]+)$'){ $n=[int]$Matches[1]; if($n -gt $max){ $max=$n } } }; $max"') do set "VIDEO_NUMBER=%%N"
)

if not defined VIDEO_NUMBER (
    echo [HATA] Video numarasi bulunamadi.
    pause
    exit /b 1
)

echo Video %VIDEO_NUMBER% icin temel SRT altyazilari hazirlaniyor...
".venv\Scripts\python.exe" "src\caption_stage.py" --video %VIDEO_NUMBER% --action generate
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
    echo Altyazilar hazir: Video %VIDEO_NUMBER%\06_captions\captions.srt
) else (
    echo [HATA] Altyazi asamasi tamamlanamadi.
)
pause
exit /b %RESULT%
