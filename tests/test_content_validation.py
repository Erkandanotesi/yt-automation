from __future__ import annotations

import unittest

from content_validation import (
    ContentValidationError,
    trim_narration_to_word_limit,
    validate_narration,
    word_count,
)


def valid_material() -> tuple[str, str]:
    sentence = (
        "The company described its privacy update in a public briefing and "
        "said the change would remain optional for customers."
    )
    narration = " ".join([sentence] * 8)
    source = (
        "OpenAI published a detailed source document. "
        + " ".join([sentence] * 10)
        + " OpenAI privacy update public briefing."
    )
    return narration, source


class ContentValidationTests(unittest.TestCase):
    def test_overlong_narration_is_trimmed_at_sentence_boundary_locally(self) -> None:
        sentence = "This supported sentence contains exactly seven useful words."
        overlong = " ".join([sentence] * 30)
        trimmed = trim_narration_to_word_limit(overlong)
        self.assertLessEqual(word_count(trimmed), 200)
        self.assertGreaterEqual(word_count(trimmed), 150)
        self.assertTrue(trimmed.endswith("words."))

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
        self.assertGreaterEqual(result["word_count"], 150)
        self.assertLessEqual(result["word_count"], 200)

    def test_wrong_word_count_is_rejected(self) -> None:
        _, source = valid_material()
        with self.assertRaisesRegex(ContentValidationError, "required range"):
            validate_narration(
                title="Too short",
                narration="This is far too short.",
                source_text=source,
            )

    def test_unsupported_number_is_rejected(self) -> None:
        narration, source = valid_material()
        words = narration.split()
        words[20] = "987654"
        with self.assertRaisesRegex(ContentValidationError, "numbers absent"):
            validate_narration(
                title="Unsupported number",
                narration=" ".join(words),
                source_text=source,
            )

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


if __name__ == "__main__":
    unittest.main()
