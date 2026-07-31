from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from file_utils import atomic_write_json, json_fingerprint, sha256_file, sha256_text  # noqa: E402
from project_paths import AUTOMATION_ROOT, PROJECTS_ROOT  # noqa: E402
from storyboard_stage import probe_image  # noqa: E402


INDEX_SCHEMA_VERSION = 1
SUPPORTED_EXTENSIONS = {
    "",
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
GENERIC_FILENAME_TOKENS = {
    "copy",
    "download",
    "file",
    "image",
    "images",
    "img",
    "photo",
    "picture",
    "screenshot",
    "thumb",
    "thumbnail",
    "untitled",
}
KEYWORD_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "copy",
    "for",
    "from",
    "image",
    "images",
    "in",
    "main",
    "new",
    "of",
    "on",
    "photo",
    "picture",
    "the",
    "to",
    "v",
    "with",
}
ALIASES = {
    "chatgpt": {"openai", "chatbot", "ai"},
    "xbox": {"microsoft", "gaming", "console"},
    "steam": {"valve", "gaming", "store"},
    "starlink": {"spacex", "satellite", "internet"},
    "falcon": {"spacex", "rocket"},
    "android": {"google", "mobile", "phone"},
    "instagram": {"meta", "social", "media"},
    "facebook": {"meta", "social", "media"},
    "zuckerberg": {"meta", "person"},
    "newell": {"valve", "person"},
    "torvalds": {"linux", "person"},
    "nvidia": {"gpu", "chip", "semiconductor"},
    "intel": {"cpu", "chip", "semiconductor"},
    "amd": {"cpu", "gpu", "chip", "semiconductor"},
    "tesla": {"car", "vehicle", "ev"},
    "byd": {"car", "vehicle", "ev"},
    "geely": {"car", "vehicle", "ev"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MediaRoot:
    label: str
    path: Path
    repository_license_hint: str


@dataclass(frozen=True)
class MediaIndexSettings:
    index_path: Path
    roots: tuple[MediaRoot, ...]
    workers: int = 6
    minimum_width: int = 320
    minimum_height: int = 180
    crop_retention_threshold: float = 0.55

    @classmethod
    def from_environment(cls) -> "MediaIndexSettings":
        configured = os.getenv("MEDIA_INDEX_PATH", "indexes/media_index.json").strip()
        index_path = Path(configured)
        if not index_path.is_absolute():
            index_path = AUTOMATION_ROOT / index_path
        return cls(
            index_path=index_path.resolve(),
            roots=(
                MediaRoot(
                    label="primary",
                    path=(PROJECTS_ROOT / "Assets" / "Media").resolve(),
                    repository_license_hint="user_confirmed_no_copyright",
                ),
                MediaRoot(
                    label="no_copyright_repository",
                    path=(
                        PROJECTS_ROOT / "Assets" / "Media (No-Copyright)"
                    ).resolve(),
                    repository_license_hint="user_confirmed_no_copyright",
                ),
            ),
            workers=max(1, int(os.getenv("MEDIA_INDEX_WORKERS", "6"))),
            minimum_width=max(16, int(os.getenv("MEDIA_MIN_WIDTH", "320"))),
            minimum_height=max(16, int(os.getenv("MEDIA_MIN_HEIGHT", "180"))),
            crop_retention_threshold=float(
                os.getenv("MEDIA_CROP_RETENTION_THRESHOLD", "0.55")
            ),
        )

    def validate(self) -> None:
        if not self.roots:
            raise ValueError("At least one media root is required.")
        if self.workers < 1:
            raise ValueError("MEDIA_INDEX_WORKERS must be positive.")
        if not 0 < self.crop_retention_threshold <= 1:
            raise ValueError("MEDIA_CROP_RETENTION_THRESHOLD must be in (0, 1].")


def normalize_search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_text.casefold()).strip()


def tokenize(value: str) -> list[str]:
    normalized = normalize_search_text(value)
    raw_tokens = re.findall(r"[a-z0-9]+", normalized)
    tokens: list[str] = []
    for token in raw_tokens:
        if token in KEYWORD_STOP_WORDS:
            continue
        if len(token) < 2:
            continue
        if re.fullmatch(r"[0-9a-f]{12,}", token):
            continue
        if token not in tokens:
            tokens.append(token)
        for alias in sorted(ALIASES.get(token, set())):
            if alias not in tokens:
                tokens.append(alias)
    return tokens


def parsed_keywords(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root)
    parts = list(relative.parts[:-1]) + [path.stem]
    tokens: list[str] = []
    for part in parts:
        for token in tokenize(part):
            if token not in tokens:
                tokens.append(token)
    return tokens


def filename_caption(path: Path) -> str:
    value = normalize_search_text(path.stem)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"\b[0-9a-f]{12,}\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value


def crop_retention(aspect_ratio: float, target: float = 16 / 9) -> float:
    if aspect_ratio <= 0:
        return 0.0
    return round(min(aspect_ratio / target, target / aspect_ratio), 6)


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".metadata.json")


