@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"

echo ============================================================
echo RSS Haber Otomasyonu - Kurulum
echo ============================================================
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo [HATA] Python Launcher bulunamadi.
    echo Python 3.11 veya daha yeni bir surum kur ve "Add Python to PATH" secenegini isaretle.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Sanal ortam olusturuluyor...
    py -3 -m venv ".venv"
    if errorlevel 1 (
        echo [HATA] Sanal ortam olusturulamadi.
        pause
        exit /b 1
    )
)

echo Paketler yukleniyor...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :install_error

".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :install_error

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo .env dosyasi olusturuldu.
)

if not exist "feeds.opml" (
    copy /y "feeds_TEMPLATE.opml" "feeds.opml" >nul
    echo feeds.opml sablonu olusturuldu.
)

if not exist "channel_profile.txt" (
    copy /y "channel_profile_TEMPLATE.txt" "channel_profile.txt" >nul
)

echo.
echo Kurulum tamamlandi.
echo.
echo Simdi:
echo 1. .env dosyasina Gemini veya OpenAI API anahtarini yaz.
echo 2. FeedFlow'dan OPML disa aktar ve feeds.opml dosyasinin uzerine kaydet.
echo 3. create_video.bat dosyasini calistir.
echo.
pause
exit /b 0

:install_error
echo.
echo [HATA] Python paketleri yuklenemedi.
echo Internet baglantini ve Python kurulumunu kontrol et.
pause
exit /b 1
