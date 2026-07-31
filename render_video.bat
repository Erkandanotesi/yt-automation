@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title News Studio - Milestone 8 Render

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

set "VIDEO_NUMBER=%~1"
if not defined VIDEO_NUMBER (
    echo Kullanim: render_video.bat 5
    pause
    exit /b 1
)

echo Video %VIDEO_NUMBER% tam preview ve final render baslatiliyor...
".venv\Scripts\python.exe" "render_stage.py" --video %VIDEO_NUMBER% --action render
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" (
    echo Milestone 8 tamamlandi.
) else (
    echo [HATA] Render tamamlanamadi.
)
pause
exit /b %RESULT%
