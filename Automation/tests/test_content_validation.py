from __future__ import annotations

import unittest

from content_validation import (
    ContentValidationError,
    MAX_NARRATION_WORDS,
    MIN_NARRATION_WORDS,
    TARGET_NARRATION_WORDS,
    narration_length_guidance,
    trim_narration_to_word_limit,
    validate_narration,
    word_count,
)


def valid_material() -> tuple[str, str]:
    sentence = (
        "The company described its privacy update in a public briefing and "
        "said the change would remain optional for customers."
    )
    repetitions = (TARGET_NARRATION_WORDS + word_count(sentence) - 1) // word_count(sentence)
    narration = " ".join([sentence] * repetitions)
    source = (
        "OpenAI published a detailed source document. "
        + " ".join([sentence] * 10)
        + " OpenAI privacy update public briefing."
    )
    return narration, source


class ContentValidationTests(unittest.TestCase):
    def test_overlong_narration_is_trimmed_at_sentence_boundary_locally(self) -> None:
        sentence = "This supported sentence contains exactly seven useful words."
        repetitions = (MAX_NARRATION_WORDS // word_count(sentence)) + 10
        overlong = " ".join([sentence] * repetitions)
        trimmed = trim_narration_to_word_limit(overlong)
        self.assertLessEqual(word_count(trimmed), MAX_NARRATION_WORDS)
        self.assertGreaterEqual(word_count(trimmed), MIN_NARRATION_WORDS)
        self.assertTrue(trimmed.endswith("words."))

    def test_length_guidance_matches_the_enforced_policy(self) -> None:
        self.assertIn(str(TARGET_NARRATION_WORDS), narration_length_guidance())
        self.assertIn(str(MIN_NARRATION_WORDS), narration_length_guidance())
        self.assertIn(str(MAX_NARRATION_WORDS), narration_length_guidance())

    def test_valid_narration_passes(self) -> None:
        narration, source = valid_material()
        result = validate_narration(
            title="A supported technology update",
            narration=narration,
            source_text=source,
            factual_review={
                "names_entities_checked": ["OpenAI"],
                "numbers_dates_checked": [],
                "quotes_checked": [],
                "attributions_preserved": ["public briefing"],
                "unsupported_claims_removed": [],
            },
        )
        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["word_count"], MIN_NARRATION_WORDS)
        self.assertLessEqual(result["word_count"], MAX_NARRATION_WORDS)

    def test_wrong_word_count_is_rejected(self) -> None:
        _, source = valid_material()
        with self.assertRaisesRegex(ContentValidationError, "required range"):
            validate_narration(
                title="Too short",
                narration="This is far too short.",
                source_text=source,
            )

    def test_unsupported_number_is_recorded_without_rewrite(self) -> None:
        narration, source = valid_material()
        words = narration.split()
        words[20] = "987654"
        result = validate_narration(
            title="Audited number",
            narration=" ".join(words),
            source_text=source,
            factual_review={"names_entities_checked": ["OpenAI"]},
        )

        self.assertEqual(result["status"], "passed")
        self.assertIn("987654", result["checked_numbers"])

    def test_unsigned_narration_number_matches_source_currency_amount(self) -> None:
        narration, source = valid_material()
        narration = narration.replace("customers.", "customers with 22 billion records.", 1)
        source += " The filing reports a $22 billion amount."

        result = validate_narration(
            title="Supported currency amount",
            narration=narration,
            source_text=source,
            factual_review={
                "names_entities_checked": ["OpenAI"],
                "numbers_dates_checked": [],
                "quotes_checked": [],
                "attributions_preserved": [],
                "unsupported_claims_removed": [],
            },
        )

        self.assertEqual(result["status"], "passed")

    def test_currency_mismatch_is_recorded_without_rewrite(self) -> None:
        narration, source = valid_material()
        narration = narration.replace("customers.", "customers with $22 billion records.", 1)
        source += " The filing reports a €22 billion amount."

        result = validate_narration(
            title="Audited currency amount",
            narration=narration,
            source_text=source,
            factual_review={"names_entities_checked": ["OpenAI"]},
        )

        self.assertEqual(result["status"], "passed")
        self.assertIn("$22", result["checked_numbers"])

    def test_fabricated_direct_quote_is_rejected(self) -> None:
        narration, source = valid_material()
        words = narration.split()
        words[10:12] = ['"never', 'happened"']
        with self.assertRaisesRegex(ContentValidationError, "direct quote absent"):
            validate_narration(
                title="Unsupported quotation",
                narration=" ".join(words),
                source_text=source,
            )

    def test_supported_quote_allows_source_punctuation_difference(self) -> None:
        narration, source = valid_material()
        narration = narration.replace("privacy update", '"privacy update,"', 1)
        result = validate_narration(
            title="Supported quotation",
            narration=narration,
            source_text=source,
            factual_review={
                "names_entities_checked": ["OpenAI"],
                "numbers_dates_checked": [],
                "quotes_checked": [],
                "attributions_preserved": [],
                "unsupported_claims_removed": [],
            },
        )
        self.assertEqual(result["status"], "passed")

    def test_missing_entity_review_is_rejected(self) -> None:
        narration, source = valid_material()
        with self.assertRaisesRegex(ContentValidationError, "name or entity"):
            validate_narration(
                title="Missing factual review",
                narration=narration,
                source_text=source,
                factual_review={
                    "names_entities_checked": [],
                    "numbers_dates_checked": [],
                    "quotes_checked": [],
                    "attributions_preserved": [],
                    "unsupported_claims_removed": [],
                },
            )

    def test_evidence_allows_wrapping_quote_and_source_punctuation(self) -> None:
        narration, source = valid_material()
        source += ' Proofpoint tracks the group as TA488, or "Laundry Bear," today.'
        result = validate_narration(
            title="A supported technology update",
            narration=narration,
            source_text=source,
            factual_review={
                "names_entities_checked": ['"Laundry Bear"'],
                "numbers_dates_checked": [],
                "quotes_checked": [],
                "attributions_preserved": ["public briefing"],
                "unsupported_claims_removed": [],
            },
        )
        self.assertEqual(result["status"], "passed")

    def test_incomplete_audit_lists_do_not_rewrite_supported_narration(self) -> None:
        narration, source = valid_material()
        narration = narration.replace("The company", 'The company said "privacy update"', 1)
        source += ' The source says "privacy update" happened in 2026.'
        words = narration.split()
        words[30] = "2026"
        result = validate_narration(
            title="A supported technology update",
            narration=" ".join(words),
            source_text=source,
            factual_review={
                "names_entities_checked": ["OpenAI"],
                "numbers_dates_checked": [],
                "quotes_checked": [],
                "attributions_preserved": [],
                "unsupported_claims_removed": [],
            },
        )
        self.assertEqual(result["status"], "passed")

    def test_ungrounded_audit_metadata_is_discarded_without_rewriting(self) -> None:
        narration, source = valid_material()
        result = validate_narration(
            title="A supported technology update",
            narration=narration,
            source_text=source,
            factual_review={
                "names_entities_checked": ["OpenAI"],
                "numbers_dates_checked": ["2026-07-31T21:35:56+00:00"],
                "quotes_checked": [],
                "attributions_preserved": ["public briefing"],
                "unsupported_claims_removed": [],
            },
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["factual_review"]["numbers_dates_checked"], [])
        self.assertEqual(
            result["discarded_ungrounded_review_items"]["numbers_dates_checked"],
            ["2026-07-31T21:35:56+00:00"],
        )

    def test_trim_narration_preserves_paragraph_breaks(self) -> None:
        sentence = "This supported sentence contains exactly seven useful words."
        per_para = (MAX_NARRATION_WORDS // (3 * word_count(sentence))) + 4
        para = " ".join([sentence] * per_para)
        overlong = f"{para}\n\n{para}\n\n{para}"

        trimmed = trim_narration_to_word_limit(overlong)
        self.assertLessEqual(word_count(trimmed), MAX_NARRATION_WORDS)
        self.assertGreaterEqual(word_count(trimmed), MIN_NARRATION_WORDS)
        self.assertIn("\n\n", trimmed)

        short_multi_para = f"{sentence} {sentence}\n\n{sentence}"
        self.assertEqual(
            trim_narration_to_word_limit(short_multi_para, max_words=100),
            short_multi_para,
        )


if __name__ == "__main__":
    unittest.main()
