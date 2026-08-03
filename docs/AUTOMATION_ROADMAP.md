# Tech News Video Automation — Complete Roadmap

## 1. Target outcome

Build a resumable production system that turns ranked technology news into a finished
YouTube package:

- source articles and faithful narration scripts;
- ElevenLabs voiceovers with word/character timing;
- a deliberate manual checkpoint for the creator-made “Tech News” separator animations;
- a configurable 1-4 AI-generated hero images for each news story;
- context-matched archive images for the rest of every narration;
- restrained image motion, TV frame, glitch accents, sound effects, and repeated background music;
- synchronized subtitles;
- an FFmpeg-rendered final video;
- a thumbnail, title candidates, and publishing metadata.

The final system should be one-button, but it must remain stage-based internally. Every
stage must be independently testable, repeatable, and resumable.

## 2. What the current editing screenshot tells us

The intended timeline is not a generic slideshow. It has a consistent layered grammar:

1. Caption/text layer.
2. Story labels or supporting graphics when needed.
3. A global TV-frame/effect layer.
4. Manually produced “Tech News” separator animation before each story.
5. A global/light glitch layer.
6. A sequence of short supporting-image shots.
7. Voiceover clips.
8. A repeated background-music bed across the full program.

The automated render should preserve this grammar. The manual separator animations
remain human-made; the automation validates and places them.

## 3. Milestone 0 baseline

Completed on 2026-07-30:

- `Automation` is now a Git repository.
- `.env`, `.venv`, caches, portable binaries, and generated projects are excluded.
- `.env` remains beside the scripts for simple local loading; `.env.example` is tracked.
- `AUTOMATION_ROOT` resolves to the code/data directory and `PROJECTS_ROOT` resolves to
  `D:\Dosyalar\İş\YouTube\Tech News`.
- Numbered projects use the exact `Video N` form. Existing `Video 1` through `Video 4`
  make `Video 5` the next project.
- Legacy `Old` and `Video1` outputs were preserved under
  `Legacy Automation Outputs`, outside the code directory.
- FFmpeg and FFprobe 8.1.2 are installed portably and checksum-verified.
- `preflight.py` validates paths, secrets, dependencies, feeds, disk space, and the next
  project number before production begins.
- Whisper is not a dependency. ElevenLabs timestamps and forced alignment will handle
  all narration timing and caption alignment.

## 4. Core architecture decisions

### 4.1 One manifest is the source of truth

Each `Video N` project gets a `manifest.json`. All stages read and update this file.
Filenames are never used as the only source of meaning.

The manifest records:

- project ID, category, creation date, format, resolution, and frame rate;
- story order, rank, source URL, source text, summary, and factual-review status;
- voiceover path, duration, checksum, timing/alignment path, and TTS settings;
- separator animation path and duration;
- AI-image prompts, generation IDs, paths, and selected story beats;
- archive-image choices, source/license metadata, and exact in/out times;
- timeline events, captions, music, effects, render settings, and output paths;
- per-stage status, timestamps, input fingerprints, output fingerprints, and errors.

### 4.2 Explicit state machine

Use these normal states:

`NEW → NEWS_READY → VOICE_READY → WAITING_FOR_MANUAL_ANIMATIONS →`
`MANUAL_ANIMATIONS_READY → STORYBOARD_READY → VISUALS_READY →`
`CAPTIONS_READY → TIMELINE_READY → RENDERED → PUBLISH_PACKAGE_READY`

Exceptional states:

- `FAILED`: retain all successful artifacts and the exact error.
- `CANCELLED`: stop future work but retain artifacts.

Entering `0` at the manual checkpoint must **not delete anything**. Destructive cleanup
should be a separate, explicit command such as `--purge-project`.

### 4.3 Idempotent stages

A stage skips valid outputs whose input hashes have not changed. `--force-stage NAME`
rebuilds only that stage and invalidates its downstream dependents. Interrupted writes
use temporary files followed by atomic rename.

### 4.4 Timeline as data before video

Create `timeline.json` before rendering. It contains exact events such as:

```json
{
  "start": 42.18,
  "end": 47.90,
  "story_id": "02",
  "type": "archive_image",
  "asset": "D:/.../Assets/Media/Android/example.jpg",
  "motion": "smooth_push_in",
  "caption_segment_ids": [31, 32]
}
```

This makes image selection, captions, effects, music, and rendering auditable.

## 5. Implemented modular folder structure

The migration is complete. `src/file_locations.py` is the single path authority.
Generated production state is separated from source code and ignored by Git.

