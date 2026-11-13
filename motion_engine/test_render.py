"""Render a three-second sample scene from the motion engine."""

from __future__ import annotations

from pathlib import Path

from motion_renderer import render_scene


ROOT = Path(__file__).resolve().parent


def main() -> None:
    config = {
        "template_type": "bento_stat",
        "kicker": "GPU MARKET UPDATE",
        "value": "1,247.5M",
        "label": "AI accelerators shipped worldwide this year",
        "footnote": "GLOBAL SHIPMENTS · 2026",
        "logo_icon": "cpu",
    }
    cached_logo = ROOT / "cache" / "logos" / "nvidia.svg"
    if cached_logo.is_file():
        config["logo_svg"] = cached_logo.read_text(encoding="utf-8")

    output = ROOT / "output" / "bento_stat_test.mp4"
    print("Rendering three-second bento_stat sample...", flush=True)
    rendered = render_scene(config, duration=3.0, output_path=str(output), fps=60)
    message = f"Rendered sample scene: {rendered}"
    print(message.encode("ascii", "backslashreplace").decode("ascii"))


if __name__ == "__main__":
    main()
