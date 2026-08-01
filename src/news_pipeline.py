from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from trafilatura import extract

from article_validation import (
    article_quality_report,
    format_article_text,
    source_matches_title,
    validate_article_quality,
    validate_unique_source_content,
)
from content_validation import (
    ContentValidationError,
    trim_narration_to_word_limit,
    validate_narration,
    word_count,
)
from file_utils import (
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    sha256_text,
    stable_story_id,
)
from news_archive import ArchiveSettings, NewsArchive


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =========================================================
# TEMEL YOLLAR VE ORTAM AYARLARI
# =========================================================

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent
load_dotenv(SCRIPT_DIR / ".env")

from file_locations import (  # noqa: E402 - .env must be loaded first
    AUTOMATION_ROOT,
    GAMING_FEEDS_FILE,
    GAMING_PROFILE_FILE,
    PROCESSED_URLS_FILE,
    PROJECTS_ROOT,
    TECH_FEEDS_FILE,
    TECH_PROFILE_FILE,
    project_dir,
    project_name,
)

GLOBAL_STATE_PATH = PROCESSED_URLS_FILE
TECH_OPML_PATH = TECH_FEEDS_FILE
GAMING_OPML_PATH = GAMING_FEEDS_FILE
TECH_PROFILE_PATH = TECH_PROFILE_FILE
GAMING_PROFILE_PATH = GAMING_PROFILE_FILE

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6").strip()
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()

MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "36"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "25"))
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "30"))
TOP_N_DEFAULT = int(os.getenv("TOP_N", "5"))
RANKING_RESERVE = int(os.getenv("RANKING_RESERVE", "15"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))
MIN_ARTICLE_CHARS = int(os.getenv("MIN_ARTICLE_CHARS", "500"))
REQUIRE_FULL_TOP_N = os.getenv("REQUIRE_FULL_TOP_N", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
API_RETRIES = int(os.getenv("API_RETRIES", "3"))
RSS_RETRIES = int(os.getenv("RSS_RETRIES", "2"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 RSSNewsAutomation/1.0",
)

TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}

DEFAULT_TECH_KEYWORDS = {
    "ai",
    "artificial intelligence",
    "openai",
    "chatgpt",
    "anthropic",
    "claude",
    "gemini",
    "google",
    "microsoft",
    "apple",
    "meta",
    "amazon",
    "nvidia",
    "amd",
    "intel",
    "linux",
    "windows",
    "android",
    "security",
    "cybersecurity",
    "privacy",
    "data breach",
    "cve",
    "vulnerability",
    "crypto",
    "blockchain",
    "bitcoin",
    "chip",
    "semiconductor",
    "robot",
    "startup",
    "regulation",
    "ban",
    "lawsuit",
    "hack",
    "launch",
    "model",
}

DEFAULT_GAMING_KEYWORDS = {
    "game",
    "gaming",
    "gamer",
    "steam",
    "valve",
    "playstation",
    "xbox",
    "nintendo",
    "pc gaming",
    "console",
    "release",
    "launch",
    "update",
    "patch",
    "dlc",
    "expansion",
    "studio",
    "publisher",
    "developer",
    "esports",
    "mod",
    "modding",
    "early access",
    "game pass",
    "epic games",
    "ubisoft",
    "ea",
    "take-two",
    "rockstar",
    "bethesda",
    "activision",
    "blizzard",
}


# =========================================================
# KOMUT SATIRI
# =========================================================

def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan RSS/Atom feeds, select the best stories, and save "
            "150-200 word English scripts in a Video N project folder."
        )
    )
    parser.add_argument(
        "--video",
        type=int,
        required=True,
        help="Video number. Example: --video 1",
    )
    parser.add_argument(
        "--provider",
        choices=("gemini", "openai"),
        help="Override AI_PROVIDER from .env for this run.",
    )
    parser.add_argument(
        "--category",
        choices=("tech", "gaming"),
        default="tech",
        help="Content category: tech or gaming.",
    )
    parser.add_argument(
        "--top",
        type=int,
        help="Override TOP_N from .env for this run.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Ignore processed URL history while scanning; the persistent news "
            "archive still prevents duplicate story creation."
        ),
    )
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help=(
            "Repair source/title mismatches in an existing completed project "
            "without reranking valid stories."
        ),
    )
    parser.add_argument(
        "--confirm-ai-cost",
        action="store_true",
        help="Confirm paid summary calls required by --repair-existing.",
    )
    return parser.parse_args(argv)


def configure_run(args: argparse.Namespace) -> None:
    global ARGS
    global VIDEO_NAME, PROJECT_DIR, OUTPUT_DIR, LOG_PATH
    global PROJECT_INFO_PATH, CANDIDATES_PATH, COMBINED_SCRIPT_PATH
    global PROVIDER, TOP_N, CONTENT_CATEGORY, IS_GAMING, CONTENT_LABEL
    global OPML_PATH, CHANNEL_PROFILE_PATH

    if args.video < 1:
        raise ValueError("Video number must be 1 or greater.")
    if args.top is not None and args.top < 1:
        raise ValueError("--top must be 1 or greater.")

    ARGS = args
    VIDEO_NAME = project_name(args.video)
    PROJECT_DIR = project_dir(args.video)
    OUTPUT_DIR = PROJECT_DIR / "Haberler"
    LOG_PATH = PROJECT_DIR / "pipeline.log"
    PROJECT_INFO_PATH = PROJECT_DIR / "project_info.json"
    CANDIDATES_PATH = PROJECT_DIR / "aday_haberler.json"
    COMBINED_SCRIPT_PATH = PROJECT_DIR / "video_script.txt"
    PROVIDER = args.provider or AI_PROVIDER
    TOP_N = args.top or TOP_N_DEFAULT
    CONTENT_CATEGORY = args.category
    IS_GAMING = CONTENT_CATEGORY == "gaming"
    CONTENT_LABEL = "gaming news" if IS_GAMING else "technology news"
    OPML_PATH = GAMING_OPML_PATH if IS_GAMING else TECH_OPML_PATH
    CHANNEL_PROFILE_PATH = GAMING_PROFILE_PATH if IS_GAMING else TECH_PROFILE_PATH