```text
Tech News/
├── Automation/
│   ├── .git/
│   ├── .env                    # ignored secrets
│   ├── create_video.bat
│   ├── news-studio.bat
│   ├── setup.bat
│   ├── src/
│   │   ├── file_locations.py
│   │   ├── news_studio.py
│   │   └── stage modules
│   ├── config/                 # feeds and channel profiles
│   ├── data/                   # ignored runtime state and indexes
│   ├── docs/
│   ├── references/
│   ├── scripts/
│   ├── tests/
│   └── tools/ffmpeg/
├── Assets/
│   ├── Audio/
│   │   └── background_music/
│   ├── Media/
│   ├── Media (No-Copyright)/
│   ├── Outro/
│   │   └── outro_sound/
│   └── Overlays/
├── Legacy Automation Outputs/
└── Video N/
    ├── project_info.json
    ├── state.json
    ├── Haberler/
    ├── Sesler/
    ├── 03_manual_separators/
    ├── 04_storyboard/
    ├── 05_visuals/
    ├── 06_captions/
    ├── 07_effects/
    ├── 08_render/
    ├── 09_publish/
    └── orchestration/
```

## 6. Implementation milestones

## Milestone 0 — Secure and normalize the existing system

**Status: complete (2026-07-30).**

### Work

1. Make `Automation` the code/data root and its parent the numbered-project root.
2. Replace hard-coded paths with validated `AUTOMATION_ROOT` and `PROJECTS_ROOT`
   values resolved relative to the scripts.
3. Back up current code and initialize Git.
4. Add `.gitignore` for `.env`, `.venv`, caches, logs, temporary files, model downloads,
   rendered video, and generated per-video artifacts as appropriate.
5. Commit `.env.example`, never `.env`.
6. Inventory existing scripts, old project folders, media folders, and reusable assets.
7. Add a preflight command that checks Python, FFmpeg, FFprobe, API configuration,
   writable directories, fonts, music, overlays, and available disk space.
8. Normalize UTF-8 console/file handling; the current README display shows encoding
   corruption in PowerShell output.

### Test

- Run the existing news and TTS flow on a copied fixture, not the only live project.
- Confirm every output stays under the actual `Automation` root.
- Confirm `git status` contains no secret or virtual-environment files.

### Done when

- One clean baseline commit exists.
- A preflight report passes.
- No output points to the obsolete root.

## Milestone 1 — Stabilize news, scripts, and TTS

**Status: complete (2026-07-30).**

### Work

1. Preserve the current two files per story:
   - faithful extracted original article;
   - 150–200 word narration script.
2. Add stable story IDs independent of titles.
3. Store ranking score and make the highest-rated story `story 01`.
4. Add factual checks for names, numbers, dates, quotations, and unsupported claims.
5. Validate file count, non-empty text, word range, source URL, and story ordering.
6. Wrap existing ElevenLabs logic as a stage.
7. Record voice settings, request IDs, character cost, duration, checksum, and retry
   history.
8. Preserve existing audio unless source text or TTS settings changed.

### Test

- Run one copied real `Video N` fixture with five stories without modifying the original.
- Compare output text/audio with the current working behavior.
- Simulate one bad API key, one quota failure, and one interrupted file write.
- Keep provider smoke tests very short; do not spend quota on a five-story test run
  merely to verify local validation and caching.

### Done when

- Five valid source/script pairs and five valid voiceovers are reproducibly generated.
- A rerun does not charge for or overwrite unchanged narration.

### Implemented verification

- Seventeen offline unit/integration tests pass.
- The copied five-story legacy fixture contains five source/script pairs in the
  150–200 word range and five FFprobe-valid MP3 files.
- Bad-request, quota-key fallback, incomplete-manifest, stale-file, invalid-audio,
  and interrupted-atomic-write behavior are tested without contacting a provider.
- API key slots are numbered without a fixed maximum. Requests are not disguised
  through IP rotation; configured keys must be used within provider rules.

## Milestone 2 — Extract speech timing and story beats

**Status: complete (2026-07-30).**

### Primary timing method

Switch ElevenLabs generation to its “with timestamps” response, which provides
character-level alignment, then aggregate it into words, phrases, and sentences.
For existing MP3 files, use forced alignment against the already known script.

Do not retranscribe synthetic narration. All timing work uses the approved canonical
script plus ElevenLabs alignment, avoiding text substitutions and an extra local model.

### Work

1. Save `alignment.json` for every voiceover.
2. Generate exact sentence start/end times.
3. Detect natural visual beats from sentence boundaries and pauses.
4. Generate an initial story timing map with:
   - narration in/out;
   - important claims;
   - named people, companies, products, and places;
   - suggested visual queries;
   - “must show” versus “supporting” importance.
