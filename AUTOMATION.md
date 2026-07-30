# Tech News automation layout

This file is the canonical map of the current workspace. `Automation` contains
automation code, configuration, tests, dependencies, and global state only.
Reusable production assets and numbered video projects live beside it.

```text
Tech News/
├── Assets/
│   ├── Audio/
│   │   └── background_music/
│   ├── Media/
│   │   ├── <company-or-topic>/
│   │   └── _combined/
│   ├── Outro/
│   │   └── outro_sound/
│   └── Overlays/
├── Automation/
│   ├── .env                  # local secrets; ignored by Git
│   ├── .env.example
│   ├── AUTOMATION.md
│   ├── AUTOMATION_ROADMAP.md
│   ├── README_TR.txt
│   ├── news_pipeline.py
│   ├── elevenlabs_tts.py
│   ├── alignment_stage.py
│   ├── alignment_utils.py
│   ├── manual_checkpoint.py
│   ├── resume_video.bat
│   ├── content_validation.py
│   ├── file_utils.py
│   ├── project_paths.py
│   ├── tooling.py
│   ├── processed_urls.json
│   ├── tests/
│   └── tools/
│       └── ffmpeg/
├── Media (No-Copyright)/
│   ├── <company-or-topic>/
│   └── _Combined/
├── Legacy Automation Outputs/
├── Video 1/
├── Video 2/
├── Video 3/
├── Video 4/
└── tech news thumbnail prompt.txt
```

Only folders named exactly `Video N` participate in automatic project numbering.
The next project is currently `Video 5`. New project artifacts must never be written
inside `Automation`.

Milestone 2 keeps the current compatibility folders inside a generated project:

```text
Video N/
├── Haberler/
├── Sesler/
│   ├── *.mp3
│   ├── tts_info.json
│   └── alignments/
│       └── *.alignment.json
├── project_info.json
├── story_timing.json
├── state.json
├── 03_manual_separators/
│   ├── 01_separator.mp4
│   └── ...
└── timing_diagnostic.mp4      # optional local QA render
```

`Assets/Media` is the primary reusable visual repository.
`Media (No-Copyright)` remains a separate licensing-conscious repository until the
media-index milestone records provenance and attribution requirements.
