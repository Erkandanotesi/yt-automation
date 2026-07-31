@echo off
setlocal
cd /d "%~dp0"
echo Wikimedia Commons search test
set /p QUERY=Search query: 
set /p ENTITY=Required exact entity: 
set /p CONTEXT=Spoken context: 
".venv\Scripts\python.exe" online_media_stage.py --video 5 --query "%QUERY%" --entity "%ENTITY%" --context "%CONTEXT%"
echo.
echo Search only. Review the manifest before downloading a candidate.
pause