5. Start each separator as a visual overlay at the same instant as its narration.

### Test

- Render a diagnostic video with a timer, transcript, and sentence-boundary markers.
- Spot-check first, middle, and last words for drift.

### Done when

- The final narration word ends within 100 ms of the alignment end.
- No story overlaps the next story; a separator intentionally overlaps its own narration.

### Implemented verification

- New TTS uses the timestamped endpoint and stores original-text character alignment
  beside each MP3.
- Existing MP3 files use forced alignment with the approved narration; they are not
  retranscribed.
- Alignment is rejected if reconstructed text differs from the canonical script,
  timestamps are non-monotonic, hashes are stale, or the final-word delta exceeds
  100 ms.
- Character timing is deterministically aggregated into words, phrases, sentences,
  named-entity candidates, important claims, visual queries, and two initial
  `must_show` beats per story.
- `story_timing.json` starts each separator and narration together and validates that
  distinct story ranges do not overlap.
- Twenty-eight offline tests pass, including a copied five-story timeline and an
  FFmpeg timer/transcript diagnostic render.
- A live 12-character timestamp smoke test succeeded with a 0 ms final-word delta and
  a reported character cost of 12.

## Milestone 3 — Manual “Tech News” separator checkpoint

**Status: complete (2026-07-30).**

This is the deliberate human pause.

### User-facing behavior

After news text and TTS are complete, display:

> The summary and the original news texts are saved, waiting for the animation
> production. Press Enter after ready, or 0 to cancel.

The prompt must also show:

- project folder;
- number of required separator animations;
- expected filenames;
- accepted codecs/resolution/frame rate;
- where to place them.

Recommended naming:

```text
03_manual_separators/
├── 01_separator.mp4
├── 02_separator.mp4
├── 03_separator.mp4
├── 04_separator.mp4
└── 05_separator.mp4
```

### Resume behavior

On Enter, validate every required file with FFprobe:

- video decodes successfully;
- supported frame rate; non-output resolutions are accepted and normalized with
  aspect-ratio-preserving scale and padding during render;
- supported pixel format;
- non-zero duration;
- audio presence is known and handled consistently.

On `0`, set state to `CANCELLED`, preserve all work, and print the resume command.

### Test

- Missing file, corrupt file, alternate resolution, and valid file cases.
- Close the console while waiting, then resume later without repeating prior stages.

### Implemented verification

- `03_manual_separators/` is created automatically before the workflow pauses.
- The checkpoint persists `WAITING`, `VALIDATING`, `READY`, or `CANCELLED` in
  `state.json`; closing the console preserves `WAITING`.
- `resume_video.bat` opens the newest numbered project directly at this checkpoint,
  without repeating news, TTS, or alignment.
- FFprobe checks container metadata, input resolution, codec, frame rate, pixel format,
  duration, and audio presence. Alternate resolutions are marked for normalization,
  and FFmpeg fully decodes every accepted stream.
- Audio presence follows a configurable policy and defaults to consistent across all
  separators.
- Validated real separator durations replace placeholder durations in
  `story_timing.json`; separators remain opening overlays and do not delay narration.
- File hashes invalidate `READY` if an approved separator is later changed or removed.
- Offline tests cover expected filenames, missing and corrupt files, alternate-resolution
  normalization, mixed audio, Enter-to-continue, cancel, close-window, and resume cases.

### Done when

- The project can wait indefinitely and resume safely after a restart.

## Milestone 4 — Storyboard and configurable AI images per news story

**Status: complete (2026-07-30).**

### Work

1. The AI reads each narration plus source text and alignment map.
2. It selects exactly two important, visually distinct story beats.
3. Selection rules:
   - prefer concrete events, products, people, or consequences;
   - avoid two images showing the same idea;
   - do not generate logos, UI screenshots, or factual diagrams when archive material
     is more accurate;
   - avoid unsupported visual claims;
   - keep the first/highest-rated story visually strongest because it also drives the
     thumbnail.
4. Save `storyboard.json` before generating anything.
5. Each prompt records story ID, beat text, time range, aspect ratio, visual style,
   negative constraints, and generation seed/ID when available.
6. Generate 16:9 images with safe space for captions.
7. Validate image dimensions, readability, and duplicates.

### Test

- Generate images for one story first.
- Render a 30-second proof containing its separator, voiceover, two hero images, and
  temporary archive fillers.

### Implemented verification

