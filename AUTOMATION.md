# Tech News automation layout

This file is the canonical map of the current workspace. `Automation` contains
automation code, configuration, tests, dependencies, and global state only.
Reusable production assets and numbered video projects live beside it.

```text
Tech News/
├── Assets/
│   ├── Audio/
│   │   ├── background_music/
│   │   │   ├── background_music.mp3
│   │   │   └── background_music.license.json
│   │   └── sfx/
│   │       ├── glitch_transition.wav
│   │       └── glitch_transition.license.json
│   ├── Media/
│   │   ├── AI Generated/       # approved AI images with descriptive filenames
│   │   ├── Online Retrieved/
│   │   │   └── Wikimedia Commons/ # downloaded images + metadata sidecars
│   │   ├── <company-or-topic>/
│   │   └── _combined/
│   ├── Media (No-Copyright)/
│   │   ├── <company-or-topic>/
│   │   └── _Combined/
│   ├── Outro/
│   │   └── outro_sound/
│   ├── Overlays/
│   │   ├── tv_frame.png
│   │   └── green-screen-tv-effect.mov # native-alpha animated image treatment
│   └── example_seperator/
│       └── Tech News Template(1)-1.mp4
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
│   ├── storyboard_stage.py
│   ├── storyboard_video.bat
│   ├── media_index.py
│   ├── media_index.bat
│   ├── media_selection_stage.py
│   ├── media_selection_video.bat
│   ├── online_media_stage.py
│   ├── online_media.bat
│   ├── ONLINE_MEDIA.md
│   ├── media_index_eval.py
│   ├── media_retrieval_benchmark.json
│   ├── caption_stage.py
│   ├── captions_video.bat
│   ├── effects_stage.py
│   ├── effects_video.bat
│   ├── render_stage.py
│   ├── render_video.bat
│   ├── capcut_screenshot.png       # canonical editing-order reference
│   ├── test_program.bat
│   ├── content_validation.py
│   ├── file_utils.py
│   ├── project_paths.py
│   ├── tooling.py
│   ├── processed_urls.json
│   ├── indexes/
│   │   └── media_index.json         # generated locally; ignored by Git
│   ├── tests/
│   └── tools/
│       └── ffmpeg/
├── Legacy Automation Outputs/
├── Video 1/
├── Video 2/
├── Video 3/
├── Video 4/
└── tech news thumbnail prompt.txt
```

Only folders named exactly `Video N` participate in automatic project numbering.
The next project is currently `Video 6`. New project artifacts must never be written
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
├── 04_storyboard/
│   ├── storyboard.json
│   └── story_01_30s_proof.mp4
├── 05_visuals/
│   ├── media_plan.json
│   ├── online_searches/             # auditable Commons search manifests
│   ├── generated/
│   │   ├── 01_hero_01.png
│   │   ├── 01_hero_02.png
│   │   └── ...
│   └── selected_archive/           # optional when copying is enabled
├── 06_captions/
│   ├── captions.json
│   ├── captions.srt
│   ├── story_01.srt
│   └── ...
├── 07_effects/
│   ├── effects_plan.json
│   ├── ffmpeg_filter_templates.txt
│   ├── milestone6_reference.srt
│   ├── milestone6_reference_60s.mp4
│   ├── milestone6_reference_60s.json
│   └── milestone6_reference_contact_sheet.jpg
├── 08_render/
│   ├── render_plan.json
│   ├── render_report.json
│   ├── attributions/                # generated drawtext files per visual event
│   ├── Video N_final.mp4
│   ├── Video N_preview_540p.mp4
│   ├── milestone8_contact_sheet.jpg
│   └── intermediates/
└── timing_diagnostic.mp4      # optional local QA render
```

`Assets/Media` and `Assets/Media (No-Copyright)` are both indexed reusable visual
repositories. The user has explicitly classified both repositories as
`user_confirmed_no_copyright`; neither receives a verification warning or selection
penalty.

When the Milestone 5 selector explicitly reports `images_are_irrelevant` for every
local shortlist candidate, the production flow can query Wikimedia Commons for an
exact named entity. Only accepted commercial-reuse licenses, supported bitmap
formats, adequate resolution, and exact title/object-name evidence are allowed.
Downloads remain reusable assets under `Assets/Media/Online Retrieved/Wikimedia
Commons`, with adjacent attribution metadata. Disable this removable provider layer
with `WIKIMEDIA_FALLBACK_ENABLED=false`; local selection continues unchanged.

Milestone 4 uses `gpt-5.6-luna` only for selecting two supported, visually
distinct timed beats and writing the scene-specific prompt. Pixel generation uses
the separate `gpt-image-2` model at native 1280x720. The default production quality
is `medium`; the one-request provider smoke test always overrides this to `low`.
Every approved image is also copied to `Assets/Media/AI Generated` with a descriptive
scene filename and checksum suffix so it can be indexed and reused later.

Milestone 7 basic generates plain UTF-8 SubRip captions directly from the approved
canonical narration and ElevenLabs alignment. A separator overlays the start of its
narration, so the first caption starts with the separator rather than after it. The
stage writes combined and per-story `.srt` files and makes no
transcription or provider request. Styled `.ass` captions and word highlighting
remain deferred.

Milestone 6 reads the completed visual and caption manifests and uses static
center-cropped images. FFmpeg `zoompan` was removed because its integer coordinate
rounding produced visible pixel stepping. Exact one-second inter-story gaps hold
the previous image while voiceover, captions, and story title are absent; music
continues. Short glitch accents remain below a 3% timeline cap. The TV frame is
rendered with opaque non-transparent pixels, and music is looped/crossfaded with
narration ducking. The native-alpha `green-screen-tv-effect.mov` is looped above
image/glitch layers and below manual separators, then explicitly disabled throughout
each separator interval. Its audio stream is ignored. The opaque TV frame, persistent
title, attribution, and captions remain above it in CapCut reference order.

Milestone 8 normalizes separators, renders and caches uniform visual intermediates,
composes finite video and audio tracks independently, then muxes a complete 1920x1080
H.264/AAC master and 540p preview. QA checks both stream durations—not only container
duration—plus codecs, pixel format, sample rate, channels, and measured audio peak.
Following the CapCut reference, each story title remains visible above the TV frame
for the complete narration range, and burned-in captions use a raised lower-third
margin. When a selected asset's sidecar says attribution is required, a compact
`Source: creator · provider · license` label appears at the bottom-right inside the
TV frame for exactly that asset's visible interval. Public-domain and CC0 assets do
not receive an unnecessary label.
