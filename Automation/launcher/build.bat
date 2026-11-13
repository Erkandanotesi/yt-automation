@echo off
setlocal
cd /d "%~dp0"

set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo [ERROR] Could not find .NET Framework C# compiler at:
    echo   %CSC%
    exit /b 1
)

set "ICON=..\NewsStudio.App\Assets\TechLogo.ico"
set "OUT=..\..\yt-automation.exe"

"%CSC%" /nologo /target:winexe /optimize+ /win32icon:"%ICON%" /out:"%OUT%" /reference:System.Windows.Forms.dll "Program.cs"
if errorlevel 1 (
    echo [ERROR] Failed to build yt-automation.exe.
    exit /b 1
)

echo [OK] Built %OUT%
exit /b 0
