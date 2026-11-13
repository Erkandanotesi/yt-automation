# Tech News Studio

End-to-end automated YouTube Tech & Gaming News video production suite: RSS ingestion, AI scripting and editorial ranking, ElevenLabs / local Qwen3 TTS voiceover, WhisperX forced alignment, motion cards (`motion_engine`), FFmpeg compositing, WPF desktop studio (`NewsStudio.App`), and scheduled YouTube publishing.

## Repository Structure

```text
Tech News/
├── setup.bat                  # One-click portable environment setup & repair
├── create_video_app.bat       # Launch the NewsStudio desktop app
├── create_video.bat           # Run the autonomous video creation CLI workflow
├── news-studio.bat            # CLI wrapper for Automation/src/news_studio.py
├── Automation/                # Core Python pipeline, WPF desktop UI, tests & docs
│   ├── NewsStudio.App/        # .NET 10 WPF desktop interface
│   ├── src/                   # Pipeline modules (all paths via file_locations.py)
│   ├── config/                # RSS OPML feeds & channel editorial profiles
│   ├── prompts/               # LLM editorial/visual prompt templates
│   ├── scripts/               # Standalone stage & maintenance utilities
│   ├── docs/                  # Architecture & operational guides
│   └── tests/                 # Unit test suite & manual probes
├── motion_engine/             # Playwright + GSAP/Lucide HTML motion-card renderer
├── Projects/                  # Video N workspaces & _Archive (ignored by Git)
├── Assets/                    # Audio, overlays, branding & stock media (ignored by Git)
└── models/                    # Optional local Qwen3-TTS weights (ignored by Git)
```

## Quick Setup (Fresh Machine or Relocated Folder)

This project uses workspace-relative path resolution (`Automation/src/file_locations.py`), so you can clone or move the entire folder anywhere without breaking references.

1. Run `setup.bat` from the repository root (or `Automation\setup.bat`).
   - Repairs or creates `Automation\.venv` (handles relocated virtual environments automatically)
   - Installs Python dependencies (`Automation\requirements.txt`)
   - Installs Playwright Chromium and `motion_engine` JS assets
   - Ensures local `ffmpeg` / `ffprobe` are present in `Automation\tools\ffmpeg\`
   - Initializes `Automation\.env`, `Automation\config\feeds.opml`, and `Automation\config\channel_profile.txt` from templates if missing
   - Builds `Automation\NewsStudio.App` if the .NET SDK is installed
2. Add your API keys and YouTube OAuth client secret under `Automation\.env` and `Automation\.youtube_credentials\` (never committed to Git).
3. Launch with `create_video_app.bat` (Desktop UI) or `create_video.bat` (CLI).

## Verification & Maintenance

- Run unit tests:
  ```bat
  Automation\.venv\Scripts\python.exe -m unittest discover -s Automation\tests
  ```
- Run environment & preflight checks:
  ```bat
  Automation\.venv\Scripts\python.exe Automation\src\env_doctor.py
  Automation\.venv\Scripts\python.exe Automation\src\preflight.py
  ```
- Inspect project status:
  ```bat
  news-studio.bat status "Video 95"
  ```
- See [Automation/docs/AUTOMATION.md](Automation/docs/AUTOMATION.md) for detailed pipeline architecture and troubleshooting.
