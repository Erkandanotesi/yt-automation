@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0.."
title News Studio - Media Rescan

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] Python ortami bulunamadi. setup.bat dosyasini calistir.
    pause
    exit /b 1
)

echo Medya klasorleri artimli olarak yeniden taraniyor...
".venv\Scripts\python.exe" "src\media_index.py" --scan
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
    echo Tarama tamamlandi. Dosyalar yeniden adlandirilmadi veya degistirilmedi.
) else (
    echo [HATA] Medya taramasi tamamlanamadi.
)
pause
exit /b %RESULT%