- `gpt-5.6-luna` reads the approved narration, bounded original source text, and the
  timed candidate map, then returns exactly two distinct candidate IDs through a
  strict structured-output schema.
- The application appends a stable API-ready prompt contract: native 1280x720
  16:9 composition, central crop safety, a calm bottom 22% caption area, factual
  boundaries, and negative constraints for text, logos, UI, charts, diagrams,
  watermarks, and unsupported details.
- `storyboard.json` is atomically saved before any image request. It records story
  ID, exact local/global time range, prompt, style, constraints, model, quality,
  request/generation ID, nullable seed, checksums, and validation results.
- Pixel generation uses the dedicated `gpt-image-2` Image API model. Production
  defaults to balanced `medium` quality; the isolated live smoke uses `low`.
- Paid generation requires `--confirm-cost`, respects a hard request limit, caches
  unchanged valid images, and never auto-buys a retry for a visual duplicate.
- Approved images are copied into `Assets/Media/AI Generated` with descriptive scene
  filenames and checksum suffixes for later indexing and reuse.
- FFprobe/FFmpeg verify dimensions and decodability. A perceptual dHash rejects
  near-duplicates, while a local lower-third luma/edge proxy records caption-safe
  readability warnings without another paid vision request.
- The pilot renderer creates a 30-second proof with the validated manual separator,
  narration, both hero images, and a temporary archive filler.
- Forty-two offline tests pass, including saved-before-generation, exact timing,
  cost blocking, cache reuse, duplicate rejection, and the 30-second proof.
- One authorized live low-quality 1280x720 request verifies current provider access;
  the temporary test image is deleted after validation.

### Done when

- Every story has the configured number of approved hero images assigned to exact narration ranges.

## Milestone 5 — Index and select archive imagery

**Status: complete (2026-07-30).**

Do not send the full folder tree to an AI on every run. Index once and update only
changed files.

### Media index

For every image, store:

- absolute and project-relative path;
- filename and folder/category;
- extension, width, height, aspect ratio, checksum;
- parsed filename keywords;
- optional local visual caption/embedding;
- source URL, creator, license, and attribution requirement;
- quality flags and prior-use count.

The current `Assets/Media` and `Assets/Media (No-Copyright)` repositories are
configured sources. The second repository is classified as
`user_confirmed_no_copyright` based on the owner's confirmation; it is not marked for
verification and receives no warning or ranking penalty.

### Selection flow

1. Split each story’s uncovered narration into 3–7 second visual windows at sentence or
   phrase boundaries.
2. Extract entities and scene intent for each window.
3. Retrieve a small local shortlist using filename/category/semantic similarity.
4. Send only the shortlist and context to the AI for final selection.
5. Apply constraints:
   - no immediate duplicate;
   - avoid reusing the same asset in one video unless intentional;
   - favor 16:9 or crop-safe images;
   - match the precise spoken claim, not merely the company name;
   - fall back to a neutral category image when confidence is low.
6. Save every decision, confidence, and alternative in `media_plan.json`.
7. Copy selected assets into the project or record immutable source paths and hashes.

If the AI explicitly returns `images_are_irrelevant` for every local candidate, query
Wikimedia Commons by exact named entity plus scene intent. Google/Yandex Images may be used only
to discover an original source page, never as an automatic download source. Reject
the fallback unless the title/description/category matches the named entity and the
image itself passes visual relevance review. Save accepted downloads under
`Assets/Media/Online Retrieved` with a descriptive filename and a JSON sidecar that
records source page, creator, license, license URL, retrieval date, query, checksum,
and relevance evidence.

### Test

- Evaluate at least 20 manually labeled narration windows.
- Track top-1 acceptance, wrong-entity rate, duplicate rate, and crop failures.

### Implemented verification

- `media_index.py` scans both media repositories and stores path, dimensions, aspect
  ratio, SHA-256, parsed filename/folder keywords, local caption, repository,
  metadata sidecar fields, quality flags, and prior-use events.
- Rescans compare path, size, modification time, and metadata-sidecar time. Unchanged
  files are reused, changed files are reprobed, and checksum matching preserves asset
  identity and usage history after a rename. Media files are never renamed or edited.
- `media_index.bat` provides a double-click rescan. Selection also rescans
  automatically before making a plan.
- Approved hero-image ranges are subtracted from narration. Remaining time is split
  into 3–7 second windows at phrase, sentence, or visual-beat boundaries.
- Local retrieval sends only an eight-item shortlist per window to
  `gpt-5.6-luna`; a zero-cost deterministic selector is available for tests and
  emergency offline production. Paid final selection requires explicit confirmation.
