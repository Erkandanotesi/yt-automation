@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"
title News Studio Automation
for %%I in ("%~dp0..") do set "PROJECTS_ROOT=%%~fI"

for /F "delims=" %%E in ('powershell.exe -NoProfile -Command "[char]27"') do set "ESC=%%E"
set "RESET=%ESC%[0m"
set "BOLD=%ESC%[1m"
set "CYAN=%ESC%[96m"
set "BLUE=%ESC%[94m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "RED=%ESC%[91m"
set "GRAY=%ESC%[90m"

cls
echo %BOLD%%CYAN% NEWS STUDIO%RESET%  %GRAY%RSS + AI + ElevenLabs%RESET%
echo %BLUE%------------------------------------------------------------%RESET%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo %RED%[X] Python ortami bulunamadi.%RESET% setup.bat dosyasini calistir.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo %RED%[X] .env bulunamadi.%RESET% setup.bat dosyasini calistir.
    echo.
    pause
    exit /b 1
)

echo %CYAN%[0/5]%RESET% Sistem kontrol ediliyor...
".venv\Scripts\python.exe" "preflight.py"
if errorlevel 1 (
    echo.
    echo %RED%[X] Sistem kontrolu basarisiz.%RESET%
    echo %GRAY%Yukaridaki FAIL satirlarini duzeltip tekrar calistir.%RESET%
    echo.
    pause
    exit /b 1
)
echo.

echo %BOLD%Ne yapmak istiyorsun?%RESET%
echo   %CYAN%[1]%RESET% Tech     %GRAY%Yeni video%RESET%
echo   %CYAN%[2]%RESET% Gaming   %GRAY%Yeni video%RESET%
echo   %YELLOW%[0]%RESET% TTS      %GRAY%Son videodaki metinlerden ses uret%RESET%
echo.
echo %GRAY%Override: 1 o / 2 o = tum proje, 0 o = yalnizca sesler.%RESET%
echo.
set "USER_SELECTION="
set /p "USER_SELECTION=Secim [0/1/2] (istege bagli: o): "

set "MENU_CHOICE="
set "MENU_MODIFIER="
for /f "tokens=1,2" %%A in ("%USER_SELECTION%") do (
    set "MENU_CHOICE=%%A"
    set "MENU_MODIFIER=%%B"
)

rem Bosluksuz 0o, 1o ve 2o yazimlarini da kabul et.
if /i "%MENU_CHOICE%"=="0o" (
    set "MENU_CHOICE=0"
    set "MENU_MODIFIER=o"
)
if /i "%MENU_CHOICE%"=="1o" (
    set "MENU_CHOICE=1"
    set "MENU_MODIFIER=o"
)
if /i "%MENU_CHOICE%"=="2o" (
    set "MENU_CHOICE=2"
    set "MENU_MODIFIER=o"
)

if not "%MENU_CHOICE%"=="0" if not "%MENU_CHOICE%"=="1" if not "%MENU_CHOICE%"=="2" (
    echo.
    echo %RED%[X] Gecersiz secim.%RESET% 0, 1, 2 veya sonuna o yaz.
    pause
    exit /b 1
)

if defined MENU_MODIFIER if /i not "%MENU_MODIFIER%"=="o" (
    echo.
    echo %RED%[X] Gecersiz ek:%RESET% %MENU_MODIFIER%  %GRAY%Override icin o kullan.%RESET%
    pause
    exit /b 1
)

set "TTS_OVERWRITE_ARG="
if /i "%MENU_MODIFIER%"=="o" set "TTS_OVERWRITE_ARG=--overwrite"

set "SKIP_NEWS=0"
set "REBUILD_PROJECT=0"
if "%MENU_CHOICE%"=="0" (
    set "SKIP_NEWS=1"
    set "CATEGORY_NAME=TTS only"
) else if "%MENU_CHOICE%"=="2" (
    set "CONTENT_CATEGORY=gaming"
    set "FEED_FILE=gaming_feeds.opml"
    set "CATEGORY_NAME=Gaming"
) else (
    set "CONTENT_CATEGORY=tech"
    set "FEED_FILE=feeds.opml"
    set "CATEGORY_NAME=Tech News"
)

