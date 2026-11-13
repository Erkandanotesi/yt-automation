from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import news_studio


_transport_out = sys.stdout
_transport_in = sys.stdin
_current_video: int | None = None
RENDER_PROGRESS_PREFIX = "NEWS_STUDIO_RENDER_PROGRESS "


def emit(event_type: str, **payload: Any) -> None:
    message = {"type": event_type, **payload}
    _transport_out.write(json.dumps(message, ensure_ascii=False) + "\n")
    _transport_out.flush()


class EventTextStream:
    def __init__(self, level: str) -> None:
        self.level = level
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                emit("log", level=self.level, message=line.rstrip("\r"))
        return len(value)

    def flush(self) -> None:
        if self.buffer.strip():
            emit("log", level=self.level, message=self.buffer.rstrip("\r"))
            self.buffer = ""

    def isatty(self) -> bool:
        return False


class GuiProgress:
    def __init__(self, video_number: int, **_: Any) -> None:
        global _current_video
        _current_video = video_number
        self.video_number = video_number
        self.total = len(news_studio.FLOW_STAGES)
        self.current_stage: str | None = None
        self.completed = 0

    def start_stage(self, stage: str, completed: int) -> None:
        self.current_stage = stage
        self.completed = completed
        emit(
            "stage_started",
            video_number=self.video_number,
            stage=stage,
            label=news_studio.STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
            completed=completed,
            total=self.total,
        )

    def write_subprocess_line(self, line: str) -> None:
        normalized = line.strip()
        if not normalized:
            return
        if normalized.startswith(RENDER_PROGRESS_PREFIX):
            try:
                progress_payload = json.loads(normalized[len(RENDER_PROGRESS_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                progress_payload = None
            if isinstance(progress_payload, dict):
                emit(
                    "render_progress",
                    video_number=self.video_number,
                    stage=self.current_stage,
                    **progress_payload,
                )
                return
        lowered = normalized.casefold()
        level = "error" if "error" in lowered or "failed" in lowered else (
            "warning" if "warning" in lowered else "info"
        )
        emit("log", level=level, message=normalized, stage=self.current_stage)

    def complete_stage(self, completed: int) -> None:
        self.completed = completed
        emit(
            "stage_completed",
            video_number=self.video_number,
            stage=self.current_stage,
            completed=completed,
            total=self.total,
        )

    def waiting(self, completed: int) -> None:
        self.completed = completed
        emit(
            "stage_waiting",
            video_number=self.video_number,
            stage=self.current_stage,
            completed=completed,
            total=self.total,
        )

    def network_waiting(self, attempt: int, retry_seconds: float) -> None:
        emit(
            "network_waiting",
            video_number=self.video_number,
            stage=self.current_stage,
            attempt=attempt,
            retry_seconds=retry_seconds,
        )

    def network_resumed(self) -> None:
        emit(
            "network_resumed",
            video_number=self.video_number,
            stage=self.current_stage,
        )

    def failed(self, completed: int) -> None:
        emit(
            "stage_failed",
            video_number=self.video_number,
            stage=self.current_stage,
            completed=completed,
            total=self.total,
        )

    def cancellation_requested(self) -> bool:
        path = os.getenv("NEWS_STUDIO_GUI_CANCEL_FILE", "").strip()
        return bool(path) and Path(path).is_file()


class RecoveringProjectLock(news_studio.ProjectLock):
    """Recover only locks whose recorded process no longer exists.

    Closing the GUI can terminate the bridge between Python finally blocks. The
    next GUI run must remain resumable without weakening the live-process lock.
    """

    def __init__(self, video_dir: Path, *, recover_stale: bool = False) -> None:
        super().__init__(video_dir, recover_stale=True)


def _preference_enabled(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _preference_choice(prompt: str, default: str) -> str | None:
    normalized = prompt.casefold()
    if "choose the production action" in normalized:
        if _preference_enabled("NEWS_STUDIO_GUI_DIRECT_NEW_VIDEO", False):
            return "1"
        if _preference_enabled("NEWS_STUDIO_GUI_DIRECT_RESUME_LATEST", False):
            return "2"
        if os.getenv("NEWS_STUDIO_GUI_DIRECT_RESUME_VIDEO", "").strip():
            return "2"
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_PRODUCTION_ACTION"
    elif "choose the section to continue from" in normalized:
        if _preference_enabled("NEWS_STUDIO_GUI_DIRECT_RESUME_LATEST", False):
            return default
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_RESUME_SECTION"
    elif "allow the listed provider requests" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_PROVIDER_REQUESTS"
    elif "which news should be first" in normalized and "youtube title" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_LEAD_STORY"
    elif "manual video separator checkpoint" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_SEPARATORS"
    elif "add ai-generated videos" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_AI_VIDEOS"
    elif "approve the review preview" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_PREVIEW"
    elif "review or change the youtube title" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_TITLE"
    elif "upload the video" in normalized and "youtube" in normalized:
        preference = "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_UPLOAD"
    else:
        return None
    if _preference_enabled(preference, False):
        return default
    return None


def gui_timed_choice(
    prompt: str,
    options: dict[str, str],
    *,
    default: str,
    timeout_seconds: float,
    allow_shift_enter_fast_mode: bool = False,
    detail_items: list[str] | None = None,
    wait_indefinitely: bool = False,
    asset_path: str | None = None,
    asset_label: str | None = None,
    editable_text: str | None = None,
    editable_label: str | None = None,
    editable_max_length: int = 100,
    **_: Any,
) -> str:
    news_studio._last_choice_custom_text = None
    is_preview_review = "review preview" in prompt.casefold()
    if asset_path is None and is_preview_review and _current_video is not None:
        asset_path = str(
            news_studio.project_dir(_current_video)
            / "07_effects"
            / "milestone6_reference_60s.mp4"
        )
        asset_label = "Open preview"

    preferred = None if wait_indefinitely else _preference_choice(prompt, default)
    if preferred in options:
        emit(
            "choice_selected",
            prompt=prompt,
            choice=preferred,
            label=options[preferred],
            automatic=True,
            preference=True,
        )
        return preferred

    if (
        not wait_indefinitely
        and news_studio._bool_env(news_studio.FAST_DEFAULTS_ENV, False)
    ):
        emit(
            "choice_selected",
            prompt=prompt,
            choice=default,
            label=options[default],
            automatic=True,
        )
        return default

    gui_options = (
        {key: value for key, value in options.items() if key != "c"}
        if editable_text is not None
        else options
    )
    request_payload: dict[str, Any] = {
        "prompt": prompt,
        "options": gui_options,
        "default": default,
        "timeout_seconds": timeout_seconds,
        "allow_fast_defaults": allow_shift_enter_fast_mode,
        "asset_path": asset_path,
        "asset_label": asset_label or "Open file",
        "auto_open_asset": is_preview_review,
        "detail_items": detail_items or [],
        "wait_indefinitely": wait_indefinitely,
    }
    if editable_text is not None:
        request_payload["editable_text"] = editable_text
        request_payload["editable_label"] = editable_label or "Custom value"
        request_payload["editable_max_length"] = editable_max_length
    emit("choice_requested", **request_payload)
    selected = default
    fast_defaults = False
    custom_text_selected: str | None = None
    while True:
        raw = _transport_in.readline()
        if not raw:
            break
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if response.get("type") != "choice_response":
            continue
        if bool(response.get("cancel", False)):
            emit("choice_cancelled", prompt=prompt)
            raise KeyboardInterrupt
        candidate = str(response.get("choice", ""))
        if candidate in options:
            selected = candidate
        elif candidate.casefold() in options:
            selected = candidate.casefold()
        raw_custom = response.get("custom_text")
        if isinstance(raw_custom, str) and raw_custom.strip():
            custom_text_selected = raw_custom.strip()
            news_studio._last_choice_custom_text = custom_text_selected
        fast_defaults = bool(response.get("fast_defaults", False))
        break
    if allow_shift_enter_fast_mode and fast_defaults:
        os.environ[news_studio.FAST_DEFAULTS_ENV] = "1"
    selected_payload: dict[str, Any] = {
        "prompt": prompt,
        "choice": selected,
        "label": options[selected],
        "automatic": False,
        "fast_defaults": fast_defaults,
    }
    if custom_text_selected is not None:
        selected_payload["custom_text"] = custom_text_selected
    emit("choice_selected", **selected_payload)
    return selected


def main() -> int:
    sys.stdout = EventTextStream("info")
    sys.stderr = EventTextStream("error")
    news_studio.timed_choice = gui_timed_choice
    news_studio.ProgressDisplay = GuiProgress
    news_studio.ProjectLock = RecoveringProjectLock
    emit("bridge_ready", stages=list(news_studio.FLOW_STAGES))
    try:
        result = news_studio.interactive()
    except KeyboardInterrupt:
        emit("bridge_finished", exit_code=130, status="CANCELLED")
        return 130
    except Exception as exc:  # noqa: BLE001
        emit(
            "fatal_error",
            message=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
        )
        emit("bridge_finished", exit_code=1, status="FAILED")
        return 1
    if _current_video is not None:
        try:
            status = news_studio.collect_project_status(_current_video)
            next_stage = status.get("next_stage")
            if next_stage in {"manual_separators", "ai_story_videos"}:
                emit(
                    "attention_required",
                    video_number=_current_video,
                    stage=next_stage,
                    detail=status.get("stages", {}).get(next_stage, {}).get("detail", ""),
                )
        except Exception:  # noqa: BLE001
            pass
    final_status = "COMPLETE" if result == 0 else "FAILED"
    if result == 0 and _current_video is not None:
        try:
            if news_studio.collect_project_status(_current_video).get("next_stage"):
                final_status = "WAITING"
        except Exception:  # noqa: BLE001
            pass
    emit("bridge_finished", exit_code=result, status=final_status)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
