from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from effects_stage import (
    EffectsSettings,
    build_effects_plan,
    build_timeline_sfx,
    build_visual_events,
    render_reference_proof,
    validate_visual_coverage,
)
from file_utils import sha256_file
from tooling import find_executable


def fixture_settings(root: Path) -> EffectsSettings:
    return EffectsSettings(
        width=320,
        height=180,
        fps=30,
        motion_scale=1.012,
        hero_motion_scale=1.008,
        tv_frame_file=root / "tv_frame.png",
        image_tv_effect_file=root / "green-screen-tv-effect.mov",
        paper_sfx_file=root / "paper_sound.MP3",
        shutter_sfx_file=root / "shutter_sound.MP3",
        music_directory=root / "music",
        proof_width=320,
        proof_height=180,
        editing_reference_file=root / "capcut.png",
    )


def write_plan_fixture(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "05_visuals").mkdir()
    (root / "06_captions").mkdir()
    assets = []
    for name in ("hero1.png", "archive1.png", "hero2.png", "archive2.png"):
        path = root / name
        path.write_bytes(name.encode("utf-8"))
        assets.append(path)
    separator = root / "separator.mp4"
    narration = root / "voice.mp3"
    overlay = root / "tv_frame.png"
    image_tv_effect = root / "green-screen-tv-effect.mov"
    reference = root / "capcut.png"
    music_directory = root / "music"
    music_directory.mkdir()
    music = music_directory / "bed.wav"
    sfx = root / "glitch.wav"
    paper = root / "paper_sound.MP3"
    shutter = root / "shutter_sound.MP3"
    for path in (
        separator,
        narration,
        overlay,
        image_tv_effect,
        reference,
        music,
        sfx,
        paper,
        shutter,
    ):
        path.write_bytes(path.name.encode("utf-8"))
    captions = root / "06_captions" / "captions.srt"
    captions.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\nTest caption\n",
        encoding="utf-8",
    )
    (root / "state.json").write_text(
        json.dumps({"stages": {"manual_separators": {"status": "READY"}}}),
        encoding="utf-8",
    )
    timing = {
        "duration_seconds": 20,
        "quality": {"status": "passed", "manual_separators_validated": True},
        "stories": [
            {
                "story_id": "story_1",
                "order": 1,
                "audio_file": str(narration),
                "separator": {
                    "file": str(separator),
                    "start": 0,
                    "end": 2,
                    "duration": 2,
                },
                "narration": {"start": 2, "end": 20, "duration": 18},
            }
        ],
    }
    (root / "story_timing.json").write_text(json.dumps(timing), encoding="utf-8")
    media = {
        "quality": {"status": "passed"},
        "stories": [
            {
                "story_id": "story_1",
                "order": 1,
                "hero_ranges": [
                    {"image_index": 1, "start": 0, "end": 4, "asset": str(assets[0])},
                    {"image_index": 2, "start": 10, "end": 14, "asset": str(assets[2])},
                ],
                "windows": [
                    {
                        "window_id": "window_1",
                        "start": 4,
                        "end": 10,
                        "selected_asset": {
                            "absolute_path": str(assets[1]),
                            "sha256": sha256_file(assets[1]),
                            "source_url": "https://commons.wikimedia.org/wiki/File:Test",
                            "creator": "Test Creator",
                            "license": {
                                "name": "CC BY-SA 4.0",
                                "attribution_required": True,
                                "attribution_text": "Test Creator; CC BY-SA 4.0",
                            },
                            "quality_flags": [],
                        },
                    },
                    {
                        "window_id": "window_2",
                        "start": 14,
                        "end": 18,
                        "selected_asset": {
                            "absolute_path": str(assets[3]),
                            "sha256": sha256_file(assets[3]),
                            "quality_flags": [],
                        },
                    },
                ],
            }
        ],
    }
    (root / "05_visuals" / "media_plan.json").write_text(
        json.dumps(media), encoding="utf-8"
    )
    (root / "06_captions" / "captions.json").write_text(
        json.dumps(
            {
                "combined_srt_file": str(captions),
                "combined_srt_sha256": sha256_file(captions),
                "quality": {"status": "passed"},
            }
        ),
        encoding="utf-8",
    )
    return {"music": music, "sfx": sfx, "paper": paper, "shutter": shutter}


