from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from file_locations import ENV_FILE
from file_utils import atomic_write_text


ASSIGNMENT_PATTERN = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*="
)
MOJIBAKE_MARKERS = ("Ã", "Ä", "Å", "Â", "â€", "�")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _decode_env(data: bytes) -> tuple[str, str, bool]:
    has_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        return data.decode("utf-8-sig"), "utf-8", has_bom
    except UnicodeDecodeError:
        try:
            return data.decode("cp1254"), "cp1254", has_bom
        except UnicodeDecodeError as exc:
            raise RuntimeError(".env is neither valid UTF-8 nor Windows-1254.") from exc


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in MOJIBAKE_MARKERS)


def repair_comment(comment: str) -> str:
    if _mojibake_score(comment) == 0:
        return comment
    try:
        candidate = comment.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return comment
    return candidate if _mojibake_score(candidate) < _mojibake_score(comment) else comment


def inspect_env(path: Path = ENV_FILE) -> dict:
    data = path.read_bytes()
    text, encoding, has_bom = _decode_env(data)
    occurrences: dict[str, list[int]] = {}
    mojibake_comment_lines: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT_PATTERN.match(line)
        if match:
            occurrences.setdefault(match.group(1), []).append(number)
        if line.lstrip().startswith("#") and _mojibake_score(line):
            mojibake_comment_lines.append(number)
    return {
        "path": str(path),
        "encoding": encoding,
        "has_utf8_bom": has_bom,
        "variable_count": sum(len(lines) for lines in occurrences.values()),
        "duplicate_variables": {
            name: lines for name, lines in occurrences.items() if len(lines) > 1
        },
        "mojibake_comment_lines": mojibake_comment_lines,
    }


def fix_env(path: Path = ENV_FILE) -> dict:
    data = path.read_bytes()
    text, _, _ = _decode_env(data)
    lines = text.splitlines()
    last_assignment: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = ASSIGNMENT_PATTERN.match(line)
        if match:
            last_assignment[match.group(1)] = index

    cleaned: list[str] = []
    removed_duplicates: list[str] = []
    repaired_comments = 0
    for index, line in enumerate(lines):
        match = ASSIGNMENT_PATTERN.match(line)
        if match and last_assignment[match.group(1)] != index:
            removed_duplicates.append(match.group(1))
            continue
        if line.lstrip().startswith("#"):
            repaired = repair_comment(line)
            repaired_comments += int(repaired != line)
            line = repaired
        cleaned.append(line.rstrip())

    while cleaned and not cleaned[-1]:
        cleaned.pop()
    atomic_write_text(path, "\n".join(cleaned) + "\n")
    return {
        "status": "fixed",
        "path": str(path),
        "removed_duplicate_variables": sorted(set(removed_duplicates)),
        "repaired_comment_count": repaired_comments,
        "secret_values_printed": False,
        "after": inspect_env(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or safely normalize .env without displaying values."
    )
    parser.add_argument("command", choices=("status", "fix"), nargs="?", default="status")
    parser.add_argument("--path", type=Path, default=ENV_FILE)
    args = parser.parse_args()
    result = fix_env(args.path) if args.command == "fix" else inspect_env(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
