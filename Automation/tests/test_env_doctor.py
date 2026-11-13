from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from env_doctor import fix_env, inspect_env


class EnvironmentDoctorTests(unittest.TestCase):
    def test_fix_preserves_active_secret_without_reporting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(
                b"\xef\xbb\xbf# AI SA\xc3\x84\xc5\xbeLAYICISI\n"
                b"API_KEY=old-secret\n"
                b"API_KEY=current-secret\n"
            )

            before = inspect_env(path)
            result = fix_env(path)
            text = path.read_text(encoding="utf-8")

            self.assertTrue(before["has_utf8_bom"])
            self.assertEqual(before["duplicate_variables"]["API_KEY"], [2, 3])
            self.assertNotIn("old-secret", text)
            self.assertIn("API_KEY=current-secret", text)
            self.assertFalse(result["after"]["has_utf8_bom"])
            self.assertEqual(result["after"]["duplicate_variables"], {})
            self.assertNotIn("current-secret", str(result))


if __name__ == "__main__":
    unittest.main()