- Production enforcement avoids adjacent duplicates, same-story reuse, and crop-risk
  choices whenever a safe shortlist alternative exists.
- An explicit structured `images_are_irrelevant` decision triggers the optional,
  capped Wikimedia provider. Exact entity/title evidence, supported dimensions and
  commercial-reuse license metadata are required; failed searches safely return to
  the local shortlist instead of inserting a wrong-entity web image.
- Online images and attribution sidecars live separately under `Assets/Media/Online
  Retrieved/Wikimedia Commons`. Search manifests are stored per video, and the whole
  provider can be disabled with `WIKIMEDIA_FALLBACK_ENABLED=false`.
- `media_plan.json` records exact local/global time ranges, immutable source path,
  SHA-256, dimensions, repository classification, confidence, reason, override,
  alternatives, and full shortlist.
- Six new offline index/selection tests pass (48 total). A 20-case benchmark against the
  currently renamed assets reached 20/20 top-1 acceptance, zero wrong-entity results,
  zero adjacent duplicates, and recorded two raw retrieval crop risks. Production
  crop enforcement replaces those raw risky choices.
- No OpenAI or ElevenLabs request was made during Milestone 5 verification.

### Done when

- Every non-separator narration interval has a valid visual.
- At least 80% of selections are accepted without manual replacement in the pilot.

## Milestone 6 — Motion, TV treatment, effects, sound, and music

**Status: complete (2026-08-01).**

### Image motion

Hero and archive images use a restrained center push-in rendered with FFmpeg's
floating-point `perspective` transform and cubic interpolation. Motion follows a
smoothstep curve so it accelerates and decelerates gently. The quantized `zoompan`
path is prohibited because its whole-pixel crop positions create visible stepping.
One-second inter-story holds remain static; no directional pan, reverse zoom, or
random movement is used.

### TV frame and glitch

Treat these as controlled timeline layers:

- a transparent TV-frame/scanline overlay across the program when desired;
- short glitch accents at separators and selected cuts;
- optional RGB split, horizontal displacement, noise, or tape dropout;
- intensity capped so captions and faces remain readable;
- never apply heavy glitch continuously.

Build the initial version with FFmpeg filters and reusable overlay assets. Match the
screenshot’s “Shutter Frame” and “Tape Dropouts” feel using a short approved reference
render before applying it to a complete video.

### Per-section background music

1. Add licensed background tracks to `Assets/Audio/background_music/`.
2. Shuffle them reproducibly for each video and assign a different track to each
   news section whenever the library contains enough tracks.
3. Trim long tracks at the section boundary and loop short tracks only inside their
   assigned section.
4. Apply loudness normalization.
5. Duck the music automatically while narration is active.
6. Permit slightly higher music during separators if it does not conflict with their
   audio.
7. Crossfade adjacent section tracks, and add short fades at the program start/end.
8. Record every assignment, license, checksum, timeline window, and mix setting in the
   manifest.

Starting mix targets should be treated as test values, not laws:

- narration: clear and dominant;
- background music: roughly 18–24 dB below narration during speech;
- separator/SFX peaks: controlled and non-startling;
- final YouTube mix: loudness and true-peak checked before approval.

### Test

- Render 60 seconds containing a separator, several stills, captions, a loop boundary,
  glitch accents, voiceover, and music ducking.
- Listen on headphones, laptop speakers, and a phone.

### Done when

- Voice remains intelligible, music loops are not distracting, and effects look
  intentional rather than constant.

### Implemented

- `effects_stage.py` validates finalized separators, complete Milestone 5 coverage,
  and basic captions before writing `07_effects/effects_plan.json`.
- Every hero/archive interval receives a deterministic, center-safe subpixel push-in.
  `motion_filters.py` owns the FFmpeg expression, and render-cache versioning prevents
  older quantized segments from being reused. Directional pans and the `zoompan` path
  are prohibited.
- Each boundary between stories has an exact one-second silent hold. The prior
  image and repeated music continue, while narration, captions, and story title stop.
- The native-alpha `green-screen-tv-effect.mov` loops over story images and gap holds.
  It sits above tape/glitch treatment but below manual separators and is explicitly
  disabled during every separator range. Its audio stream is ignored. The opaque
  TV frame, title, attribution, and captions remain above it.
- `capcut_screenshot.png` is hashed into the plan as the canonical track-order
  reference: captions, TV/shutter frame, separators, tape-dropout glitch, visuals,
  voice/SFX, then repeated music from top to bottom.
