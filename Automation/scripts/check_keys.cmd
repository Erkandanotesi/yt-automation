cd /d "%~dp0.."
cmd /k .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); import elevenlabs_tts as t; print([label for label, _ in t.API_KEYS])"