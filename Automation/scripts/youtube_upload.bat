@echo off
setlocal
cd /d "%~dp0.."
set /p VIDEO_NUMBER=Video number: 

echo Validating the upload package without using quota...
.\.venv\Scripts\python.exe "src\youtube_upload.py" --video %VIDEO_NUMBER% --action validate
if errorlevel 1 goto :end

echo.
set /p CONFIRM=Type UPLOAD to publish this video privately to YouTube: 
if /I not "%CONFIRM%"=="UPLOAD" goto :end
.\.venv\Scripts\python.exe "src\youtube_upload.py" --video %VIDEO_NUMBER% --action upload --confirm-upload

:end
pause
