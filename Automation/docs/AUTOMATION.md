# Tech News Automation — canonical file layout

`Automation` contains source code, configuration, runtime data, documentation,
references, launchers, tests, and portable tools. Numbered video projects and reusable
media remain beside it under the `Tech News` production root.

All code obtains paths from `src/file_locations.py`. Do not reconstruct these paths in
individual stages.

```text
Tech News/
|-- setup.bat                        # portable root setup launcher
|-- create_video_app.bat             # launches NewsStudio desktop UI
|-- create_video.bat                 # launches autonomous/scheduled CLI workflow
|-- news-studio.bat                  # stable CLI wrapper
|-- Assets/
|   |-- Audio/
|   |   |-- background_music/
|   |   `-- sfx/
|   |       |-- paper_sound.MP3
|   |       `-- shutter_sound.MP3
|   |-- Branding/
|   |-- Media/
|   |   |-- AI Generated/
|   |   |-- Online Retrieved/
|   |   |   |-- Pexels Videos/
|   |   |   |-- Pixabay Videos/
|   |   |   `-- Unsplash/
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
|   |-- create_video_app.bat         # NewsStudio WPF app launcher
|   |-- news-studio.bat              # stable CLI wrapper
|   |-- setup.bat
|   |-- NewsStudio.App/              # WPF desktop application (.NET 10)
|   |-- prompts/
|   |-- src/
|   |   |-- file_locations.py        # single source of truth for every path
|   |   |-- news_studio.py           # Milestone 10 orchestrator
|   |   |-- news_pipeline.py
|   |   |-- feed_audit.py            # provider-free live RSS quality audit
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
|   |   |-- unsplash_media_stage.py
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
|   |   |-- news_history_reset.json  # prevents old projects reappearing after reset
|   |   |-- news_archive/            # pulled sources, summaries, index, AI cache
|   |   |-- history_backups/         # timestamped recoverable cleanup backups
|   |   |-- indexes/                 # generated media indexes/reports
|   |   `-- benchmarks/
|   |-- docs/
|   |   |-- AUTOMATION.md
|   |   |-- AUTOMATION_ROADMAP.md
|   |   |-- ONLINE_MEDIA.md
|   |   `-- README_TR.txt
|   |-- references/
|   |   `-- capcut_screenshot.png
|   |-- scripts/                     # optional individual-stage & maintenance launchers
|   |-- tests/
|   `-- tools/ffmpeg/
|-- models/                          # optional local TTS models (ignored by Git)
|-- motion_engine/                   # Playwright/GSAP motion card renderer
`-- Projects/
    |-- Video 1/
    |-- ...
    |-- Video N/
    `-- _Archive/                    # legacy outputs & manual test recordings
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
3. Only strongly plausible cross-feed matches reach AI. Similar context, companies,
   products, people, industries, or ongoing storylines are explicitly allowed. AI may
   reject a candidate only when the discrete event and its core facts match, there is
   no materially new development, and confidence is at least 0.90. Ambiguous reviews
   default to acceptance.
4. The decision is cached by the new/prior content hashes and model namespace, so an
   interrupted or repeated run does not pay for the same comparison twice.

Completed, QA-passing `Video N` projects are backfilled automatically before a new news run. This
backfill performs no AI requests. Inspect it manually with:

```text
scripts\news_archive.bat status
scripts\news_archive.bat backfill
```

To reset all previous-news history without deleting it permanently, close any running
News Studio process and double-click `scripts\clean_news_history.bat`. Type `CLEAN` at the
confirmation prompt. The command moves `processed_urls.json`, `news_archive/`, and any
earlier reset policy into `data/history_backups/<timestamp>/`, creates empty URL and
semantic-duplicate stores, and records every currently existing `Video N` directory in
`news_history_reset.json`. Those old projects are then excluded from automatic archive
backfill, so the reset is not silently undone on the next run. Newly completed videos
are archived normally. Use `scripts\clean_news_history.bat --dry-run` to inspect the operation
or `scripts\clean_news_history.bat --yes` for an explicitly authorized non-interactive reset.
The cleaner refuses to run while a project lock exists.

