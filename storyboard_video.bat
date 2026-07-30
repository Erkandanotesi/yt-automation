@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"
title News Studio - Storyboard and AI Images

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

set "VIDEO_NUMBER=%~1"
if not defined VIDEO_NUMBER (
    for %%I in ("%~dp0..") do set "PROJECTS_ROOT=%%~fI"
    for /f %%N in ('powershell.exe -NoProfile -Command "$max=0; foreach($d in (Get-ChildItem -LiteralPath '%PROJECTS_ROOT%' -Directory)){ if($d.Name -match '^Video ([0-9]+)$'){ $n=[int]$Matches[1]; if($n -gt $max){ $max=$n } } }; $max"') do set "VIDEO_NUMBER=%%N"
)

if not defined VIDEO_NUMBER (
    echo [HATA] Video numarasi bulunamadi.
    pause
    exit /b 1
)

echo Video %VIDEO_NUMBER% icin iki gorsellik storyboard hazirlaniyor...
".venv\Scripts\python.exe" "storyboard_stage.py" --video %VIDEO_NUMBER% --action plan
if errorlevel 1 (
    echo [HATA] Storyboard hazirlanamadi.
    pause
    exit /b 1
)

echo.
echo Ucretli GPT Image 2 istekleri:
echo   [1] Pilot: yalnizca Haber 1 icin 2 orta kaliteli gorsel + 30 sn proof
echo   [2] Tam: tum haberler icin 2'ser orta kaliteli gorsel + Haber 1 proof
echo   [0] Simdilik yalnizca storyboard'u kaydet
echo.
set "IMAGE_CHOICE="
set /p "IMAGE_CHOICE=Secim [0/1/2]: "

if "%IMAGE_CHOICE%"=="0" (
    echo Storyboard kaydedildi. Ucretli gorsel istegi yapilmadi.
    exit /b 0
)
if "%IMAGE_CHOICE%"=="1" (
    ".venv\Scripts\python.exe" "storyboard_stage.py" --video %VIDEO_NUMBER% --action generate --story 1 --confirm-cost --max-requests 2
    if errorlevel 1 exit /b 1
    ".venv\Scripts\python.exe" "storyboard_stage.py" --video %VIDEO_NUMBER% --action proof --story 1
    if errorlevel 1 exit /b 1
    call "media_selection_video.bat" %VIDEO_NUMBER% 1
    if errorlevel 1 exit /b 1
    exit /b 0
)
if "%IMAGE_CHOICE%"=="2" (
    ".venv\Scripts\python.exe" "storyboard_stage.py" --video %VIDEO_NUMBER% --action generate --confirm-cost
    if errorlevel 1 exit /b 1
    ".venv\Scripts\python.exe" "storyboard_stage.py" --video %VIDEO_NUMBER% --action proof --story 1
    if errorlevel 1 exit /b 1
    call "media_selection_video.bat" %VIDEO_NUMBER%
    if errorlevel 1 exit /b 1
    exit /b 0
)

echo [HATA] Gecersiz secim.
exit /b 1
