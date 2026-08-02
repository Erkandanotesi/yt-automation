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
|   |       |-- paper_sound.MP3
|   |       `-- shutter_sound.MP3
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
|   |-- create_video.bat             # autonomous/scheduled one-button workflow
|   |-- news-studio.bat              # stable CLI wrapper
|   |-- setup.bat
|   |-- src/
|   |   |-- file_locations.py        # single source of truth for every path
|   |   |-- news_studio.py           # Milestone 10 orchestrator
|   |   |-- news_pipeline.py
|   |   |-- news_archive.py          # persistent hash + AI duplicate guard
|   |   |-- env_doctor.py            # secret-safe .env encoding/duplicate repair
|   |   |-- elevenlabs_tts.py
|   |   |-- alignment_stage.py
|   |   |-- manual_checkpoint.py
|   |   |-- separator_policy.py      # shared manual/skipped checkpoint rules
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

Fetched articles stay inside their `Video N` project while production is incomplete.
Only after final-render QA passes are the selected sources committed under
`data/news_archive/articles/`. Each filename contains both the stable story ID derived
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

Completed, QA-passing `Video N` projects are backfilled automatically before a new news run. This
backfill performs no AI requests. Inspect it manually with:

```text
scripts\news_archive.bat status
scripts\news_archive.bat backfill
```

Before a pulled page reaches the archive or summary model, it must pass the source
quality gate. The gate rejects HTTP errors, unsupported content types, access-denied
and CAPTCHA pages, title/source mismatches, unusually short or sentence-poor text,
low lexical diversity, and repeated boilerplate. If the full article fails, the RSS
body is allowed only when it independently passes the same checks. Long aggregator
headlines receive limited paraphrase tolerance when at least two significant title
terms remain grounded in the article; short titles retain the stricter threshold.
Ungrounded items in the model's audit metadata are discarded locally instead of
spending another summary request when the narration itself passes its number and quote
checks.

`RSS_FEED_TIME_INTERVAL_HOURS` in `.env` controls the RSS publication-age window in
whole hours. For example, `24` accepts feed entries published during the preceding
24 hours and rejects older entries before article extraction or paid AI processing.
`MIN_SCORE` is the primary editorial threshold (currently `50`). If primary stories
are rejected and fewer than `TOP_N` remain, the pipeline attempts the best lower-ranked
RSS reserves in order; those reserves still must pass every source, duplicate, and
factual-quality gate. A rerun restores the validated prefix of an incomplete project.
News ranking uses `OPENAI_NEWS_SELECTION_MODEL=gpt-5.6-luna` with
`OPENAI_NEWS_SELECTION_REASONING_EFFORT=medium` and receives up to
`NEWS_SELECTION_CANDIDATE_COUNT=40` candidates in its single selection request. The
40 are selected deterministically from a larger local pool: publishers are rotated
round-robin and strongly similar cross-feed headlines are deferred. This avoids both
feed-order bias and random, irreproducible selections while preserving local relevance
order within each publisher. `MAX_CANDIDATES=80` controls the larger local pool and
`NEWS_SELECTION_TITLE_SIMILARITY_THRESHOLD=0.60` controls headline suppression. The
all Luna-backed text stages use `medium` reasoning effort, including summaries,
duplicate review, storyboard planning, archive selection, and publishing copy.
Rejected sources never occupy a video slot. The news stage immediately tries the
next ranked RSS candidate from a 15-story reserve prepared in the same ranking call.
If the feeds still cannot supply the requested number of valid articles, the project
is marked incomplete; broken material is never used merely to fill the quota.

Accepted source prose is normalized into short article paragraphs before it is saved
as `Haberler/*-orijinal.txt`. Whitespace-only formatting does not change duplicate
identity because the archive hashes normalized content.

Archive-image decisions use each window plus the complete story title and entity
context. If an AI-generated Wikimedia entity is worded as an ungrounded paraphrase, it
is repaired locally to a story entity. If no grounded entity exists, the window uses a
low-confidence local fallback instead of failing the complete production run.

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

`create_video.bat` is the autonomous launcher. Each ordinary choice waits 30 seconds
before taking its displayed default:
create a new Tech News project, allow the shown provider-cost plan, skip manual
separators, and approve the checksum-bound preview. The final upload choice waits 120
seconds and defaults to uploading the MP4 plus its thumbnail, title, description, and
chapter metadata. Press the alternative key during the countdown to override any
default. The timeout values can be changed with
`AUTOMATION_DECISION_TIMEOUT_SECONDS` and `AUTOMATION_UPLOAD_TIMEOUT_SECONDS`.
The interactive launcher clears the screen between major decisions and stages. Its
compact preflight reports only the overall result and actionable warnings; full stage
output remains in each project's `orchestration/logs/` folder. During production, a
color progress panel shows the current stage, percentage, and completed/total count.
Completed rectangles are green, a failed stage segment is red, waiting states are
yellow, and remaining rectangles stay dim. Headless scheduled runs emit plain progress
snapshots without destructive screen clearing.

`AI_GENERATED_IMAGES_PER_STORY` controls the paid hero-image count for every story
(1-4, default 2). Keep `MAX_IMAGE_REQUESTS_PER_RUN` at least as high as `TOP_N`
multiplied by that value for a fresh complete production. When manual separators are
skipped, a short page-flip reveal is applied at the beginning of every chapter,
including chapter 1. `PAGE_FLIP_ENABLED` and `PAGE_FLIP_DURATION_SECONDS` control it;
manual separators disable the transition automatically.
Successful autonomous runs exit normally. If the launcher returns an error, its console
stays open indefinitely at a failure prompt so the red stage and traceback can be
inspected; press any key only when ready to close it.

## Milestone 10 lifecycle

1. The one-button launcher runs a no-cost preflight before project creation. It validates
   the modular layout, configuration, news-pipeline startup, assets, and tools.
2. `new` creates the next numbered project and runs news, TTS, and alignment.
3. At the separator choice, the default is `skip`. Skip records a durable `SKIPPED`
   checkpoint, uses no separator files, and starts narration, captions, and relevant
   images at each chapter boundary. AI hero-image candidates inside a real manual
   separator's covered interval are excluded so paid generations are not hidden.
4. If manual mode is selected, the flow creates `03_manual_separators/` and stops
   without losing state. Windows Explorer opens `Haberler/` first for reading and then
   `03_manual_separators/` as the save destination.
5. `resume` validates manual separators when selected, then produces storyboard images, archive
   selections, captions, effects, and the 60-second review preview.
6. The review preview must be approved. Approval is tied to its SHA-256; replacing the
   preview invalidates approval automatically.
7. The final render runs only after approval.
   Per-image animation clips are deleted as soon as the base visual is built. Once
   the final master and preview pass QA, the remaining `08_render/intermediates/`
   directory is deleted. Final videos, previews, reports, and source assets remain.
8. Publishing copy uses short editorial hooks: titles target 4–7 words (50 characters
   maximum), while thumbnail copy is a distinct 2–3 word phrase capped at 18
   characters. One series-aligned AI thumbnail is generated or reused from cache.
   Its typography keeps a consistent left-side anchor but uses a flexible text zone,
   so the image model never compresses letter spacing to satisfy a rigid percentage.
   Thumbnail generation is text-to-image only: existing story-image pixels are never
   uploaded or reused, and the prompt requests a fresh, high-impact composition.
   The complete `09_publish/` package is then saved.
9. The launcher waits two minutes at the final upload decision and uploads by default.
   Complete one OAuth browser authorization first so later scheduled runs can use the
   cached refresh token without interaction.

The image-only animated TV overlay keeps its native alpha but defaults to 18% overall
opacity; the additional procedural noise/dropout glitch is disabled by default. The
static TV frame remains fully opaque and the animated overlay remains disabled during
manual separators. `paper_sound.MP3` plays at every news-section start at `-10.0 dB`.
Smooth archive-image zoom now uses `EFFECTS_MOTION_SCALE=1.025` and hero-image zoom
uses `EFFECTS_HERO_MOTION_SCALE=1.01875`, a 25% increase in zoom distance while
retaining cubic subpixel motion.
`shutter_sound.MP3` plays at `-10.0 dB` on every third actual image event; separators and repeated
inter-story hold frames are not counted, and a shutter cue hidden by a separator moves
to that image's first visible frame.

Every mutating flow creates an exclusive `.news_studio.lock` inside the project. A
second process cannot edit the same project. Ctrl+C preserves completed outputs and
removes its own lock. Use `--recover-stale-lock` only when the recorded process has
stopped.

Stage events are appended to `Video N/orchestration/events.jsonl`. Every subprocess
log under `Video N/orchestration/logs/` includes its command, interpreter, working
directory, start/finish timestamps, complete child output, exit code, duration, and a
project recovery snapshot on failure. Orchestrated child commands also emit their full
Python traceback to this log while the main console keeps only the concise error line.

Media-selector output is treated as a recommendation rather than trusted routing data.
Missing windows, foreign asset IDs, inconsistent relevance fields, and ungrounded
online entities are repaired deterministically from that window's own shortlist and
the complete story context. These recoverable model mistakes do not abort production.
The selector sends no local image pixels. Candidate metadata must now pass a local
context-grounding gate before Luna may select it: an exact company/product/person match
or at least one distinctive non-generic context term is required. Entity context is
carried across pronouns and split sentences, while choices below
`MEDIA_MIN_AI_CONFIDENCE=0.45` are rejected in favor of the existing Wikimedia path.
This improves precision without adding a vision request or another paid AI call.
If no acceptable local or Wikimedia result exists, FFmpeg creates a unique dark,
context-safe chapter background under `05_visuals/safe_fallbacks/`. The persistent
chapter title remains visible, production continues without an API request, and no
unrelated person, company, product, or event is implied.
Folders named `Thumbnails` are excluded both when the media index is scanned and when
an in-video shortlist is built. The second guard also blocks thumbnail files lingering
in an older cached index; thumbnail creation and YouTube publishing remain unchanged.
Each raw per-story selector response is saved before validation under
`05_visuals/selector_cache/`. A matching fingerprint lets a resumed run reuse that
response without paying for the same selector request again.
`MEDIA_MAX_USES_PER_VIDEO=2` is a hard
SHA-256-based cap across the complete video, including generated hero images, local
archive images, and Wikimedia downloads. Selection repairs and cached story resumes
must respect the same limit.

## Environment-file health

The environment doctor never displays assignment values. It reports encoding,
duplicate variable names, BOM state, and mojibake comment line numbers. `fix` keeps
the last assignment, removes earlier duplicates, repairs comments, and writes UTF-8
without BOM:

```text
scripts\env_doctor.bat status
scripts\env_doctor.bat fix
```

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

The upload manifest checksum-locks the final MP4 and thumbnail. Uploads are scheduled
for the next free 12:00 or 20:00 GMT+3 Turkey slot. YouTube requires scheduled videos
to be inserted as private; `publishAt` automatically makes them public at that slot.
After the video upload succeeds, `data/youtube_schedule_state.json` stores its scheduled
release. If that release is current or future, the next project advances from it
(20:00 advances to the next day at 12:00). If it is already in the past, current time
is used only to select the next non-past 12:00/20:00 slot. Pending manifests are also
reserved, and missing schedule history stops with an explicit error.
The title always ends in ` | Tech Reports`, descriptions omit a SOURCES section, and
both metadata and original-audio language are set to English. YouTube then generates
and publishes automatic English captions when the video is eligible; the Data API has
no separate creator-side auto-caption switch. Direct uploader commands still require
`--confirm-upload`; the autonomous
launcher supplies it only after the visible two-minute opt-out countdown.

Before the first autonomous upload, save a Google OAuth Desktop-app credential as
`.youtube_credentials/client_secret.json`, then run
`scripts\youtube_authorize.bat`. The one-time browser sign-in creates the ignored
refresh-token file without uploading a video. Later runs reuse that token. A completed
upload is idempotent, and a run interrupted after the video but before its thumbnail
resumes the thumbnail step without uploading a duplicate video.

For unattended long-term use, do not leave the Google OAuth consent configuration in
`Testing`: Google expires testing authorizations (including offline refresh tokens)
after seven days. Use an appropriate production/personal-use OAuth configuration and
request only the `youtube.upload` scope used by this project.
