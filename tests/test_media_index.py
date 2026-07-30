from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from media_index import MediaIndexSettings, MediaRoot, scan_media_index
from tooling import find_executable


def make_image(path: Path, color: str) -> None:
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("FFmpeg is unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1280x720",
            "-frames:v",
            "1",
            "-y",
            str(path),
        ],
        check=True,
    )


class MediaIndexTests(unittest.TestCase):
    def test_incremental_rescan_and_user_confirmed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "Media"
            cleared = root / "Media (No-Copyright)"
            make_image(primary / "Valve" / "steam wishlist categories.png", "blue")
            make_image(
                cleared / "OpenAI" / "openai logo on phone.png",
                "green",
            )
            settings = MediaIndexSettings(
                index_path=root / "index.json",
                roots=(
                    MediaRoot("primary", primary, "unknown"),
                    MediaRoot(
                        "no_copyright_repository",
                        cleared,
                        "user_confirmed_no_copyright",
                    ),
                ),
                workers=2,
            )

            first = scan_media_index(settings)
            second = scan_media_index(settings)

            self.assertEqual(first["stats"]["new"], 2)
            self.assertEqual(second["stats"]["reused_unchanged"], 2)
            self.assertEqual(second["stats"]["new"], 0)
            cleared_asset = next(
                item
                for item in second["assets"]
                if item["media_root"] == "no_copyright_repository"
            )
            self.assertEqual(
                cleared_asset["license"]["status"],
                "user_confirmed_no_copyright",
            )
            self.assertFalse(cleared_asset["license"]["review_required"])
            self.assertIn("openai", cleared_asset["keywords"])
            self.assertIn("phone", cleared_asset["keywords"])

    def test_rename_is_detected_and_usage_history_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "Media"
            original = media / "generic image.png"
            renamed = media / "Linux" / "linus torvalds portrait.png"
            make_image(original, "red")
            settings = MediaIndexSettings(
                index_path=root / "index.json",
                roots=(MediaRoot("primary", media, "unknown"),),
                workers=1,
            )
            first = scan_media_index(settings)
            first_id = first["assets"][0]["asset_id"]
            first["assets"][0]["usage_events"] = ["video_4:story_01:window_001"]
            first["assets"][0]["prior_use_count"] = 1
            settings.index_path.write_text(
                __import__("json").dumps(first),
                encoding="utf-8",
            )

            renamed.parent.mkdir(parents=True)
            original.rename(renamed)
            second = scan_media_index(settings)

            self.assertEqual(second["stats"]["renamed"], 1)
            asset = second["assets"][0]
            self.assertEqual(asset["asset_id"], first_id)
            self.assertEqual(asset["prior_use_count"], 1)
            self.assertEqual(asset["renamed_from"], str(original.resolve()))
            self.assertIn("linus", asset["keywords"])
            self.assertIn("torvalds", asset["keywords"])


if __name__ == "__main__":
    unittest.main()
