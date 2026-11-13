from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import elevenlabs_tts as tts
import requests
from alignment_utils import build_alignment_artifact
from tests.test_alignment_utils import proportional_alignment
from tooling import find_executable


class ElevenLabsBehaviorTests(unittest.TestCase):
    def test_timestamp_response_decodes_audio_and_original_alignment(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "audio_base64": base64.b64encode(b"audio").decode("ascii"),
                "alignment": {
                    "characters": ["H", "i"],
                    "character_start_times_seconds": [0, 0.1],
                    "character_end_times_seconds": [0.1, 0.2],
                },
                "normalized_alignment": None,
            }
        ).encode("utf-8")
        audio, alignment = tts.decode_timestamp_response(response)
        self.assertEqual(audio, b"audio")
        self.assertEqual(alignment["characters"], ["H", "i"])

    def test_speech_request_uses_with_timestamps_endpoint(self) -> None:
        sentinel = requests.Response()
        with patch.object(tts, "request_with_retry", return_value=sentinel) as request:
            result = tts.speech_request("Tiny test.", "hidden-key")
        self.assertIs(result, sentinel)
        args, kwargs = request.call_args
        self.assertTrue(args[1].endswith("/with-timestamps"))
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    def test_mocked_timestamp_generation_saves_audio_and_alignment(self) -> None:
        ffmpeg = find_executable("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            news = project / "Haberler"
            news.mkdir()
            text_path = news / "01_story_123456789abc_test.txt"
            text = "A tiny timestamp test."
            text_path.write_text(text, encoding="utf-8")
            (project / "project_info.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "expected_count": 1,
                        "stories": [
                            {
                                "story_id": "story_123456789abc",
                                "text": str(text_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source_audio = project / "source.mp3"
            subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "libmp3lame",
                    "-y",
                    str(source_audio),
                ],
                check=True,
            )
            audio_bytes = source_audio.read_bytes()
            with (
                patch.object(tts, "project_dir", return_value=project),
                patch.object(tts, "project_name", return_value="Video 5"),
                patch.object(tts, "validate_voice", return_value={"name": "Mock"}),
                patch.object(
                    tts,
                    "generate_speech",
                    return_value=(
                        audio_bytes,
                        {
                            "api_key_slot": "mock",
                            "character_cost": "22",
                            "request_id": "request_mock",
                            "content_type": "audio/mpeg",
                            "response_bytes": len(audio_bytes),
                        },
                        proportional_alignment(text, 1.0),
                    ),
                ),
            ):
                self.assertEqual(tts.synthesize_video(5, overwrite=False), 0)

            info = json.loads(
                (project / "Sesler" / "tts_info.json").read_text(encoding="utf-8")
            )
            entry = info["files"][0]
            alignment_path = Path(entry["alignment_file"])
            self.assertTrue(alignment_path.is_file())
            self.assertEqual(entry["alignment_method"], "tts_with_timestamps")
            alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
            self.assertEqual(alignment["canonical_text"], text)
            self.assertEqual(alignment["quality"]["status"], "passed")

    def test_fingerprint_changes_with_text_or_settings(self) -> None:
        first = tts.tts_input_fingerprint("A tiny test.")
        second = tts.tts_input_fingerprint("A different tiny test.")
        self.assertNotEqual(first, second)

        with patch.object(tts, "SPEED", tts.SPEED + 0.01):
            changed_settings = tts.tts_input_fingerprint("A tiny test.")
        self.assertNotEqual(first, changed_settings)

    def test_quota_error_switches_to_next_configured_slot_without_network(self) -> None:
        original_keys = tts.API_KEYS
        original_index = tts.ACTIVE_KEY_INDEX
        original_remaining = dict(tts.KNOWN_REMAINING_CREDITS)
        calls: list[str] = []
        try:
            tts.API_KEYS = [("1", "hidden-one"), ("6", "hidden-six")]
            tts.ACTIVE_KEY_INDEX = 0
            tts.KNOWN_REMAINING_CREDITS.clear()

            def operation(key: str) -> str:
                calls.append(key)
                if key == "hidden-one":
                    raise tts.ElevenLabsAPIError(
                        429,
                        "quota_exceeded",
                        "You have 0 credits remaining",
                    )
                return "ok"

            result, label = tts.with_key_fallback(operation)

            self.assertEqual(result, "ok")
            self.assertEqual(label, "6")
            self.assertEqual(calls, ["hidden-one", "hidden-six"])
        finally:
            tts.API_KEYS = original_keys
            tts.ACTIVE_KEY_INDEX = original_index
            tts.KNOWN_REMAINING_CREDITS.clear()
            tts.KNOWN_REMAINING_CREDITS.update(original_remaining)

    def test_non_switchable_error_stops_immediately(self) -> None:
        original_keys = tts.API_KEYS
        original_index = tts.ACTIVE_KEY_INDEX
        try:
            tts.API_KEYS = [("1", "hidden-one"), ("2", "hidden-two")]
            tts.ACTIVE_KEY_INDEX = 0
            with self.assertRaises(tts.ElevenLabsAPIError):
                tts.with_key_fallback(
                    lambda _: (_ for _ in ()).throw(
                        tts.ElevenLabsAPIError(400, "bad_request", "invalid text")
                    )
                )
            self.assertEqual(tts.ACTIVE_KEY_INDEX, 0)
        finally:
            tts.API_KEYS = original_keys
            tts.ACTIVE_KEY_INDEX = original_index

    def test_incomplete_project_manifest_blocks_tts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "Haberler").mkdir()
            (project / "project_info.json").write_text(
                json.dumps({"status": "incomplete", "stories": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                tts.summary_files(project)

    def test_complete_manifest_ignores_stale_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            news = project / "Haberler"
            news.mkdir()
            selected = news / "01_story_abc_script.txt"
            selected.write_text("selected", encoding="utf-8")
            (news / "99_stale.txt").write_text("stale", encoding="utf-8")
            (project / "project_info.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "expected_count": 1,
                        "stories": [{"text": str(selected)}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(tts.summary_files(project), [selected])

    def test_reconcile_emotion_tagged_text_preserves_canonical_words_and_filters_stage_directions(
        self,
    ) -> None:
        canonical = "OpenAI released Model 5 today. Critics raised 3 privacy concerns."
        # AI accidentally rewrote "3" -> "three" and slipped in "[music]" and "[cut]".
        ai_candidate = (
            "[serious] OpenAI released Model 5 today. "
            "[music] [skeptical] Critics raised three privacy concerns. [cut]"
        )

        reconciled, applied_tags = tts.reconcile_emotion_tagged_text(
            canonical,
            ai_candidate,
        )

        self.assertEqual(
            reconciled,
            "[serious] OpenAI released Model 5 today. "
            "[skeptical] Critics raised 3 privacy concerns.",
        )
        self.assertEqual(applied_tags, ["[serious]", "[skeptical]"])
        self.assertEqual(tts.strip_emotion_tags(reconciled), canonical)

    def test_generate_speech_sends_ai_emotion_tagged_text_to_elevenlabs(self) -> None:
        canonical = "OpenAI announced a new tool. Skeptics questioned the timeline."
        tagged = (
            "[serious] OpenAI announced a new tool. "
            "[skeptical] Skeptics questioned the timeline."
        )
        sent_payloads: list[dict] = []

        def fake_request(method: str, url: str, **kwargs):
            sent_payloads.append(kwargs["json"])
            resp = requests.Response()
            resp.status_code = 200
            resp.headers["content-type"] = "application/json"
            resp.headers["request-id"] = "req_emotion_test"
            resp.headers["character-cost"] = str(len(kwargs["json"]["text"]))
            resp._content = json.dumps(
                {
                    "audio_base64": base64.b64encode(b"fake-mp3-bytes").decode("ascii"),
                    "alignment": proportional_alignment(kwargs["json"]["text"], 4.0),
                }
            ).encode("utf-8")
            return resp

        ai_result = tts.EmotionTaggedNarration(
            tagged_text=tagged,
            applied_tags=["[serious]", "[skeptical]"],
        )

        original_keys = tts.API_KEYS
        original_index = tts.ACTIVE_KEY_INDEX
        try:
            tts.API_KEYS = [("1", "hidden-key")]
            tts.ACTIVE_KEY_INDEX = 0
            with (
                patch.object(tts, "MODEL_ID", "eleven_v4_turbo"),
                patch.object(tts, "EMOTIONS_ENABLED", True),
                patch.object(tts, "tech_summary_skeptical_enabled", return_value=False),
                patch.object(
                    tts,
                    "request_ai_emotion_tagged_text",
                    return_value=ai_result,
                ),
                patch.object(tts, "request_with_retry", side_effect=fake_request),
            ):
                audio, metadata, raw_alignment = tts.generate_speech(canonical)
        finally:
            tts.API_KEYS = original_keys
            tts.ACTIVE_KEY_INDEX = original_index

        self.assertEqual(audio, b"fake-mp3-bytes")
        self.assertEqual(len(sent_payloads), 1)
        self.assertEqual(sent_payloads[0]["text"], tagged)
        self.assertTrue(metadata["emotions_enabled"])
        self.assertEqual(metadata["emotion_tags"], ["[serious]", "[skeptical]"])
        self.assertEqual(metadata["emotion_tag_count"], 2)
        self.assertEqual(metadata["expressed_text"], tagged)

        artifact = build_alignment_artifact(
            story_id="story_123456789abc",
            canonical_text=canonical,
            raw_alignment=raw_alignment,
            method="tts_with_timestamps",
            audio_sha256="a" * 64,
            audio_duration_seconds=4.0,
        )
        self.assertEqual(artifact["canonical_text"], canonical)
        self.assertEqual(artifact["quality"]["status"], "passed")

    def test_split_text_for_capacities_never_splits_inside_or_after_emotion_tag(
        self,
    ) -> None:
        tagged = (
            "[serious] First sentence ends cleanly. "
            "[short pause] [skeptical] Second sentence continues here."
        )
        # Choose capacity 47 so the cut falls right inside "[short pause]" or before it,
        # forcing split_text_for_capacities to cut at the clean sentence boundary instead.
        chunks = tts.split_text_for_capacities(
            tagged,
            [("1", "k1", 47), ("2", "k2", 120)],
        )
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][2], "[serious] First sentence ends cleanly.")
        self.assertEqual(
            chunks[1][2],
            "[short pause] [skeptical] Second sentence continues here.",
        )

        canonical = tts.strip_emotion_tags(tagged)
        merged = tts.merge_timestamp_alignments(
            canonical,
            [
                (
                    tts.strip_emotion_tags(chunk),
                    proportional_alignment(chunk, 2.0),
                )
                for _, _, chunk in chunks
            ],
        )
        self.assertEqual("".join(merged["characters"]), canonical)

    def test_legacy_v2_model_disables_inline_emotion_tags(self) -> None:
        self.assertFalse(tts.model_supports_emotion_tags("eleven_multilingual_v2"))
        self.assertFalse(tts.model_supports_emotion_tags("eleven_turbo_v2_5"))
        self.assertTrue(tts.model_supports_emotion_tags("eleven_v4_turbo"))
        self.assertTrue(tts.model_supports_emotion_tags("eleven_v3"))

        with (
            patch.object(tts, "EMOTIONS_ENABLED", True),
            patch.object(tts, "MODEL_ID", "eleven_multilingual_v2"),
        ):
            self.assertFalse(tts.emotions_active())

        with (
            patch.object(tts, "EMOTIONS_ENABLED", True),
            patch.object(tts, "MODEL_ID", "eleven_v4_turbo"),
        ):
            self.assertTrue(tts.emotions_active())

    def test_normalize_emotion_tag_aliases_and_filters_theatrical_tags(self) -> None:
        self.assertEqual(tts.normalize_emotion_tag("[sarcastic]"), "dryly")
        self.assertEqual(tts.normalize_emotion_tag("[worried]"), "concerned")
        self.assertEqual(tts.normalize_emotion_tag("[pause]"), "short pause")
        self.assertIsNone(tts.normalize_emotion_tag("[sighs]"))
        self.assertIsNone(tts.normalize_emotion_tag("[laughs]"))
        self.assertIsNone(tts.normalize_emotion_tag("[dramatically]"))

    def test_reconcile_enforces_tag_spacing_and_cap_on_full_scripts(self) -> None:
        sentences = [
            "OpenAI introduced a major security update for enterprise teams today.",
            "Engineers detailed how the new audit logs work across cloud workspaces.",
            "Meanwhile, smaller startups questioned whether pricing remains fair for builders.",
            "Analysts warned that migration deadlines could still catch IT teams off guard.",
            "Executives promised smooth rollouts, though past updates caused brief outages.",
            "In the end, administrators will have to test every workflow before upgrading.",
        ]
        canonical = " ".join(sentences)  # > 60 words
        over_tagged = " ".join(
            f"[serious] {s}" if i % 2 == 0 else f"[curious] {s}"
            for i, s in enumerate(sentences)
        )

        reconciled, applied = tts.reconcile_emotion_tagged_text(canonical, over_tagged)
        self.assertEqual(tts.strip_emotion_tags(reconciled), canonical)
        self.assertLessEqual(len(applied), 3)
        self.assertGreaterEqual(len(applied), 2)

    def test_settings_payload_tracks_expression_prompt_and_skeptical_mode(self) -> None:
        with (
            patch.object(tts, "EMOTIONS_ENABLED", True),
            patch.object(tts, "MODEL_ID", "eleven_v4_turbo"),
            patch.object(tts, "tech_summary_skeptical_enabled", return_value=True),
        ):
            payload = tts.tts_settings_payload()
        self.assertEqual(
            payload["expression_prompt_version"],
            tts.ELEVENLABS_EXPRESSION_PROMPT_VERSION,
        )
        self.assertTrue(payload["skeptical_mode"])


if __name__ == "__main__":
    unittest.main()