ARGS: argparse.Namespace
VIDEO_NAME: str
PROJECT_DIR: Path
OUTPUT_DIR: Path
LOG_PATH: Path
PROJECT_INFO_PATH: Path
CANDIDATES_PATH: Path
COMBINED_SCRIPT_PATH: Path
PROVIDER: str
TOP_N: int
CONTENT_CATEGORY: str
IS_GAMING: bool
CONTENT_LABEL: str
OPML_PATH: Path
CHANNEL_PROFILE_PATH: Path

configure_run(
    argparse.Namespace(
        video=1,
        provider=None,
        category="tech",
        top=None,
        reset=False,
        repair_existing=False,
        confirm_ai_cost=False,
    )
)


# =========================================================
# LOG
# =========================================================

def configure_logging() -> logging.Logger:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("rss_news_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


LOGGER = logging.getLogger("rss_news_pipeline")
LOGGER.addHandler(logging.NullHandler())


# =========================================================
# YAPILANDIRILMIŞ AI ÇIKTILARI
# =========================================================

class RankedNews(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(description="Zero-based article index in the candidate list.")
    score: int = Field(ge=0, le=100)
    reason: str


class RankingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: list[RankedNews]


class FactualReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names_entities_checked: list[str] = Field(default_factory=list)
    numbers_dates_checked: list[str] = Field(default_factory=list)
    quotes_checked: list[str] = Field(default_factory=list)
    attributions_preserved: list[str] = Field(default_factory=list)
    unsupported_claims_removed: list[str] = Field(default_factory=list)


class NewsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_en: str
    summary_en: str
    factual_review: FactualReview


class NewsDuplicateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_duplicate: bool
    duplicate_archive_id: str = ""
    confidence: float = Field(ge=0, le=1)
    reason: str


SchemaT = TypeVar("SchemaT", bound=BaseModel)


# =========================================================
# YARDIMCI FONKSİYONLAR
# =========================================================

def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(filtered_query),
            "",
        )
    )


def normalized_title(title: str) -> str:
    title = unicodedata.normalize("NFKD", title.lower())
    title = title.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def slugify(value: str, max_length: int = 90) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:max_length] or "haber"


def story_id_for(candidate: dict) -> str:
    return stable_story_id(canonical_url(candidate["url"]))


def safe_iso_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat()


def parse_entry_date(entry: dict) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    return datetime.now(timezone.utc)


def load_channel_profile() -> str:
    if CHANNEL_PROFILE_PATH.exists():
        profile = CHANNEL_PROFILE_PATH.read_text(encoding="utf-8").strip()
        if profile:
            return profile

    return (
        "English-language gaming news channel focused on players, games, "
        "platforms, studios, publishers, ownership, privacy, and player rights."
        if IS_GAMING
        else
        "English-language technology news channel focused on artificial "
        "intelligence, big tech, privacy, cybersecurity, platforms, chips, "
        "and technology regulation."
    )


def load_state() -> set[str]:
    if ARGS.reset:
        LOGGER.warning(
            "--reset ignores URL history only; hash and AI archive duplicate "
            "protection remains active."
        )
        return set()

    if not GLOBAL_STATE_PATH.exists():
        return set()

    try:
        data = json.loads(GLOBAL_STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("used_urls", []))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("İşlenmiş URL dosyası okunamadı: %s", exc)
        return set()


def save_state(used_urls: set[str]) -> None:
    AUTOMATION_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_video": VIDEO_NAME,
        "used_urls": sorted(used_urls),
    }
    atomic_write_json(GLOBAL_STATE_PATH, payload)


def retry_call(function, *, attempts: int, description: str):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return function()
        except Exception as exc:  # noqa: BLE001 - burada genel yeniden deneme amaçlıdır.
            last_error = exc
            status_code = getattr(exc, "status_code", None)
            if (
                isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in {408, 409, 429}
            ):
                LOGGER.error(
                    "%s kalıcı istem hatası verdi (%s); yeniden denenmeyecek: %s",
                    description,
                    status_code,
                    exc,
                )
                raise
            if attempt >= attempts:
                break
            wait_seconds = min(2 ** (attempt - 1), 8)
            LOGGER.warning(
                "%s başarısız (%s/%s): %s. %s saniye sonra tekrar deneniyor.",
                description,
                attempt,
                attempts,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    if last_error is None:
        raise RuntimeError(f"{description} başarısız oldu.")
    raise last_error


# =========================================================
# OPML VE RSS
# =========================================================

def load_feed_urls() -> list[str]:
    if not OPML_PATH.exists():
        raise FileNotFoundError(
            f"FeedFlow OPML dosyası bulunamadı: {OPML_PATH}"
        )

    try:
        root = ET.parse(OPML_PATH).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"{OPML_PATH.name} geçerli bir OPML/XML dosyası değil: {exc}") from exc

    urls: list[str] = []
    for element in root.iter():
        url = element.attrib.get("xmlUrl") or element.attrib.get("xmlurl")
        if url:
            url = url.strip()
            if url and url not in urls:
                urls.append(url)

    if not urls:
        raise RuntimeError(
            f"{OPML_PATH.name} içinde RSS/Atom adresi bulunamadı."
        )

    return urls


def build_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, application/xml, "
                "text/xml, text/html;q=0.9, */*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.8,tr;q=0.7",
        }
    )
    return session


HTTP = build_http_session()


def fetch_bytes(url: str, *, description: str) -> bytes:
    def operation() -> bytes:
        response = HTTP.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.content

    return retry_call(
        operation,
        attempts=RSS_RETRIES,
        description=description,
    )