def _read_sidecar(path: Path) -> dict:
    sidecar = _sidecar_path(path)
    if not sidecar.is_file():
        return {}
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"sidecar_error": str(exc)}
    return value if isinstance(value, dict) else {"sidecar_error": "Not a JSON object."}


def _generic_filename(path: Path) -> bool:
    tokens = set(tokenize(path.stem))
    return not tokens or tokens <= GENERIC_FILENAME_TOKENS


def _quality_flags(
    *,
    path: Path,
    width: int,
    height: int,
    bytes_count: int,
    retention: float,
    settings: MediaIndexSettings,
) -> list[str]:
    flags: list[str] = []
    if width < settings.minimum_width or height < settings.minimum_height:
        flags.append("low_resolution")
    if retention < settings.crop_retention_threshold:
        flags.append("crop_risk")
    if width < height:
        flags.append("portrait")
    elif abs(width - height) / max(width, height) < 0.08:
        flags.append("square")
    if bytes_count < 8_000:
        flags.append("small_file")
    if _generic_filename(path):
        flags.append("generic_filename")
    normalized_name = normalize_search_text(path.name)
    if "screenshot" in normalized_name:
        flags.append("likely_screenshot")
    if "generated image" in normalized_name or "gemini generated" in normalized_name:
        flags.append("generated_image")
    if not path.suffix:
        flags.append("missing_extension")
    return flags


def _workspace_relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECTS_ROOT))
    except ValueError:
        return str(path)


def _build_asset(
    path: Path,
    root: MediaRoot,
    settings: MediaIndexSettings,
) -> dict:
    stat = path.stat()
    checksum = sha256_file(path)
    media = probe_image(path)
    width = int(media["width"])
    height = int(media["height"])
    aspect = width / max(1, height)
    retention = crop_retention(aspect)
    sidecar = _read_sidecar(path)
    license_status = str(
        sidecar.get("license_status")
        or root.repository_license_hint
    ).strip()
    asset_id = (
        f"asset_{checksum[:16]}_"
        f"{sha256_text(_workspace_relative(path))[:8]}"
    )
    return {
        "asset_id": asset_id,
        "absolute_path": str(path),
        "workspace_relative_path": _workspace_relative(path),
        "media_root": root.label,
        "root_relative_path": str(path.relative_to(root.path)),
        "folder": str(path.relative_to(root.path).parent),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": checksum,
        "codec": media.get("codec", ""),
        "pixel_format": media.get("pixel_format", ""),
        "width": width,
        "height": height,
        "aspect_ratio": round(aspect, 6),
        "crop_retention_16_9": retention,
        "keywords": parsed_keywords(path, root.path),
        "local_caption": filename_caption(path),
        "caption_source": "filename_and_folder",
        "source_url": str(sidecar.get("source_url", "")).strip(),
        "creator": str(sidecar.get("creator", "")).strip(),
        "license": {
            "status": license_status,
            "name": str(sidecar.get("license_name", "")).strip(),
            "url": str(sidecar.get("license_url", "")).strip(),
            "attribution_required": sidecar.get("attribution_required"),
            "attribution_text": str(
                sidecar.get("attribution_text", "")
            ).strip(),
            "basis": (
                "sidecar_metadata"
                if sidecar.get("license_status")
                else "user_repository_classification"
                if root.repository_license_hint
                == "user_confirmed_no_copyright"
                else "unspecified"
            ),
            "review_required": license_status in {"", "unknown"},
        },
        "quality_flags": _quality_flags(
            path=path,
            width=width,
            height=height,
            bytes_count=stat.st_size,
            retention=retention,
            settings=settings,
        ),
        "prior_use_count": 0,
        "usage_events": [],
        "indexed_at_utc": utc_now(),
        "sidecar_path": (
            str(_sidecar_path(path))
            if _sidecar_path(path).is_file()
            else ""
        ),
        "sidecar_error": sidecar.get("sidecar_error", ""),
    }


