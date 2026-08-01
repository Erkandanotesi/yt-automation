from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from render_stage import (
    RenderSettings,
    _compose_video_track,
    _mix_audio_track,
    _render_preview,
    build_render_plan,
)


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
        json.dumps(
            {
                "stages": {
                    "effects": {"status": "EFFECTS_READY"},
                    "manual_separators": {"status": "READY"},
                }
            }
        ),
        encoding="utf-8",
    )
    timing = {
        "duration_seconds": 2.0,
        "quality": {"status": "passed", "manual_separators_validated": True},
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
                "story_id": "story_1",
                "timeline_start": 0,
                "timeline_end": 2,
                "attribution": {
                    "required": True,
                    "creator": "Example Creator",
                    "source_url": "https://commons.wikimedia.org/wiki/File:Example",
                    "license_name": "CC BY-SA 4.0",
                    "provided_text": "Example Creator; CC BY-SA 4.0",
                },
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
    def test_timeline_paper_and_shutter_sfx_are_delayed_and_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "duration_seconds": 10,
                "narration": [
                    {"file": str(root / "voice.mp3"), "start": 0, "duration": 10}
                ],
                "separators": [],
                "background_music": None,
                "glitch_events": [],
                "glitch_sfx": {},
                "timeline_sfx": {
                    "assets": {
                        "paper": {
                            "file": str(root / "paper.mp3"),
                            "gain_db": -10,
                            "duration_seconds": 1.85,
                        },
                        "shutter": {
                            "file": str(root / "shutter.mp3"),
                            "gain_db": -10,
                            "duration_seconds": 0.44,
                        },
                    },
                    "events": [
                        {"sound": "paper", "start": 0},
                        {"sound": "paper", "start": 5},
                        {"sound": "shutter", "start": 3},
                    ],
                },
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _mix_audio_track(root, plan, [], RenderSettings())

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("asplit=2[timeline_paper_0][timeline_paper_1]", filters)
            self.assertIn("adelay=5000|5000[timeline_delayed_paper_1]", filters)
            self.assertIn("adelay=3000|3000[timeline_delayed_shutter_0]", filters)
            self.assertIn("amix=inputs=4", filters)

    def test_skipped_separator_plan_uses_images_from_program_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            write_fixture(root)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            state["stages"]["manual_separators"] = {"status": "SKIPPED"}
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            timing = json.loads(
                (root / "story_timing.json").read_text(encoding="utf-8")
            )
            timing["quality"] = {
                "status": "passed",
                "manual_separators_validated": False,
                "manual_separators_skipped": True,
                "images_start_at_chapter_start": True,
            }
            timing["stories"][0]["separator"] = {
                "start": 0,
                "end": 0,
                "duration": 0,
                "file": "",
                "skipped": True,
            }
            (root / "story_timing.json").write_text(
                json.dumps(timing), encoding="utf-8"
            )
            (root / "separator.mp4").unlink()

            with patch("render_stage.project_dir", return_value=root):
                plan = build_render_plan(
                    5,
                    RenderSettings(
                        width=320,
                        height=180,
                        preview_width=320,
                        preview_height=180,
                    ),
                )

            self.assertEqual(plan["separators"], [])
            self.assertTrue(plan["quality"]["images_start_at_chapter_start"])

    def test_preview_encode_limits_aac_peak_overshoot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _render_preview(root / "final.mp4", root, RenderSettings())

            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("-af") + 1],
                "volume=-1dB,alimiter=limit=0.794:level=false",
            )

    def test_animated_tv_effect_preserves_alpha_and_skips_separator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "duration_seconds": 4,
                "separators": [
                    {"start": 1, "end": 2, "duration": 1}
                ],
                "glitch_events": [],
                "image_tv_effect": {
                    "enabled": True,
                    "file": str(root / "effect.mov"),
                    "opacity": 1.0,
                },
                "tv_frame": {"file": str(root / "frame.png"), "opacity": 1.0},
                "story_titles": [],
                "source_attributions": [],
                "captions": {"file": str(root / "captions.srt")},
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _compose_video_track(
                    root,
                    plan,
                    root / "base.mp4",
                    [root / "separator.mp4"],
                    RenderSettings(width=320, height=180),
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("format=rgba,colorchannelmixer=aa=1.0", filters)
            self.assertIn(
                "enable='not(between(t\\,1.000000\\,2.000000))'",
                filters,
            )
            self.assertLess(filters.index("[imageeffect]"), filters.index("[sep1]"))
            self.assertLess(filters.index("[sep1]"), filters.index("[frame]"))

    def test_attribution_filter_is_anchored_bottom_right(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = RenderSettings(
                width=320,
                height=180,
                attribution_margin_right=17,
                attribution_margin_bottom=23,
            )
            plan = {
                "duration_seconds": 2,
                "separators": [],
                "glitch_events": [],
                "tv_frame": {},
                "story_titles": [],
                "source_attributions": [
                    {
                        "start": 0.25,
                        "end": 1.75,
                        "text_file": str(root / "source.txt"),
                    }
                ],
                "captions": {"file": str(root / "captions.srt")},
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _compose_video_track(
                    root,
                    plan,
                    root / "base.mp4",
                    [],
                    settings,
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("x=w-text_w-17:y=h-text_h-23", filters)
            self.assertIn("between(t\\,0.250000\\,1.750000)", filters)

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
            self.assertEqual(len(plan["source_attributions"]), 1)
            self.assertIn("Source: Example Creator", plan["source_attributions"][0]["text"])


if __name__ == "__main__":
    unittest.main()
