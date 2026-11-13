@echo off
call "%~dp0Automation\create_video.bat" %*
exit /b %ERRORLEVEL%
