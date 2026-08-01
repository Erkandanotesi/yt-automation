@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0.."
title News Studio - Archive Image Selection

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

set "VIDEO_NUMBER=%~1"
set "STORY_NUMBER=%~2"
if not defined VIDEO_NUMBER (
    echo [HATA] Video numarasi gerekli.
    pause
    exit /b 1
)

echo.
echo Medya arsivi once artimli olarak yeniden taranacak.
echo AI'ye tum klasor agaci degil, her zaman araligi icin yalnizca kisa aday listesi gider.
echo.
echo   [1] gpt-5.6-luna ile son secim
echo   [0] Ucretsiz yerel puanlayici ile secim
echo.
set "MEDIA_CHOICE="
set /p "MEDIA_CHOICE=Secim [0/1]: "

set "STORY_ARG="
if defined STORY_NUMBER set "STORY_ARG=--story %STORY_NUMBER%"

if "%MEDIA_CHOICE%"=="1" (
    ".venv\Scripts\python.exe" "src\media_selection_stage.py" --video %VIDEO_NUMBER% %STORY_ARG% --confirm-ai-cost
    exit /b %ERRORLEVEL%
)
if "%MEDIA_CHOICE%"=="0" (
    ".venv\Scripts\python.exe" "src\media_selection_stage.py" --video %VIDEO_NUMBER% %STORY_ARG% --offline
    exit /b %ERRORLEVEL%
)

echo [HATA] Gecersiz secim.
exit /b 1
