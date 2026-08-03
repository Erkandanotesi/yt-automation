# Pexels fallback provider

The production order is fixed:

1. Select from the indexed local repositories.
2. If every local candidate is misleading, map the narration window to a broad,
   truthful photographic topic.
3. Reuse an already downloaded Pexels photo when it matches that topic and remains
   below `MEDIA_MAX_USES_PER_VIDEO`.
4. Search Pexels once per topic pool, download enough distinct landscape photos for
   the unresolved windows, and save them under
   `Assets/Media/Online Retrieved/Pexels/`.
5. If no grounded Pexels result exists, render a unique local neutral background.
   This safe last resort never stops video production.

The provider receives text queries and context only. Image pixels are not sent to
OpenAI. Exact company, person, and product names are deliberately broadened into
photographic concepts such as cybersecurity, smart television, publishing, or
cryptocurrency. This prevents literal but irrelevant matches such as a rocket for a
software launch.

Downloaded files have descriptive filenames and JSON metadata sidecars containing
the Pexels page, photographer, license, retrieval time, query, and relevance
evidence. The renderer displays attribution at the bottom-right when required.

Configuration:

```dotenv
PEXELS_API_KEY=
PEXELS_FALLBACK_ENABLED=true
PEXELS_MAX_DOWNLOADS_PER_VIDEO=20
PEXELS_MAX_SEARCHES_PER_VIDEO=20
PEXELS_REQUEST_INTERVAL=0.25
PEXELS_REQUEST_RESULT_LIMIT=24
PEXELS_SEARCH_LIMIT=8
PEXELS_TIMEOUT=30
PEXELS_MAX_DOWNLOAD_BYTES=25000000
PEXELS_MIN_WIDTH=640
PEXELS_MIN_HEIGHT=360
PEXELS_MIN_RELEVANCE_SCORE=12
```

Searches are pooled by topic, so the search cap is not one request per visual
window. A downloaded image can be used at most twice per video by default.
