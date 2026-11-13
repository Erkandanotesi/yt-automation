"""Create the local motion-engine folders and download its JavaScript assets."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent
ASSETS = {
    "vendor/gsap.min.js": "https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js",
    "vendor/lucide.min.js": "https://unpkg.com/lucide@latest/dist/umd/lucide.min.js",
}


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_assets() -> list[Path]:
    (ROOT / "vendor").mkdir(parents=True, exist_ok=True)
    (ROOT / "cache" / "logos").mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    with _session() as session:
        for relative_path, url in ASSETS.items():
            destination = ROOT / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            response = session.get(url, timeout=(10, 60))
            response.raise_for_status()
            content = response.content
            if not content or content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise RuntimeError(f"The asset URL returned an empty or HTML response: {url}")

            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
            )
            try:
                with os.fdopen(handle, "wb") as temporary_file:
                    temporary_file.write(content)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            downloaded.append(destination)
            printable_path = str(destination).encode("ascii", "backslashreplace").decode("ascii")
            print(f"Downloaded {url} -> {printable_path}")

    return downloaded


if __name__ == "__main__":
    download_assets()
