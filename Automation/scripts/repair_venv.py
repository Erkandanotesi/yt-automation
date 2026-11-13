"""Repair or validate a relocated Python virtual environment on Windows."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


_VIRTUAL_ENV_PATTERNS = (
    (re.compile(r'^(set\s+"?VIRTUAL_ENV=)([^"\r\n]+)("?)', re.IGNORECASE | re.MULTILINE), "bat"),
    (re.compile(r"^(\$env:VIRTUAL_ENV\s*=\s*['\"])([^'\"]+)(['\"])", re.MULTILINE), "ps1"),
    (re.compile(r"^(VIRTUAL_ENV=['\"])([^'\"]+)(['\"])", re.MULTILINE), "sh"),
)


def parse_pyvenv_cfg(cfg_path: Path) -> dict[str, str]:
    """Read key=value entries from pyvenv.cfg."""
    entries: dict[str, str] = {}
    if not cfg_path.exists():
        return entries
    for raw_line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        entries[key.strip()] = value.strip()
    return entries


def update_activate_scripts(venv_dir: Path) -> list[Path]:
    """Update hardcoded VIRTUAL_ENV paths inside activate scripts to match venv_dir."""
    scripts_dir = venv_dir / "Scripts"
    if not scripts_dir.exists():
        return []

    target_str = str(venv_dir.resolve())
    updated: list[Path] = []
    targets = {
        "activate.bat": _VIRTUAL_ENV_PATTERNS[0][0],
        "Activate.ps1": _VIRTUAL_ENV_PATTERNS[1][0],
        "activate": _VIRTUAL_ENV_PATTERNS[2][0],
    }

    for filename, pattern in targets.items():
        script_path = scripts_dir / filename
        if not script_path.exists():
            continue
        original = script_path.read_text(encoding="utf-8", errors="replace")

        def _replace(match: re.Match[str]) -> str:
            return f"{match.group(1)}{target_str}{match.group(3)}"

        rewritten = pattern.sub(_replace, original)
        if rewritten != original:
            script_path.write_text(rewritten, encoding="utf-8")
            updated.append(script_path)
    return updated


def update_pyvenv_command(cfg_path: Path, venv_dir: Path) -> bool:
    """Update the informational `command = ...` line in pyvenv.cfg if relocated."""
    if not cfg_path.exists():
        return False
    original = cfg_path.read_text(encoding="utf-8", errors="replace")
    resolved_venv = str(venv_dir.resolve())
    lines: list[str] = []
    changed = False
    for line in original.splitlines():
        if line.strip().startswith("command") and "=" in line:
            key, val = line.split("=", 1)
            if " -m venv " in val:
                prefix, _old_target = val.split(" -m venv ", 1)
                new_line = f"{key.strip()} = {prefix.strip()} -m venv {resolved_venv}"
                if new_line != line:
                    changed = True
                lines.append(new_line)
                continue
        lines.append(line)
    if changed:
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _can_run(executable: Path, *args: str) -> bool:
    if not executable.exists():
        return False
    try:
        completed = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        return completed.returncode == 0
    except OSError:
        return False


def repair_venv(venv_dir: Path) -> int:
    """Validate and repair a moved virtual environment.

    Returns:
        0 if the venv is healthy (or repaired cleanly),
        2 if the base interpreter or venv python is broken and must be recreated.
    """
    resolved = venv_dir.resolve()
    python_exe = resolved / "Scripts" / "python.exe"
    cfg_path = resolved / "pyvenv.cfg"

    if not python_exe.exists() or not cfg_path.exists():
        print(f"[MISSING] Virtual environment incomplete at {resolved}")
        return 2

    cfg = parse_pyvenv_cfg(cfg_path)
    home = cfg.get("home", "")
    if home and not Path(home).exists():
        print(f"[BROKEN] Base Python home no longer exists: {home}")
        return 2

    if not _can_run(python_exe, "-c", "import sys; print(sys.prefix)"):
        print(f"[BROKEN] Could not execute {python_exe}")
        return 2

    updated_scripts = update_activate_scripts(resolved)
    updated_cfg = update_pyvenv_command(cfg_path, resolved)

    pip_exe = resolved / "Scripts" / "pip.exe"
    if pip_exe.exists() and not _can_run(pip_exe, "--version"):
        print(f"[REPAIR] Rebuilding relocated pip script wrappers in {resolved}...")
        reinstall = subprocess.run(
            [str(python_exe), "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-index", "pip"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if reinstall.returncode != 0:
            # Retry without --no-index or check if python -m pip still works.
            if not _can_run(python_exe, "-m", "pip", "--version"):
                print(f"[BROKEN] pip is unusable in {resolved}")
                return 2
            print(f"[WARN] pip.exe wrapper could not be rebuilt offline, but `python -m pip` works.")

    if updated_scripts or updated_cfg:
        print(f"[REPAIRED] Updated relocated venv paths in {resolved}")
    else:
        print(f"[OK] Virtual environment healthy: {resolved}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and repair relocated Python virtual environments.")
    parser.add_argument(
        "--venv",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".venv",
        help="Path to virtual environment directory (defaults to Automation/.venv).",
    )
    args = parser.parse_args(argv)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return repair_venv(args.venv)


if __name__ == "__main__":
    raise SystemExit(main())