def _load_existing(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def discover_files(roots: Iterable[MediaRoot]) -> list[tuple[Path, MediaRoot]]:
    found: list[tuple[Path, MediaRoot]] = []
    for root in roots:
        if not root.path.is_dir():
            continue
        for path in root.path.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".metadata.json"):
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            found.append((path.resolve(), root))
    return sorted(found, key=lambda item: _path_key(item[0]))


def _preserve_user_metadata(new: dict, old: dict, *, renamed_from: str = "") -> dict:
    for field in ("source_url", "creator"):
        if not new.get(field) and old.get(field):
            new[field] = old[field]
    old_license = old.get("license", {})
    if (
        isinstance(old_license, dict)
        and old_license.get("status") not in {"", "unknown"}
        and new.get("license", {}).get("status") in {"", "unknown"}
    ):
        new["license"] = old_license
    events = old.get("usage_events", [])
    new["usage_events"] = list(dict.fromkeys(events if isinstance(events, list) else []))
    new["prior_use_count"] = len(new["usage_events"])
    if renamed_from:
        history = list(old.get("rename_history", []))
        history.append(
            {
                "from": renamed_from,
                "to": new["absolute_path"],
                "detected_at_utc": utc_now(),
            }
        )
        new["rename_history"] = history[-20:]
        new["renamed_from"] = renamed_from
        new["asset_id"] = old.get("asset_id", new["asset_id"])
    return new


def scan_media_index(
    settings: MediaIndexSettings | None = None,
) -> dict:
    settings = settings or MediaIndexSettings.from_environment()
    settings.validate()
    discovered = discover_files(settings.roots)
    existing = _load_existing(settings.index_path)
    old_assets = [
        item
        for item in existing.get("assets", [])
        if isinstance(item, dict) and item.get("absolute_path")
    ]
    old_by_path = {
        _path_key(item["absolute_path"]): item
        for item in old_assets
    }
    discovered_keys = {_path_key(path) for path, _ in discovered}
    missing_old = [
        item
        for key, item in old_by_path.items()
        if key not in discovered_keys
    ]
    missing_by_checksum: dict[str, list[dict]] = {}
    for item in missing_old:
        missing_by_checksum.setdefault(str(item.get("sha256", "")), []).append(item)

    assets: list[dict] = []
    pending: list[tuple[Path, MediaRoot, dict | None]] = []
    reused_count = 0
    for path, root in discovered:
        previous = old_by_path.get(_path_key(path))
        stat = path.stat()
        sidecar = _sidecar_path(path)
        sidecar_mtime = sidecar.stat().st_mtime_ns if sidecar.is_file() else 0
        previous_sidecar_mtime = int(previous.get("sidecar_mtime_ns", 0)) if previous else 0
        if (
            previous
            and int(previous.get("bytes", -1)) == stat.st_size
            and int(previous.get("mtime_ns", -1)) == stat.st_mtime_ns
            and sidecar_mtime == previous_sidecar_mtime
        ):
            reused = dict(previous)
            reused["media_root"] = root.label
            if (
                root.repository_license_hint == "user_confirmed_no_copyright"
                and reused.get("license", {}).get("basis")
                != "sidecar_metadata"
            ):
                reused["license"] = {
                    **reused.get("license", {}),
                    "status": "user_confirmed_no_copyright",
                    "basis": "user_repository_classification",
                    "review_required": False,
                }
            reused["last_seen_at_utc"] = utc_now()
            assets.append(reused)
            reused_count += 1
        else:
            pending.append((path, root, previous))

    indexed: list[tuple[dict, dict | None]] = []
    rejected: list[dict] = []
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        futures = {
            executor.submit(_build_asset, path, root, settings): (
                path,
                previous,
            )
            for path, root, previous in pending
        }
        for future in as_completed(futures):
            path, previous = futures[future]
            try:
                asset = future.result()
                sidecar = _sidecar_path(path)
                asset["sidecar_mtime_ns"] = (
                    sidecar.stat().st_mtime_ns if sidecar.is_file() else 0
                )
                indexed.append((asset, previous))
            except Exception as exc:  # noqa: BLE001
                rejected.append(
                    {
                        "path": str(path),
                        "error": str(exc),
                    }
                )

    renamed_count = 0
    new_count = 0
    changed_count = 0
    consumed_missing_ids: set[str] = set()
    for asset, previous in indexed:
        if previous is not None:
            assets.append(_preserve_user_metadata(asset, previous))
            changed_count += 1
            continue
        candidates = [
            item
            for item in missing_by_checksum.get(asset["sha256"], [])
            if item.get("asset_id") not in consumed_missing_ids
        ]
        if candidates:
            old = candidates[0]
            consumed_missing_ids.add(str(old.get("asset_id", "")))
            assets.append(
                _preserve_user_metadata(
                    asset,
                    old,
                    renamed_from=str(old.get("absolute_path", "")),
                )
            )
            renamed_count += 1
        else:
            assets.append(asset)
            new_count += 1

    checksum_counts: dict[str, int] = {}
    for asset in assets:
        checksum_counts[asset["sha256"]] = checksum_counts.get(asset["sha256"], 0) + 1
    for asset in assets:
        flags = list(asset.get("quality_flags", []))
        if checksum_counts.get(asset["sha256"], 0) > 1 and "duplicate_content" not in flags:
            flags.append("duplicate_content")
        elif checksum_counts.get(asset["sha256"], 0) <= 1:
            flags = [flag for flag in flags if flag != "duplicate_content"]
        asset["quality_flags"] = flags
        asset["last_seen_at_utc"] = utc_now()
        asset["prior_use_count"] = len(asset.get("usage_events", []))

    assets.sort(
        key=lambda item: normalize_search_text(item["workspace_relative_path"])
    )
    retired = [
        {
            "asset_id": item.get("asset_id", ""),
            "absolute_path": item.get("absolute_path", ""),
            "sha256": item.get("sha256", ""),
            "retired_at_utc": utc_now(),
        }
        for item in missing_old
        if item.get("asset_id") not in consumed_missing_ids
    ]
    previous_retired = [
        item for item in existing.get("retired_assets", []) if isinstance(item, dict)
    ]
    known_retired = {
        (item.get("asset_id"), item.get("absolute_path")): item
        for item in [*previous_retired, *retired]
    }
    index_fingerprint = json_fingerprint(
        [
            {
                "asset_id": item["asset_id"],
                "path": item["absolute_path"],
                "sha256": item["sha256"],
                "metadata": item.get("license", {}),
                "keywords": item.get("keywords", []),
            }
            for item in assets
        ]
    )
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "updated_at_utc": utc_now(),
        "index_path": str(settings.index_path),
        "roots": [
            {
                **asdict(root),
                "path": str(root.path),
                "exists": root.path.is_dir(),
            }
            for root in settings.roots
        ],
        "index_fingerprint": index_fingerprint,
        "stats": {
            "asset_count": len(assets),
            "reused_unchanged": reused_count,
            "new": new_count,
            "changed": changed_count,
            "renamed": renamed_count,
            "retired": len(retired),
            "rejected": len(rejected),
            "license_needs_review": sum(
                item.get("license", {}).get("status")
                in {"", "unknown"}
                for item in assets
            ),
            "crop_risk": sum(
                "crop_risk" in item.get("quality_flags", [])
                for item in assets
            ),
        },
        "assets": assets,
        "retired_assets": list(known_retired.values())[-500:],
        "rejected_files": rejected,
    }
    atomic_write_json(settings.index_path, payload)
    return payload


