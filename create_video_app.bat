@echo off
call "%~dp0Automation\create_video_app.bat" %*
exit /b %ERRORLEVEL%
