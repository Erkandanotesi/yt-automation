from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news_studio import (
    FLOW_STAGES,
    ProjectLock,
    _preview_status,
    approve_preview,
    run_flow,
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
            with patch("news_studio.project_dir", return_value=root):
                approve_preview(6)
            self.assertEqual(_preview_status(root)[0], "COMPLETE")

            proof.write_bytes(b"preview-v2")
            self.assertEqual(_preview_status(root)[0], "WAITING")


if __name__ == "__main__":
    unittest.main()
