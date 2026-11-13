@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0.."
title News Studio - Motion, TV, Glitch and Music

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

set "VIDEO_NUMBER=%~1"
if not defined VIDEO_NUMBER (
    for %%I in ("%~dp0..\..\Projects") do set "PROJECTS_ROOT=%%~fI"
    for /f %%N in ('powershell.exe -NoProfile -Command "$max=0; foreach($d in (Get-ChildItem -LiteralPath '%PROJECTS_ROOT%' -Directory)){ if($d.Name -match '^Video ([0-9]+)$'){ $n=[int]$Matches[1]; if($n -gt $max){ $max=$n } } }; $max"') do set "VIDEO_NUMBER=%%N"
)

if not defined VIDEO_NUMBER (
    echo [HATA] Video numarasi bulunamadi.
    pause
    exit /b 1
)

echo Video %VIDEO_NUMBER% icin Milestone 6 efekt ve ses plani hazirlaniyor...
".venv\Scripts\python.exe" "src\effects_stage.py" --video %VIDEO_NUMBER% --action generate
if errorlevel 1 (
    echo [HATA] Milestone 6 tamamlanamadi. Once separator, storyboard, medya ve altyazi asamalarini tamamla.
    pause
    exit /b 1
)

echo 60 saniyelik kontrol videosu render ediliyor...
".venv\Scripts\python.exe" "src\effects_stage.py" --video %VIDEO_NUMBER% --action reference-proof --proof-seconds 60
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" (
    echo Hazir: Video %VIDEO_NUMBER%\07_effects\milestone6_reference_60s.mp4
) else (
    echo [HATA] Referans videosu render edilemedi.
)
pause
exit /b %RESULT%
