from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


SOURCE_ROOT = Path(__file__).resolve().parent
AUTOMATION_ROOT = SOURCE_ROOT.parent
load_dotenv(AUTOMATION_ROOT / ".env")

from file_utils import atomic_write_json  # noqa: E402
from provider_rate_limits import load_observation  # noqa: E402


SCHEMA_VERSION = 6
CACHE_PATH = AUTOMATION_ROOT / "data" / "provider_quota_status.json"
PLACEHOLDERS = ("BURAYA_", "YOUR_", "CHANGE_ME", "REPLACE_ME")
TIMEOUT_SECONDS = 8.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configured(value: str | None) -> bool:
    normalized = (value or "").strip().strip("\"'")
    return bool(normalized) and not any(marker in normalized.upper() for marker in PLACEHOLDERS)


def _provider(
    provider_id: str,
    name: str,
    status: str,
    primary: str,
    detail: str,
    dashboard_url: str,
    *,
    meter: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": provider_id,
        "name": name,
        "status": status,
        "primary": primary,
        "detail": detail,
        "dashboard_url": dashboard_url,
    }
    if meter is not None:
        result["meter"] = max(0.0, min(1.0, meter))
    return result


def _elevenlabs_keys() -> list[tuple[str, str]]:
    values: dict[int, str] = {}
    env_path = AUTOMATION_ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^\s*ELEVENLABS_API_KEY_(\d+)\s*=\s*(.*?)\s*$", line)
            if not match:
                continue
            value = match.group(2).strip().strip("\"'")
            if _configured(value) and int(match.group(1)) not in values:
                values[int(match.group(1))] = value
    for name, value in os.environ.items():
        match = re.fullmatch(r"ELEVENLABS_API_KEY_(\d+)", name)
        if match and _configured(value):
            values[int(match.group(1))] = value.strip()
    legacy = os.getenv("ELEVENLABS_API_KEY", "").strip()
    result = [(str(slot), value) for slot, value in sorted(values.items())]
    if _configured(legacy) and all(value != legacy for _, value in result):
        result.append(("legacy", legacy))
    return result