def rough_score(candidate: dict, keywords: set[str]) -> float:
    now = datetime.now(timezone.utc)
    age_hours = max(
        0.0,
        (now - candidate["published"]).total_seconds() / 3600,
    )

    recency_score = max(0.0, 35.0 - (age_hours * 0.9))
    searchable = f'{candidate["title"]} {candidate["rss_text"]}'.lower()
    hits = sum(1 for keyword in keywords if keyword in searchable)
    keyword_score = min(35.0, hits * 6.0)
    title_score = 10.0 if 25 <= len(candidate["title"]) <= 170 else 4.0
    content_score = min(20.0, len(candidate["rss_text"]) / 55)

    return round(recency_score + keyword_score + title_score + content_score, 2)


def collect_candidates(feed_urls: list[str], used_urls: set[str]) -> list[dict]:
    now = datetime.now(timezone.utc)
    candidates: list[dict] = []
    current_urls: set[str] = set()
    current_titles: set[str] = set()
    keywords = set(
        DEFAULT_GAMING_KEYWORDS if IS_GAMING else DEFAULT_TECH_KEYWORDS
    )

    for feed_number, feed_url in enumerate(feed_urls, start=1):
        LOGGER.info("RSS okunuyor (%s/%s): %s", feed_number, len(feed_urls), feed_url)

        try:
            raw_feed = fetch_bytes(
                feed_url,
                description=f"RSS indirme: {feed_url}",
            )
            feed = feedparser.parse(raw_feed)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("RSS okunamadı: %s | %s", feed_url, exc)
            continue

        source_name = strip_html(feed.feed.get("title", "")) or urlsplit(feed_url).netloc

        if getattr(feed, "bozo", False):
            LOGGER.warning(
                "RSS biçim uyarısı: %s | %s",
                feed_url,
                getattr(feed, "bozo_exception", "bilinmeyen hata"),
            )

        for entry in feed.entries[:MAX_FEED_ITEMS]:
            raw_url = str(entry.get("link", "")).strip()
            title = strip_html(str(entry.get("title", "")))

            if not raw_url or not title:
                continue

            url = canonical_url(raw_url)
            title_key = normalized_title(title)

            if not url.startswith(("http://", "https://")):
                continue
            if url in used_urls or url in current_urls:
                continue
            if title_key and title_key in current_titles:
                continue

            published = parse_entry_date(entry)
            age_hours = (now - published).total_seconds() / 3600

            # Gelecek tarihli hatalı feed kayıtlarını 0 saat yaşında kabul et.
            age_hours = max(0.0, age_hours)
            if age_hours > MAX_AGE_HOURS:
                continue

            content_parts: list[str] = []
            for content in entry.get("content", []):
                value = content.get("value")
                if value:
                    content_parts.append(str(value))

            raw_text = " ".join(content_parts)
            if not raw_text:
                raw_text = str(entry.get("summary", "") or entry.get("description", ""))

            rss_text = strip_html(raw_text)

            candidate = {
                "url": url,
                "title": title,
                "source": source_name,
                "feed_url": feed_url,
                "published": published,
                "rss_text": rss_text[:4000],
            }
            candidate["rough_score"] = rough_score(candidate, keywords)

            candidates.append(candidate)
            current_urls.add(url)
            if title_key:
                current_titles.add(title_key)

    candidates.sort(
        key=lambda item: (item["rough_score"], item["published"]),
        reverse=True,
    )
    return candidates[:MAX_CANDIDATES]


def save_candidates(candidates: list[dict]) -> None:
    serializable = []
    for index, candidate in enumerate(candidates):
        serializable.append(
            {
                "index": index,
                "title": candidate["title"],
                "source": candidate["source"],
                "published_utc": safe_iso_datetime(candidate["published"]),
                "url": candidate["url"],
                "rough_score": candidate["rough_score"],
                "rss_summary": candidate["rss_text"],
            }
        )

    atomic_write_json(CANDIDATES_PATH, serializable)


# =========================================================
# AI SAĞLAYICILARI
# =========================================================

