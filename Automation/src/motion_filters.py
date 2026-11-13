from __future__ import annotations


MOTION_RENDERER_NAME = "perspective_cubic_subpixel"
MOTION_RENDERER_VERSION = 2


def smooth_push_in_filter(
    *,
    width: int,
    height: int,
    fps: int,
    duration: float,
    frames: int,
    maximum_scale: float,
) -> str:
    """Return an FFmpeg filter chain for a smooth, centered image push-in.

    FFmpeg's zoompan filter quantizes crop coordinates to whole pixels. At subtle
    zoom levels that produces visible pauses and one-pixel jumps. Perspective
    evaluates floating-point source coordinates on every frame and resamples
    them cubically. Smoothstep easing also removes abrupt starts and stops.
    """
    if width <= 0 or height <= 0 or fps <= 0 or duration <= 0 or frames <= 0:
        raise ValueError("Smooth motion dimensions, FPS, duration, and frames must be positive.")
    if maximum_scale < 1.0:
        raise ValueError("Smooth motion maximum_scale must be at least 1.0.")

    last_frame = max(1, frames - 1)
    progress = f"on/{last_frame}"
    eased = f"({progress})*({progress})*(3-2*({progress}))"
    zoom = f"1+({maximum_scale}-1)*({eased})"
    margin_x = f"W*(1-1/({zoom}))/2"
    margin_y = f"H*(1-1/({zoom}))/2"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},"
        f"tpad=stop_mode=clone:stop_duration={duration},fps={fps},"
        f"perspective=x0='{margin_x}':y0='{margin_y}':"
        f"x1='W-{margin_x}':y1='{margin_y}':"
        f"x2='{margin_x}':y2='H-{margin_y}':"
        f"x3='W-{margin_x}':y3='H-{margin_y}':"
        "sense=source:eval=frame:interpolation=cubic,format=yuv420p"
    )
