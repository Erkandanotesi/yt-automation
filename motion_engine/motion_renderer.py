"""Render deterministic 1920x1080 motion scenes through Playwright and FFmpeg."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
LOCAL_PYTHON_PACKAGES = ROOT / "vendor" / "python"
if LOCAL_PYTHON_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PYTHON_PACKAGES))

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "cache" / "browsers"))


def _find_brave() -> Path | None:
    configured = os.environ.get("MOTION_ENGINE_BRAVE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(
                Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
            )
    located = shutil.which("brave") or shutil.which("brave-browser")
    if located:
        candidates.insert(0, Path(located))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def _find_ffmpeg() -> str:
    configured = os.environ.get("MOTION_ENGINE_FFMPEG")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise FileNotFoundError(f"MOTION_ENGINE_FFMPEG does not point to a file: {candidate}")

    located = shutil.which("ffmpeg")
    if located:
        return located

    local_ffmpeg_root = ROOT.parent / "Automation" / "tools" / "ffmpeg"
    if local_ffmpeg_root.is_dir():
        candidates = sorted(local_ffmpeg_root.glob("*/bin/ffmpeg.exe"), reverse=True)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())

    raise FileNotFoundError(
        "FFmpeg was not found on PATH or under Automation/tools/ffmpeg. "
        "Set MOTION_ENGINE_FFMPEG to its executable path."
    )


def _prepare_page(browser: Any, scene_config: dict[str, Any], duration: float) -> Any:
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    template = ROOT / "templates" / "base_stage.html"
    if not template.is_file():
        raise FileNotFoundError(f"Motion template not found: {template}")
    page.goto(template.as_uri(), wait_until="load")
    page.evaluate(
        "([config, sceneDuration]) => { window.initScene(config, sceneDuration); return true; }",
        [scene_config, duration],
    )
    return page


def _ffmpeg_command(
    ffmpeg_path: str, output_path: Path, fps: int, encoder_args: list[str]
) -> list[str]:
    return [
        ffmpeg_path,
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-framerate",
        str(fps),
        "-i",
        "-",
        "-c:v",
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]


def _render_attempt(
    browser: Any,
    scene_config: dict[str, Any],
    duration: float,
    output_path: Path,
    fps: int,
    encoder_args: list[str],
    ffmpeg_path: str,
) -> tuple[int, str]:
    total_frames = int(duration * fps)
    page = _prepare_page(browser, scene_config, duration)
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    process: subprocess.Popen[bytes] | None = None
    pipe_error: BrokenPipeError | None = None

    try:
        process = subprocess.Popen(
            _ffmpeg_command(ffmpeg_path, output_path, fps, encoder_args),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
        assert process.stdin is not None
        for frame in range(total_frames):
            page.evaluate("time => { window.tl.seek(time, false); return true; }", frame / fps)
            jpeg_bytes = page.screenshot(type="jpeg", quality=92)
            process.stdin.write(jpeg_bytes)
        process.stdin.close()
        return_code = process.wait()
    except BrokenPipeError as exc:
        pipe_error = exc
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        return_code = process.wait() if process is not None else 1
    except BaseException:
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait()
        raise
    finally:
        page.close()

    stderr_file.seek(0)
    stderr = stderr_file.read().decode("utf-8", errors="replace").strip()
    stderr_file.close()
    if pipe_error is not None and not stderr:
        stderr = str(pipe_error)
    return return_code, stderr


def render_scene(
    scene_config: dict[str, Any],
    duration: float,
    output_path: str,
    fps: int = 60,
) -> str:
    """Render a scene to a video file and return its absolute output path."""
    if not isinstance(scene_config, dict):
        raise TypeError("scene_config must be a dictionary")
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    if fps <= 0:
        raise ValueError("fps must be greater than zero")
    if int(duration * fps) < 1:
        raise ValueError("duration and fps must produce at least one frame")

    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    browser_cache = ROOT / "cache" / "browsers"
    browser_cache.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = _find_ffmpeg()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        bundled_chromium = Path(playwright.chromium.executable_path)
        brave = _find_brave()
        launch_options: dict[str, Any] = {"headless": True}
        if not bundled_chromium.is_file() and brave is not None:
            launch_options["executable_path"] = str(brave)
        browser = playwright.chromium.launch(**launch_options)
        try:
            code, stderr = _render_attempt(
                browser,
                scene_config,
                duration,
                target,
                fps,
                ["h264_nvenc", "-preset", "p4", "-b:v", "8M"],
                ffmpeg_path,
            )
            if code != 0:
                target.unlink(missing_ok=True)
                code, stderr = _render_attempt(
                    browser,
                    scene_config,
                    duration,
                    target,
                    fps,
                    ["libx264", "-preset", "fast", "-crf", "18"],
                    ffmpeg_path,
                )
            if code != 0:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"FFmpeg failed with exit code {code}: {stderr or 'no diagnostic output'}")
        finally:
            browser.close()

    return str(target)
