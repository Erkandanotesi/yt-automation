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
    ProgressDisplay,
    ProjectLock,
    _run_subprocess,
    _preview_status,
    approve_preview,
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
                call.kwargs["timeout_seconds"] == 30
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
            with patch("news_studio.project_dir", return_value=root):
                approve_preview(6)
            self.assertEqual(_preview_status(root)[0], "COMPLETE")

            proof.write_bytes(b"preview-v2")
            self.assertEqual(_preview_status(root)[0], "WAITING")


if __name__ == "__main__":
    unittest.main()