Before a pulled page reaches the archive or summary model, it must pass the source
quality gate. The gate rejects HTTP errors, unsupported content types, access-denied
and CAPTCHA pages, title/source mismatches, unusually short or sentence-poor text,
low lexical diversity, and repeated boilerplate. If the full article fails, the RSS
body is allowed only when it independently passes the same checks. Long aggregator
headlines receive limited paraphrase tolerance when at least two significant title
terms remain grounded in the article; short titles retain the stricter threshold.
Consumer promotions are rejected deterministically before AI ranking and checked
again after full-article extraction. The gate looks for shopping-focused headline
structures such as deal roundups, currency/percentage discounts, lowest-price posts,
coupons, and shopping guides, with affiliate disclosures, repeated purchase calls,
and dense price lists retained as supporting evidence. Generic feed boilerplate such
as `AD` or `REG AD`, an embedded deal widget inside an otherwise legitimate article,
news about advertising systems, and corporate acquisition deals are not rejection
signals by themselves. This prevents RSS ad fragments from suppressing real news.
Newsletter issues and multi-story digest entries are also rejected locally before
ranking. Recognized forms include numbered issues, daily/weekly briefings,
`The Download:`-style bundles, several headlines joined with plus signs, and explicit
roundup titles. This keeps one selector candidate equal to one news event.
Ungrounded items in the model's audit metadata are discarded locally instead of
spending another summary request when the narration itself passes its structural and
direct-quote checks. Narration numbers are recorded in validation metadata but are not
matched against source-number strings and never trigger a paid rewrite; factual number
discipline therefore relies on the generation prompt and model review.

`RSS_FEED_TIME_INTERVAL_HOURS` in `.env` controls the RSS publication-age window in
whole hours. For example, `24` accepts feed entries published during the preceding
24 hours and rejects older entries before article extraction or paid AI processing.
`MIN_SCORE` is the primary editorial threshold (currently `50`). If primary stories
are rejected and fewer than `TOP_N` remain, the pipeline attempts the best lower-ranked
RSS reserves in order; those reserves still must pass every source, duplicate, and
factual-quality gate. AI-selected stories always remain above locally appended
reserves: a reserve's recency/keyword pre-score cannot promote an item that the model
omitted into the lead position. A rerun restores the validated prefix of an incomplete
project.
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
The broad Tom's Hardware feed is excluded because its mixture of benchmarks, reviews,
shopping posts, and news was not reliable enough for the general Tech Reports queue.

An editorially wrong story in an unfinished project can be replaced from that
project's saved RSS pool without regenerating the other four summaries:

```text
.venv\Scripts\python.exe src\news_pipeline.py --video 17 --category tech --repair-existing --replace-order 1 --replacement-url "https://example.com/replacement" --confirm-ai-cost
```

The replacement must come from `aday_haberler.json` and from a feed that is still
enabled. The command creates a recoverable project backup, invalidates stale stage
manifests, and preserves unaffected story artifacts. Any manual separator for the
replaced chapter must be recreated before resume.

Run a provider-free live feed audit with:

```text
.venv\Scripts\python.exe src\feed_audit.py --sample-size 20
```

It checks RSS health, recent-title technology relevance, commerce signals, and
clustered-newsletter signals without calling OpenAI or ElevenLabs. The regenerable
report is written to `data/feed_audit_latest.json` and excluded from Git.

Accepted source prose is normalized into short article paragraphs before it is saved
as `Haberler/*-orijinal.txt`. Whitespace-only formatting does not change duplicate
identity because the archive hashes normalized content.

Visual decisions use each exact timed window plus the full containing sentence, story
title, and entity context. Each window first tries Pexels Videos and then Pixabay
Videos. If neither has a verified clip, the unchanged image path tries the local
archive and Unsplash before producing a visibly designed contextual graphic. Empty or
solid-color frames are never used.

