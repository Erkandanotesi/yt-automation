from __future__ import annotations

import io
import json
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
    _tts_complete,
    _is_network_failure,
    _preview_status,
    _render_recovery_action,
    approve_preview,
    execute_stage,
    interactive,
    main,
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
        self.assertIn("27%", line)

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
        self.assertIn("82%", output)
        self.assertIn("91%", output)
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
                side_effect=["1", "1", "2", "1", "1"],
            ) as choices,
            patch("news_studio.run_flow", side_effect=results) as run,
            patch(
                "news_studio.upload_project",
                return_value={"status": "UPLOAD_COMPLETE"},
            ) as upload,
        ):
            exit_code = interactive()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [call.kwargs["manual_separator_mode"] for call in run.call_args_list],
            ["defer", "skip", "skip"],
        )
        self.assertEqual(
            [call.kwargs["approve_current_preview"] for call in run.call_args_list],
            [False, False, True],
        )
        upload.assert_called_once_with(6)
        self.assertTrue(
            all(
                call.kwargs["timeout_seconds"] == 120
                for call in choices.call_args_list[:-1]
            )
        )
        self.assertEqual(choices.call_args_list[-1].kwargs["timeout_seconds"], 120)

    def test_timed_choice_uses_configured_default_at_timeout(self) -> None:
        with patch("news_studio.sys.stdin.isatty", return_value=False):
            selected = timed_choice(
                "Decision",
                {"1": "manual", "2": "skip"},
                default="2",
                timeout_seconds=0,
            )
        self.assertEqual(selected, "2")

    def test_enter_selects_timed_choice_default_immediately(self) -> None:
        fake_msvcrt = SimpleNamespace(kbhit=lambda: True, getwch=lambda: "\r")
        with (
            patch("news_studio.sys.stdin.isatty", return_value=True),
            patch("news_studio.os.name", "nt"),
            patch.dict("sys.modules", {"msvcrt": fake_msvcrt}),
        ):
            selected = timed_choice(
                "Decision",
                {"1": "continue", "0": "cancel"},
                default="1",
                timeout_seconds=30,
            )
        self.assertEqual(selected, "1")

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
                json.dumps({"effects_plan_sha256": sha256_file(effects_plan)}),
                encoding="utf-8",
            )
            with (
                patch("news_studio.project_dir", return_value=root),
                patch(
                    "effects_stage.effects_status",
                    return_value={"status": "EFFECTS_READY"},
                ),
            ):
                approve_preview(6)
                self.assertEqual(_preview_status(root)[0], "COMPLETE")

                proof.write_bytes(b"preview-v2")
                self.assertEqual(_preview_status(root)[0], "WAITING")


if __name__ == "__main__":
    unittest.main()