class EffectsStageTests(unittest.TestCase):
    def test_shutter_counts_images_only_and_moves_after_separator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = fixture_settings(root)
            settings.paper_sfx_file.write_bytes(b"paper")
            settings.shutter_sfx_file.write_bytes(b"shutter")
            timing = {
                "quality": {"manual_separators_skipped": False},
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "narration": {"start": 0},
                        "separator": {"start": 0, "end": 2},
                    }
                ],
            }
            visuals = [
                {
                    "event_id": f"visual_{index}",
                    "story_id": "story_1",
                    "story_order": 1,
                    "source_type": "hero",
                    "timeline_start": 0,
                    "timeline_end": 5,
                }
                for index in range(1, 4)
            ]
            visuals.append(
                {
                    "event_id": "hold",
                    "story_id": "story_1",
                    "story_order": 1,
                    "source_type": "inter_story_hold",
                    "timeline_start": 5,
                    "timeline_end": 6,
                }
            )
            with patch(
                "effects_stage.probe_media",
                return_value={
                    "duration_seconds": 1,
                    "streams": [{"codec_type": "audio"}],
                },
            ):
                plan = build_timeline_sfx(timing, visuals, settings)

            shutter = next(
                event for event in plan["events"] if event["sound"] == "shutter"
            )
            self.assertEqual(plan["image_event_count"], 3)
            self.assertEqual(shutter["visual_sequence_number"], 3)
            self.assertEqual(shutter["start"], 2)

    def test_visual_global_times_follow_current_story_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "archive.png"
            image.write_bytes(b"archive")
            timing = {
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "narration": {"start": 11, "end": 16, "duration": 5},
                    }
                ]
            }
            media = {
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "hero_ranges": [],
                        "windows": [
                            {
                                "window_id": "window_1",
                                "start": 0,
                                "end": 5,
                                "timeline_start": 99,
                                "timeline_end": 104,
                                "selected_asset": {
                                    "absolute_path": str(image),
                                    "sha256": sha256_file(image),
                                    "quality_flags": [],
                                },
                            }
                        ],
                    }
                ]
            }

            events = build_visual_events(timing, media, fixture_settings(root))

            self.assertEqual(events[0]["timeline_start"], 11)
            self.assertEqual(events[0]["timeline_end"], 16)

    def test_plan_covers_motion_frame_glitch_sfx_and_ducked_music(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            generated = write_plan_fixture(root)
            settings = fixture_settings(root)
            with (
                patch("effects_stage.project_dir", return_value=root),
                patch(
                    "effects_stage.ensure_default_audio_assets",
                    return_value=generated,
                ),
                patch(
                    "effects_stage.select_music_track",
                    return_value=(
                        generated["music"],
                        {
                            "title": "Test bed",
                            "license": "test",
                            "seamless_loop": False,
                        },
                    ),
                ),
                patch(
                    "effects_stage.probe_media",
                    side_effect=lambda path: {
                        "duration_seconds": 1.0 if Path(path).suffix.lower() == ".mp3" else 7,
                        "streams": (
                            [{"codec_type": "audio", "codec_name": "mp3"}]
                            if Path(path).suffix.lower() == ".mp3"
                            else [
                                {
                                    "codec_type": "video",
                                    "codec_name": "qtrle",
                                    "pix_fmt": "argb",
                                }
                            ]
                        ),
                    },
                ),
            ):
                plan = build_effects_plan(video_number=5, settings=settings)
                cached = build_effects_plan(video_number=5, settings=settings)

            self.assertEqual(plan["quality"]["status"], "passed")
            self.assertEqual(len(plan["visual_events"]), 4)
            self.assertEqual(
                [item["motion"]["type"] for item in plan["visual_events"]],
                ["static"] * 4,
            )
            self.assertTrue(plan["quality"]["no_directional_pans"])
            self.assertTrue(plan["quality"]["glitch_under_three_percent"])
            self.assertTrue(plan["quality"]["music_covers_complete_timeline"])
            self.assertEqual(
                len(plan["audio"]["background_music"]["ducking"]["speech_windows"]),
                1,
            )
            timeline_sfx = plan["audio"]["timeline_sfx"]
            self.assertEqual(
                len([event for event in timeline_sfx["events"] if event["sound"] == "paper"]),
                1,
            )
            shutter_events = [
                event for event in timeline_sfx["events"] if event["sound"] == "shutter"
            ]
            self.assertEqual(len(shutter_events), 1)
            self.assertEqual(shutter_events[0]["visual_sequence_number"], 3)
            self.assertTrue(plan["quality"]["shutter_sfx_excludes_separator_intervals"])
            self.assertEqual(
                plan["ffmpeg_templates"]["layer_order"],
                [
                    "base_visual",
                    "static_center_crop",
                    "tape_dropout_glitch_accents",
                    "animated_tv_effect_images_only",
                    "manual_separator",
                    "tv_frame",
                    "persistent_story_title",
                    "source_attribution_bottom_right",
                    "captions",
                ],
            )
            self.assertTrue(plan["image_tv_effect"]["native_alpha"])
            self.assertEqual(
                plan["image_tv_effect"]["layer_position"],
                "above_glitch_below_manual_separator",
            )
            self.assertTrue(plan["quality"]["image_tv_effect_excludes_separators"])
            self.assertEqual(plan["quality"]["attribution_event_count"], 1)
            self.assertEqual(
                sum(item.get("attribution") is not None for item in plan["visual_events"]),
                1,
            )
            self.assertEqual(cached["run_status"], "cached")
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stages"]["effects"]["status"], "EFFECTS_READY")

    def test_visual_overlap_is_rejected(self) -> None:
        timing = {
            "stories": [
                {
                    "story_id": "story_1",
                    "narration": {"duration": 10},
                }
            ]
        }
        events = [
            {"story_id": "story_1", "local_start": 0, "local_end": 6},
            {"story_id": "story_1", "local_start": 5, "local_end": 10},
        ]
        result = validate_visual_coverage(events, timing)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["stories"][0]["overlaps"])

    def test_real_ffmpeg_reference_proof_contains_video_and_audio(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            raise unittest.SkipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for index, color in enumerate(("red", "green", "blue"), start=1):
                image = root / f"image{index}.png"
                subprocess.run(
                    [
                        str(ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"color=c={color}:s=320x180",
                        "-frames:v", "1", "-y", str(image),
                    ],
                    check=True,
                )
                images.append(image)
            overlay = root / "tv_frame.png"
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black@0.0:s=320x180,format=rgba",
                    "-frames:v", "1", "-y", str(overlay),
                ],
                check=True,
            )
            audio_files = {}
            for name, source, duration in (
                ("voice", "sine=frequency=440", 6),
                ("music", "sine=frequency=110", 2),
                ("sfx", "anoisesrc=color=pink:amplitude=0.1", 0.16),
            ):
                path = root / f"{name}.wav"
                subprocess.run(
                    [
                        str(ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"{source}:duration={duration}:sample_rate=48000",
                        "-c:a", "pcm_s16le", "-y", str(path),
                    ],
                    check=True,
                )
                audio_files[name] = path
            settings = replace(
                fixture_settings(root),
                tv_frame_file=overlay,
                image_tv_effect_enabled=False,
                paper_sfx_enabled=False,
                shutter_sfx_enabled=False,
            )
            report = render_reference_proof(
                output_path=root / "proof.mp4",
                images=images,
                narration_file=audio_files["voice"],
                music_file=audio_files["music"],
                sfx_file=audio_files["sfx"],
                settings=settings,
                duration_seconds=6,
            )
            self.assertEqual(report["quality"]["status"], "passed")
            self.assertTrue((root / "proof.mp4").is_file())
            self.assertAlmostEqual(report["duration_seconds"], 6, delta=0.15)


if __name__ == "__main__":
    unittest.main()
