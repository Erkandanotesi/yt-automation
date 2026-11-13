from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import feedparser
import requests

from file_locations import TECH_FEEDS_FILE
from file_utils import atomic_write_json
from news_pipeline import (
    DEFAULT_TECH_KEYWORDS,
    USER_AGENT,
    clustered_content_reasons,
    promotional_content_reasons,
    strip_html,
)


DEFAULT_OUTPUT = TECH_FEEDS_FILE.parent.parent / "data" / "feed_audit_latest.json"


def _feed_records(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    records = []
    for element in root.iter():
        url = element.attrib.get("xmlUrl") or element.attrib.get("xmlurl")
        if not url:
            continue
        records.append({
            "name": element.attrib.get("title") or element.attrib.get("text") or url,
            "feed_url": url.strip(),
            "site_url": (element.attrib.get("htmlUrl") or "").strip(),
        })
    return records


def _commerce_signal(title: str, summary: str) -> bool:
    candidate = {"title": title, "rss_text": summary}
    if promotional_content_reasons(candidate):
        return True
    text = f"{title} {summary}".casefold()
    return bool(re.search(
        r"\b(?:deal|deals|discount|coupon|buying guide|gift guide|best price|"
        r"price drop|save \$|save £|save €|shop now|buy now|reviewed)\b",
        text,
    ))


def _technology_signal(title: str, summary: str) -> bool:
    text = f"{title} {summary}".casefold()
    return any(keyword in text for keyword in DEFAULT_TECH_KEYWORDS)


def audit_feed(
    session: requests.Session,
    record: dict,
    *,
    sample_size: int,
    timeout: int,
) -> dict:
    url = record["feed_url"]
    result = {**record, "host": urlsplit(url).netloc.casefold()}
    try:
        response = session.get(url, timeout=timeout)
        result["http_status"] = response.status_code
        result["content_type"] = response.headers.get("content-type", "")
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001
        return {
            **result,
            "status": "unusable",
            "reason": f"feed_fetch_failed: {type(exc).__name__}: {exc}",
            "entry_count": 0,
            "samples": [],
        }

    entries = list(parsed.entries[:sample_size])
    samples = []
    for entry in entries:
        title = strip_html(str(entry.get("title", ""))).strip()
        summary = strip_html(
            str(entry.get("summary", "") or entry.get("description", ""))
        ).strip()
        samples.append({
            "title": title,
            "url": str(entry.get("link", "")).strip(),
            "promotion_signal": _commerce_signal(title, summary),
            "technology_signal": _technology_signal(title, summary),
            "clustered_signal": bool(clustered_content_reasons({"title": title})),
        })

    count = len(samples)
    promotion_count = sum(item["promotion_signal"] for item in samples)
    technology_count = sum(item["technology_signal"] for item in samples)
    clustered_count = sum(item["clustered_signal"] for item in samples)
    promotion_ratio = promotion_count / count if count else 0.0
    technology_ratio = technology_count / count if count else 0.0
    bozo = bool(getattr(parsed, "bozo", False))
    reasons = []
    status = "usable"
    if not count:
        status = "unusable"
        reasons.append("feed_has_no_entries")
    if "youtube.com/feeds/videos" in url:
        status = "unusable"
        reasons.append("video_channel_feed_not_article_news")
    if "news.ycombinator.com" in url:
        status = "unusable"
        reasons.append("aggregator_feed_requested_for_removal")
    if promotion_ratio >= 0.45:
        status = "unusable"
        reasons.append("promotion_ratio_at_least_45_percent")
    elif promotion_ratio >= 0.25 and status == "usable":
        status = "review"
        reasons.append("promotion_ratio_at_least_25_percent")
    clustered_ratio = clustered_count / count if count else 0.0
    if clustered_ratio >= 0.75:
        status = "unusable"
        reasons.append("clustered_multi_story_ratio_at_least_75_percent")
    elif clustered_ratio >= 0.25 and status == "usable":
        status = "review"
        reasons.append("clustered_multi_story_ratio_at_least_25_percent")
    if count >= 8 and technology_ratio < 0.25:
        status = "unusable"
        reasons.append("technology_relevance_below_25_percent")
    elif count >= 8 and technology_ratio < 0.40 and status == "usable":
        status = "review"
        reasons.append("technology_relevance_below_40_percent")
    if bozo:
        reasons.append(f"parser_warning: {getattr(parsed, 'bozo_exception', '')}")

    return {
        **result,
        "status": status,
        "reason": "; ".join(reasons) or "healthy_general_technology_feed",
        "entry_count": len(parsed.entries),
        "sample_count": count,
        "promotion_count": promotion_count,
        "promotion_ratio": round(promotion_ratio, 4),
        "technology_count": technology_count,
        "technology_ratio": round(technology_ratio, 4),
        "clustered_count": clustered_count,
        "clustered_ratio": round(clustered_ratio, 4),
        "parser_warning": bozo,
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit configured tech RSS feeds.")
    parser.add_argument("--feeds", type=Path, default=TECH_FEEDS_FILE)
    parser.add_argument(
        "--url",
        action="append",
        help="Audit an RSS URL without editing OPML; may be repeated.",
    )
    parser.add_argument("--name", default="Ad-hoc feed")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    if not 5 <= args.sample_size <= 50:
        raise ValueError("--sample-size must be between 5 and 50.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    results = []
    records = (
        [
            {
                "name": (
                    args.name
                    if len(args.url) == 1
                    else urlsplit(url).path.strip("/").replace("/rss.xml", "")
                ),
                "feed_url": url,
                "site_url": "",
            }
            for url in args.url
        ]
        if args.url
        else _feed_records(args.feeds.resolve())
    )
    for index, record in enumerate(records, start=1):
        print(f"[{index:02d}/{len(records):02d}] {record['name']}", flush=True)
        result = audit_feed(
            session,
            record,
            sample_size=args.sample_size,
            timeout=args.timeout,
        )
        results.append(result)
        print(
            f"  {result['status'].upper()} | entries={result.get('entry_count', 0)} | "
            f"promo={result.get('promotion_ratio', 0):.0%} | "
            f"tech={result.get('technology_ratio', 0):.0%} | "
            f"clustered={result.get('clustered_ratio', 0):.0%}",
            flush=True,
        )

    report = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "feed_file": str(args.feeds.resolve()),
        "sample_size_per_feed": args.sample_size,
        "feed_count": len(results),
        "counts": {
            status: sum(item["status"] == status for item in results)
            for status in ("usable", "review", "unusable")
        },
        "feeds": results,
    }
    atomic_write_json(args.output.resolve(), report)
    print(json.dumps(report["counts"], indent=2))
    print(f"Report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, ET.ParseError) as exc:
        print(f"[FEED AUDIT ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