- Glitch accents occur only at separator exits and selected cuts, are spaced apart,
  and cover less than 3% of the program. The paired glitch SFX is generated locally.
- User-supplied background music is reproducibly shuffled per video. Each story gets a
  different song when enough tracks exist; long songs are trimmed, short songs repeat
  only inside that story, and adjacent songs overlap with a one-second fade-out/fade-in.
  Every assignment, checksum, license, source duration, timeline window, and loop count
  is recorded.
- `effects_video.bat` produces the plan and a real 60-second H.264/AAC reference
  render with motion, TV frame, glitch/SFX, captions, voice, and repeated music.
- Three Milestone 6 tests include a real FFmpeg audio/video render; the full suite is
  provider-free.

## Milestone 7 — Captions

**Status: basic SRT complete (2026-07-30); advanced styling deferred.**

### Work

1. Build captions directly from the canonical narration and TTS alignment.
2. Export both:
   - `.srt` for YouTube/editing portability;
   - `.ass` for styled burned-in captions.
3. Caption layout rules:
   - one or two lines;
   - readable phrase boundaries;
   - sensible characters per line;
   - no orphaned article/preposition at a line end;
   - minimum display duration;
   - caption-safe lower-third area;
   - optional active-word highlighting only after the basic version is stable.
4. Offset each story’s local alignment by its story start; the separator begins at
   that same timestamp.
5. Validate monotonic timestamps, overlaps, missing text, final duration, and safe-area
   placement.

### Test

- Compare the first and last caption of every story.
- Render captions over both bright and dark imagery.

### Basic SRT implemented verification

- `caption_stage.py` reads only the approved canonical narration, its saved
  ElevenLabs alignment, and the finalized separator/narration offsets in
  `story_timing.json`. Whisper and provider API calls are not used.
- Caption chunks follow the saved phrase and exact word boundaries, use one or two
  lines, and default to 42 characters per line, nine words, and a 350 ms minimum
  target.
- The stage writes a combined `06_captions/captions.srt`, per-story SRT files, and an
  auditable `captions.json` containing checksums, input fingerprint, cue counts, and
  first/last-caption timing.
- Validation rejects stale alignment text, non-positive or overlapping cues,
  unexpected separator overlap in legacy timing mode, line-limit failures, and
  captions beyond the project duration. Overlay timing intentionally permits it.
- A valid unchanged package is reused by checksum without rewriting its SRT files.
  Changed alignment, narration, settings, or finalized separator timing invalidates
  the cache.
- Five caption tests pass offline. The full 53-test suite passes without OpenAI or
  ElevenLabs usage.
- `captions_video.bat` runs the caption stage for the latest project, while
  `test_program.bat` runs preflight, all offline tests, and media-index status.
- `.ass`, burn-in styling, caption-safe visual rendering, and active-word highlighting
  are intentionally left for Milestone 7 advanced.

### Done when

- Caption text exactly matches the approved narration and stays synchronized through
  the final word.

## Milestone 8 — Build the FFmpeg timeline and final render

**Status: complete (2026-07-31).**

### Work

1. Generate a normalized intermediate for every separator animation.
2. Build story sequences from timeline events.
3. Compose layers in this order:
   - base visual;
   - image motion/crop;
   - glitch accents;
   - native-alpha animated TV effect, excluding separator intervals;
   - separator or supporting graphics;
   - TV frame;
   - persistent story title;
   - captions;
   - voiceover, separator audio, SFX, and looped/ducked music.
4. Use a consistent production format, initially:
   - 1920×1080;
   - 30 fps unless the manual animation reference requires another value;
   - H.264 delivery video;
   - AAC 48 kHz audio;
   - `yuv420p` compatibility.
5. Produce:
   - low-resolution preview;
   - final master;
   - optional clean master without burned-in captions;
   - render report from FFprobe.
6. Add an outro only after the main sequence is stable.

### Automated QA

- file opens and decodes;
- expected resolution/frame rate/codecs;
- audio and video durations agree within tolerance;
- no black gaps, missing assets, frozen invalid frames, or silent narration;
- no timeline overlaps outside intentional transitions;
- captions do not extend past video end;
- output size and duration are plausible;
- loudness and peak checks pass.

### Done when

- A complete `Video N` project renders from artifacts with one command and passes automated plus
  human review.

### Implemented

- `render_stage.py` and `render_video.bat` create cached 1080p visual intermediates,
  normalized separators, a final master, a 540p preview, and an FFprobe-backed report
  under `08_render/`.
- Separator video/audio, narration, captions, and persistent story title begin
  together at each story start.
