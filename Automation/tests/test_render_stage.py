from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from render_stage import (
    RenderSettings,
    _cleanup_paths,
    _compact_story_title,
    _compose_video_track,
    _mix_audio_track,
    _mux_master,
    _render_preview,
    _report_render_progress,
    _render_visual_segments,
    _write_story_title_ass,
    build_render_plan,
)
from motion_filters import smooth_push_in_filter
from tooling import find_executable


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
                    "source_url": "https://unsplash.com/photos/example-123",
                    "license_name": "Unsplash License",
                    "provided_text": "Photo by Example Creator on Unsplash",
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
    def test_render_progress_heartbeat_is_machine_readable(self) -> None:
        output = io.StringIO()
        with patch("sys.stdout", output):
            _report_render_progress(
                "visuals",
                "Rendered visual segments: 5/12.",
                completed=5,
                total=12,
            )

        prefix, encoded = output.getvalue().strip().split(" ", 1)
        self.assertEqual(prefix, "NEWS_STUDIO_RENDER_PROGRESS")
        payload = json.loads(encoded)
        self.assertEqual(payload["phase"], "visuals")
        self.assertEqual(payload["completed"], 5)
        self.assertEqual(payload["total"], 12)

    def test_real_ffmpeg_native_video_path_outputs_video_only_exact_interval(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
        if ffmpeg is None or ffprobe is None:
            self.skipTest("FFmpeg tools are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "1.mp4"
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", "-y", str(clip),
                ],
                check=True,
            )
            outputs = _render_visual_segments(
                root,
                {"visual_events": [{
                    "event_id": "real_ai_video",
                    "asset": {
                        "file": str(clip),
                        "kind": "video",
                        "sha256": "fixture",
                    },
                    "duration": 1.2,
                    "motion": {"type": "native_video", "maximum_scale": 1.0},
                }]},
                RenderSettings(width=320, height=180, fps=30),
            )
            probe = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,width,height", "-of", "json",
                    str(outputs[0]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(probe.stdout)

        self.assertEqual([item["codec_type"] for item in metadata["streams"]], ["video"])
        self.assertEqual(metadata["streams"][0]["width"], 320)
        self.assertEqual(metadata["streams"][0]["height"], 180)
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 1.2, delta=0.05)

    def test_native_video_is_trimmed_scaled_and_has_audio_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "1.mp4"
            clip.write_bytes(b"input-video")
            plan = {
                "visual_events": [{
                    "event_id": "ai_video_1",
                    "asset": {
                        "file": str(clip),
                        "kind": "video",
                        "sha256": "fixture",
                        "source_start_seconds": 2.5,
                    },
                    "duration": 7.0,
                    "motion": {"type": "native_video", "maximum_scale": 1.0},
                }]
            }

            def create_output(command: list[str], _label: str) -> None:
                Path(command[-1]).write_bytes(b"rendered")

            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run", side_effect=create_output) as run,
            ):
                _render_visual_segments(root, plan, RenderSettings(width=320, height=180, fps=30))

            command = run.call_args.args[0]
            video_filter = command[command.index("-vf") + 1]
            self.assertIn("trim=start=2.5:duration=7.0", video_filter)
            self.assertIn("setpts=PTS-STARTPTS", video_filter)
            self.assertIn("scale=320:180", video_filter)
            self.assertIn("crop=320:180", video_filter)
            self.assertIn("-an", command)

    def test_independent_visual_segments_render_in_parallel_and_keep_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            barrier = threading.Barrier(4)
            plan = {
                "visual_events": [
                    {
                        "event_id": f"visual_{index:04d}",
                        "asset": {"file": str(root / f"image_{index}.png")},
                        "duration": 1,
                        "motion": {"type": "static", "maximum_scale": 1.0},
                    }
                    for index in range(1, 5)
                ]
            }

            def create_output(command: list[str], _label: str) -> None:
                barrier.wait(timeout=2)
                Path(command[-1]).write_bytes(b"rendered")

            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run", side_effect=create_output),
            ):
                outputs = _render_visual_segments(
                    root,
                    plan,
                    RenderSettings(
                        width=320,
                        height=180,
                        fps=30,
                        segment_workers=4,
                    ),
                )

        self.assertEqual(
            [path.name for path in outputs],
            [
                "0001_visual_0001.mp4",
                "0002_visual_0002.mp4",
                "0003_visual_0003.mp4",
                "0004_visual_0004.mp4",
            ],
        )

    def test_brand_logo_segment_uses_alpha_overlay_on_neutral_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logo = root / "logo.png"
            logo.write_bytes(b"transparent-logo")
            plan = {
                "visual_events": [
                    {
                        "event_id": "logo_1",
                        "asset": {"file": str(logo)},
                        "duration": 2,
                        "brand_logo": True,
                        "motion": {"type": "static", "maximum_scale": 1.0},
                    }
                ]
            }

            def create_output(command: list[str], _label: str) -> None:
                Path(command[-1]).write_bytes(b"rendered-segment")

            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run", side_effect=create_output) as run,
            ):
                _render_visual_segments(
                    root,
                    plan,
                    RenderSettings(width=320, height=180, fps=30),
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("color=c=0xe9edf2:s=320x180:r=30", command)
            self.assertIn("format=rgba[logo]", filters)
            self.assertIn("overlay=(W-w)/2:(H-h)/2", filters)

    def test_cleanup_removes_only_render_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            segments = root / "08_render" / "intermediates" / "visuals_1080p"
            segments.mkdir(parents=True)
            (segments / "segment.mp4").write_bytes(b"temporary-video")
            final = root / "08_render" / "Video 5_final.mp4"
            final.write_bytes(b"final-video")

            result = _cleanup_paths(root, [segments], label="test")

            self.assertFalse(segments.exists())
            self.assertTrue(final.is_file())
            self.assertEqual(result["removed_files"], 1)
            self.assertEqual(result["removed_bytes"], len(b"temporary-video"))

    def test_cleanup_rejects_paths_outside_intermediate_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            protected = root / "08_render" / "Video 5_final.mp4"
            protected.parent.mkdir(parents=True)
            protected.write_bytes(b"final-video")

            with self.assertRaisesRegex(RuntimeError, "outside intermediate root"):
                _cleanup_paths(root, [protected], label="test")

            self.assertTrue(protected.is_file())

    def test_cleanup_lock_is_deferred_without_failing_a_passing_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            intermediates = root / "08_render" / "intermediates"
            intermediates.mkdir(parents=True)
            (intermediates / "still-locked.mp4").write_bytes(b"temporary-video")

            with (
                patch(
                    "render_stage.shutil.rmtree",
                    side_effect=PermissionError(32, "directory is in use"),
                ),
                patch("render_stage.time.sleep"),
            ):
                result = _cleanup_paths(root, [intermediates], label="test")

            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["deferred_paths"], [str(intermediates.resolve())])
            self.assertTrue(intermediates.is_dir())
            self.assertIn("PermissionError", result["errors"][0])

    def test_smooth_push_in_uses_floating_point_cubic_motion(self) -> None:
        value = smooth_push_in_filter(
            width=1920,
            height=1080,
            fps=30,
            duration=3.0,
            frames=90,
            maximum_scale=1.02,
        )

        self.assertIn("flags=lanczos", value)
        self.assertIn("perspective=", value)
        self.assertIn("eval=frame", value)
        self.assertIn("interpolation=cubic", value)
        self.assertIn("on/89", value)
        self.assertNotIn("zoompan", value)
        self.assertNotIn("floor(", value)

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

    def test_story_music_is_trimmed_delayed_and_faded_around_silence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "duration_seconds": 20,
                "narration": [
                    {"file": str(root / "voice.mp3"), "start": 0, "duration": 20}
                ],
                "separators": [],
                "background_music": {
                    "tracks": [
                        {
                            "track_file": str(root / "long.mp3"),
                            "timeline_start": 0,
                            "segment_duration_seconds": 9,
                            "fade_in_seconds": 1,
                            "fade_out_seconds": 1,
                        },
                        {
                            "track_file": str(root / "short.mp3"),
                            "timeline_start": 10,
                            "segment_duration_seconds": 10,
                            "fade_in_seconds": 1,
                            "fade_out_seconds": 1,
                        },
                    ],
                    "normalization": {"target_lufs": -20},
                    "ducking": {"speech_gain_factor": 0.063096},
                },
                "glitch_events": [],
                "glitch_sfx": {},
                "timeline_sfx": {"assets": {}, "events": []},
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _mix_audio_track(
                    root,
                    plan,
                    [],
                    RenderSettings(voiceover_gain_db=-6.0),
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertEqual(command.count("-stream_loop"), 2)
            self.assertIn("volume=0.501187,adelay=0|0[voice0]", filters)
            self.assertIn("atrim=duration=9.0", filters)
            self.assertIn("afade=t=out:st=8.0:d=1.0", filters)
            self.assertIn("adelay=10000|10000[music1]", filters)
            self.assertIn("[voice0][music0][music1]amix=inputs=3", filters)

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
            self.assertTrue(plan["page_flip"]["enabled"])
            self.assertEqual(len(plan["page_flip"]["events"]), 1)
            self.assertEqual(plan["page_flip"]["events"][0]["start"], 0)

    def test_page_flip_overlay_is_used_only_for_planned_chapter_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "duration_seconds": 4,
                "separators": [],
                "glitch_events": [],
                "image_tv_effect": {"enabled": False},
                "tv_frame": {},
                "page_flip": {
                    "enabled": True,
                    "events": [
                        {"start": 0, "end": 0.7, "duration": 0.7},
                        {"start": 2, "end": 2.7, "duration": 0.7},
                    ],
                },
                "story_titles": [],
                "source_attributions": [],
                "captions": {"file": str(root / "captions.srt")},
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch(
                    "render_stage._ensure_page_flip_overlay",
                    return_value=root / "page_flip.mov",
                ),
                patch("render_stage._run") as run,
            ):
                _compose_video_track(
                    root,
                    plan,
                    root / "base.mp4",
                    [],
                    RenderSettings(
                        width=320,
                        height=180,
                        video_filter_threads=3,
                    ),
                )

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertEqual(
                command[command.index("-filter_complex_threads") + 1],
                "3",
            )

        self.assertIn("page_flip.mov", " ".join(str(item) for item in command))
        self.assertIn("split=2[pageflipsource1][pageflipsource2]", filters)
        self.assertIn("setpts=PTS-STARTPTS+0.000000/TB[pageflip1]", filters)
        self.assertIn("setpts=PTS-STARTPTS+2.000000/TB[pageflip2]", filters)
        self.assertIn("repeatlast=0", filters)

    def test_master_uses_aac_coder_that_avoids_peak_overshoot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _mux_master(
                    root,
                    {"video_number": 5, "duration_seconds": 2},
                    root / "video.mp4",
                    root / "audio.wav",
                )

            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-aac_coder") + 1], "fast")

    def test_preview_copies_safe_master_audio_without_reencoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _render_preview(root / "final.mp4", root, RenderSettings())

            command = run.call_args.args[0]
            self.assertNotIn("-af", command)
            self.assertEqual(command[command.index("-c:a") + 1], "copy")

    def test_tv_overlays_skip_separator_video_and_ai_summary_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "duration_seconds": 5,
                "separators": [
                    {"start": 1, "end": 2, "duration": 1}
                ],
                "visual_events": [
                    {
                        "timeline_start": 2.2,
                        "timeline_end": 2.8,
                        "source_type": "ai_generated_story_video",
                        "asset": {"kind": "video"},
                    },
                    {
                        "timeline_start": 3.2,
                        "timeline_end": 3.8,
                        "source_type": "generated_separator_summary",
                        "asset": {"kind": "image"},
                    },
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
            self.assertIn(
                "enable='not(between(t\\,1.000000\\,2.000000)+"
                "between(t\\,2.200000\\,2.800000)+"
                "between(t\\,3.200000\\,3.800000))'[vframed]",
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
            self.assertIn("text_align=R", filters)
            self.assertIn("x=w-text_w-25:y=h-text_h-23", filters)
            self.assertIn("MarginV=24", filters)
            self.assertIn("between(t\\,0.250000\\,1.750000)", filters)

    def test_default_attribution_sits_at_bottom_safe_edge(self) -> None:
        settings = RenderSettings()

        self.assertEqual(settings.attribution_margin_right, 0)
        self.assertEqual(settings.attribution_margin_bottom, 8)

    def test_paper_title_is_anchored_at_upper_left(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = RenderSettings(
                width=320,
                height=180,
                title_margin_left=19,
                title_margin_top=11,
            )
            plan = {
                "duration_seconds": 2,
                "separators": [],
                "glitch_events": [],
                "tv_frame": {},
                "story_titles": [
                    {
                        "start": 0,
                        "end": 2,
                        "text_file": str(root / "title.txt"),
                    }
                ],
                "source_attributions": [],
                "captions": {"file": str(root / "captions.srt")},
            }
            with (
                patch("render_stage._ffmpeg", return_value=Path("ffmpeg")),
                patch("render_stage._run") as run,
            ):
                _compose_video_track(root, plan, root / "base.mp4", [], settings)

            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("story_titles_paper.ass", filters)
            self.assertNotIn("boxcolor=black@0.72", filters)
            title_ass = root / "08_render" / "titles" / "story_titles_paper.ass"
            ass_text = title_ass.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1600", ass_text)
            self.assertIn(r"\pos(36,62)", ass_text)
            self.assertIn(r"\pos(53,75)\fs29", ass_text)
            self.assertIn("“", ass_text)

    def test_story_title_compaction_preserves_long_titles_for_two_line_layout(self) -> None:
        compact = _compact_story_title(
            "Anthropic Warns Claude Sessions Are Being Hijacked by "
            "Infostealer Malware Across Multiple Systems",
            1,
        )

        self.assertNotIn("\n", compact)
        self.assertLessEqual(len(compact), 164)
        self.assertIn("Across Multiple Systems", compact)

    def test_long_story_title_uses_two_lines_compact_height_and_fixed_font(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "long_title.ass"
            report = _write_story_title_ass(
                [
                    {
                        "start": 0,
                        "end": 8,
                        "text": (
                            "Anthropic Warns Claude Sessions Are Being Hijacked by "
                            "Infostealer Malware Across Multiple Enterprise Systems"
                        ),
                    }
                ],
                output,
            )

            ass_text = output.read_text(encoding="utf-8")
            self.assertIn(r"\N", ass_text)
            self.assertIn(r"\fs29", ass_text)
            self.assertNotIn(r"\fs28", ass_text)
            self.assertEqual(report["paper_height"], 80)
            self.assertEqual(report["two_line_title_count"], 1)
            self.assertEqual(report["minimum_font_size"], 29)

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
            self.assertEqual(plan["tv_frame"], {"enabled": False})
            self.assertFalse(plan["page_flip"]["enabled"])
            self.assertEqual(plan["page_flip"]["events"], [])
            self.assertTrue(
                plan["quality"]["separator_and_narration_start_together"]
            )
            self.assertTrue(plan["quality"]["first_caption_starts_at_program_start"])
            self.assertTrue(plan["quality"]["no_directional_pans"])
            self.assertTrue(plan["quality"]["persistent_titles_cover_all_stories"])
            self.assertTrue(
                plan["quality"]["paper_title_overlay_covers_all_stories"]
            )
            self.assertEqual(len(plan["story_titles"]), 1)
            self.assertEqual(
                plan["story_title_overlay"]["style"],
                "upper_left_torn_paper_reference_v1",
            )
            self.assertEqual(plan["story_title_overlay"]["paper_height"], 50)
            self.assertTrue(Path(plan["story_title_overlay"]["file"]).is_file())
            self.assertEqual(len(plan["source_attributions"]), 1)
            self.assertIn("Source: Example Creator", plan["source_attributions"][0]["text"])

    def test_plan_accepts_forced_alignment_leading_silence_on_first_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            write_fixture(root)
            timing_path = root / "story_timing.json"
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            timing["stories"][0]["sentences"] = [
                {
                    "index": 0,
                    "start": 0.16,
                    "end": 2.0,
                    "timeline_start": 0.16,
                    "timeline_end": 2.0,
                }
            ]
            timing_path.write_text(json.dumps(timing), encoding="utf-8")
            captions_path = root / "06_captions" / "captions.json"
            captions = json.loads(captions_path.read_text(encoding="utf-8"))
            captions["quality"]["first_caption_start"] = 0.16
            captions_path.write_text(json.dumps(captions), encoding="utf-8")
            settings = RenderSettings(
                width=320,
                height=180,
                preview_width=320,
                preview_height=180,
            )
            with patch("render_stage.project_dir", return_value=root):
                plan = build_render_plan(5, settings)
            self.assertEqual(plan["quality"]["status"], "passed")
            self.assertTrue(plan["quality"]["first_caption_starts_at_program_start"])

    def test_plan_rejects_delayed_first_caption_and_reports_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 5"
            write_fixture(root)
            captions_path = root / "06_captions" / "captions.json"
            captions = json.loads(captions_path.read_text(encoding="utf-8"))
            captions["quality"]["first_caption_start"] = 3.0
            captions_path.write_text(json.dumps(captions), encoding="utf-8")
            settings = RenderSettings(
                width=320,
                height=180,
                preview_width=320,
                preview_height=180,
            )
            with patch("render_stage.project_dir", return_value=root):
                with self.assertRaises(RuntimeError) as context:
                    build_render_plan(5, settings)
            message = str(context.exception)
            self.assertIn("Milestone 8 plan quality failed", message)
            self.assertIn("'status': 'failed'", message)
            self.assertIn("'first_caption_starts_at_program_start': False", message)


if __name__ == "__main__":
    unittest.main()
