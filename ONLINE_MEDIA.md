# Optional Wikimedia Commons provider

The downloader stays in a separate module, but it is now available to the production
Milestone 5 selector. It runs only after the AI explicitly returns
`images_are_irrelevant` for every local candidate. Disable it at any time with
`WIKIMEDIA_FALLBACK_ENABLED=false`; the local media workflow continues unchanged.

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
6. downloads only after explicit standalone confirmation or a confirmed production
   AI-selection run;
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

## Production fallback

`media_selection_stage.py --confirm-ai-cost` lets the selector return an explicit
irrelevant result plus a grounded exact entity and search query. The integration:

1. reuses a matching indexed Commons asset before making a network request;
2. caps searches and downloads across the complete video;
3. throttles requests and stops further searches if Commons returns HTTP 429;
4. downloads only the best exact-title, accepted-license candidate;
5. rescans the media index and records the immutable selected path and checksum;
6. propagates creator/license/source metadata into effects and render manifests;
7. shows `Source: creator · Wikimedia Commons · license` at the bottom-right inside
   the TV frame only when the license requires attribution.

CC0 and public-domain images are intentionally rendered without a source label.

## Verified trial

The initial broad search returned two Motorola phone photos whose descriptions happened
to mention Outlook. They were correctly treated as a failure and never downloaded.
After tightening the title gate, the same workflow returned exactly one exact-title
candidate: `Microsoft Outlook-Logo.wine.png`, marked CC0 on its Commons file page.

The image was downloaded, visually inspected, indexed, and rendered into a separate
six-second proof under `Video 5/05_visuals/online_trial/`. It was then selected by the
production fallback for two Microsoft Outlook windows in Video 5 and included in the
rebuilt final video. Its CC0 metadata correctly suppresses the attribution overlay.

## Complete removal

To stop using online media without deleting anything, set:

```text
WIKIMEDIA_FALLBACK_ENABLED=false
```

For full removal, first disable the setting, then remove only:

- `online_media_stage.py`
- `online_media.bat`
- `ONLINE_MEDIA.md`
- `tests/test_online_media_stage.py`
- `Assets/Media/Online Retrieved/`
- each `Video N/05_visuals/online_searches/` and `online_trial/` folder

Then run `media_index.bat` once. The disabled integration safely falls back to local
candidates, so the rest of production remains usable.
