# Pexels fallback provider

The production order is fixed:

1. Select from the indexed local repositories. Previously downloaded Pexels files
   are excluded from this first pass, so they cannot crowd out curated assets.
2. If local candidates are missing, neutral, misleading, adjacent duplicates, or
   over the two-use cap, send the complete sentence context for those unresolved
   windows to a cached Luna-low query planner.
3. The planner preserves visible product and company context and produces one exact
   stock-photo query plus up to two descriptive alternatives. For example, Google
   Earth becomes `Google Earth satellite map computer screen`, not generic AI.
4. Reuse an already downloaded Pexels photo when it matches that topic and remains
   below `MEDIA_MAX_USES_PER_VIDEO`.
5. Search Pexels once per topic pool, trying exact then descriptive alternatives,
   download enough distinct landscape photos for
   the unresolved windows, and save them under
   `Assets/Media/Online Retrieved/Pexels/`.
6. If no grounded Pexels result exists, render a unique local neutral background.
   This safe last resort never stops video production.

The query planner receives text and metadata only; image pixels are not sent to
OpenAI. It keeps a named company or product when that name describes something that
can actually be shown. Its deterministic fallback still converts abstract claims to
honest photographic concepts, preventing matches such as a rocket for a software
launch. One cached planner request covers all unresolved windows in a story.

Downloaded files have descriptive filenames and JSON metadata sidecars containing
the Pexels page, photographer, license, retrieval time, query, and relevance
evidence. The renderer displays attribution at the bottom-right when required.

Configuration:

```dotenv
PEXELS_API_KEY=
PEXELS_FALLBACK_ENABLED=true
PEXELS_QUERY_MODEL=gpt-5.6-luna
PEXELS_QUERY_REASONING_EFFORT=low
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

Searches are pooled by compatible visual subject, so the search cap is not one
request per visual window. A downloaded image can be used at most twice per video.
The current story may also reuse its own generated hero once, for two total uses;
heroes from other stories stay excluded.
