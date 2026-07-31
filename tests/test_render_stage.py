from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from render_stage import RenderSettings, build_render_plan


def write_fixture(root: Path, *, overlay_mode: bool = True) -> None:
    (root / "06_captions").mkdir(parents=True)
    (root / "07_effects").mkdir()
    visual = root / "visual.png"
    separator = root / "separator.mp4"
    narration = root / "voice.mp3"
    captions = root / "06_captions" / "captions.srt"
    frame = root / "frame.png"
    for path in (visual, separator, narration, frame):
        path.write_bytes(path.name.encode())
    captions.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTest\n",
        encoding="utf-8",
    )
    (root / "state.json").write_text(
        json.dumps({"stages": {"effects": {"status": "EFFECTS_READY"}}}),
        encoding="utf-8",
    )
    timing = {
        "duration_seconds": 2.0,
        "separator_timing_mode": (
            "overlay_at_narration_start" if overlay_mode else "reserved_before_narration"
        ),
        "stories": [
            {
                "story_id": "story_1",
                "order": 1,
                "audio_file": str(narration),
                "separator": {
                    "start": 0,
                    "end": 1,
                    "duration": 1,
                    "file": str(separator),
                    "has_audio": False,
                },
                "narration": {"start": 0, "end": 2, "duration": 2},
            }
        ],
    }
    (root / "story_timing.json").write_text(json.dumps(timing), encoding="utf-8")
    effects = {
        "quality": {"status": "passed", "no_directional_pans": True},
        "visual_events": [
            {
                "event_id": "visual_0001",
                "duration": 2,
                "asset": {"file": str(visual)},
                "motion": {"type": "slow_push_in", "maximum_scale": 1.008},
            }
        ],
        "glitch_events": [],
        "tv_frame": {"file": str(frame), "opacity": 0.8},
        "audio": {"background_music": None, "glitch_sfx": {}},
    }
    (root / "07_effects" / "effects_plan.json").write_text(
        json.dumps(effects), encoding="utf-8"
    )
    (root / "06_captions" / "captions.json").write_text(
        json.dumps(
            {
                "combined_srt_file": str(captions),
                "quality": {
                    "status": "passed",
                    "cue_count": 1,
                    "first_caption_start": 0,
                },
            }
        ),
        encoding="utf-8",
    )


class RenderStageTests(unittest.TestCase):
    def test_plan_requires_separator_overlay_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            write_fixture(root, overlay_mode=False)
            with patch("render_stage.project_dir", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "separators must overlay"):
                    build_render_plan(5, RenderSettings(width=320, height=180))

    def test_plan_starts_separator_narration_and_captions_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            write_fixture(root)
            settings = RenderSettings(
                width=320,
                height=180,
                preview_width=320,
                preview_height=180,
            )
            with patch("render_stage.project_dir", return_value=root):
                plan = build_render_plan(5, settings)
            self.assertEqual(plan["quality"]["status"], "passed")
            self.assertTrue(
                plan["quality"]["separator_and_narration_start_together"]
            )
            self.assertTrue(plan["quality"]["first_caption_starts_at_program_start"])
            self.assertTrue(plan["quality"]["no_directional_pans"])
            self.assertTrue(plan["quality"]["persistent_titles_cover_all_stories"])
            self.assertEqual(len(plan["story_titles"]), 1)


if __name__ == "__main__":
    unittest.main()
