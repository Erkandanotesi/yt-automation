from __future__ import annotations

import unittest

from article_validation import (
    article_quality_report,
    duplicate_source_fingerprints,
    format_article_text,
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
    def test_long_aggregator_headline_allows_grounded_paraphrase(self) -> None:
        title = "Claude published malicious code online and attacked real companies"
        source = " ".join(
            [
                "Claude was used in an autonomous security experiment affecting companies.",
                "Researchers documented the model's actions, containment controls,",
                "network behavior, safeguards, and the organizations' response in detail.",
            ]
            * 12
        )

        report = title_source_relevance(title, source)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["minimum_match_ratio"], 0.25)

    def test_well_formed_article_passes_quality_gate(self) -> None:
        source = " ".join(
            [
                "Microsoft published a security update for enterprise Windows customers on Tuesday.",
                "The release addresses an authentication weakness reported by independent researchers.",
                "Administrators can install the patch through the company management console this week.",
                "Affected organizations were advised to review access logs before deploying the package.",
                "Researchers said they had not observed widespread exploitation in production networks.",
                "The company credited the reporting team and published technical mitigation guidance.",
                "Cloud deployments receive the protection automatically during their normal maintenance window.",
                "On-premises customers must test compatibility with existing identity infrastructure first.",
                "A later bulletin will document additional indicators for incident response specialists.",
                "Microsoft recommends installing the enterprise security update as soon as practical.",
            ]
        )

        report = article_quality_report(
            "Microsoft publishes enterprise Windows security update",
            source,
        )

        self.assertEqual(report["status"], "passed", report["errors"])

    def test_readable_format_adds_stable_article_paragraphs(self) -> None:
        sentences = [
            f"Microsoft described security change number {index} for enterprise customers."
            for index in range(1, 18)
        ]
        formatted = format_article_text(" ".join(sentences))

        self.assertIn("\n\n", formatted)
        self.assertTrue(all(line.strip() for line in formatted.split("\n\n")))
        self.assertEqual(
            format_article_text(formatted),
            formatted,
        )

    def test_error_page_is_rejected_even_when_padded_with_title_terms(self) -> None:
        source = (
            "Access denied. Verify you are human. Enable JavaScript and cookies "
            "to continue. Cloudflare Ray ID 123. "
            + "OpenAI enterprise security update customers. " * 40
        )
        report = article_quality_report(
            "OpenAI releases enterprise security update",
            source,
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("broken_page_marker" in item for item in report["errors"]))

    def test_repeated_non_article_prose_is_rejected(self) -> None:
        sentence = "Nvidia announced a graphics update for laptop customers worldwide."
        report = article_quality_report(
            "Nvidia announces laptop graphics update",
            " ".join([sentence] * 20),
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("repeated_prose" in item for item in report["errors"]))

    def test_paywall_teaser_is_rejected(self) -> None:
        source = (
            "Apple described a new privacy feature for application developers. " * 15
            + "Subscribe to continue reading."
        )
        report = article_quality_report(
            "Apple describes privacy feature for developers",
            source,
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("paywall" in item for item in report["errors"]))

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
