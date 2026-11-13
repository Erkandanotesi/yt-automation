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
    _music_plan,
    build_effects_plan,
    build_timeline_sfx,
    build_visual_events,
    render_reference_proof,
    select_music_tracks,
    validate_visual_cadence,
    validate_visual_coverage,
)
from file_locations import ASSETS_ROOT
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
                            "source_url": "https://unsplash.com/photos/test-123",
                            "creator": "Test Creator",
                            "license": {
                                "name": "Unsplash License",
                                "attribution_required": True,
                                "attribution_text": "Photo by Test Creator on Unsplash",
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
    def test_ai_story_video_is_marked_as_native_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "1.mp4"
            clip.write_bytes(b"validated clip")
            timing = {
                "stories": [{"story_id": "story_1", "order": 1, "narration": {"start": 0, "end": 5, "duration": 5}}]
            }
            media = {
                "stories": [{
                    "story_id": "story_1",
                    "order": 1,
                    "hero_ranges": [{
                        "image_index": 1,
                        "role": "ai_generated_story_video",
                        "asset_kind": "video",
                        "start": 0,
                        "end": 5,
                        "asset": str(clip),
                    }],
                    "windows": [],
                }]
            }
            events = build_visual_events(timing, media, fixture_settings(root))

        self.assertEqual(events[0]["source_type"], "ai_generated_story_video")
        self.assertEqual(events[0]["asset"]["kind"], "video")
        self.assertEqual(events[0]["motion"]["type"], "native_video")
        self.assertEqual(events[0]["motion"]["maximum_scale"], 1.0)

    def test_selected_stock_video_keeps_trim_offset_and_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "pexels.mp4"
            clip.write_bytes(b"validated stock clip")
            timing = {
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "narration": {"start": 10, "end": 15, "duration": 5},
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
                                "selected_asset": {
                                    "absolute_path": str(clip),
                                    "sha256": sha256_file(clip),
                                    "kind": "video",
                                    "source_start_seconds": 3.5,
                                    "source_url": "https://www.pexels.com/video/101/",
                                    "creator": "Example Creator",
                                    "license": {
                                        "name": "Pexels License",
                                        "attribution_required": True,
                                        "attribution_text": (
                                            "Video by Example Creator on Pexels"
                                        ),
                                    },
                                    "quality_flags": [],
                                },
                            }
                        ],
                    }
                ]
            }

            events = build_visual_events(timing, media, fixture_settings(root))

        self.assertEqual(events[0]["asset"]["kind"], "video")
        self.assertEqual(events[0]["asset"]["source_start_seconds"], 3.5)
        self.assertEqual(events[0]["motion"]["type"], "native_video")
        self.assertEqual(events[0]["timeline_start"], 10)
        self.assertTrue(events[0]["attribution"]["required"])

    def test_skipped_separator_summary_role_reaches_effects_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "summary.png"
            image.write_bytes(b"summary image")
            timing = {
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "narration": {"start": 0, "end": 5, "duration": 5},
                    }
                ]
            }
            media = {
                "stories": [
                    {
                        "story_id": "story_1",
                        "order": 1,
                        "hero_ranges": [
                            {
                                "image_index": 1,
                                "role": "skipped_separator_story_summary",
                                "start": 0,
                                "end": 5,
                                "asset": str(image),
                            }
                        ],
                        "windows": [],
                    }
                ]
            }

            events = build_visual_events(timing, media, fixture_settings(root))

        self.assertEqual(events[0]["source_type"], "generated_separator_summary")
        self.assertEqual(events[0]["timeline_start"], 0)

    def test_music_playlist_is_reproducibly_random_and_unique_per_story(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "music"
            music_dir.mkdir()
            for index in range(7):
                (music_dir / f"song_{index}.mp3").write_bytes(bytes([index + 1]))
            settings = replace(fixture_settings(root), music_directory=music_dir)

            first, first_seed = select_music_tracks(
                settings, story_count=5, video_number=16
            )
            second, second_seed = select_music_tracks(
                settings, story_count=5, video_number=16
            )

            self.assertEqual(first_seed, second_seed)
            self.assertEqual(
                [track.name for track, _ in first],
                [track.name for track, _ in second],
            )
            self.assertEqual(len({track for track, _ in first}), 5)

    def test_configured_first_music_is_always_assigned_to_story_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "müzik arşivi"
            music_dir.mkdir()
            forced = music_dir / "Tech_News_Glow 2.mp3"
            forced.write_bytes(b"forced lead music")
            for index in range(1, 7):
                (music_dir / f"song_{index}.mp3").write_bytes(bytes([index]))
            settings = replace(
                fixture_settings(root),
                music_directory=music_dir,
                first_music_file=forced,
            )

            first, first_seed = select_music_tracks(
                settings, story_count=5, video_number=27
            )
            second, second_seed = select_music_tracks(
                settings, story_count=5, video_number=27
            )

            self.assertEqual(first[0][0], forced.resolve())
            self.assertNotIn(forced.resolve(), [track for track, _ in first[1:]])
            self.assertEqual(
                [track for track, _ in first],
                [track for track, _ in second],
            )
            self.assertEqual(first_seed, second_seed)

    def test_missing_configured_first_music_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "music"
            music_dir.mkdir()
            (music_dir / "available.mp3").write_bytes(b"available")
            missing = root / "Türkçe klasör" / "missing.mp3"
            settings = replace(
                fixture_settings(root),
                music_directory=music_dir,
                first_music_file=missing,
            )

            with self.assertRaisesRegex(
                FileNotFoundError,
                "FIRST_BACKGROUND_MUSIC does not exist",
            ):
                select_music_tracks(settings, story_count=5, video_number=27)

    def test_music_plan_has_exact_silence_between_fade_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracks = []
            for index in range(1, 4):
                path = root / f"track_{index}.mp3"
                path.write_bytes(f"track {index}".encode("utf-8"))
                tracks.append((path, {"title": f"Track {index}"}))
            timing = {
                "duration_seconds": 30,
                "stories": [
                    {
                        "story_id": f"story_{index}",
                        "order": index,
                        "narration": {
                            "start": (index - 1) * 10,
                            "end": index * 10,
                        },
                    }
                    for index in range(1, 4)
                ],
            }
            settings = replace(
                fixture_settings(root),
                music_section_fade_seconds=2.0,
                music_section_silence_seconds=1.0,
            )
            with patch(
                "effects_stage.probe_media",
                return_value={"duration_seconds": 60},
            ):
                plan = _music_plan(tracks, "seed", timing, settings)

        self.assertEqual(plan["transition_mode"], "fade_out_then_silence_then_fade_in")
        self.assertEqual(plan["section_silence_seconds"], 1.0)
        self.assertEqual(
            [
                (track["timeline_start"], track["timeline_end"])
                for track in plan["tracks"]
            ],
            [(0.0, 9.0), (10.0, 19.0), (20.0, 30.0)],
        )
        self.assertEqual(
            [
                (item["timeline_start"], item["timeline_end"], item["duration_seconds"])
                for item in plan["silence_intervals"]
            ],
            [(9.0, 10.0, 1.0), (19.0, 20.0, 1.0)],
        )
        self.assertEqual(plan["tracks"][0]["fade_out_seconds"], 2.0)
        self.assertEqual(plan["tracks"][1]["fade_in_seconds"], 2.0)

    def test_first_music_environment_path_preserves_turkish_characters(self) -> None:
        configured = str(
            ASSETS_ROOT / "Audio" / "background_music" / "Tech_News_Glow 2.mp3"
        )
        with patch.dict(
            "effects_stage.os.environ",
            {"FIRST_BACKGROUND_MUSIC": configured},
        ):
            settings = EffectsSettings.from_environment()

        self.assertEqual(settings.first_music_file, Path(configured).resolve())

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
                                    "brand_logo": True,
                                },
                            }
                        ],
                    }
                ]
            }

            events = build_visual_events(timing, media, fixture_settings(root))

            self.assertEqual(events[0]["timeline_start"], 11)
            self.assertEqual(events[0]["timeline_end"], 16)
            self.assertTrue(events[0]["brand_logo"])
            self.assertEqual(events[0]["motion"]["type"], "static")

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
                ["smooth_push_in"] * 4,
            )
            self.assertTrue(
                all(
                    item["motion"]["renderer"] == "perspective_cubic_subpixel"
                    and item["motion"]["easing"] == "smoothstep"
                    for item in plan["visual_events"]
                )
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

    def test_visual_cadence_rejects_long_and_adjacent_identical_holds(self) -> None:
        events = [
            {
                "event_id": "one",
                "story_id": "story_1",
                "source_type": "archive",
                "timeline_start": 0,
                "timeline_end": 10,
                "asset": {"file": "long.png", "sha256": "a" * 64},
            },
            {
                "event_id": "two",
                "story_id": "story_2",
                "source_type": "archive",
                "timeline_start": 20,
                "timeline_end": 25,
                "asset": {"file": "repeat.png", "sha256": "b" * 64},
            },
            {
                "event_id": "three",
                "story_id": "story_2",
                "source_type": "generated_hero",
                "timeline_start": 25,
                "timeline_end": 30,
                "asset": {"file": "repeat-copy.png", "sha256": "b" * 64},
            },
        ]

        result = validate_visual_cadence(events, 9)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["long_events"][0]["event_id"], "one")
        self.assertEqual(result["repeated_asset_runs"][0]["duration"], 10)

    def test_visual_cadence_accepts_small_alignment_rounding_overshoot(self) -> None:
        result = validate_visual_cadence(
            [
                {
                    "event_id": "rounded",
                    "story_id": "story_1",
                    "source_type": "archive",
                    "timeline_start": 0,
                    "timeline_end": 9.067,
                    "asset": {"file": "image.png", "sha256": "a" * 64},
                }
            ],
            9,
        )

        self.assertEqual(result["status"], "passed")

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
            native_video = root / "1.mp4"
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=640x360:r=30:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(native_video),
                ],
                check=True,
            )
            images[0] = native_video
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
            captions = root / "captions.ass"
            captions.write_text(
                "\n".join(
                    [
                        "[Script Info]",
                        "ScriptType: v4.00+",
                        "PlayResX: 320",
                        "PlayResY: 180",
                        "",
                        "[V4+ Styles]",
                        (
                            "Format: Name, Fontname, Fontsize, PrimaryColour, "
                            "SecondaryColour, OutlineColour, BackColour, Bold, "
                            "Italic, Underline, StrikeOut, ScaleX, ScaleY, "
                            "Spacing, Angle, BorderStyle, Outline, Shadow, "
                            "Alignment, MarginL, MarginR, MarginV, Encoding"
                        ),
                        (
                            "Style: Default,Arial,18,&H00FFFFFF,&H0000D7FF,"
                            "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,"
                            "1,2,0,2,10,10,10,1"
                        ),
                        "",
                        "[Events]",
                        (
                            "Format: Layer, Start, End, Style, Name, MarginL, "
                            "MarginR, MarginV, Effect, Text"
                        ),
                        (
                            "Dialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,"
                            "0,,{\\c&H0000D7FF&}Yellow{\\c&H00FFFFFF&} proof"
                        ),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            title_text = root / "title.txt"
            title_text.write_text("TEST STORY TITLE", encoding="utf-8")
            attribution_text = root / "attribution.txt"
            attribution_text.write_text(
                "Source: Example Creator · Pexels · Pexels License",
                encoding="utf-8",
            )
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
                image_durations=[2, 2, 2],
                visual_kinds=["video", "image", "image"],
                visual_source_types=[
                    "ai_generated_story_video",
                    "archive",
                    "generated_separator_summary",
                ],
                story_titles=[
                    {
                        "start": 0,
                        "end": 6,
                        "text": "TEST STORY TITLE",
                        "text_file": str(title_text),
                    }
                ],
                source_attributions=[
                    {
                        "start": 0,
                        "end": 2,
                        "text": "Source: Example Creator · Pexels · Pexels License",
                        "text_file": str(attribution_text),
                    }
                ],
                include_separator=False,
                captions_file=captions,
            )
            self.assertEqual(report["quality"]["status"], "passed")
            self.assertTrue(report["uses_actual_visual_timeline"])
            self.assertEqual(report["image_durations"], [2, 2, 2])
            self.assertEqual(report["visual_kinds"], ["video", "image", "image"])
            self.assertEqual(
                report["tv_frame_excluded_ranges"],
                [],
            )
            self.assertNotIn("tv_frame_overlay", report["verified_features"])
            self.assertTrue(report["renderer_sha256"])
            self.assertEqual(Path(report["captions_file"]).suffix, ".ass")
            self.assertEqual(report["captions_source_file"], str(captions))
            self.assertIn(
                "active_word_caption_highlighting",
                report["verified_features"],
            )
            self.assertIn("story_title_overlays", report["verified_features"])
            self.assertTrue(Path(report["story_title_overlay_file"]).is_file())
            self.assertIn(
                "upper-left torn-paper story titles",
                Path(report["story_title_overlay_file"]).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "source_attribution_watermarks",
                report["verified_features"],
            )
            self.assertIn(
                "&H0000D7FF",
                Path(report["captions_file"]).read_text(encoding="utf-8"),
            )
            self.assertTrue((root / "proof.mp4").is_file())
            self.assertAlmostEqual(report["duration_seconds"], 6, delta=0.15)


if __name__ == "__main__":
    unittest.main()