if "%SKIP_NEWS%"=="0" if /i "%MENU_MODIFIER%"=="o" set "REBUILD_PROJECT=1"

if "%SKIP_NEWS%"=="0" if not exist "%FEED_FILE%" (
    echo.
    echo %RED%[X] Feed bulunamadi:%RESET% %FEED_FILE%
    pause
    exit /b 1
)

echo.
echo %CYAN%[1/5]%RESET% ElevenLabs kontrol ediliyor...
".venv\Scripts\python.exe" "elevenlabs_tts.py" --check > "%TEMP%\news_studio_eleven_check.log" 2>&1
if errorlevel 1 (
    echo.
    echo %RED%[X] ElevenLabs ayarlari gecersiz.%RESET%
    type "%TEMP%\news_studio_eleven_check.log"
    echo.
    pause
    exit /b 1
)
echo       %GREEN%OK%RESET%

set "LATEST_VIDEO_NUMBER="
for /f %%N in ('powershell.exe -NoProfile -Command "$max=0; foreach($d in (Get-ChildItem -LiteralPath '%PROJECTS_ROOT%' -Directory)){ if($d.Name -match '^Video ([0-9]+)$'){ $n=[int]$Matches[1]; if($n -gt $max){ $max=$n } } }; $max"') do set "LATEST_VIDEO_NUMBER=%%N"

if not defined LATEST_VIDEO_NUMBER set "LATEST_VIDEO_NUMBER=0"

set "VIDEO_NUMBER="
if "%SKIP_NEWS%"=="1" set "VIDEO_NUMBER=%LATEST_VIDEO_NUMBER%"
if "%REBUILD_PROJECT%"=="1" set "VIDEO_NUMBER=%LATEST_VIDEO_NUMBER%"
if "%SKIP_NEWS%"=="0" if "%REBUILD_PROJECT%"=="0" set /a VIDEO_NUMBER=LATEST_VIDEO_NUMBER+1

if not defined VIDEO_NUMBER set "VIDEO_NUMBER=0"
if "%VIDEO_NUMBER%"=="0" (
    echo.
        echo %RED%[X] Kullanilabilecek "Video N" klasoru bulunamadi.%RESET%
    pause
    exit /b 1
)

set "VIDEO_DIR=%PROJECTS_ROOT%\Video %VIDEO_NUMBER%"
if "%SKIP_NEWS%"=="0" (
    if "%REBUILD_PROJECT%"=="1" (
        echo.
        echo %YELLOW%[!] Override:%RESET% Video %VIDEO_NUMBER% bastan yazilacak.
        echo %CYAN%[2/5]%RESET% Eski proje ciktilari temizleniyor...

        powershell.exe -NoProfile -Command "$root=[IO.Path]::GetFullPath('%PROJECTS_ROOT%').TrimEnd('\'); $target=[IO.Path]::GetFullPath('%VIDEO_DIR%'); if ([IO.Path]::GetDirectoryName($target) -ne $root -or [IO.Path]::GetFileName($target) -notmatch '^Video [0-9]+$') { throw 'Guvenli olmayan proje yolu: ' + $target }; foreach ($name in @('Haberler','Sesler')) { $path=Join-Path $target $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force } }; foreach ($name in @('aday_haberler.json','video_script.txt','project_info.json','story_timing.json','state.json','timing_diagnostic_story_01.mp4','pipeline.log','elevenlabs.log')) { $path=Join-Path $target $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }"
        if errorlevel 1 (
            echo.
            echo %RED%[X] Eski proje ciktilari temizlenemedi.%RESET%
            pause
            exit /b 1
        )
    )

    if not exist "%VIDEO_DIR%" mkdir "%VIDEO_DIR%"

    if "%REBUILD_PROJECT%"=="1" (
        echo %CYAN%[2/5]%RESET% Video %VIDEO_NUMBER% yeniden hazirlaniyor  %GRAY%[%CATEGORY_NAME%]%RESET%
    ) else (
        echo %CYAN%[2/5]%RESET% Video %VIDEO_NUMBER% hazirlaniyor  %GRAY%[%CATEGORY_NAME%]%RESET%
    )

    ".venv\Scripts\python.exe" "news_pipeline.py" --video %VIDEO_NUMBER% --category %CONTENT_CATEGORY% >nul 2>&1

    if errorlevel 1 (
        echo.
        echo %RED%[X] Haber/transcript asamasi basarisiz.%RESET%
        echo %GRAY%Detay: %VIDEO_DIR%\pipeline.log%RESET%
        echo.
        pause
        exit /b 1
    )
    echo       %GREEN%OK%RESET%  %GRAY%Haberler ve transcriptler hazir.%RESET%
) else (
    echo %CYAN%[2/5]%RESET% Transcript atlandi  %GRAY%[Video %VIDEO_NUMBER%]%RESET%
    echo       %GREEN%OK%RESET%
)