Only folders named exactly `Video N` participate in project numbering.

## Stable commands

```text
create_video.bat
news-studio.bat new --category tech --dry-run
news-studio.bat new --category tech --confirm-api-costs
news-studio.bat resume "Video N" --confirm-api-costs
news-studio.bat resume "Video N" --from-stage effects --confirm-api-costs
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

After `Continue from Video N` is selected in the interactive launcher, a second menu
offers every completed section as `rebuild from here` plus the first unfinished section
as the recommended default. Sections after an unfinished prerequisite are not offered.
For a completed production whose YouTube upload was skipped, the default is
`Upload/publish the completed package to YouTube`; this does not regenerate the title,
thumbnail, render, or other provider-backed artifacts. The `--from-stage` option exposes
the same safe rebuild boundary for direct CLI resumes.

`create_video.bat` is the autonomous launcher. Every timed choice waits at least 120 seconds
before taking its displayed default:
create a new Tech News project, allow the shown provider-cost plan, skip manual
separators, and approve the checksum-bound preview. The final upload choice also waits
at least 120 seconds and defaults to uploading the MP4 plus its thumbnail, title, description, and
chapter metadata. Press the alternative key during the countdown to override any
default. At the first production-action menu, press `Shift+Enter` to create a new video
and immediately accept every later timed default without countdowns. Plain `Enter`
accepts only the current default and leaves all later waits unchanged. The timeout values can be changed with
`AUTOMATION_DECISION_TIMEOUT_SECONDS` and `AUTOMATION_UPLOAD_TIMEOUT_SECONDS`.
After the configured number of news scripts pass validation, the launcher lists their
titles and asks which story should open the video and drive its YouTube title and
thumbnail. Choice 1 is the default after exactly 120 seconds. The selected story is
moved to order 1; every other story retains its ranked order. The choice is stored by
stable story ID before the manifest is changed, so a crash or retry replays the same
decision without another prompt or another paid news request. The default format is
three stories, with 350–390 words per narration and a 360-word editorial target.
`FIRST_BACKGROUND_MUSIC` in `.env` pins one audio file to that selected first story.
The path may be absolute (including Turkish characters and spaces) or relative to the
`Automation` directory. The remaining stories continue using the reproducible shuffled playlist;
a missing or unsupported configured first track stops the effects stage with an explicit
error instead of silently using different music.
`BACKGROUND_MUSIC_SECTION_SILENCE_SECONDS` controls the music-only silence between
stories and defaults to `1.0`. Each outgoing track fades out completely, the configured
silence runs immediately before the next story, and the incoming track begins fading in
at that story's start. `BACKGROUND_MUSIC_SECTION_FADE_SECONDS` controls both transition
fades. Narration and story timestamps are not shifted.
When manual separators are skipped, the first paid AI image for every story is used as
a chapter-start whole-story summary instead of adding another paid request. Its English
prompt describes a distressed vintage editorial collage: warm beige paper, black and
brick-red condensed typography, grayscale cut-paper subject, arrows and geometric flow,
supporting symbolic mini-scenes, halftone grain, torn edges, and muted mustard/dusty-teal
accents. The prompt requires English-only text and source-grounded setup, action,
consequence, and stakes; it explicitly forbids copying Turkish words, people, logos, or
story-specific symbols from the style reference. With real separators, ordinary timed
hero prompts remain unchanged.
The interactive launcher clears the screen between major decisions and stages. Its
compact preflight reports only the overall result and actionable warnings; full stage
output remains in each project's `orchestration/logs/` folder. During production, a
color progress panel shows the current stage, percentage, and completed/total count.
Completed rectangles are green, a failed stage segment is red, waiting states are
yellow, and remaining rectangles stay dim. Headless scheduled runs emit plain progress
snapshots without destructive screen clearing.

`AI_GENERATED_IMAGES_PER_STORY` controls the paid hero-image count for every story
(0-4, default 2). Setting it and `MAX_IMAGE_REQUESTS_PER_RUN` to `0` disables AI story
image planning and generation. The media-selection stage then covers the full
narration with stock videos, local/archive images, Unsplash images, or its safe local
fallback. Validated manual separators are still overlaid when present; skipped
separators let stock media begin at the chapter boundary. The optional AI story-video
checkpoint is skipped. `AI_GENERATED_THUMBNAIL_ENABLED=true` independently keeps one
dedicated AI-rendered publishing thumbnail, even when story images are disabled; set it
to `false` only to request the legacy stock/archive background with local text overlay.
For nonzero story-image counts,
keep `MAX_IMAGE_REQUESTS_PER_RUN` at least as high as `TOP_N` multiplied by that value
for a fresh complete production. When manual separators are skipped, a short page-flip
reveal is applied at the beginning of every chapter, including chapter 1.
`PAGE_FLIP_ENABLED` and `PAGE_FLIP_DURATION_SECONDS` control it; manual separators
disable the transition automatically.
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
   without losing state. It writes and opens `ORIGINAL_NEWS.txt` there with all ordered
   source stories, then opens the same folder as the separator save destination.
5. After storyboard generation, the launcher separately asks whether you will add one
   AI-generated video per story. The default is **No** after two minutes. Choosing No
   records a durable `SKIPPED` decision and keeps the storyboard images. Choosing Yes
   writes `04_ai_story_videos/AI_VIDEO_PROMPTS.txt` with a numbered English depiction,
   exact local start/end time, and duration for each generated interval, then opens the
   prompt sheet and destination folder. Save the five clips as `1.mp4`, `2.mp4`,
   `3.mp4`, `4.mp4`, and `5.mp4`; `.mov`, `.mkv`, `.webm`, and `.m4v` are also accepted.
   Validation checks count, names, checksums, video streams, dimensions, durations, and
   actual FFmpeg decoding. Accepted clips replace only the corresponding opening image
   intervals; their audio is discarded, longer clips are trimmed, and output is safely
   scaled/cropped to 16:9. Re-running preserves the decision and validates it again.
   This checkpoint is skipped automatically when AI images are disabled because there
   is no generated opening interval to replace.
6. `resume` then produces archive selections, captions, effects, and the 60-second
   review preview.
7. The review preview must be approved. Approval is tied to its SHA-256; replacing the
   preview invalidates approval automatically.
8. The final render runs only after approval.
   Per-image animation clips are deleted as soon as the base visual is built. Once
   the final master and preview pass QA, the remaining `08_render/intermediates/`
   directory is deleted. Final videos, previews, reports, and source assets remain.
9. Publishing copy uses short editorial hooks: titles target 4–7 words (50 characters
   maximum), while thumbnail copy is a distinct 2–3 word phrase capped at 18
   characters. With AI images enabled, one series-aligned AI thumbnail is generated or
   reused from cache. With AI images disabled, a selected lead-story stock/archive
   visual is normalized to 1280x720 and receives the normal local headline overlay.
   Its typography keeps a consistent left-side anchor but uses a flexible text zone,
   so the image model never compresses letter spacing to satisfy a rigid percentage.
   Thumbnail generation is text-to-image only: existing story-image pixels are never
   uploaded or reused, and the prompt requests a fresh, high-impact composition.
   The complete `09_publish/` package is then saved.
10. The launcher waits two minutes at the final upload decision and uploads by default.
   Complete one OAuth browser authorization first so later scheduled runs can use the
   cached refresh token without interaction.

The image-only animated TV overlay keeps its native alpha but defaults to 18% overall
opacity; the additional procedural noise/dropout glitch is disabled by default. The
obsolete static TV-frame layer is retired, while the subtle animated overlay remains
disabled during manual separators. The `SOUND LEVELS` block in `.env` keeps voiceover, separator,
paper/shutter/glitch SFX, background-music ducking, and final master loudness controls
in one place. Values ending in `_GAIN_DB` use `0` for unchanged level, negative values
for quieter audio, and positive values for louder audio. Music and final program
targets use LUFS; the final peak ceiling remains in dB.
Smooth archive-image zoom now uses `EFFECTS_MOTION_SCALE=1.025` and hero-image zoom
uses `EFFECTS_HERO_MOTION_SCALE=1.01875`, a 25% increase in zoom distance while
retaining cubic subpixel motion.
`shutter_sound.MP3` plays on every third actual image event; separators and repeated
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

When a child stage reports a genuine connectivity failure (DNS resolution, timeout,
connection reset/refusal, or an unreachable network), the orchestrator keeps the same
stage on screen and waits indefinitely. It probes the configured endpoints every
`NETWORK_RETRY_INTERVAL_SECONDS` seconds and reruns that same stage from its cached
project state after connectivity returns. Provider validation, quota, authentication,
and content errors are deliberately not treated as outages. Press Ctrl+C to cancel the
otherwise indefinite wait. `NETWORK_PROBE_TARGETS` may be changed to a comma-separated
list of `host:port` endpoints when the default probes are unsuitable.

Media-selector output is treated as a recommendation rather than trusted routing data.
Missing windows, foreign asset IDs, inconsistent relevance fields, and ungrounded
online entities are repaired deterministically from that window's own shortlist and
the complete story context. These recoverable model mistakes do not abort production.
The local-image selector sends no local image pixels. Candidate metadata must pass a local
context-grounding gate before Luna may select it: an exact company/product/person match
or at least one distinctive non-generic context term is required. Entity context is
carried across pronouns and split sentences, while choices below
`MEDIA_MIN_AI_CONFIDENCE=0.45` are rejected in favor of the online-provider path.
Stock-video previews are a separate visual relevance check made only before a provider
clip is downloaded.
Visual timing stays precise without discarding grammatical context. Long AI-hero beats
and archive windows target 7 seconds and are capped by
`MEDIA_MAX_WINDOW_SECONDS` (9 seconds by default),
prefer cuts at periods, commas, semicolons, colons, question marks, and exclamation
marks, and fall back to aligned phrase boundaries when needed. The selector receives
both the exact timed clause and its complete containing sentence, so a long sentence
can change images naturally while names such as Microsoft or Steve Jobs remain attached
to later clauses. Ambiguous verbs such as `launch` never imply spaceflight unless the
context also contains space-specific evidence such as a rocket, satellite, or orbit.
Each window tries Pexels Videos and then Pixabay Videos before any still-image choice.
A cached Luna-low planner uses the exact clause plus the complete sentence to produce
short, concrete stock-search phrases; the APIs receive at most seven words. Top result
previews are checked for visual relevance before download. A longer accepted clip is
center-trimmed to the exact narration duration and its provider audio is removed; a
shorter clip is rejected. Search results remain cached for 24 hours and downloaded
clips are validated and reused by provider ID and checksum. If both video providers
fail, local images are considered first and provider downloads cannot compete in that
curated shortlist. The existing Unsplash planner then preserves visible names such as
Google Earth and tries descriptive alternatives only when needed. If no acceptable
local or Unsplash result exists, the selector salvages the strongest slightly relevant
grounded local visual below the reuse cap. Only when no usable image exists does FFmpeg
create a unique, visibly designed context graphic under `05_visuals/safe_fallbacks/`;
solid blank frames are prohibited. The persistent chapter title remains visible and no
unrelated person, product, or event is implied.
Fallback uniqueness is verified from the rendered file checksum. If FFmpeg color
quantization makes two digest-derived graphics pixel-identical, the renderer
tries deterministic local variants until it produces distinct pixels; this collision
cannot abort archive selection or consume another AI request.
Legacy Commons download folders and folders named `Thumbnails` are excluded both when the media index is scanned and when
an in-video shortlist is built. The second guard also blocks thumbnail files lingering
in an older cached index; thumbnail creation and YouTube publishing remain unchanged.
Each raw per-story selector response is saved before validation under
`05_visuals/selector_cache/`. A matching fingerprint lets a resumed run reuse that
response without paying for the same selector request again.
`MEDIA_MAX_USES_PER_VIDEO=2` is a hard
SHA-256-based cap across the complete video, including stock clips, generated hero
images, local archive images and Unsplash downloads. Selection repairs and cached story resumes
must respect the same limit.
The current story may reuse its own hero image once, keeping the global two-use cap.
Hero checksums from other stories remain excluded from that story's archive windows.
`EFFECTS_MAX_VISUAL_HOLD_SECONDS=9` is an independent final cadence gate: effects and
render preparation stop if any still event, or any adjacent run of identical pixels,
meaningfully exceeds nine seconds. Newly split archive windows obey the hard limit;
the effects validator accepts at most 0.10 seconds of timestamp/alignment rounding so
an effective 9.067-second boundary cannot abort an otherwise valid cached project.
Full containing sentences still accompany shorter timed clauses
in the selector prompt, preserving relevance without creating long on-screen holds.
The 60-second review proof now reads the actual effects-plan visual events and their
real durations. It no longer substitutes three demonstration images lasting twenty
seconds each, so cancelling or approving from the preview reflects production cadence.

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
|-- 04_ai_story_videos/
|   |-- AI_VIDEO_PROMPTS.txt
|   |-- 1.mp4 ... 5.mp4
|   `-- ai_video_manifest.json
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

