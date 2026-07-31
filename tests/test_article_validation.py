from __future__ import annotations

import unittest

from article_validation import (
    duplicate_source_fingerprints,
    source_matches_title,
    title_source_relevance,
    validate_unique_source_content,
)


GENERIC_REGISTER_PAGE = """
Most popular technology headlines. Russians are posing as Signal support to
launch phishing attacks. Microsoft patches failed to fix on-prem SharePoint.
China upgrades smartphone surveillance tools. Ring changes its privacy stance.
Linux, Debian, Wayland, ransomware, AI infrastructure, and storage news are
also listed on this general publication page.
"""


class ArticleValidationTests(unittest.TestCase):
    def test_correct_rss_article_matches_its_title(self) -> None:
        title = (
            "Hims & Hers accused of sharing health secrets and hiding the "
            "cancel button"
        )
        source = (
            "The Federal Trade Commission is suing telehealth company Hims & "
            "Hers over allegations that it shared customers' sensitive health "
            "information with advertising platforms. The complaint also says "
            "the cancellation option remained buried and that the cancel "
            "button appeared only after several account steps. "
        ) * 4

        result = title_source_relevance(title, source)

        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["match_ratio"], 0.35)

    def test_generic_listing_is_rejected_for_distinct_register_articles(self) -> None:
        titles = [
            "Russian spies take their half-click email attack from Zimbra to Outlook",
            "Hims & Hers accused of sharing health secrets and hiding the cancel button",
            "Microsoft makes Copilot harder to miss in Classic Outlook",
        ]

        self.assertTrue(
            all(
                not source_matches_title(title, GENERIC_REGISTER_PAGE)
                for title in titles
            )
        )

    def test_identical_sources_for_different_stories_are_detected(self) -> None:
        items = [
            {
                "story_id": "story_one",
                "source_text": GENERIC_REGISTER_PAGE,
            },
            {
                "story_id": "story_two",
                "source_text": GENERIC_REGISTER_PAGE,
            },
        ]

        duplicates = duplicate_source_fingerprints(items)
        self.assertEqual(len(duplicates), 1)
        with self.assertRaisesRegex(ValueError, "identical source content"):
            validate_unique_source_content(items)


if __name__ == "__main__":
    unittest.main()