- Titles stay visible for their complete narration range. Captions are burned in at a
  raised lower-third position, and non-transparent TV-frame pixels are forced opaque.
- Attribution-required assets receive a compact source label at the bottom-right
  inside the TV frame for only their visible interval; CC0/public-domain assets do
  not receive a label.
- Four exact one-second waits produce the current 473.066-second Video 5 render.
- Finite video and audio tracks are rendered independently before muxing, making
  retries recoverable and avoiding Windows FFmpeg shutdown deadlocks.
- QA validates actual video and audio stream durations, 1920x1080/30 fps H.264
  `yuv420p`, AAC 48 kHz stereo, preview format, hashes, and measured sample peak.

## Milestone 9 — Thumbnail and YouTube title package

The first story is the highest-rated story and drives the publishing package.

### Work

1. Use story 01’s source, approved narration, ranking rationale, and strongest visual
   concept.
2. Generate:
   - 5–10 title candidates;
   - one recommended title with a reason;
   - 2–3 thumbnail concepts;
   - final 1280×720 thumbnail variants;
   - short thumbnail text options;
   - description draft, chapter timestamps, source links, and tags.
3. Keep titles accurate:
   - no claim stronger than the source;
   - important company/product term early;
   - avoid duplicating all thumbnail text in the title;
   - no fabricated quotation or number.
4. Score candidates for clarity, curiosity, specificity, and factual risk.
5. Save all publishing output in `09_publish/`.

### Test

- Verify the final title and thumbnail against the original article, not only the
  summary.
- Check thumbnail legibility at phone size.

### Done when

- One approved title and thumbnail pair plus a complete metadata package exist.

### Video 5 implementation report (complete, 2026-08-01)

- Story 01 is verified as the highest-rated lead before any publishing output is made.
- One cached low-reasoning editorial request produced seven scored title candidates,
  concepts, description copy, tags, and hashtags; reruns are free while inputs match.
- The selected striking title remains source-grounded and under 100 characters.
- Five valid chapters start at `0:00`, remain at least ten seconds apart, and are
  embedded verbatim in the final YouTube description with all five source URLs.
- The final 1280x720 thumbnail follows `tech_reports_ai_headline_v1`: headline left
  46%, story depiction right 54%, black/high-contrast layout, and a single red/yellow
  emphasis. Its exact headline is AI-rendered into the image.
- Final thumbnail, generation prompt, checksum, alignment contract, descriptive
  filename, and metadata sidecar are archived in `Assets/Media/Thumbnails`.
- `youtube_upload_manifest.json` locks the final video and thumbnail by SHA-256.
  Offline validation uses no quota. The compatible uploader uses OAuth, resumable
  chunks, private-by-default status, explicit confirmation, and duplicate protection.

## Milestone 10 — One-button orchestration and recovery

**Status: complete (2026-08-01).**

### Commands

Provide a small stable command surface:

```text
create_video.bat
news-studio new --category tech
news-studio resume "Video N"
news-studio status "Video N"
news-studio run-stage "Video N" captions
news-studio validate "Video N"
news-studio preview "Video N"
news-studio render "Video N"
```

### One-button behavior

1. Preflight.
2. Create next project safely.
3. Produce news/source/scripts.
4. Produce TTS and alignment.
5. Offer the manual checkpoint, defaulting to separator-free continuation after 30 seconds.
6. Resume after separator validation, or persist `SKIPPED` and continue immediately.
7. Storyboard and generate the configured AI-image count per story.
8. Select archive images.
9. Generate captions and timeline.
10. Mix effects/music and render preview.
11. Offer preview approval, defaulting to approval after 30 seconds.
12. Render final and create publishing package.
13. Offer YouTube upload, defaulting to upload after a two-minute opt-out window.

### Recovery

- `resume` starts from the first incomplete/invalid stage.
- Network stages use bounded retries and preserve request IDs.
- API-cost stages show planned request count before execution.
- Every stage writes a structured log plus a concise user-facing error.
- Ctrl+C and shutdown leave the project resumable.
- A lock file prevents two processes from mutating the same `Video N` project.
- A `dry-run` shows planned actions without API calls or writes.

### Done when

- A new project can complete unattended when the separator-free defaults are accepted;
  manual separators and stopping before upload remain available countdown choices.

### Implementation report (complete, 2026-08-01)

- `news-studio.bat` exposes `new`, `resume`, `status`, `run-stage`, `validate`,
  `preview`, `approve-preview`, and `render`; `create_video.bat` is the interactive
  one-button entry point.
