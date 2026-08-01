# Tech News Automation — canonical file layout

`Automation` contains source code, configuration, runtime data, documentation,
references, launchers, tests, and portable tools. Numbered video projects and reusable
media remain beside it under the `Tech News` production root.

All code obtains paths from `src/file_locations.py`. Do not reconstruct these paths in
individual stages.

```text
Tech News/
|-- Assets/
|   |-- Audio/
|   |   |-- background_music/
|   |   `-- sfx/
|   |-- Media/
|   |   |-- AI Generated/
|   |   |-- Online Retrieved/Wikimedia Commons/
|   |   `-- Thumbnails/
|   |-- Media (No-Copyright)/
|   |-- Overlays/
|   |   |-- tv_frame.png
|   |   `-- green-screen-tv-effect.mov
|   `-- example_seperator/
|-- Automation/
|   |-- .env                         # secrets; ignored by Git
|   |-- .env.example
|   |-- requirements.txt
|   |-- create_video.bat             # interactive one-button workflow
|   |-- news-studio.bat              # stable CLI wrapper
|   |-- setup.bat
|   |-- src/
|   |   |-- file_locations.py        # single source of truth for every path
|   |   |-- news_studio.py           # Milestone 10 orchestrator
|   |   |-- news_pipeline.py
|   |   |-- news_archive.py          # persistent hash + AI duplicate guard
|   |   |-- elevenlabs_tts.py
|   |   |-- alignment_stage.py
|   |   |-- manual_checkpoint.py
|   |   |-- storyboard_stage.py
|   |   |-- media_index.py
|   |   |-- media_selection_stage.py
|   |   |-- online_media_stage.py
|   |   |-- caption_stage.py
|   |   |-- effects_stage.py
|   |   |-- render_stage.py
|   |   |-- publish_stage.py
|   |   |-- youtube_upload.py
|   |   `-- supporting modules
|   |-- config/
|   |   |-- feeds.opml
|   |   |-- gaming_feeds.opml
|   |   |-- channel_profile.txt
|   |   `-- gaming_channel_profile.txt
|   |-- data/
|   |   |-- processed_urls.json      # runtime history; ignored by Git
|   |   |-- news_archive/            # pulled sources, summaries, index, AI cache
|   |   |-- indexes/                 # generated media indexes/reports
|   |   `-- benchmarks/
|   |-- docs/
|   |   |-- AUTOMATION.md
|   |   |-- AUTOMATION_ROADMAP.md
|   |   |-- ONLINE_MEDIA.md
|   |   `-- README_TR.txt
|   |-- references/
|   |   `-- capcut_screenshot.png
|   |-- scripts/                     # optional individual-stage launchers
|   |-- tests/
|   `-- tools/ffmpeg/
|-- Video 1/
|-- ...
`-- Video N/
```

## Pulled-news archive and duplicate prevention

Every full article fetched by the news stage is saved under
`data/news_archive/articles/`. Its filename contains both the stable story ID derived
from the canonical URL and a normalized source-text SHA-256 prefix; `index.json`
stores the complete hash. Successfully used
narrations are mirrored under `data/news_archive/summaries/`, while `index.json`
records which `Video N` used each story.

Duplicate checks are deliberately layered:

1. Identical normalized source hashes are rejected locally with no provider request.
2. Unrelated files are cleared locally with no provider request.
3. For plausible cross-feed matches, the new file and up to three strongest previous
   files are sent in one structured AI request. The AI must distinguish the same event
   from a genuinely new follow-up.
4. The decision is cached by the new/prior content hashes and model namespace, so an
   interrupted or repeated run does not pay for the same comparison twice.

Completed `Video N` projects are backfilled automatically before a new news run. This
backfill performs no AI requests. Inspect it manually with:

```text
scripts\news_archive.bat status
scripts\news_archive.bat backfill
```

Only folders named exactly `Video N` participate in project numbering.

## Stable commands

```text
create_video.bat
news-studio.bat new --category tech --dry-run
news-studio.bat new --category tech --confirm-api-costs
news-studio.bat resume "Video N" --confirm-api-costs
news-studio.bat status "Video N"
news-studio.bat run-stage "Video N" captions
news-studio.bat validate "Video N"
news-studio.bat preview "Video N"
news-studio.bat approve-preview "Video N"
news-studio.bat render "Video N"
```

`--dry-run` makes no writes and performs no provider requests. Paid/provider-backed
stages do not begin without `--confirm-api-costs`. Individual story-image and publishing
generators retain their own confirmation gates as a second safety layer.

## Milestone 10 lifecycle

1. Preflight validates the modular layout, configuration, assets, and tools.
2. `new` creates the next numbered project and runs news, TTS, and alignment.
3. The flow creates `03_manual_separators/` and stops without losing state.
4. `resume` validates the manual separators, then produces storyboard images, archive
   selections, captions, effects, and the 60-second review preview.
5. The review preview must be approved. Approval is tied to its SHA-256; replacing the
   preview invalidates approval automatically.
6. The final render runs only after approval.
7. Publishing copy and one series-aligned AI thumbnail are generated or reused from
   cache, then the complete `09_publish/` package is saved.

Every mutating flow creates an exclusive `.news_studio.lock` inside the project. A
second process cannot edit the same project. Ctrl+C preserves completed outputs and
removes its own lock. Use `--recover-stale-lock` only when the recorded process has
stopped.

Stage events are appended to `Video N/orchestration/events.jsonl`; full subprocess
output is stored under `Video N/orchestration/logs/`.

## Generated project structure

```text
Video N/
|-- Haberler/
|-- Sesler/
|   `-- alignments/
|-- 03_manual_separators/
|-- 04_storyboard/
|-- 05_visuals/
|-- 06_captions/
|-- 07_effects/
|-- 08_render/
|-- 09_publish/
|-- orchestration/
|   |-- events.jsonl
|   |-- logs/
|   `-- preview_approval.json
|-- project_info.json
|-- story_timing.json
`-- state.json
```

The upload manifest remains private-by-default and checksum-locks the final MP4 and
thumbnail. Actual YouTube upload is separate and always requires `--confirm-upload`.
