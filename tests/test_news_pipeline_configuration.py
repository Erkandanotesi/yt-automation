from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import news_pipeline
from file_locations import AUTOMATION_ROOT


class NewsPipelineConfigurationTests(unittest.TestCase):
    def test_launcher_pauses_only_after_a_failure(self) -> None:
        launcher = (AUTOMATION_ROOT / "create_video.bat").read_text(encoding="utf-8")
        self.assertIn('if not "%RESULT%"=="0"', launcher)
        self.assertIn("pause", launcher.casefold())
        self.assertLess(launcher.index("if not"), launcher.casefold().index("pause"))

    def test_central_roots_are_available_during_configuration_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / "Automation"
            projects = root / "Projects"
            with (
                patch.object(news_pipeline, "AUTOMATION_ROOT", automation),
                patch.object(news_pipeline, "PROJECTS_ROOT", projects),
                patch.object(news_pipeline, "PROVIDER", "openai"),
                patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
                patch.object(
                    news_pipeline.ArchiveSettings,
                    "from_environment",
                ) as archive_settings,
            ):
                news_pipeline.validate_configuration()

            self.assertTrue(automation.is_dir())
            self.assertTrue(projects.is_dir())
            archive_settings.return_value.validate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
