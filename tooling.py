from __future__ import annotations

import os
import shutil
from pathlib import Path

from project_paths import AUTOMATION_ROOT


def find_executable(name: str) -> Path | None:
    configured_bin = os.getenv("FFMPEG_BIN_DIR", "").strip()
    if configured_bin:
        bin_dir = Path(configured_bin).expanduser()
        if not bin_dir.is_absolute():
            bin_dir = AUTOMATION_ROOT / bin_dir
        candidate = bin_dir / f"{name}.exe"
        if candidate.is_file():
            return candidate.resolve()

    system_path = shutil.which(name)
    if system_path:
        return Path(system_path).resolve()

    portable_root = AUTOMATION_ROOT / "tools" / "ffmpeg"
    if portable_root.exists():
        matches = sorted(portable_root.glob(f"*/bin/{name}.exe"))
        if matches:
            return matches[-1].resolve()
        direct = portable_root / "bin" / f"{name}.exe"
        if direct.is_file():
            return direct.resolve()

    return None

