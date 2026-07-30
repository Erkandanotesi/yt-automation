@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"
title News Studio - Offline Program Test

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

echo [1/3] Sistem on kontrolu...
".venv\Scripts\python.exe" "preflight.py"
if errorlevel 1 goto :failed

echo.
echo [2/3] Ucretsiz yerel testler...
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto :failed

echo.
echo [3/3] Mevcut medya indeksi...
".venv\Scripts\python.exe" "media_index.py" --status
if errorlevel 1 goto :failed

echo.
echo [OK] Programin mevcut yerel durumu hazir.
echo Bu test OpenAI veya ElevenLabs API kotasi kullanmadi.
pause
exit /b 0

:failed
echo.
echo [HATA] Yukaridaki asamalardan en az biri basarisiz oldu.
pause
exit /b 1