The upload manifest checksum-locks the final MP4 and thumbnail. At upload time, the
uploader reads the channel's actual private scheduled videos from YouTube, starts at the
nearest future 20:00 GMT+3 Turkey slot, and advances by the configured interval only
while a real YouTube video occupies the candidate slot. Local manifests and
`data/youtube_schedule_state.json` are never treated as reservations; the state file is
retained only for audit and recovery. This means a slot freed by publishing, moving, or
deleting a video in YouTube Studio is reused automatically. If live availability cannot
be verified, upload stops before creating a video instead of guessing. YouTube requires
scheduled videos to be inserted as private; `publishAt` automatically makes them public.

`src/youtube_schedule.py` is the single source of scheduling rules. Set
`YOUTUBE_PUBLISH_HOUR` and `YOUTUBE_PUBLISH_INTERVAL_DAYS` in `.env` to change the
release hour or cadence without editing code. The publish stage, uploader, and manual
reset utility all load that shared configuration when their process starts. New upload
manifests record a provisional configuration snapshot; the authoritative slot is always
recalculated from live YouTube status immediately before upload. An operating-system environment
variable with the same name takes precedence over `.env`. Configuration changes do not
retroactively modify videos already scheduled on YouTube.
Titles are published without a channel-name suffix, descriptions omit a SOURCES
section, and both metadata and original-audio language are set to English. YouTube then generates
and publishes automatic English captions when the video is eligible; the Data API has
no separate creator-side auto-caption switch. Direct uploader commands still require
`--confirm-upload`; the autonomous
launcher supplies it only after the visible two-minute opt-out countdown.

Before the first autonomous upload, save a Google OAuth Desktop-app credential as
`.youtube_credentials/client_secret.json`, then run
`scripts\youtube_authorize.bat`. Authorization grants upload and read-only channel access
so scheduling can inspect actual private scheduled videos. The one-time browser sign-in
creates the ignored refresh-token file without uploading a video. Existing upload-only
tokens require one new consent after this update; later runs reuse that token. A completed
upload is idempotent, and a run interrupted after the video but before its thumbnail
resumes the thumbnail step without uploading a duplicate video.

For unattended long-term use, do not leave the Google OAuth consent configuration in
`Testing`: Google expires testing authorizations (including offline refresh tokens)
after seven days. Use an appropriate production/personal-use OAuth configuration and
request only the `youtube.upload` scope used by this project.