echo.
echo %CYAN%[3/5]%RESET% Sesler uretiliyor  %GRAY%[Video %VIDEO_NUMBER%]%RESET%
if defined TTS_OVERWRITE_ARG (
    echo       %YELLOW%Override acik%RESET%
) else (
    echo       %GRAY%Mevcut MP3 dosyalari korunacak.%RESET%
)
".venv\Scripts\python.exe" "elevenlabs_tts.py" --video %VIDEO_NUMBER% %TTS_OVERWRITE_ARG% --compact

if errorlevel 1 (
    echo.
    echo %RED%%BOLD% SES URETILEMEDI%RESET%
    echo %YELLOW%Neden yukarida yaziyor.%RESET%
    echo %GRAY%Teknik log: %VIDEO_DIR%\elevenlabs.log%RESET%
    echo.
    pause
    exit /b 1
)

echo.
echo %CYAN%[4/5]%RESET% Kelime ve cumle zamanlari hazirlaniyor  %GRAY%[Video %VIDEO_NUMBER%]%RESET%
".venv\Scripts\python.exe" "alignment_stage.py" --video %VIDEO_NUMBER%
if errorlevel 1 (
    echo.
    echo %RED%%BOLD% ZAMANLAMA URETILEMEDI%RESET%
    echo %GRAY%Mevcut MP3 korunur; baglanti geldikten sonra ayni komutu yeniden calistirabilirsin.%RESET%
    echo.
    pause
    exit /b 1
)

echo.
echo %CYAN%[5/5]%RESET% Manuel separator animasyonlari bekleniyor  %GRAY%[Video %VIDEO_NUMBER%]%RESET%
".venv\Scripts\python.exe" "manual_checkpoint.py" --video %VIDEO_NUMBER%
set "CHECKPOINT_EXIT=%ERRORLEVEL%"
if "%CHECKPOINT_EXIT%"=="2" (
    echo.
    echo %YELLOW%Islem durduruldu; tum uretilen dosyalar korundu.%RESET%
    echo %GRAY%Devam etmek icin resume_video.bat dosyasini calistir.%RESET%
    pause
    exit /b 0
)
if not "%CHECKPOINT_EXIT%"=="0" (
    echo.
    echo %RED%%BOLD% MANUEL CHECKPOINT HAZIRLANAMADI%RESET%
    echo %GRAY%Devam etmek icin resume_video.bat dosyasini calistir.%RESET%
    pause
    exit /b 1
)

echo.
echo %GREEN%%BOLD% TAMAMLANDI  Video %VIDEO_NUMBER%%RESET%
echo %BLUE%------------------------------------------------------------%RESET%
echo %GRAY%Metin :%RESET% %VIDEO_DIR%\Haberler
echo %GRAY%Ses   :%RESET% %VIDEO_DIR%\Sesler
echo %GRAY%Zaman :%RESET% %VIDEO_DIR%\story_timing.json
echo %GRAY%Ayirac:%RESET% %VIDEO_DIR%\03_manual_separators
echo %GRAY%Log   :%RESET% pipeline.log + elevenlabs.log
echo.

if exist "%VIDEO_DIR%" explorer "%VIDEO_DIR%"

pause