def _get_elevenlabs_subscription(slot: str, key: str) -> dict[str, Any]:
    response = requests.get(
        "https://api.elevenlabs.io/v1/user/subscription",
        headers={"xi-api-key": key, "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        status_code = int(getattr(response, "status_code", 0) or 0)
        error = (
            "unauthorized"
            if status_code in {401, 403}
            else "rate_limited"
            if status_code == 429
            else "http_error"
        )
        return {"slot": slot, "error": error, "http_status": status_code}
    payload = response.json()
    used = int(payload.get("character_count", 0))
    limit = int(payload.get("character_limit", 0))
    return {
        "slot": slot,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "reset": payload.get("next_character_count_reset_unix"),
    }


def _validate_elevenlabs_key(slot: str, key: str) -> bool:
    try:
        response = requests.get(
            "https://api.elevenlabs.io/v2/voices",
            params={"page_size": "1"},
            headers={"xi-api-key": key, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def elevenlabs_status() -> dict[str, Any]:
    keys = _elevenlabs_keys()
    dashboard = "https://elevenlabs.io/app/usage"
    if not keys:
        return _provider(
            "elevenlabs",
            "ElevenLabs",
            "not_configured",
            "Not configured",
            "Add an ElevenLabs API key in Settings.",
            dashboard,
        )
    results: list[dict[str, Any]] = []
    failures: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
        pending = {
            executor.submit(_get_elevenlabs_subscription, slot, key): slot
            for slot, key in keys
        }
        for future in as_completed(pending):
            try:
                result = future.result()
                if error := result.get("error"):
                    failures[error] = failures.get(error, 0) + 1
                else:
                    results.append(result)
            except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
                failures["network_or_response"] = failures.get("network_or_response", 0) + 1
    if not results:
        rejected = failures.get("unauthorized", 0)
        rate_limited = failures.get("rate_limited", 0)
        if rejected == len(keys):
            with ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
                usable = sum(
                    1
                    for valid in executor.map(
                        lambda item: _validate_elevenlabs_key(*item),
                        keys,
                    )
                    if valid
                )
            if usable:
                return _provider(
                    "elevenlabs",
                    "ElevenLabs",
                    "limited" if usable == len(keys) else "warning",
                    f"{usable}/{len(keys)} keys usable",
                    "Voice access works · subscription balance permission is unavailable.",
                    dashboard,
                )
            primary = "Keys rejected"
            detail = f"ElevenLabs rejected all {len(keys)} configured key(s)."
        elif rate_limited == len(keys):
            primary = "Rate limited"
            detail = f"All {len(keys)} configured key(s) are temporarily rate limited."
        else:
            primary = "Could not check"
            detail = f"{len(keys)} configured key(s); no usable subscription response."
        return _provider(
            "elevenlabs",
            "ElevenLabs",
            "error",
            primary,
            detail,
            dashboard,
        )
    total_remaining = sum(item["remaining"] for item in results)
    total_used = sum(item["used"] for item in results)
    total_limit = sum(item["limit"] for item in results)
    detail = f"{len(results)} key(s) · {total_used:,} used of {total_limit:,}"
    failure_count = sum(failures.values())
    if failure_count:
        detail += f" · {failure_count} unavailable"
    return _provider(
        "elevenlabs",
        "ElevenLabs",
        "available" if not failure_count else "warning",
        f"{total_remaining:,} credits left",
        detail,
        dashboard,
        meter=(total_remaining / total_limit) if total_limit else 0.0,
    )


def _observed_rate_status(
    provider_id: str,
    name: str,
    dashboard: str,
    not_configured_detail: str,
) -> dict[str, Any]:
    """Show a real media-request observation without making a quota probe."""
    key_name = f"{provider_id.upper()}_API_KEY"
    if not _configured(os.getenv(key_name, "").strip()):
        return _provider(
            provider_id,
            name,
            "not_configured",
            "Not configured",
            not_configured_detail,
            dashboard,
        )
    observed = load_observation(provider_id)
    if observed is None:
        return _provider(
            provider_id,
            name,
            "limited",
            "API key ready",
            "Quota appears after the next media request; refresh does not spend quota.",
            dashboard,
        )
    limit = int(observed["limit"])
    remaining = int(observed["remaining"])
    timestamp = str(observed["observed_at_utc"])
    return _provider(
        provider_id,
        name,
        "available",
        f"{remaining:,} requests left",
        f"of {limit:,} · last observed during a media request at {timestamp}",
        dashboard,
        meter=remaining / limit,
    )


def unsplash_status() -> dict[str, Any]:
    return _observed_rate_status(
        "unsplash",
        "Unsplash",
        "https://unsplash.com/oauth/applications",
        "Local media remains available; add a key for online fallback.",
    )


def pexels_status() -> dict[str, Any]:
    key = os.getenv("PEXELS_API_KEY", "").strip()
    dashboard = "https://www.pexels.com/api/"
    if not _configured(key):
        return _provider(
            "pexels",
            "Pexels Videos",
            "not_configured",
            "Not configured",
            "Pixabay or the image fallback pipeline remains available.",
            dashboard,
        )
    try:
        response = requests.get(
            "https://api.pexels.com/v1/videos/popular",
            params={"page": "1", "per_page": "1"},
            headers={"Authorization": key, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        limit = int(response.headers.get("X-Ratelimit-Limit", "0"))
        remaining = int(response.headers.get("X-Ratelimit-Remaining", "0"))
    except (requests.RequestException, ValueError):
        return _provider(
            "pexels",
            "Pexels Videos",
            "error",
            "Could not check",
            "The key is configured, but Pexels did not accept the status request.",
            dashboard,
        )
    if limit <= 0 or remaining < 0:
        return _provider(
            "pexels",
            "Pexels Videos",
            "limited",
            "API key ready",
            "The status request succeeded; Pexels did not expose a numeric remaining quota.",
            dashboard,
        )
    return _provider(
        "pexels",
        "Pexels Videos",
        "available",
        f"{remaining:,} requests left",
        f"of {limit:,} in the current limit window · refresh uses one request",
        dashboard,
        meter=remaining / limit,
    )


def pixabay_status() -> dict[str, Any]:
    return _observed_rate_status(
        "pixabay",
        "Pixabay Videos",
        "https://pixabay.com/api/docs/",
        "The image fallback pipeline remains available.",
    )


def openai_status() -> dict[str, Any]:
    configured = _configured(os.getenv("OPENAI_API_KEY"))
    return _provider(
        "openai",
        "OpenAI API",
        "limited" if configured else "not_configured",
        "Balance not exposed" if configured else "Not configured",
        (
            "API key ready · remaining credits are available only in Billing."
            if configured
            else "Add OPENAI_API_KEY in Settings."
        ),
        "https://platform.openai.com/usage",
    )


def gemini_status() -> dict[str, Any]:
    configured = _configured(os.getenv("GEMINI_API_KEY"))
    return _provider(
        "gemini",
        "Gemini API",
        "limited" if configured else "not_configured",
        "Quota in AI Studio" if configured else "Not configured",
        (
            "Key ready · RPM, TPM, and daily limits are project-managed."
            if configured
            else "Add GEMINI_API_KEY in Settings."
        ),
        "https://aistudio.google.com/app/usage",
    )


def youtube_status() -> dict[str, Any]:
    client_file = Path(
        os.getenv(
            "YOUTUBE_CLIENT_SECRETS_FILE",
            str(AUTOMATION_ROOT / ".youtube_credentials" / "client_secret.json"),
        )
    )
    token_file = Path(
        os.getenv(
            "YOUTUBE_TOKEN_FILE",
            str(AUTOMATION_ROOT / ".youtube_credentials" / "youtube_token.json"),
        )
    )
    if not client_file.is_absolute():
        client_file = AUTOMATION_ROOT / client_file
    if not token_file.is_absolute():
        token_file = AUTOMATION_ROOT / token_file
    configured = client_file.is_file() and token_file.is_file()
    return _provider(
        "youtube",
        "YouTube Data",
        "limited" if configured else "not_configured",
        "Quota in Cloud Console" if configured else "Authorization needed",
        (
            "OAuth ready · live remaining units are console-managed."
            if configured
            else "Authorize YouTube to enable uploads."
        ),
        "https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas",
    )


def collect_status() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=4) as executor:
        eleven_future = executor.submit(elevenlabs_status)
        pexels_future = executor.submit(pexels_status)
        pixabay_future = executor.submit(pixabay_status)
        unsplash_future = executor.submit(unsplash_status)
        providers = [
            openai_status(),
            gemini_status(),
            eleven_future.result(),
            pexels_future.result(),
            pixabay_future.result(),
            unsplash_future.result(),
            youtube_status(),
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at_utc": utc_now(),
        "cached": False,
        "data_source": "local_observations",
        "providers": providers,
    }


def load_cached(max_age_seconds: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(payload["updated_at_utc"]))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION or age > max_age_seconds:
        return None
    payload["cached"] = True
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(prog="provider-quota-status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    arguments = parser.parse_args()
    try:
        cache_seconds = max(0, int(os.getenv("PROVIDER_QUOTA_CACHE_SECONDS", "300")))
    except ValueError:
        cache_seconds = 300
    payload = None if arguments.refresh else load_cached(cache_seconds)
    if payload is None:
        payload = collect_status()
        atomic_write_json(CACHE_PATH, payload)
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for provider in payload["providers"]:
            print(f"{provider['name']}: {provider['primary']} — {provider['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
