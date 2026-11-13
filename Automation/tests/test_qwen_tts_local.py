from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import qwen_tts_local as qwen  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover - standard .venv skips numpy DSP tests
    np = None  # type: ignore[assignment]


class QwenTtsLocalTests(unittest.TestCase):
    def test_split_prosodic_chunks_preserves_paragraph_breaks_and_bounds(self) -> None:
        text = (
            "Nvidia's longtime CEO stepped down today. "
            "The board named an interim chief right away.\n\n"
            "Investigators had opened a review weeks earlier. "
            "Security staff reportedly approached a bystander at the hotel. "
            "Yet executives declined to answer basic questions about oversight.\n\n"
            "Congress now plans to hold hearings next month."
        )
        chunks = qwen.split_prosodic_chunks(text, max_chars=130)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(any(c.paragraph_break_after for c in chunks[:-1]))
        self.assertFalse(chunks[-1].paragraph_break_after)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 130)

    def test_fallback_beat_directions_assigns_varied_tones_and_beat_pauses(self) -> None:
        text = (
            "A major cloud outage disrupted Microsoft services today.\n\n"
            "Security researchers flagged a critical flaw behind the failure.\n\n"
            "Executives promised reliability, yet users are still waiting—why?\n\n"
            "Teams should verify backup plans before tomorrow."
        )
        chunks = qwen.split_prosodic_chunks(text, max_chars=220)
        beats = qwen.fallback_beat_directions(chunks, skeptical=True)
        tones = [b["tone_label"] for b in beats]
        self.assertEqual(tones[0], "hook")
        self.assertEqual(tones[-1], "closing")
        self.assertIn("serious", tones)
        self.assertIn("skeptical", tones)
        self.assertEqual(beats[0]["pause_after"], "beat")

    def test_plan_story_beats_sanitizes_ai_instruct_and_keeps_verbatim_text(self) -> None:
        text = "First beat sentence.\n\nSecond beat raises a basic question?"
        fake_plan = qwen.StoryBeatPlan(
            beats=[
                qwen.BeatDirection(
                    beat_index=1,
                    tone_label="hook",
                    instruct="[warm] Warm, clear, engaging newsroom opening, steady pace.",
                    pause_after="beat",
                ),
                qwen.BeatDirection(
                    beat_index=2,
                    tone_label="skeptical",
                    instruct="Dry, understated skeptical delivery, calm inflection.",
                    pause_after="normal",
                ),
            ]
        )
        with (
            patch.object(qwen, "tech_summary_skeptical_enabled", return_value=True),
            patch.object(qwen, "request_ai_beat_plan", return_value=fake_plan),
        ):
            beats, source = qwen.plan_story_beats(text, dynamic_instruct=True)

        self.assertEqual(source, "ai")
        self.assertEqual(beats[0]["text"], "First beat sentence.")
        self.assertEqual(beats[1]["text"], "Second beat raises a basic question?")
        self.assertNotIn("[", beats[0]["instruct"])
        self.assertEqual(beats[0]["pause_after"], "beat")

    def test_plan_story_beats_falls_back_to_heuristic_on_ai_error(self) -> None:
        text = "First sentence here.\n\nSecond sentence here."
        with (
            patch.object(qwen, "tech_summary_skeptical_enabled", return_value=False),
            patch.object(qwen, "request_ai_beat_plan", side_effect=RuntimeError("offline")),
            patch.object(qwen.time, "sleep"),
        ):
            beats, source = qwen.plan_story_beats(text, dynamic_instruct=True)
        self.assertEqual(source, "heuristic")
        self.assertEqual(len(beats), 2)

    def test_prepare_chunk_for_qwen_speech_cleans_punctuation_only(self) -> None:
        raw = "Nvidia\u2019s CEO\u2014speaking briefly\u2014said \u201cno comment.\u201d"
        cleaned = qwen.prepare_chunk_for_qwen_speech(raw)
        self.assertEqual(cleaned, 'Nvidia\'s CEO \u2014 speaking briefly \u2014 said "no comment."')

    def test_fingerprint_changes_with_dynamic_or_sampling_settings(self) -> None:
        base = qwen.tts_input_fingerprint("Sample text.", "ryan", qwen.DEFAULT_INSTRUCT, "English")
        diff_chunk = qwen.tts_input_fingerprint(
            "Sample text.",
            "ryan",
            qwen.DEFAULT_INSTRUCT,
            "English",
            max_chunk_chars=260,
        )
        self.assertNotEqual(base, diff_chunk)
        with patch.object(qwen, "QWEN_SEED", qwen.QWEN_SEED + 1):
            diff_seed = qwen.tts_input_fingerprint(
                "Sample text.",
                "ryan",
                qwen.DEFAULT_INSTRUCT,
                "English",
            )
        self.assertNotEqual(base, diff_seed)

    @unittest.skipIf(np is None, "numpy not installed in this environment")
    def test_audio_stitching_trims_silence_levels_loudness_and_inserts_pauses(self) -> None:
        sr = 24000
        tone_a = 0.15 * np.sin(np.linspace(0.0, 200.0, 6000, dtype=np.float32))
        tone_b = 0.60 * np.sin(np.linspace(0.0, 200.0, 6000, dtype=np.float32))
        padded_a = np.concatenate([np.zeros(4800, dtype=np.float32), tone_a, np.zeros(4800, dtype=np.float32)])
        padded_b = np.concatenate([np.zeros(4800, dtype=np.float32), tone_b, np.zeros(4800, dtype=np.float32)])

        leveled = qwen.match_chunk_loudness([tone_a, tone_b])
        rms_a = float(np.sqrt(np.mean(leveled[0] ** 2)))
        rms_b = float(np.sqrt(np.mean(leveled[1] ** 2)))
        self.assertLess(abs(rms_a - rms_b) / max(rms_a, rms_b), 0.35)

        stitched_normal = qwen.stitch_beat_waveforms([padded_a, padded_b], ["normal", "normal"], sr)
        stitched_beat = qwen.stitch_beat_waveforms([padded_a, padded_b], ["beat", "normal"], sr)
        expected_diff = int(round((qwen.PAUSE_BEAT_SECONDS - qwen.PAUSE_NORMAL_SECONDS) * sr))
        self.assertAlmostEqual(len(stitched_beat) - len(stitched_normal), expected_diff, delta=4)
        self.assertEqual(float(stitched_beat[0]), 0.0)


if __name__ == "__main__":
    unittest.main()