def load_media_index(
    settings: MediaIndexSettings | None = None,
    *,
    rescan: bool = True,
) -> dict:
    settings = settings or MediaIndexSettings.from_environment()
    if rescan:
        return scan_media_index(settings)
    value = _load_existing(settings.index_path)
    if not value:
        raise FileNotFoundError(
            f"Media index does not exist; run --scan: {settings.index_path}"
        )
    return value


def update_usage_events(
    index: dict,
    *,
    project_prefix: str,
    selections: list[tuple[str, str]],
    index_path: Path,
) -> dict:
    event_to_asset = {event: asset_id for event, asset_id in selections}
    for asset in index.get("assets", []):
        events = [
            event
            for event in asset.get("usage_events", [])
            if not str(event).startswith(project_prefix)
        ]
        for event, selected_asset_id in event_to_asset.items():
            if selected_asset_id == asset.get("asset_id") and event not in events:
                events.append(event)
        asset["usage_events"] = sorted(events)
        asset["prior_use_count"] = len(events)
    index["updated_at_utc"] = utc_now()
    atomic_write_json(index_path, index)
    return index


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally scan and index the reusable media repositories."
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan now (this is also the default action).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show the existing index without rescanning.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        settings = MediaIndexSettings.from_environment()
        payload = load_media_index(settings, rescan=not args.status)
        result = {
            "index_path": str(settings.index_path),
            "index_fingerprint": payload.get("index_fingerprint", ""),
            "stats": payload.get("stats", {}),
            "roots": payload.get("roots", []),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Media index: {settings.index_path}")
            for key, value in result["stats"].items():
                print(f"  {key}: {value}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[MEDIA INDEX ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
