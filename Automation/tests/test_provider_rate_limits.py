from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_rate_limits as rate_limits


class ProviderRateLimitTests(unittest.TestCase):
    def test_records_headers_case_insensitively_for_a_later_quota_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.json"
            with patch.object(rate_limits, "OBSERVATIONS_PATH", path):
                rate_limits.record_observation(
                    "pixabay",
                    {"x-ratelimit-limit": "100", "X-RateLimit-Remaining": "87"},
                )
                observed = rate_limits.load_observation("pixabay")
        self.assertIsNotNone(observed)
        self.assertEqual(observed["limit"], 100)
        self.assertEqual(observed["remaining"], 87)

    def test_ignores_missing_rate_limit_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.json"
            with patch.object(rate_limits, "OBSERVATIONS_PATH", path):
                rate_limits.record_observation("unsplash", {"Content-Type": "application/json"})
                observed = rate_limits.load_observation("unsplash")
        self.assertIsNone(observed)


if __name__ == "__main__":
    unittest.main()
