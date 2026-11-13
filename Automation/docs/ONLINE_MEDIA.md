# Contextual online media pipeline

The production order is fixed for every exact narration window:

1. Search Pexels Videos when its provider and key are enabled.
2. If no verified Pexels clip is suitable, search Pixabay Videos.
3. If neither video provider yields a suitable clip, run the existing indexed-local-
   image selector unchanged. Previously downloaded provider files remain excluded
   from this curated local pass.
4. If local candidates are missing, misleading, duplicated, or over the reuse cap,
   run the existing Unsplash image fallback.
5. Recover the strongest grounded local image when possible; otherwise render a
   visibly designed contextual technology graphic. Empty or solid-color frames are
   never used.

## Context and search queries

The stock-video query planner receives the exact timed clause, its full containing
sentence, the story title, and known entities. It returns a concrete primary phrase
plus up to two increasingly broad alternatives. Provider APIs receive only compact
search text—normally two to six words and never more than seven—because stock search
performs better with observable subjects and actions than with a copied news sentence.

For example, a window about engineers inspecting AI servers may search
`engineers inspecting data center`, then `data center engineers`, rather than sending
the complete narration or the vague word `technology`. Named products and people are
kept only when they can plausibly be visible. Ambiguous terms such as `launch` do not
become rocket footage without space-specific context.

Pexels receives the primary query and its alternatives before the same process moves
to Pixabay. Searches request safe landscape footage. Results must be HTTPS, meet the
minimum resolution and crop-retention thresholds, fit within the maximum source
duration, and be at least as long as the narration window.

The top candidate previews are checked against the exact clause and full sentence by
the configured low-cost review model. A clip is downloaded only when the preview is
visually relevant, honest, and above `STOCK_VIDEO_MIN_REVIEW_CONFIDENCE`. If preview
review is unavailable or uncertain, the candidate is rejected rather than silently
placing misleading footage.

## Exact timing and audio

The selected source may be longer than the narration window. Rendering starts from a
centered source offset and cuts it to the window's exact duration and frame count. A
source shorter than the required window is rejected. Provider audio is always removed,
so narration, music, and the existing sound design remain authoritative.

Downloaded clips are stored under:

- `Assets/Media/Online Retrieved/Pexels Videos/`
- `Assets/Media/Online Retrieved/Pixabay Videos/`

Each file has a JSON sidecar containing the provider ID and page, creator/profile,
license link, dimensions, source duration, retrieval time, search-query history,
checksum, and chosen rendition. Search results are cached for 24 hours. Per-provider
locks, atomic downloads, checksums, and FFprobe validation allow safe reuse on resumed
runs without downloading the same clip again. API keys are never written to plans,
sidecars, logs, or status output.

## Configuration

```dotenv
STOCK_VIDEO_ENABLED=true
PEXELS_API_KEY=
PIXABAY_API_KEY=
PEXELS_VIDEO_ENABLED=true
PIXABAY_VIDEO_ENABLED=true
STOCK_VIDEO_QUERY_MODEL=gpt-5.6-luna
STOCK_VIDEO_QUERY_REASONING_EFFORT=low
STOCK_VIDEO_REVIEW_MODEL=gpt-5.6-luna
STOCK_VIDEO_REVIEW_REASONING_EFFORT=low
STOCK_VIDEO_REVIEW_CANDIDATE_LIMIT=4
STOCK_VIDEO_MIN_REVIEW_CONFIDENCE=0.65
STOCK_VIDEO_MAX_DOWNLOADS_PER_VIDEO=60
STOCK_VIDEO_MAX_SEARCHES_PER_PROVIDER=60
STOCK_VIDEO_REQUEST_INTERVAL=0.25
STOCK_VIDEO_REQUEST_RESULT_LIMIT=20
STOCK_VIDEO_CANDIDATE_LIMIT=6
STOCK_VIDEO_TIMEOUT=30
STOCK_VIDEO_MAX_DOWNLOAD_BYTES=200000000
STOCK_VIDEO_MIN_WIDTH=1280
STOCK_VIDEO_MIN_HEIGHT=720
STOCK_VIDEO_MAX_SOURCE_DURATION=120
STOCK_VIDEO_LOCK_TIMEOUT=30
PEXELS_VIDEO_DESTINATION=
PIXABAY_VIDEO_DESTINATION=
```

The original local-image and Unsplash configuration remains in effect after both
video providers fail. Unsplash query planning, attribution, capped downloads, and
safe fallback behavior are unchanged.
