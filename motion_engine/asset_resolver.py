"""Resolve Simple Icons brand marks into the motion engine's local cache."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import requests


ROOT = Path(__file__).resolve().parent
LOGO_CACHE = ROOT / "cache" / "logos"
SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def get_brand_logo(slug: str) -> str | None:
    """Return a local SVG path for a Simple Icons slug, or ``None`` on 404."""
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("slug must contain only lowercase letters, digits, and hyphens")

    LOGO_CACHE.mkdir(parents=True, exist_ok=True)
    destination = LOGO_CACHE / f"{slug}.svg"
    if destination.is_file() and destination.stat().st_size:
        return str(destination)

    response = requests.get(
        f"https://cdn.simpleicons.org/{slug}/white",
        timeout=(10, 30),
        headers={"Accept": "image/svg+xml"},
    )
    if response.status_code == requests.codes.not_found:
        return None
    response.raise_for_status()

    content = response.content
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Simple Icons returned invalid SVG data for {slug!r}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError(f"Simple Icons returned a non-SVG response for {slug!r}")

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{slug}.", suffix=".svg", dir=LOGO_CACHE
    )
    try:
        with os.fdopen(handle, "wb") as temporary_file:
            temporary_file.write(content)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)

    return str(destination)
