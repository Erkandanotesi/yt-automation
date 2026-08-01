from __future__ import annotations

from typing import Literal


SeparatorMode = Literal["manual", "skipped"]


def require_separator_checkpoint(state: dict, timing: dict) -> SeparatorMode:
    """Validate and return the project's separator mode for downstream stages."""
    checkpoint = state.get("stages", {}).get("manual_separators", {})
    status = checkpoint.get("status")
    quality = timing.get("quality", {})
    if quality.get("status") not in {None, "passed"}:
        raise RuntimeError("Story timing quality gate has not passed.")
    if status == "READY":
        if not quality.get("manual_separators_validated", False):
            raise RuntimeError("Validated manual separator timing is missing.")
        return "manual"
    if status == "SKIPPED":
        if not quality.get("manual_separators_skipped", False):
            raise RuntimeError("Skipped separator timing is missing or stale.")
        if any(
            float(story.get("separator", {}).get("duration", 0)) > 0
            for story in timing.get("stories", [])
        ):
            raise RuntimeError("Skipped separator mode contains a non-zero separator.")
        return "skipped"
    raise RuntimeError(
        "Manual separator checkpoint must be READY or explicitly SKIPPED."
    )


def separators_are_skipped(state: dict, timing: dict) -> bool:
    return require_separator_checkpoint(state, timing) == "skipped"
