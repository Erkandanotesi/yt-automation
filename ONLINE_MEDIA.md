# Optional online media experiment

This experiment is deliberately isolated from the production orchestration. It does
not modify `media_selection_stage.py`, the existing `media_plan.json`, or the final
render unless the user explicitly promotes a downloaded image later.

## Why Wikimedia Commons first

Wikimedia Commons has a stable, keyless MediaWiki API, structured creator/license
metadata, full-resolution or resized download URLs, and strong coverage for named
companies, products, people, locations, and logos. It is the first provider for exact
tech-news entities. Openverse can be added later as a second provider for generic
conceptual photography.

Google Images and Yandex Images are discovery engines, not downloadable media
repositories. Their thumbnails are never downloaded automatically.

## Safety and relevance gates

The standalone `online_media_stage.py` utility:

1. searches only Wikimedia's file namespace and requests bitmap files;
2. accepts public-domain/CC0, CC BY, and CC BY-SA metadata;
3. rejects noncommercial, no-derivatives, unknown, undersized, or unsupported files;
4. requires the complete entity phrase in the file title/object name—not merely in a
   long description or broad category;
5. scores query/context evidence but returns no result when the exact entity cannot be
   proven;
6. downloads only when `--download-best --confirm-download` are both present;
7. writes a descriptive filename and adjacent `.metadata.json` attribution sidecar.

The API client sends a descriptive user agent, uses only one request at a time, and
retries temporary throttling/server errors with backoff. Set `WIKIMEDIA_USER_AGENT`
to a more specific value with your contact URL or email before regular production use.

## Commands

Search only:

```powershell
.\.venv\Scripts\python.exe online_media_stage.py `
  --video 5 `
  --query "Microsoft Outlook logo" `
  --entity "Microsoft Outlook" `
  --context "Microsoft Outlook Web Access OWA email cybersecurity attack"
```

Download the best accepted candidate:

```powershell
.\.venv\Scripts\python.exe online_media_stage.py `
  --video 5 `
  --query "Microsoft Outlook logo" `
  --entity "Microsoft Outlook" `
  --context "Microsoft Outlook Web Access OWA email cybersecurity attack" `
  --download-best --confirm-download
```

Double-click `online_media.bat` for a search-only prompt. Search manifests are stored
under `Video N/05_visuals/online_searches/`. Downloads and their sidecars are stored
under `Assets/Media/Online Retrieved/Wikimedia Commons/` and are discovered by the
normal media-index rescan.

## Current trial

The initial broad search returned two Motorola phone photos whose descriptions happened
to mention Outlook. They were correctly treated as a failure and never downloaded.
After tightening the title gate, the same workflow returned exactly one exact-title
candidate: `Microsoft Outlook-Logo.wine.png`, marked CC0 on its Commons file page.

The image was downloaded, visually inspected, indexed, and rendered into a separate
six-second proof under `Video 5/05_visuals/online_trial/`. The current Video 5 media
plan and final video were intentionally left unchanged.

## Complete removal

If this experiment is not useful, remove only:

- `online_media_stage.py`
- `online_media.bat`
- `ONLINE_MEDIA.md`
- `tests/test_online_media_stage.py`
- `Assets/Media/Online Retrieved/`
- each `Video N/05_visuals/online_searches/` and `online_trial/` folder

Then run `media_index.bat` once. No production code rollback is needed.
