from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from file_utils import sha256_file
from news_studio import (
    FLOW_STAGES,
    NetworkStageError,
    ProgressDisplay,
    ProjectLock,
    _run_subprocess,
    _storyboard_complete,
    _tts_complete,
    _is_network_failure,
    _captions_complete,
    _preview_status,
    _render_recovery_action,
    _saved_checkpoint_modes,
    approve_preview,
    api_cost_plan,
    execute_stage,
    interactive,
    main,
    resume_section_choices,
    review_youtube_title,
    run_preflight,
    run_flow,
    timed_choice,
)


def empty_status(root: Path) -> dict:
    return {
        "exists": False,
        "stages": {
            stage: {"status": "NOT_STARTED", "detail": ""}
            for stage in FLOW_STAGES
        },
    }


class NewsStudioTests(unittest.TestCase):
    def test_zero_story_images_still_reports_one_thumbnail_request(self) -> None:
        with patch.dict(
            "news_studio.os.environ",
            {
                "AI_GENERATED_IMAGES_PER_STORY": "0",
                "MAX_IMAGE_REQUESTS_PER_RUN": "0",
                "TOP_N": "5",
            },
        ):
            plan = api_cost_plan(None, ["storyboard", "publish"])

        self.assertEqual(plan["story_image_requests_remaining"], 0)
        self.assertEqual(plan["thumbnail_image_requests"], 1)

    def test_zero_story_image_mode_still_generates_dedicated_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 27"
            root.mkdir()
            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio._run_subprocess") as run,
                patch.dict(
                    "news_studio.os.environ",
                    {"AI_GENERATED_IMAGES_PER_STORY": "0"},
                ),
            ):
                result = execute_stage(
                    27,
                    "publish",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                )

            self.assertEqual(result.status, "COMPLETE")
            command = run.call_args.args[2]
            self.assertIn("--confirm-ai-cost", command)
            self.assertIn("--generate-thumbnail", command)
            self.assertIn("--confirm-thumbnail-cost", command)

    def test_thumbnail_request_can_be_disabled_separately(self) -> None:
        with patch.dict(
            "news_studio.os.environ",
            {
                "AI_GENERATED_IMAGES_PER_STORY": "0",
                "AI_GENERATED_THUMBNAIL_ENABLED": "false",
                "MAX_IMAGE_REQUESTS_PER_RUN": "0",
                "TOP_N": "5",
            },
        ):
            plan = api_cost_plan(None, ["publish"])

        self.assertEqual(plan["thumbnail_image_requests"], 0)

    def test_zero_image_storyboard_is_complete_without_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project_info.json").write_text(
                json.dumps({"stories": [{"story_id": "story_1"}]}),
                encoding="utf-8",
            )
            (root / "story_timing.json").write_text(
                json.dumps(
                    {
                        "quality": {"manual_separators_skipped": True},
                        "stories": [{"story_id": "story_1"}],
                    }
                ),
                encoding="utf-8",
            )
            storyboard_dir = root / "04_storyboard"
            storyboard_dir.mkdir()
            (storyboard_dir / "storyboard.json").write_text(
                json.dumps(
                    {
                        "configuration": {"images_per_story": 0},
                        "quality": {
                            "status": "passed",
                            "images_per_story": 0,
                            "ai_image_generation_disabled": True,
                            "all_images_generated_and_validated": True,
                        },
                        "stories": [{"story_id": "story_1", "images": []}],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "news_studio.os.environ",
                {"AI_GENERATED_IMAGES_PER_STORY": "0"},
            ):
                self.assertTrue(_storyboard_complete(root))

    def test_zero_image_mode_auto_skips_optional_ai_story_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 27"
            root.mkdir()
            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio._run_subprocess") as run,
                patch.dict(
                    "news_studio.os.environ",
                    {"AI_GENERATED_IMAGES_PER_STORY": "0"},
                ),
            ):
                result = execute_stage(
                    27,
                    "ai_story_videos",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    ai_video_mode="defer",
                )

            self.assertEqual(result.status, "COMPLETE")
            self.assertEqual(run.call_args.args[2][-1], "skip")

    def test_saved_checkpoint_modes_restore_manual_resume_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 33"
            ai_directory = root / "04_ai_story_videos"
            ai_directory.mkdir(parents=True)
            (root / "state.json").write_text(
                json.dumps(
                    {
                        "stages": {
                            "manual_separators": {"status": "CANCELLED"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (ai_directory / "ai_video_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "WAITING",
                        "decision": "use_ai_story_videos",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_saved_checkpoint_modes(root), ("manual", "manual"))

            (root / "state.json").write_text(
                json.dumps(
                    {
                        "stages": {
                            "manual_separators": {"status": "SKIPPED"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (ai_directory / "ai_video_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "SKIPPED",
                        "decision": "use_storyboard_images",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_saved_checkpoint_modes(root), ("defer", "defer"))

    def test_api_cost_plan_only_lists_selected_resume_stages(self) -> None:
        plan = api_cost_plan(None, ["tts", "alignment"])
        self.assertEqual(
            set(plan),
            {"elevenlabs_tts_stories", "forced_alignment_requests"},
        )

    def test_skipped_separator_rejects_cached_ordinary_storyboard_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "hero.png"
            image.write_bytes(b"generated image")
            (root / "project_info.json").write_text(
                json.dumps({"stories": [{"story_id": "story_1"}]}),
                encoding="utf-8",
            )
            (root / "story_timing.json").write_text(
                json.dumps(
                    {
                        "quality": {"manual_separators_skipped": True},
                        "stories": [{"story_id": "story_1"}],
                    }
                ),
                encoding="utf-8",
            )
            storyboard_dir = root / "04_storyboard"
            storyboard_dir.mkdir()
            manifest = {
                "quality": {
                    "status": "passed",
                    "all_images_generated_and_validated": True,
                },
                "stories": [
                    {
                        "story_id": "story_1",
                        "images": [
                            {
                                "role": "timed_story_hero",
                                "output_file": str(image),
                                "validation": {"status": "passed"},
                                "generation": {"file_sha256": sha256_file(image)},
                            }
                        ],
                    }
                ],
            }
            manifest_path = storyboard_dir / "storyboard.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertFalse(_storyboard_complete(root))

            manifest["stories"][0]["images"][0]["role"] = (
                "skipped_separator_story_summary"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(_storyboard_complete(root))

    def test_caption_completion_rejects_stale_global_timing(self) -> None:
        with (
            patch(
                "caption_stage.caption_status",
                return_value={"status": "STALE", "current": False},
            ),
        ):
            self.assertFalse(
                _captions_complete(
                    Path("Video 18"),
                    {"captions": {"status": "CAPTIONS_READY"}},
                )
            )

    def test_resume_menu_stops_at_first_unfinished_stage(self) -> None:
        status = {
            "next_stage": "preview_approval",
            "stages": {
                stage: {
                    "status": (
                        "COMPLETE"
                        if FLOW_STAGES.index(stage) < FLOW_STAGES.index("preview_approval")
                        else "WAITING"
                        if stage == "preview_approval"
                        else "NOT_STARTED"
                    )
                }
                for stage in FLOW_STAGES
            },
        }

        options, targets, default = resume_section_choices(status)

        self.assertEqual(default, "9")
        self.assertEqual(targets["9"], "preview_approval")
        self.assertNotIn("a", options)
        self.assertIn("recommended next", options["9"])

    def test_completed_project_resume_menu_defaults_to_youtube_upload(self) -> None:
        status = {
            "next_stage": None,
            "stages": {
                stage: {"status": "COMPLETE"} for stage in FLOW_STAGES
            },
        }

        options, targets, default = resume_section_choices(status)

        self.assertEqual(default, "u")
        self.assertEqual(targets["b"], "publish")
        self.assertEqual(targets["u"], "upload")
        self.assertIn("YouTube", options["u"])

    def test_dry_run_can_force_rebuild_from_completed_section(self) -> None:
        status = {
            "exists": True,
            "next_stage": None,
            "stages": {
                stage: {"status": "COMPLETE", "detail": ""}
                for stage in FLOW_STAGES
            },
        }
        with patch("news_studio.collect_project_status", return_value=status):
            result = run_flow(
                17,
                category="tech",
                confirm_api_costs=False,
                approve_current_preview=False,
                recover_stale_lock=False,
                dry_run=True,
                start_stage="effects",
            )

        self.assertEqual(
            result["planned_stages"],
            list(FLOW_STAGES[FLOW_STAGES.index("effects"):]),
        )

    def test_rebuild_from_stage_survives_preview_wait_and_forces_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            complete_status = {
                "exists": True,
                "complete": True,
                "stages": {
                    stage: {"status": "COMPLETE", "detail": ""}
                    for stage in FLOW_STAGES
                },
            }

            def stage_result(*args, **kwargs):
                stage = args[1]
                if stage == "preview_approval" and not kwargs["approve_current_preview"]:
                    return SimpleNamespace(status="WAITING", detail="Review proof")
                return SimpleNamespace(status="COMPLETE", detail="done")

            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio.collect_project_status", return_value=complete_status),
                patch("news_studio.execute_stage", side_effect=stage_result) as execute,
                patch("news_studio.NewsArchive"),
            ):
                first = run_flow(
                    17,
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    recover_stale_lock=False,
                    dry_run=False,
                    start_stage="effects",
                )
                plan_after_wait = json.loads(
                    (root / "orchestration" / "rebuild_plan.json").read_text(
                        encoding="utf-8"
                    )
                )
                second = run_flow(
                    17,
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=True,
                    recover_stale_lock=False,
                    dry_run=False,
                )

        self.assertEqual(first["status"], "WAITING")
        self.assertEqual(
            plan_after_wait["pending_stages"],
            ["preview_approval", "render", "publish"],
        )
        self.assertEqual(plan_after_wait["prepared_stages"], ["preview_approval"])
        self.assertEqual(second["status"], "COMPLETE")
        forced_calls = [
            (call.args[1], call.kwargs["force_rebuild"])
            for call in execute.call_args_list
        ]
        self.assertEqual(
            forced_calls,
            [
                ("effects", True),
                ("preview_approval", True),
                ("preview_approval", False),
                ("render", True),
                ("publish", True),
            ],
        )

    def test_resume_rejects_skipping_an_incomplete_prerequisite(self) -> None:
        status = empty_status(Path("Video 17"))
        status["exists"] = True
        status["stages"]["news"]["status"] = "COMPLETE"

        with (
            patch("news_studio.collect_project_status", return_value=status),
            self.assertRaisesRegex(RuntimeError, "complete these earlier sections"),
        ):
            run_flow(
                17,
                category="tech",
                confirm_api_costs=True,
                approve_current_preview=False,
                recover_stale_lock=False,
                dry_run=True,
                start_stage="render",
            )

    def test_interactive_completed_resume_can_go_directly_to_upload_prompt(self) -> None:
        status = {
            "next_stage": None,
            "stages": {
                stage: {"status": "COMPLETE"} for stage in FLOW_STAGES
            },
        }
        with (
            patch("news_studio.run_preflight", return_value=True),
            patch("news_studio.latest_project_number", return_value=17),
            patch("news_studio._read_json", return_value={"category": "tech"}),
            patch("news_studio.collect_project_status", return_value=status),
            patch("news_studio.timed_choice", side_effect=["2", "u", "0"]),
            patch("news_studio.run_flow") as run,
            patch("news_studio.upload_project") as upload,
            patch("news_studio._event") as event,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 0)
        run.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(event.call_args.args[1]["decision"], "do_not_upload")

    def test_interactive_recommended_resume_rechecks_files_without_rebuild(self) -> None:
        status = {
            "next_stage": "preview_approval",
            "stages": {
                stage: {
                    "status": (
                        "COMPLETE"
                        if FLOW_STAGES.index(stage) < FLOW_STAGES.index("preview_approval")
                        else "WAITING"
                        if stage == "preview_approval"
                        else "NOT_STARTED"
                    )
                }
                for stage in FLOW_STAGES
            },
        }
        with (
            patch("news_studio.run_preflight", return_value=True),
            patch("news_studio.latest_project_number", return_value=17),
            patch("news_studio._read_json", return_value={"category": "tech"}),
            patch("news_studio.collect_project_status", return_value=status),
            patch("news_studio.api_cost_plan", return_value={}),
            patch("news_studio.timed_choice", side_effect=["2", "9", "0"]),
            patch(
                "news_studio.run_flow",
                return_value={
                    "status": "WAITING",
                    "video_number": 17,
                    "stage": "preview_approval",
                },
            ) as run,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 0)
        self.assertIsNone(run.call_args.kwargs["start_stage"])

    def test_interactive_earlier_section_selection_still_forces_rebuild(self) -> None:
        status = {
            "next_stage": "preview_approval",
            "stages": {
                stage: {
                    "status": (
                        "COMPLETE"
                        if FLOW_STAGES.index(stage) < FLOW_STAGES.index("preview_approval")
                        else "WAITING"
                        if stage == "preview_approval"
                        else "NOT_STARTED"
                    )
                }
                for stage in FLOW_STAGES
            },
        }
        with (
            patch("news_studio.run_preflight", return_value=True),
            patch("news_studio.latest_project_number", return_value=17),
            patch("news_studio._read_json", return_value={"category": "tech"}),
            patch("news_studio.collect_project_status", return_value=status),
            patch("news_studio.api_cost_plan", return_value={}),
            patch("news_studio.timed_choice", side_effect=["2", "8"]),
            patch(
                "news_studio.run_flow",
                return_value={"status": "INCOMPLETE", "video_number": 17},
            ) as run,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 1)
        self.assertEqual(run.call_args.kwargs["start_stage"], "effects")

    def test_render_recovery_reuses_passing_master_for_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            render = root / "08_render"
            render.mkdir(parents=True)
            final = render / "Video 17_final.mp4"
            final.write_bytes(b"passing master")
            (render / "render_report.json").write_text(
                json.dumps(
                    {
                        "final": {
                            "file": str(final),
                            "qa": {
                                "status": "passed",
                                "sha256": sha256_file(final),
                            },
                        },
                        "preview": {"qa": {"status": "failed"}},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_render_recovery_action(root), "preview")
            final.write_bytes(b"changed master")
            self.assertEqual(_render_recovery_action(root), "render")

    def test_render_recovery_finalizes_when_both_outputs_already_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            render = root / "08_render"
            render.mkdir(parents=True)
            final = render / "Video 17_final.mp4"
            preview = render / "Video 17_preview.mp4"
            final.write_bytes(b"passing master")
            preview.write_bytes(b"passing preview")
            (render / "render_report.json").write_text(
                json.dumps(
                    {
                        "quality": {"status": "passed"},
                        "final": {
                            "file": str(final),
                            "qa": {"status": "passed", "sha256": sha256_file(final)},
                        },
                        "preview": {
                            "file": str(preview),
                            "qa": {"status": "passed", "sha256": sha256_file(preview)},
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_render_recovery_action(root), "finalize")

    def test_forced_preview_rebuild_does_not_reuse_current_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "news_studio._preview_status",
                    return_value=("COMPLETE", "Current proof"),
                ),
                patch("news_studio._run_subprocess") as run,
            ):
                result = execute_stage(
                    17,
                    "preview_approval",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    force_rebuild=True,
                )

        self.assertEqual(result.status, "WAITING")
        self.assertEqual(run.call_args.args[1], "preview")
        self.assertEqual(run.call_args.args[2][-2:], ["--proof-seconds", "60"])

    def test_forced_render_ignores_preview_only_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            with (
                patch("news_studio.project_dir", return_value=root),
                patch.dict("os.environ", {"REQUIRE_PREVIEW_APPROVAL": "false"}),
                patch("news_studio._render_recovery_action", return_value="preview"),
                patch("news_studio._run_subprocess") as run,
            ):
                result = execute_stage(
                    17,
                    "render",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    force_rebuild=True,
                )

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(run.call_args.args[2][-1], "render")

    def test_changed_story_invalidates_tts_and_preview_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 17"
            news = root / "Haberler"
            audio = root / "Sesler"
            news.mkdir(parents=True)
            audio.mkdir()
            current_text = news / "01_story_new.txt"
            current_text.write_text("replacement", encoding="utf-8")
            old_audio = audio / "01_story_old.mp3"
            old_audio.write_bytes(b"old audio")
            (root / "project_info.json").write_text(
                json.dumps({
                    "stories": [{
                        "story_id": "story_new",
                        "text": str(current_text),
                        "narration_sha256": sha256_file(current_text),
                    }]
                }),
                encoding="utf-8",
            )
            (audio / "tts_info.json").write_text(
                json.dumps({
                    "files": [{
                        "text_file": str(news / "01_story_old.txt"),
                        "text_sha256": "old",
                        "audio_file": str(old_audio),
                        "audio_sha256": sha256_file(old_audio),
                        "status": "cached",
                    }]
                }),
                encoding="utf-8",
            )
            (root / "story_timing.json").write_text(
                json.dumps({"stories": [{"story_id": "story_old"}]}),
                encoding="utf-8",
            )

            self.assertFalse(_tts_complete(root))
            self.assertEqual(_preview_status(root)[0], "NOT_STARTED")

    def test_network_error_retries_same_stage_indefinitely_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            root.mkdir()
            interruption = NetworkStageError("news", root / "network.log")
            with (
                patch(
                    "news_studio._run_subprocess_once",
                    side_effect=[interruption, None],
                ) as run_once,
                patch("news_studio._wait_for_internet") as wait,
            ):
                _run_subprocess(root, "news", ["news_pipeline.py"])

            self.assertEqual(run_once.call_count, 2)
            wait.assert_called_once()

    def test_network_failure_detection_excludes_provider_quota_errors(self) -> None:
        self.assertTrue(
            _is_network_failure(
                "NameResolutionError: getaddrinfo failed for api.openai.com"
            )
        )
        self.assertTrue(
            _is_network_failure("HTTPSConnectionPool: Max retries exceeded with url")
        )
        self.assertFalse(_is_network_failure("HTTP 429 quota_exceeded"))

    def test_direct_resume_enables_progress_ui_unless_json_requested(self) -> None:
        arguments = SimpleNamespace(
            command="resume",
            video="8",
            confirm_api_costs=True,
            approve_preview=False,
            recover_stale_lock=False,
            dry_run=False,
            skip_separators=False,
            json=False,
        )
        parser = MagicMock()
        parser.parse_args.return_value = arguments
        with (
            patch("news_studio.build_parser", return_value=parser),
            patch("news_studio._read_json", return_value={"category": "tech"}),
            patch(
                "news_studio.run_flow",
                return_value={"status": "COMPLETE", "video_number": 8},
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)

        self.assertIsInstance(run.call_args.kwargs["progress"], ProgressDisplay)

    def test_failed_stage_log_contains_traceback_command_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 8"
            root.mkdir()
            fixture = Path(directory) / "failing_stage.py"
            fixture.write_text(
                "import os, traceback\n"
                "try:\n"
                "    raise RuntimeError('fixture failure')\n"
                "except Exception as exc:\n"
                "    print(f'[FIXTURE ERROR] {exc}')\n"
                "    if os.getenv('NEWS_STUDIO_DETAILED_ERRORS') == '1':\n"
                "        traceback.print_exc()\n"
                "    raise SystemExit(1)\n",
                encoding="utf-8",
            )
            with (
                patch("news_studio.source_file", return_value=fixture),
                patch(
                    "news_studio.collect_project_status",
                    return_value={"next_stage": "fixture", "locked": True},
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "Stage fixture failed"),
            ):
                _run_subprocess(root, "fixture", ["failing_stage.py"])

            log = next((root / "orchestration" / "logs").glob("*_fixture.log"))
            detail = log.read_text(encoding="utf-8")
            self.assertIn("NEWS STUDIO DETAILED STAGE LOG", detail)
            self.assertIn("Command", detail)
            self.assertIn("Traceback (most recent call last)", detail)
            self.assertIn("Exit code    : 1", detail)
            self.assertIn("RECOVERY SNAPSHOT", detail)
            self.assertIn('"next_stage": "fixture"', detail)

    def test_compact_preflight_hides_pass_detail_and_keeps_warning(self) -> None:
        payload = {
            "checks": [
                {"status": "PASS", "name": "Python", "detail": "verbose detail"},
                {"status": "WARN", "name": "OAuth", "detail": "setup remains"},
            ]
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        output = io.StringIO()
        with (
            patch("news_studio.subprocess.run", return_value=completed),
            patch("news_studio._console_color_enabled", return_value=False),
            redirect_stdout(output),
        ):
            passed = run_preflight()

        self.assertTrue(passed)
        self.assertIn("PREFLIGHT PASSED", output.getvalue())
        self.assertIn("OAuth", output.getvalue())
        self.assertNotIn("verbose detail", output.getvalue())

    def test_interactive_preflight_failure_stops_before_project_creation(self) -> None:
        with (
            patch("news_studio.run_preflight", return_value=False),
            patch("news_studio.next_project_number") as next_project,
            patch("news_studio.run_flow") as run,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 1)
        next_project.assert_not_called()
        run.assert_not_called()

    def test_progress_bar_uses_green_filled_rectangles(self) -> None:
        display = ProgressDisplay(6, stream=io.StringIO(), live=False, color=False)
        display.color = True
        line = display._bar_line(3, "RUNNING")

        self.assertIn("\x1b[32m", line)
        self.assertIn("█", line)
        self.assertIn("░", line)
        self.assertIn("25%", line)

    def test_failed_stage_segment_uses_red_rectangles(self) -> None:
        display = ProgressDisplay(6, stream=io.StringIO(), live=False, color=False)
        display.color = True
        line = display._bar_line(3, "FAILED")

        self.assertIn("\x1b[32m", line)
        self.assertIn("\x1b[31m", line)
        self.assertIn("FAILED", line)

    def test_headless_progress_emits_stage_snapshots(self) -> None:
        stream = io.StringIO()
        display = ProgressDisplay(6, stream=stream, live=False, color=False, width=11)

        display.start_stage("render", 9)
        display.complete_stage(10)

        output = stream.getvalue()
        self.assertIn("Encoding the final video", output)
        self.assertIn("75%", output)
        self.assertIn("83%", output)
        self.assertIn("COMPLETE", output)

    def test_live_progress_without_color_does_not_emit_ansi_clear(self) -> None:
        stream = io.StringIO()
        display = ProgressDisplay(6, stream=stream, live=True, color=False)

        display.start_stage("render", 9)

        self.assertNotIn("\x1b[2J", stream.getvalue())

    def test_live_progress_hides_traceback_source_lines(self) -> None:
        stream = io.StringIO()
        display = ProgressDisplay(7, stream=stream, live=True, color=False)
        display.start_stage("news", 0)

        display.write_subprocess_line("    raise RuntimeError(message)\n")
        display.write_subprocess_line("RuntimeError: rejected candidate\n")

        self.assertNotIn("RuntimeError", stream.getvalue())

    def test_autonomous_defaults_skip_separators_then_upload(self) -> None:
        results = [
            {"status": "WAITING", "video_number": 6, "stage": "manual_separators"},
            {"status": "WAITING", "video_number": 6, "stage": "ai_story_videos"},
            {"status": "WAITING", "video_number": 6, "stage": "preview_approval"},
            {"status": "COMPLETE", "video_number": 6},
        ]
        with (
            patch.dict(
                "news_studio.os.environ",
                {
                    "AUTOMATION_DECISION_TIMEOUT_SECONDS": "30",
                    "AUTOMATION_UPLOAD_TIMEOUT_SECONDS": "120",
                },
            ),
            patch("news_studio.run_preflight", return_value=True),
            patch("news_studio.latest_project_number", return_value=5),
            patch("news_studio.next_project_number", return_value=6),
            patch("news_studio.api_cost_plan", return_value={}),
            patch(
                "news_studio.timed_choice",
                side_effect=["1", "2", "2", "1", "1"],
            ) as choices,
            patch("news_studio.run_flow", side_effect=results) as run,
            patch(
                "news_studio.upload_project",
                return_value={"status": "UPLOAD_COMPLETE"},
            ) as upload,
            patch("news_studio._event"),
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [call.kwargs["manual_separator_mode"] for call in run.call_args_list],
            ["defer", "skip", "skip", "skip"],
        )
        self.assertEqual(
            [call.kwargs["ai_video_mode"] for call in run.call_args_list],
            ["defer", "defer", "skip", "skip"],
        )
        self.assertEqual(
            [call.kwargs["approve_current_preview"] for call in run.call_args_list],
            [False, False, False, True],
        )
        self.assertEqual(choices.call_args_list[1].kwargs["default"], "2")
        upload.assert_called_once_with(6)
        self.assertTrue(
            all(
                call.kwargs["timeout_seconds"] == 120
                for call in choices.call_args_list[:-1]
            )
        )
        self.assertEqual(choices.call_args_list[-1].kwargs["timeout_seconds"], 120)

    def test_resume_manual_choice_forces_skipped_separator_stage_to_run(self) -> None:
        status = {
            "next_stage": "storyboard",
            "stages": {
                stage: {
                    "status": (
                        "COMPLETE"
                        if FLOW_STAGES.index(stage)
                        <= FLOW_STAGES.index("manual_separators")
                        else "NOT_STARTED"
                    )
                }
                for stage in FLOW_STAGES
            },
        }
        results = [
            {"status": "WAITING", "video_number": 26, "stage": "manual_separators"},
            {"status": "WAITING", "video_number": 26, "stage": "manual_separators"},
        ]
        with (
            patch("news_studio.run_preflight", return_value=True),
            patch("news_studio.latest_project_number", return_value=26),
            patch("news_studio._read_json", return_value={"category": "tech"}),
            patch("news_studio.collect_project_status", return_value=status),
            patch("news_studio.api_cost_plan", return_value={}),
            patch("news_studio.timed_choice", side_effect=["2", "4", "1", "1"]),
            patch("news_studio.run_flow", side_effect=results) as run,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["start_stage"], "manual_separators")
        self.assertIsNone(run.call_args_list[1].kwargs.get("start_stage"))
        self.assertEqual(run.call_args_list[1].kwargs["manual_separator_mode"], "manual")

    def test_timed_choice_uses_configured_default_at_timeout(self) -> None:
        with patch("news_studio.sys.stdin.isatty", return_value=False):
            selected = timed_choice(
                "Decision",
                {"1": "manual", "2": "skip"},
                default="2",
                timeout_seconds=0,
            )
        self.assertEqual(selected, "2")

    def test_news_stage_finalizes_pending_lead_without_repeating_pipeline(self) -> None:
        selection = {
            "selected_story_id": "story_3",
            "selected_display_position": 3,
            "decision_reused": False,
        }
        with (
            patch("news_studio.project_dir", return_value=Path("Video 8")),
            patch("news_studio.selection_is_pending", return_value=True),
            patch("news_studio.finalize_lead_story", return_value=selection) as finalize,
            patch("news_studio._run_subprocess") as run,
            patch("news_studio._event") as event,
        ):
            result = execute_stage(
                8,
                "news",
                category="tech",
                confirm_api_costs=True,
                approve_current_preview=False,
            )

        self.assertEqual(result.status, "COMPLETE")
        run.assert_not_called()
        finalize.assert_called_once_with(
            Path("Video 8"),
            choose=timed_choice,
            timeout_seconds=120.0,
        )
        self.assertEqual(event.call_args.args[1]["story_id"], "story_3")

    def test_timed_choice_supports_dash_formatted_story_list(self) -> None:
        stream = io.StringIO()
        with (
            patch("news_studio.sys.stdin.isatty", return_value=False),
            redirect_stdout(stream),
        ):
            selected = timed_choice(
                "Choose the lead",
                {"1": "First title", "2": "Second title"},
                default="1",
                timeout_seconds=0,
                separator="-",
            )
        self.assertEqual(selected, "1")
        self.assertIn("1-First title", stream.getvalue())
        self.assertIn("2-Second title", stream.getvalue())

    def test_enter_selects_timed_choice_default_immediately(self) -> None:
        availability = iter([False, True])
        fake_msvcrt = SimpleNamespace(
            kbhit=lambda: next(availability, True),
            getwch=lambda: "\r",
        )
        with (
            patch("news_studio.sys.stdin.isatty", return_value=True),
            patch("news_studio.os.name", "nt"),
            patch.dict("sys.modules", {"msvcrt": fake_msvcrt}),
            patch.dict("os.environ", {"AUTOMATION_FAST_DEFAULTS": ""}),
            patch("news_studio._windows_shift_pressed", return_value=False),
        ):
            selected = timed_choice(
                "Decision",
                {"1": "continue", "0": "cancel"},
                default="1",
                timeout_seconds=30,
                allow_shift_enter_fast_mode=True,
            )
        self.assertEqual(selected, "1")
        self.assertEqual(os.getenv("AUTOMATION_FAST_DEFAULTS", ""), "")

    def test_shift_enter_enables_fast_defaults_without_reading_later_input(self) -> None:
        availability = iter([False, True])
        read_key = MagicMock(return_value="\r")
        fake_msvcrt = SimpleNamespace(
            kbhit=lambda: next(availability, True),
            getwch=read_key,
        )
        with (
            patch("news_studio.sys.stdin.isatty", return_value=True),
            patch("news_studio.os.name", "nt"),
            patch.dict("sys.modules", {"msvcrt": fake_msvcrt}),
            patch.dict("os.environ", {"AUTOMATION_FAST_DEFAULTS": ""}),
            patch("news_studio._windows_shift_pressed", return_value=True),
        ):
            first = timed_choice(
                "Choose the production action.",
                {"1": "create", "2": "resume"},
                default="1",
                timeout_seconds=30,
                allow_shift_enter_fast_mode=True,
            )
            second = timed_choice(
                "Allow provider requests?",
                {"1": "continue", "0": "cancel"},
                default="1",
                timeout_seconds=30,
            )

            self.assertEqual(os.getenv("AUTOMATION_FAST_DEFAULTS"), "1")

        self.assertEqual(first, "1")
        self.assertEqual(second, "1")
        read_key.assert_called_once()

    def test_stale_enter_cannot_trigger_next_menu_default(self) -> None:
        availability = iter([False, True, True, False, True])
        characters = iter(["1", "\r", "0"])
        fake_msvcrt = SimpleNamespace(
            kbhit=lambda: next(availability, True),
            getwch=lambda: next(characters),
        )
        with (
            patch("news_studio.sys.stdin.isatty", return_value=True),
            patch("news_studio.os.name", "nt"),
            patch.dict("sys.modules", {"msvcrt": fake_msvcrt}),
            patch.dict("os.environ", {"AUTOMATION_FAST_DEFAULTS": ""}),
        ):
            first = timed_choice(
                "Preview",
                {"1": "approve", "0": "stop"},
                default="1",
                timeout_seconds=30,
            )
            second = timed_choice(
                "Upload",
                {"1": "upload", "0": "do not upload"},
                default="1",
                timeout_seconds=30,
            )

        self.assertEqual(first, "1")
        self.assertEqual(second, "0")

    def test_manual_separator_mode_waits_for_enter_then_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "news_studio._validate_manual_checkpoint",
                    side_effect=[False, True],
                ) as validate,
                patch("news_studio._run_subprocess") as run,
                patch("news_studio.sys.stdin.isatty", return_value=True),
                patch("builtins.input", return_value="") as read,
            ):
                result = execute_stage(
                    15,
                    "manual_separators",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    manual_separator_mode="manual",
                )

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(validate.call_count, 2)
        read.assert_called_once()
        self.assertEqual(run.call_args.args[2][-1], "prepare")

    def test_first_ai_video_checkpoint_prepares_without_false_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 27"
            root.mkdir()
            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio._validate_ai_video_checkpoint") as validate,
                patch("news_studio._run_subprocess") as run,
                patch("news_studio.sys.stdin.isatty", return_value=False),
                patch.dict(
                    "news_studio.os.environ",
                    {"AI_GENERATED_IMAGES_PER_STORY": "2"},
                ),
            ):
                result = execute_stage(
                    27,
                    "ai_story_videos",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    ai_video_mode="manual",
                )

        self.assertEqual(result.status, "WAITING")
        validate.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[2][-1], "prepare")

    def test_gui_ai_video_checkpoint_waits_and_retries_validation_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 27"
            root.mkdir()
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "news_studio._validate_ai_video_checkpoint",
                    side_effect=[False, True],
                ) as validate,
                patch("news_studio._run_subprocess") as run,
                patch("news_studio.sys.stdin.isatty", return_value=False),
                patch("news_studio.timed_choice", side_effect=["1", "1"]) as choose,
                patch.dict(os.environ, {"NEWS_STUDIO_GUI_BRIDGE": "1"}),
                patch.dict(
                    "news_studio.os.environ",
                    {"AI_GENERATED_IMAGES_PER_STORY": "2"},
                ),
            ):
                result = execute_stage(
                    27,
                    "ai_story_videos",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    ai_video_mode="manual",
                )

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(validate.call_count, 2)
        run.assert_called_once()
        self.assertEqual(choose.call_count, 2)
        self.assertTrue(choose.call_args.kwargs["wait_indefinitely"])
        self.assertEqual(choose.call_args.kwargs["asset_path"], str(root / "04_ai_story_videos"))

    def test_gui_separator_checkpoint_waits_for_explicit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 15"
            root.mkdir()
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "news_studio._validate_manual_checkpoint",
                    side_effect=[False, True],
                ) as validate,
                patch("news_studio._run_subprocess") as run,
                patch("news_studio.sys.stdin.isatty", return_value=False),
                patch("news_studio.timed_choice", return_value="1") as choose,
                patch.dict(os.environ, {"NEWS_STUDIO_GUI_BRIDGE": "1"}),
            ):
                result = execute_stage(
                    15,
                    "manual_separators",
                    category="tech",
                    confirm_api_costs=True,
                    approve_current_preview=False,
                    manual_separator_mode="manual",
                )

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(validate.call_count, 2)
        run.assert_called_once()
        self.assertTrue(choose.call_args.kwargs["wait_indefinitely"])

    def test_dry_run_does_not_create_project_or_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 6"
            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio.collect_project_status", return_value=empty_status(root)),
            ):
                result = run_flow(
                    6,
                    category="tech",
                    confirm_api_costs=False,
                    approve_current_preview=False,
                    recover_stale_lock=False,
                    dry_run=True,
                )

            self.assertEqual(result["status"], "DRY_RUN")
            self.assertFalse(root.exists())
            self.assertFalse(result["writes_performed"])
            self.assertFalse(result["api_requests_performed"])

    def test_missing_cost_confirmation_stops_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 6"
            with (
                patch("news_studio.project_dir", return_value=root),
                patch("news_studio.collect_project_status", return_value=empty_status(root)),
            ):
                result = run_flow(
                    6,
                    category="tech",
                    confirm_api_costs=False,
                    approve_current_preview=False,
                    recover_stale_lock=False,
                    dry_run=False,
                )

            self.assertEqual(result["status"], "COST_CONFIRMATION_REQUIRED")
            self.assertFalse(root.exists())

    def test_project_lock_blocks_second_writer_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 6"
            with ProjectLock(root):
                self.assertTrue((root / ".news_studio.lock").is_file())
                with self.assertRaisesRegex(RuntimeError, "locked by PID"):
                    with ProjectLock(root):
                        pass
            self.assertFalse((root / ".news_studio.lock").exists())

    def test_preview_approval_is_bound_to_current_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Video 6"
            proof = root / "07_effects" / "milestone6_reference_60s.mp4"
            proof.parent.mkdir(parents=True)
            proof.write_bytes(b"preview-v1")
            effects_plan = proof.parent / "effects_plan.json"
            effects_plan.write_text('{"quality":{"status":"passed"}}', encoding="utf-8")
            proof.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "effects_plan_sha256": sha256_file(effects_plan),
                        "renderer_sha256": "renderer-v1",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "effects_stage.effects_status",
                    return_value={"status": "EFFECTS_READY"},
                ),
                patch(
                    "effects_stage.reference_proof_renderer_fingerprint",
                    return_value="renderer-v1",
                ),
            ):
                approve_preview(6)
                self.assertEqual(_preview_status(root)[0], "COMPLETE")

                proof.write_bytes(b"preview-v2")
                self.assertEqual(_preview_status(root)[0], "WAITING")

    def test_review_youtube_title_allows_candidate_and_custom_title_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory) / "Video 51"
            publish_dir = video_dir / "09_publish"
            publish_dir.mkdir(parents=True)
            (publish_dir / "title.txt").write_text(
                "Original Recommended Title",
                encoding="utf-8",
            )
            (publish_dir / "publish_package.json").write_text(
                json.dumps(
                    {
                        "recommended_title": "Original Recommended Title",
                        "title_candidates": [
                            {"title": "Original Recommended Title"},
                            {"title": "Second Candidate Title Option"},
                        ],
                        "quality": {},
                        "grounding": {},
                    }
                ),
                encoding="utf-8",
            )
            (publish_dir / "youtube_upload_manifest.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "snippet": {
                                "title": "Original Recommended Title",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("news_studio.project_dir", return_value=video_dir),
                patch("publish_stage.project_dir", return_value=video_dir),
                redirect_stdout(io.StringIO()),
            ):
                candidate_result = review_youtube_title(
                    51,
                    choose=lambda *args, **kwargs: "2",
                    timeout_seconds=30,
                )
                self.assertTrue(candidate_result["changed"])
                self.assertEqual(
                    candidate_result["title"],
                    "Second Candidate Title Option",
                )

                with patch(
                    "builtins.input",
                    return_value="User Typed Custom Title Before Upload",
                ):
                    custom_result = review_youtube_title(
                        51,
                        choose=lambda *args, **kwargs: "c",
                        timeout_seconds=30,
                    )
                self.assertTrue(custom_result["changed"])
                self.assertEqual(
                    custom_result["title"],
                    "User Typed Custom Title Before Upload",
                )
                self.assertEqual(
                    (publish_dir / "title.txt").read_text(encoding="utf-8"),
                    "User Typed Custom Title Before Upload",
                )


if __name__ == "__main__":
    unittest.main()