def structured_ai_call(prompt: str, schema_type: type[SchemaT]) -> SchemaT:
    def operation() -> SchemaT:
        if PROVIDER == "openai":
            from openai import OpenAI

            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY .env dosyasında tanımlı değil.")

            client = OpenAI()
            response = client.responses.parse(
                model=OPENAI_MODEL,
                reasoning={"effort": OPENAI_REASONING_EFFORT},
                input=[
                    {
                        "role": "system",
                        "content": (
                            f"You are a rigorous {CONTENT_LABEL} editor. "
                            "Use only supplied sources; never invent facts."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                text_format=schema_type,
            )

            if response.output_parsed is None:
                raise RuntimeError("OpenAI yapılandırılmış çıktı döndürmedi.")

            return response.output_parsed

        if PROVIDER == "gemini":
            from google import genai

            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY .env dosyasında tanımlı değil.")

            client = genai.Client()
            interaction = client.interactions.create(
                model=GEMINI_MODEL,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_type.model_json_schema(),
                },
            )

            output_text = interaction.output_text
            if not output_text:
                raise RuntimeError("Gemini yapılandırılmış çıktı döndürmedi.")

            return schema_type.model_validate_json(output_text)

        raise ValueError("AI_PROVIDER yalnızca 'gemini' veya 'openai' olabilir.")

    return retry_call(
        operation,
        attempts=API_RETRIES,
        description=f"{PROVIDER} API çağrısı",
    )


def compare_news_files_with_ai(payload: dict) -> dict:
    """Determine whether a pulled article repeats a previously used news event."""
    previous_ids = [item["archive_id"] for item in payload["previous_files"]]
    prompt = f"""
Compare NEW_FILE with PREVIOUS_FILES and decide whether NEW_FILE reports the
same underlying news event as any previous file.

Rules:
- Reworded, syndicated, translated, or independently reported coverage of the
  same announcement, incident, lawsuit, launch, disclosure, or decision is a
  duplicate.
- Sharing a company, product, person, or broad topic is not enough.
- A genuine follow-up with a materially new event or new facts is not a
  duplicate, even when it refers to an older event.
- Base the decision only on the supplied file contents and metadata.
- If is_duplicate is true, duplicate_archive_id must be exactly one of:
  {json.dumps(previous_ids, ensure_ascii=False)}
- If is_duplicate is false, duplicate_archive_id must be an empty string.
- Keep reason concise and identify the event used for the decision.

FILES:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""
    result = structured_ai_call(prompt, NewsDuplicateReview)
    return result.model_dump()


# =========================================================
# HABER SEÇİMİ
# =========================================================

def rank_candidates(candidates: list[dict], channel_profile: str) -> list[RankedNews]:
    # Rank a large reserve in the same request so an unreadable, duplicate, or
    # invalid article is replaced by another RSS candidate without paying for
    # a second ranking request.
    target_count = min(len(candidates), TOP_N + max(0, RANKING_RESERVE))
    ranking_pool_size = min(len(candidates), max(12, target_count * 2))
    ranking_pool = candidates[:ranking_pool_size]

    article_list = []
    for index, candidate in enumerate(ranking_pool):
        article_list.append(
            {
                "index": index,
                "title": candidate["title"],
                "source": candidate["source"],
                "published_utc": safe_iso_datetime(candidate["published"]),
                "rss_summary": candidate["rss_text"][:650],
                "local_pre_score": candidate["rough_score"],
            }
        )

    prompt = f"""
Choose up to {target_count} stories for an English-language YouTube {CONTENT_LABEL}
video. The first {TOP_N} are primary choices and the rest are ranked reserves.

CHANNEL PROFILE:
{channel_profile}

Score relevance (30), impact (25), novelty (20), video potential (15), and
source clarity (10). Reject ads, deals, minor accessories, opinion-only pieces,
vague rumors, and duplicates; for duplicate events choose the strongest source.
Use only supplied data. local_pre_score is a hint, not a verdict. Return valid
candidate indexes in descending score order.

CANDIDATES:
{json.dumps(article_list, ensure_ascii=False, indent=2)}
"""

    try:
        result = structured_ai_call(prompt, RankingResult)
        valid: list[RankedNews] = []
        used_indexes: set[int] = set()

        for item in result.selected:
            if not 0 <= item.index < len(ranking_pool):
                continue
            if item.index in used_indexes:
                continue
            used_indexes.add(item.index)
            valid.append(item)

        return sorted(valid, key=lambda item: item.score, reverse=True)[:target_count]

    except Exception as exc:  # noqa: BLE001
        LOGGER.error("AI haber sıralaması başarısız: %s", exc)
        LOGGER.warning("Yerel ön puan sıralaması kullanılacak.")

        fallback: list[RankedNews] = []
        for index, candidate in enumerate(ranking_pool[:target_count]):
            fallback.append(
                RankedNews(
                    index=index,
                    score=min(100, max(0, round(candidate["rough_score"]))),
                    reason="AI sıralaması kullanılamadığı için yerel güncellik ve anahtar kelime puanı.",
                )
            )
        return fallback


# =========================================================
# HABER METNİNİ ÇIKARMA
# =========================================================

def extract_article_text(candidate: dict) -> str:
    def operation() -> tuple[str, str]:
        response = HTTP.get(candidate["url"], timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type and content_type not in {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        }:
            raise RuntimeError(f"Unsupported article content type: {content_type}")

        extracted = extract(
            response.text,
            url=candidate["url"],
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            output_format="txt",
        )

        formatted = format_article_text(extracted or "", maximum_characters=35000)
        return formatted, str(response.url)

    try:
        article_text, final_url = retry_call(
            operation,
            attempts=RSS_RETRIES,
            description=f"Haber metni çıkarma: {candidate['url']}",
        )
        quality = article_quality_report(
            candidate["title"],
            article_text,
            minimum_characters=MIN_ARTICLE_CHARS,
        )
        if quality["status"] == "passed":
            candidate["extraction"] = {
                "method": "article_page",
                "requested_url": candidate["url"],
                "final_url": final_url,
                "quality": quality,
            }
            return article_text
        candidate["extraction"] = {
            "method": "article_page_rejected",
            "requested_url": candidate["url"],
            "final_url": final_url,
            "quality": quality,
        }
        LOGGER.info(
            "Fetched page failed the article quality gate; trying RSS text: "
            "%s | errors=%s | final_url=%s",
            candidate["title"],
            quality["errors"],
            final_url,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("Full article page could not be used; trying RSS text: %s", exc)

    rss_text = format_article_text(str(candidate.get("rss_text", "")))
    quality = article_quality_report(
        candidate["title"],
        rss_text,
        minimum_characters=MIN_ARTICLE_CHARS,
    )
    if quality["status"] != "passed":
        raise RuntimeError(
            "Neither the article page nor RSS text passed the source quality gate: "
            f"{candidate['title']} | errors={quality['errors']}"
        )
    LOGGER.info("Using matching RSS text as the source fallback.")
    candidate["extraction"] = {
        "method": "rss_fallback",
        "requested_url": candidate["url"],
        "final_url": candidate.get("extraction", {}).get("final_url", ""),
        "quality": quality,
    }
    return rss_text


# =========================================================
# ÖZET
# =========================================================

def summarize_article(
    candidate: dict,
    article_text: str,
) -> NewsSummary:
    if IS_GAMING:
        writing_brief = """
Write an English YouTube gaming-news script from the source. Aim for 175-185
words and never exceed 195 words.
Preserve every material fact that fits; add nothing unsupported. Attribute
allegations and retain uncertainty. Be skeptical of large publishers, platform
holders, aggressive monetization, lock-in, and anti-consumer decisions. Support
player ownership, privacy, modding, open platforms, and free expression. Use
light sarcasm only when context supports it; otherwise sound normal.
"""
        revision_style = (
            "Keep the player-rights, ownership, privacy, open-platform, "
            "anti-lock-in, and anti-exploitation perspective."
        )
    else:
        writing_brief = """
Write an English YouTube tech-news script from the source. Aim for 175-185
words and never exceed 195 words.
Preserve every material fact that fits; add nothing unsupported. Attribute
allegations and retain uncertainty. Be skeptical of big tech and supportive of
free speech and privacy; use light sarcasm only when context supports it.
Otherwise sound normal.
"""
        revision_style = (
            "Keep the skeptical-big-tech, free-speech, and privacy perspective."
        )

    prompt = f"""
{writing_brief.strip()}
Use simple, warm, intimate but formal and kind language. Make it engaging,
legally cautious, and ready for ElevenLabs. No greeting,
farewell, heading, bullets, labels, or stage directions. Return title_en as
metadata and the narration as summary_en. Do not use direct quotations or
quotation marks; paraphrase supported statements and preserve attribution.

Before returning, compare every name/entity, number/date, direct quote, and
attribution in the narration against SOURCE TEXT. In factual_review, list the
exact source phrases you checked. List any tempting unsupported claims you
removed. Do not put source facts in factual_review unless their exact wording
appears in SOURCE TEXT. Copy checked phrases verbatim, including any internal
punctuation. Omit a review item rather than paraphrasing its source wording.

ORIGINAL TITLE:
{candidate["title"]}

SOURCE:
{candidate["source"]}

PUBLISHED UTC:
{safe_iso_datetime(candidate["published"])}

SOURCE TEXT:
{article_text}
"""

    result = structured_ai_call(prompt, NewsSummary)
    original_word_count = word_count(result.summary_en)
    result.summary_en = trim_narration_to_word_limit(result.summary_en)
    if word_count(result.summary_en) != original_word_count:
        LOGGER.info(
            "Trimmed narration locally from %s to %s words.",
            original_word_count,
            word_count(result.summary_en),
        )

    for revision_number in range(1, 4):
        try:
            validate_narration(
                title=result.title_en,
                narration=result.summary_en,
                source_text=article_text,
                factual_review=result.factual_review.model_dump(),
            )
            return result
        except ContentValidationError as exc:
            validation_error = str(exc)

        LOGGER.warning(
            "Özet doğrulaması başarısız: %s | düzeltme %s/3.",
            validation_error,
            revision_number,
        )

        revision_prompt = f"""
Fix the validation error below. Rewrite summary_en to 175-185 English words
and never exceed 195 words.
Preserve supported facts and uncertainty; add nothing. Keep the requested
skeptical, privacy-conscious, warm, legally cautious, ElevenLabs-ready style.
{revision_style} No greeting, farewell, heading, bullets, labels, or stage
directions. Do not use direct quotations or quotation marks. Rebuild
factual_review using only verbatim phrases found in SOURCE;
omit any review item that would require paraphrasing.

VALIDATION ERROR:
{validation_error}

CURRENT OUTPUT:
Title: {result.title_en}
Summary: {result.summary_en}

SOURCE FOR FACT CHECKING:
{article_text}
"""
        result = structured_ai_call(revision_prompt, NewsSummary)
        original_word_count = word_count(result.summary_en)
        result.summary_en = trim_narration_to_word_limit(result.summary_en)
        if word_count(result.summary_en) != original_word_count:
            LOGGER.info(
                "Trimmed revised narration locally from %s to %s words.",
                original_word_count,
                word_count(result.summary_en),
            )

    validate_narration(
        title=result.title_en,
        narration=result.summary_en,
        source_text=article_text,
        factual_review=result.factual_review.model_dump(),
    )
    return result


# =========================================================
# DOSYAYA KAYDETME
# =========================================================

def save_news_file(
    summary: NewsSummary,
    article_text: str,
    order: int,
    story_id: str,
) -> tuple[Path, Path, dict]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    readable_article = format_article_text(article_text)
    validation = validate_narration(
        title=summary.title_en,
        narration=summary.summary_en,
        source_text=readable_article,
        factual_review=summary.factual_review.model_dump(),
    )
    base_name = f"{order:02d}_{story_id}_{slugify(summary.title_en, 65)}"
    text_path = OUTPUT_DIR / f"{base_name}.txt"
    original_path = OUTPUT_DIR / f"{base_name}-orijinal.txt"
    narration = summary.summary_en.strip()

    atomic_write_text(text_path, narration + "\n")
    atomic_write_text(original_path, readable_article + "\n")
    return text_path, original_path, validation


def save_combined_script(created_items: list[dict]) -> None:
    sections: list[str] = []

    for item in created_items:
        summary: NewsSummary = item["summary"]
        narration = summary.summary_en.strip()
        sections.append(narration)

    combined = "\n\n".join(sections).strip()
    atomic_write_text(COMBINED_SCRIPT_PATH, combined + ("\n" if combined else ""))


def validate_created_items(created_items: list[dict]) -> None:
    if REQUIRE_FULL_TOP_N and len(created_items) != TOP_N:
        raise RuntimeError(
            f"Project has {len(created_items)} valid stories; exactly {TOP_N} "
            "are required. processed_urls.json was not changed."
        )
    if not created_items:
        raise RuntimeError("No valid story was created.")

    orders = [item["order"] for item in created_items]
    if orders != list(range(1, len(created_items) + 1)):
        raise RuntimeError(f"Story order is not contiguous: {orders}")

    story_ids = [item["story_id"] for item in created_items]
    if len(story_ids) != len(set(story_ids)):
        raise RuntimeError("Duplicate stable story IDs were created.")

    urls = [canonical_url(item["candidate"]["url"]) for item in created_items]
    if len(urls) != len(set(urls)):
        raise RuntimeError("Duplicate source URLs were created.")

    validate_unique_source_content(
        [
            {
                "story_id": item["story_id"],
                "source_text": item["article_text"],
            }
            for item in created_items
        ]
    )

    scores = [item["ranking"].score for item in created_items]
    if scores != sorted(scores, reverse=True):
        raise RuntimeError("Created stories are not in descending ranking order.")

    for item in created_items:
        validate_article_quality(
            item["candidate"]["title"],
            item["article_text"],
            minimum_characters=MIN_ARTICLE_CHARS,
        )
        if not source_matches_title(
            item["candidate"]["title"],
            item["article_text"],
        ):
            raise RuntimeError(
                "Source text does not match its RSS title: "
                f"{item['candidate']['title']}"
            )
        for key in ("text_path", "original_path"):
            path = item[key]
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"Required story artifact is missing: {path}")
        validate_narration(
            title=item["summary"].title_en,
            narration=item["summary"].summary_en,
            source_text=item["article_text"],
            factual_review=item["summary"].factual_review.model_dump(),
        )


def save_project_info(
    candidate_count: int,
    rankings: list[RankedNews],
    created_items: list[dict],
    *,
    status: str,
    error: str = "",
    repair_metadata: dict | None = None,
) -> None:
    payload = {
        "video_number": ARGS.video,
        "video_name": VIDEO_NAME,
        "category": CONTENT_CATEGORY,
        "feed_file": OPML_PATH.name,
        "provider": PROVIDER,
        "model": OPENAI_MODEL if PROVIDER == "openai" else GEMINI_MODEL,
        "project_directory": str(PROJECT_DIR),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": candidate_count,
        "ranked_count": len(rankings),
        "created_count": len(created_items),
        "expected_count": TOP_N,
        "status": status,
        "error": error,
        "repair": repair_metadata or {},
        "stories": [
            {
                "story_id": item["story_id"],
                "order": item["order"],
                "title": item["summary"].title_en,
                "source_title": item["candidate"]["title"],
                "source": item["candidate"]["source"],
                "published_utc": safe_iso_datetime(item["candidate"]["published"]),
                "text": str(item["text_path"]),
                "original": str(item["original_path"]),
                "source_url": item["candidate"]["url"],
                "score": item["ranking"].score,
                "ranking_reason": item["ranking"].reason,
                "word_count": word_count(item["summary"].summary_en),
                "narration_sha256": sha256_file(item["text_path"]),
                "source_sha256": sha256_file(item["original_path"]),
                "news_archive_id": item.get("archive_record", {}).get(
                    "archive_id", ""
                ),
                "news_archive_source_file": item.get("archive_record", {}).get(
                    "source_file", ""
                ),
                "news_archive_content_sha256": item.get("archive_record", {}).get(
                    "content_sha256", ""
                ),
                "duplicate_check": item.get("duplicate_decision", {}),
                "source_extraction": item["candidate"].get("extraction", {}),
                "validation": item["validation"],
            }
            for item in created_items
        ],
    }

    atomic_write_json(PROJECT_INFO_PATH, payload)


def _repair_candidate(record: dict) -> dict:
    published_raw = str(record.get("published_utc", ""))
    try:
        published = datetime.fromisoformat(published_raw)
    except ValueError:
        published = datetime.now(timezone.utc)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return {
        "url": canonical_url(str(record["url"])),
        "title": str(record["title"]),
        "source": str(record.get("source", "")),
        "feed_url": str(record.get("feed_url", "")),
        "published": published.astimezone(timezone.utc),
        "rss_text": str(record.get("rss_summary", "")),
        "rough_score": float(record.get("rough_score", 0)),
    }


def _existing_summary(story: dict, text_path: Path) -> NewsSummary:
    factual_review = (
        story.get("validation", {}).get("factual_review", {})
    )
    return NewsSummary(
        title_en=str(story["title"]),
        summary_en=text_path.read_text(encoding="utf-8").strip(),
        factual_review=FactualReview.model_validate(factual_review),
    )


def _repair_backup_path() -> Path:
    backup_parent = PROJECTS_ROOT / "Legacy Automation Outputs"
    backup_parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = backup_parent / f"{VIDEO_NAME} invalid-news backup {timestamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = backup_parent / (
            f"{VIDEO_NAME} invalid-news backup {timestamp}-{suffix}"
        )
    return candidate


def repair_existing_project() -> dict:
    if not ARGS.confirm_ai_cost:
        raise RuntimeError(
            "Repair may make paid summary calls. Re-run with --confirm-ai-cost."
        )
    if not PROJECT_INFO_PATH.is_file() or not CANDIDATES_PATH.is_file():
        raise FileNotFoundError(
            "Repair requires project_info.json and aday_haberler.json."
        )
    project_info = json.loads(PROJECT_INFO_PATH.read_text(encoding="utf-8"))
    candidate_records = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if project_info.get("status") != "complete":
        raise RuntimeError("Only a completed news project can be repaired in place.")
    candidates_by_url = {
        canonical_url(str(record["url"])): _repair_candidate(record)
        for record in candidate_records
    }

    inspected: list[dict] = []
    seen_source_hashes: dict[str, int] = {}
    affected_orders: list[int] = []
    for story in project_info.get("stories", []):
        order = int(story["order"])
        text_path = Path(story["text"])
        original_path = Path(story["original"])
        if not text_path.is_file() or not original_path.is_file():
            raise FileNotFoundError(
                f"Existing story files are missing for story order {order}."
            )
        source_text = original_path.read_text(encoding="utf-8").strip()
        source_hash = sha256_text(re.sub(r"\s+", " ", source_text).casefold())
        matches = source_matches_title(story["source_title"], source_text)
        duplicate_of = seen_source_hashes.get(source_hash)
        if not matches or duplicate_of is not None:
            affected_orders.append(order)
        else:
            seen_source_hashes[source_hash] = order
        inspected.append(
            {
                "story": story,
                "text_path": text_path,
                "original_path": original_path,
                "source_text": source_text,
                "matches": matches,
                "duplicate_of": duplicate_of,
            }
        )
    if not affected_orders:
        LOGGER.info("No source/title mismatch or duplicate source content was found.")
        return {
            "status": "unchanged",
            "affected_orders": [],
            "backup_directory": "",
        }

    LOGGER.warning(
        "Repairing story orders %s; valid stories will be preserved.",
        affected_orders,
    )
    prepared: list[dict] = []
    for inspected_item in inspected:
        story = inspected_item["story"]
        order = int(story["order"])
        url = canonical_url(str(story["source_url"]))
        candidate = candidates_by_url.get(url)
        if candidate is None:
            raise RuntimeError(
                f"Saved RSS candidate is missing for story order {order}: {url}"
            )
        ranking = RankedNews(
            index=0,
            score=int(story["score"]),
            reason=str(story.get("ranking_reason", "")),
        )
        if order in affected_orders:
            article_text = extract_article_text(candidate)
            summary = summarize_article(candidate, article_text)
        else:
            article_text = inspected_item["source_text"]
            summary = _existing_summary(story, inspected_item["text_path"])
            candidate["extraction"] = story.get("source_extraction", {})
        prepared.append(
            {
                "order": order,
                "story_id": story["story_id"],
                "candidate": candidate,
                "ranking": ranking,
                "summary": summary,
                "article_text": article_text,
                "old_text_path": inspected_item["text_path"],
                "old_original_path": inspected_item["original_path"],
                "old_validation": story.get("validation", {}),
            }
        )

    validate_unique_source_content(
        [
            {
                "story_id": item["story_id"],
                "source_text": item["article_text"],
            }
            for item in prepared
        ]
    )
    for item in prepared:
        if not source_matches_title(
            item["candidate"]["title"],
            item["article_text"],
        ):
            raise RuntimeError(
                "Repair source still does not match its title: "
                f"{item['candidate']['title']}"
            )

    backup_path = _repair_backup_path()
    shutil.copytree(PROJECT_DIR, backup_path)
    LOGGER.info("Recoverable pre-repair backup created: %s", backup_path)

    created_items: list[dict] = []
    for item in prepared:
        if item["order"] in affected_orders:
            text_path, original_path, validation = save_news_file(
                item["summary"],
                item["article_text"],
                item["order"],
                item["story_id"],
            )
        else:
            text_path = item["old_text_path"]
            original_path = item["old_original_path"]
            validation = item["old_validation"]
        created_items.append(
            {
                **item,
                "text_path": text_path,
                "original_path": original_path,
                "validation": validation,
            }
        )

    validate_created_items(created_items)
    save_combined_script(created_items)
    repair_metadata = {
        "status": "news_repaired_tts_pending",
        "repaired_at_utc": datetime.now(timezone.utc).isoformat(),
        "affected_story_orders": affected_orders,
        "preserved_story_orders": [
            item["order"]
            for item in prepared
            if item["order"] not in affected_orders
        ],
        "backup_directory": str(backup_path),
    }
    save_project_info(
        len(candidate_records),
        [item["ranking"] for item in created_items],
        created_items,
        status="complete",
        repair_metadata=repair_metadata,
    )

    for item in created_items:
        for old_key, new_key in (
            ("old_text_path", "text_path"),
            ("old_original_path", "original_path"),
        ):
            old_path = Path(item[old_key])
            new_path = Path(item[new_key])
            if (
                old_path.resolve() != new_path.resolve()
                and old_path.parent.resolve() == OUTPUT_DIR.resolve()
            ):
                old_path.unlink(missing_ok=True)

    state_path = PROJECT_DIR / "state.json"
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    stages = state.setdefault("stages", {})
    for downstream in ("captions", "storyboard", "archive_visuals"):
        stages.pop(downstream, None)
    stages["manual_separators"] = {
        **stages.get("manual_separators", {}),
        "status": "WAITING",
        "reason": "News repaired; TTS and alignment must be refreshed.",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(state_path, state)
    LOGGER.info(
        "News repair completed. Run TTS without --overwrite, then alignment."
    )
    return {
        "status": "news_repaired_tts_pending",
        "affected_orders": affected_orders,
        "backup_directory": str(backup_path),
    }


# =========================================================
# ÖN KONTROL
# =========================================================

def validate_configuration() -> None:
    if PROVIDER not in {"gemini", "openai"}:
        raise ValueError("AI_PROVIDER yalnızca gemini veya openai olabilir.")

    if (
        PROVIDER == "openai"
        and OPENAI_REASONING_EFFORT
        not in {"none", "low", "medium", "high", "xhigh", "max"}
    ):
        raise ValueError(
            "OPENAI_REASONING_EFFORT none, low, medium, high, xhigh veya max olmalıdır."
        )

    if not AUTOMATION_ROOT.exists():
        AUTOMATION_ROOT.mkdir(parents=True, exist_ok=True)

    if AUTOMATION_ROOT.resolve() == PROJECTS_ROOT.resolve():
        raise RuntimeError(
            "AUTOMATION_ROOT and PROJECTS_ROOT must be different directories."
        )

    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

    ArchiveSettings.from_environment().validate()

    if RANKING_RESERVE < 0:
        raise ValueError("RANKING_RESERVE cannot be negative.")
    if MAX_CANDIDATES < TOP_N:
        raise ValueError("MAX_CANDIDATES must be at least TOP_N.")

    if PROVIDER == "gemini" and not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError(
            "Gemini seçili ancak GEMINI_API_KEY eksik. "
            f"{AUTOMATION_ROOT / '.env'} dosyasını düzenle."
        )

    if PROVIDER == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OpenAI seçili ancak OPENAI_API_KEY eksik. "
            f"{AUTOMATION_ROOT / '.env'} dosyasını düzenle."
        )


# =========================================================
# ANA İŞLEM
# =========================================================

def main(argv: list[str] | None = None) -> int:
    global LOGGER
    try:
        configure_run(parse_arguments(argv))
        LOGGER = configure_logging()
        validate_configuration()

        LOGGER.info("=" * 68)
        LOGGER.info("Proje: %s", VIDEO_NAME)
        LOGGER.info("Proje klasörü: %s", PROJECT_DIR)
        LOGGER.info("Kategori: %s", CONTENT_CATEGORY)
        LOGGER.info("Feed dosyası: %s", OPML_PATH.name)
        LOGGER.info("AI sağlayıcısı: %s", PROVIDER)
        LOGGER.info(
            "Model: %s",
            OPENAI_MODEL if PROVIDER == "openai" else GEMINI_MODEL,
        )
        LOGGER.info("Seçilecek azami haber: %s", TOP_N)
        LOGGER.info("=" * 68)

        if ARGS.repair_existing:
            result = repair_existing_project()
            LOGGER.info(
                "Repair result: %s | affected=%s | backup=%s",
                result["status"],
                result["affected_orders"],
                result["backup_directory"],
            )
            return 0

        channel_profile = load_channel_profile()
        archive = NewsArchive(
            ArchiveSettings.from_environment(
                cache_namespace=(
                    f"news-duplicate-v1:{PROVIDER}:"
                    f"{OPENAI_MODEL if PROVIDER == 'openai' else GEMINI_MODEL}"
                )
            )
        )
        backfill = archive.backfill_projects(PROJECTS_ROOT)
        LOGGER.info(
            "News archive ready: %s | imported=%s | already_present=%s | skipped=%s",
            backfill["archive_root"],
            backfill["imported"],
            backfill["already_present"],
            backfill["skipped"],
        )
        feed_urls = load_feed_urls()
        used_urls = load_state()

        LOGGER.info("%s RSS/Atom feed bulundu.", len(feed_urls))
        LOGGER.info("Önceki videolarda kullanılan URL sayısı: %s", len(used_urls))

        candidates = collect_candidates(feed_urls, used_urls)
        save_candidates(candidates)

        if not candidates:
            LOGGER.warning("Yeni aday haber bulunamadı.")
            save_project_info(
                0,
                [],
                [],
                status="incomplete",
                error="No new candidate stories were found.",
            )
            return 1

        LOGGER.info("%s aday haber bulundu.", len(candidates))
        rankings = rank_candidates(candidates, channel_profile)

        if not rankings:
            LOGGER.warning("Seçilen haber bulunamadı.")
            save_project_info(
                len(candidates),
                [],
                [],
                status="incomplete",
                error="No stories were ranked.",
            )
            return 1

        created_items: list[dict] = []

        for ranking in rankings:
            if len(created_items) >= TOP_N:
                break
            if ranking.score < MIN_SCORE:
                LOGGER.info(
                    "Düşük puan nedeniyle atlandı: %s/100 | %s",
                    ranking.score,
                    candidates[ranking.index]["title"],
                )
                continue

            candidate = candidates[ranking.index]
            LOGGER.info(
                "İşleniyor: %s | %s/100",
                candidate["title"],
                ranking.score,
            )

            try:
                article_text = extract_article_text(candidate)

                if len(article_text) < 200:
                    LOGGER.warning(
                        "Kaynak metin çok kısa olduğu için atlandı: %s",
                        candidate["title"],
                    )
                    continue

                archive_result = archive.inspect_and_store(
                    canonical_url=canonical_url(candidate["url"]),
                    title=candidate["title"],
                    source=candidate["source"],
                    source_url=candidate["url"],
                    published_utc=safe_iso_datetime(candidate["published"]),
                    category=CONTENT_CATEGORY,
                    article_text=article_text,
                    video_name=VIDEO_NAME,
                    active_record_ids={
                        item["archive_record"]["archive_id"]
                        for item in created_items
                    },
                    compare_with_ai=compare_news_files_with_ai,
                )
                duplicate_decision = archive_result["decision"]
                if duplicate_decision["is_duplicate"]:
                    LOGGER.warning(
                        "Duplicate news event rejected: %s | method=%s | "
                        "duplicate_of=%s | reason=%s",
                        candidate["title"],
                        duplicate_decision["method"],
                        duplicate_decision["duplicate_archive_id"],
                        duplicate_decision["reason"],
                    )
                    continue

                try:
                    validate_unique_source_content(
                        [
                            *[
                                {
                                    "story_id": item["story_id"],
                                    "source_text": item["article_text"],
                                }
                                for item in created_items
                            ],
                            {
                                "story_id": story_id_for(candidate),
                                "source_text": article_text,
                            },
                        ]
                    )
                except ValueError as exc:
                    LOGGER.warning(
                        "Duplicate source content rejected before summarization: %s",
                        exc,
                    )
                    continue

                summary = summarize_article(
                    candidate,
                    article_text,
                )

                order = len(created_items) + 1
                story_id = story_id_for(candidate)
                text_path, original_path, validation = save_news_file(
                    summary,
                    article_text,
                    order,
                    story_id,
                )

                created_items.append(
                    {
                        "order": order,
                        "story_id": story_id,
                        "candidate": candidate,
                        "ranking": ranking,
                        "summary": summary,
                        "article_text": article_text,
                        "text_path": text_path,
                        "original_path": original_path,
                        "validation": validation,
                        "archive_record": archive_result["record"],
                        "duplicate_decision": duplicate_decision,
                    }
                )

                LOGGER.info(
                    "Kaydedildi: %s (%s kelime)",
                    text_path,
                    word_count(summary.summary_en),
                )

            except Exception as exc:  # noqa: BLE001
                expected_source_rejection = (
                    isinstance(exc, RuntimeError)
                    and "source quality gate" in str(exc)
                )
                LOGGER.warning(
                    "Aday haber atlandı; sıradaki RSS adayı denenecek: %s | %s",
                    candidate["title"],
                    exc,
                    exc_info=not expected_source_rejection,
                )

        save_combined_script(created_items)
        try:
            validate_created_items(created_items)
        except Exception as exc:  # noqa: BLE001
            save_project_info(
                len(candidates),
                rankings,
                created_items,
                status="incomplete",
                error=str(exc),
            )
            LOGGER.error("Proje kalite kapısından geçemedi: %s", exc)
            return 1

        used_urls.update(item["candidate"]["url"] for item in created_items)
        archive.mark_project_used(
            video_name=VIDEO_NAME,
            items=[
                {
                    "archive_id": item["archive_record"]["archive_id"],
                    "order": item["order"],
                    "narration": item["summary"].summary_en,
                }
                for item in created_items
            ],
        )
        save_state(used_urls)
        save_project_info(
            len(candidates),
            rankings,
            created_items,
            status="complete",
        )

        LOGGER.info("=" * 68)
        LOGGER.info("%s tamamlandı.", VIDEO_NAME)
        LOGGER.info("Oluşturulan haber sayısı: %s", len(created_items))
        LOGGER.info("Haber klasörü: %s", OUTPUT_DIR)
        LOGGER.info("Birleşik metin: %s", COMBINED_SCRIPT_PATH)
        LOGGER.info("=" * 68)

        return 0

    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("İşlem durduruldu: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