- Resume starts at the first incomplete manifest-backed stage. Valid cached stages are
  skipped, including provider stages.
- `--dry-run` returns the complete action and cost plan before creating a project; it
  performs zero writes and zero API requests.
- Provider stages require `--confirm-api-costs`; story images, publishing copy, and
  thumbnail generation retain their internal confirmation gates.
- An exclusive per-project lock prevents concurrent mutation. Explicit stale-lock
  recovery is available only after the recorded process stops.
- Structured JSONL events and full per-stage logs make failures auditable. Ctrl+C
  preserves successful outputs and releases the current process's lock.
- The manual separator checkpoint and preview approval are resumable. Preview approval
  is tied to the proof-video SHA-256 and is invalidated when that file changes.
- The autonomous launcher defaults to a new Tech News project, paid-stage approval,
  separator skipping, preview approval, and final YouTube upload. Manual choices wait
  30 seconds; upload waits 120 seconds. The skipped mode keeps visual order tied to
  narration and excludes manual-separator-covered beats from paid hero generation.
- The one-button terminal keeps a stage-level progress bar underneath live output;
  completed blocks are green and a failed stage segment is red, while
  scheduled/headless runs receive plain percentage snapshots suitable for captured
  logs.
- Final publishing automatically generates or reuses one medium-quality, series-aligned
  AI thumbnail and the complete Milestone 9 package.
- Video 5 is recognized as complete by the new orchestrator without provider calls.

## 7. Recommended implementation order

Use one real project fixture at the end of every phase.

1. Milestone 0: Git, path cleanup, preflight, safe project root.
2. Milestone 1: stabilize the current news + TTS behavior.
3. Milestone 2: alignment and timing manifest.
4. Milestone 3: manual separator checkpoint and resume.
5. Milestone 7 basic: plain SRT from alignment.
6. Milestone 4: storyboard and two AI images for one story, then all stories.
7. Milestone 5: media index and archive selection.
8. Milestone 6: image motion, TV frame, glitch, SFX, repeated music.
9. Milestone 8: preview and final FFmpeg render.
10. Milestone 9: title, thumbnail, description, chapters, sources.
11. Milestone 10: one-button orchestration, caching, and recovery.
12. Milestone 7 advanced: styled captions or word highlighting only after stability.

Basic SRT is intentionally implemented early because it is a simple proof that the
alignment and global timeline are correct.

## 8. Pilot strategy

### Pilot A — 30 seconds

One story excerpt, one separator, two images, captions, TV frame, glitch accent,
voiceover, and looped/ducked background music.

### Pilot B — One complete story

Full separator plus narration, two AI hero images, archive fillers, captions, music,
effects, and preview render.

### Pilot C — Existing complete `Video N`

Rebuild an already edited project and compare:

- duration;
- story order;
- visual pacing;
- caption sync;
- voice/music balance;
- manual replacement count;
- render time.

### Pilot D — Fresh one-button project

Use new news data, stop at the manual checkpoint, resume, approve preview, and finish
the publish package.

Only after Pilot D passes twice should the flow be considered production-stable.

## 9. Quality gates

A final render is blocked unless:

- every story has source text, approved narration, audio, timing, and visuals;
- every expected separator exists and passes FFprobe;
- `AI_GENERATED_IMAGES_PER_STORY` assigns 1-4 AI hero images per story (default 2);
- every narration interval is visually covered;
- media licensing/attribution status is recorded;
- captions match the canonical narration;
- background music covers the whole timeline and has no audible broken loop;
- no missing files, invalid paths, or stale hashes exist;
- preview approval is recorded;
- final video passes codec, duration, loudness, and visual sanity checks;
- title/thumbnail claims are supported by the original first story.

## 10. Decisions to lock during the first implementation phase

These do not block the roadmap, but must become configuration values:

- target resolution and frame rate;
- separator file format and whether separators carry audio;
- exact background-music file and its license;
- final narration/music/SFX mix targets;
- TV frame asset and preferred glitch intensity;
- average still-image duration;
- caption font, colors, placement, and whether subtitles are burned in;
- image-generation provider and budget;
- whether selected archive files are copied into each project or referenced by hash;
- whether the final output contains an intro/outro in addition to separators.

## 11. Technical references behind key choices

- ElevenLabs can return character-level timestamps with generated speech:
  <https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps>
- ElevenLabs forced alignment can align existing audio with known text and return word
  timings:
  <https://elevenlabs.io/docs/api-reference/forced-alignment/create>
- FFmpeg documentation:
  <https://ffmpeg.org/documentation.html>
