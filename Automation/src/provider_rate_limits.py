"""Persist rate-limit headers observed during normal provider work.

The media providers do not expose a free quota-status endpoint. A probe is
itself a rate-limited request, so the UI only displays headers from real media
searches and download tracking instead of spending quota to fill a status card.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from file_utils import atomic_write_json


AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
OBSERVATIONS_PATH = AUTOMATION_ROOT / "data" / "provider_rate_limit_observations.json"
SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _header(headers: Mapping[str, object], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value or "").strip()
    return ""


def _integer(value: str) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def load_observation(provider: str) -> dict[str, object] | None:
    try:
        payload = json.loads(OBSERVATIONS_PATH.read_text(encoding="utf-8"))
        item = payload.get("providers", {}).get(provider)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(item, dict):
        return None
    limit = _integer(str(item.get("limit", "")))
    remaining = _integer(str(item.get("remaining", "")))
    observed_at = str(item.get("observed_at_utc", "")).strip()
    if limit is None or remaining is None or not observed_at:
        return None
    return {
        "limit": limit,
        "remaining": remaining,
        "reset": str(item.get("reset", "")).strip(),
        "observed_at_utc": observed_at,
    }


def record_observation(provider: str, headers: Mapping[str, object]) -> None:
    """Record usable rate-limit headers without affecting a media request."""
    limit = _integer(_header(headers, "X-Ratelimit-Limit"))
    remaining = _integer(_header(headers, "X-Ratelimit-Remaining"))
    if limit is None or remaining is None or limit <= 0:
        return
    try:
        try:
            payload = json.loads(OBSERVATIONS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        providers[provider] = {
            "limit": limit,
            "remaining": min(remaining, limit),
            "reset": _header(headers, "X-Ratelimit-Reset"),
            "observed_at_utc": _utc_now(),
        }
        atomic_write_json(
            OBSERVATIONS_PATH,
            {"schema_version": SCHEMA_VERSION, "providers": providers},
        )
    except OSError:
        # Status reporting must never make a media search fail.
        return
