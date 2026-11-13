from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

import provider_quota_status as quota


class ProviderQuotaStatusTests(unittest.TestCase):
    def test_unsplash_uses_last_observed_headers_without_a_status_request(self) -> None:
        with (
            patch.dict(os.environ, {"UNSPLASH_API_KEY": "configured-key"}),
            patch.object(
                quota,
                "load_observation",
                return_value={"limit": 50, "remaining": 41, "observed_at_utc": "2026-09-12T12:00:00+00:00", "reset": ""},
            ),
            patch.object(quota.requests, "get") as request,
        ):
            result = quota.unsplash_status()
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["primary"], "41 requests left")
        self.assertAlmostEqual(result["meter"], 41 / 50)
        request.assert_not_called()

    def test_pexels_reports_rate_limit_without_exposing_key(self) -> None:
        response = Mock()
        response.headers = {
            "X-Ratelimit-Limit": "200",
            "X-Ratelimit-Remaining": "173",
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(os.environ, {"PEXELS_API_KEY": "hidden-pexels-key"}),
            patch.object(quota.requests, "get", return_value=response) as request,
        ):
            result = quota.pexels_status()
        self.assertEqual(result["primary"], "173 requests left")
        self.assertAlmostEqual(result["meter"], 173 / 200)
        self.assertNotIn("hidden-pexels-key", json.dumps(result))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "hidden-pexels-key")

    def test_pexels_negative_sentinel_is_reported_as_unknown_not_negative(self) -> None:
        response = Mock()
        response.headers = {
            "X-Ratelimit-Limit": "-1",
            "X-Ratelimit-Remaining": "-1",
        }
        response.raise_for_status.return_value = None
        with (
            patch.dict(os.environ, {"PEXELS_API_KEY": "configured-key"}),
            patch.object(quota.requests, "get", return_value=response),
        ):
            result = quota.pexels_status()
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["primary"], "API key ready")
        self.assertNotIn("meter", result)
        self.assertNotIn("-1", json.dumps(result))

    def test_pixabay_status_waits_for_a_real_media_request(self) -> None:
        with (
            patch.dict(os.environ, {"PIXABAY_API_KEY": "hidden-pixabay-key"}),
            patch.object(quota, "load_observation", return_value=None),
            patch.object(quota.requests, "get") as request,
        ):
            result = quota.pixabay_status()
        self.assertEqual(result["primary"], "API key ready")
        self.assertIn("does not spend quota", result["detail"])
        self.assertNotIn("hidden-pixabay-key", json.dumps(result))
        request.assert_not_called()

    def test_elevenlabs_combines_configured_key_credits_without_exposing_keys(self) -> None:
        subscriptions = [
            {"slot": "1", "used": 100, "limit": 1000, "remaining": 900, "reset": None},
            {"slot": "2", "used": 250, "limit": 2000, "remaining": 1750, "reset": None},
        ]
        with (
            patch.object(quota, "_elevenlabs_keys", return_value=[("1", "secret-one"), ("2", "secret-two")]),
            patch.object(quota, "_get_elevenlabs_subscription", side_effect=subscriptions),
        ):
            result = quota.elevenlabs_status()
        serialized = json.dumps(result)
        self.assertEqual(result["primary"], "2,650 credits left")
        self.assertNotIn("secret-one", serialized)
        self.assertNotIn("secret-two", serialized)

    def test_elevenlabs_explains_rejected_keys_without_exposing_them(self) -> None:
        rejected = {"slot": "1", "error": "unauthorized", "http_status": 401}
        with (
            patch.object(quota, "_elevenlabs_keys", return_value=[("1", "hidden-secret")]),
            patch.object(quota, "_get_elevenlabs_subscription", return_value=rejected),
            patch.object(quota, "_validate_elevenlabs_key", return_value=False),
        ):
            result = quota.elevenlabs_status()
        self.assertEqual(result["primary"], "Keys rejected")
        self.assertNotIn("hidden-secret", json.dumps(result))

    def test_elevenlabs_reports_usable_keys_when_only_balance_is_restricted(self) -> None:
        rejected = {"slot": "1", "error": "unauthorized", "http_status": 401}
        with (
            patch.object(quota, "_elevenlabs_keys", return_value=[("1", "hidden-secret")]),
            patch.object(quota, "_get_elevenlabs_subscription", return_value=rejected),
            patch.object(quota, "_validate_elevenlabs_key", return_value=True),
        ):
            result = quota.elevenlabs_status()
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["primary"], "1/1 keys usable")
        self.assertIn("balance permission", result["detail"])

    def test_console_managed_providers_never_invent_remaining_values(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "configured", "GEMINI_API_KEY": "configured"},
        ):
            openai = quota.openai_status()
            gemini = quota.gemini_status()
        self.assertEqual(openai["primary"], "Balance not exposed")
        self.assertEqual(gemini["primary"], "Quota in AI Studio")


if __name__ == "__main__":
    unittest.main()
