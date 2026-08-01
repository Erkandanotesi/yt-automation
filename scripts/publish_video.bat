@echo off
setlocal
cd /d "%~dp0.."
set /p VIDEO_NUMBER=Video number: 
set /p BACKGROUND=Final AI-rendered thumbnail path: 
set /p HEADLINE=Exact headline already rendered in that image: 
set /p ALLOW_AI=Type AI to permit one publishing-copy API request if cache is missing: 
set AI_FLAG=
if /I "%ALLOW_AI%"=="AI" set AI_FLAG=--confirm-ai-cost
.\.venv\Scripts\python.exe "src\publish_stage.py" --video %VIDEO_NUMBER% --background "%BACKGROUND%" --thumbnail-text-rendering ai_rendered --rendered-headline "%HEADLINE%" %AI_FLAG%
pause
