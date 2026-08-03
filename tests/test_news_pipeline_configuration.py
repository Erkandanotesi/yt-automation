from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import news_pipeline
from file_locations import AUTOMATION_ROOT, TECH_FEEDS_FILE
from file_utils import sha256_file


class NewsPipelineConfigurationTests(unittest.TestCase):
    def test_tech_feed_list_excludes_audited_bad_sources(self) -> None:
        root = ET.parse(TECH_FEEDS_FILE).getroot()
        urls = {
            element.attrib["xmlUrl"]
            for element in root.iter()
            if element.attrib.get("xmlUrl")
        }
        blocked = {
            "https://aiweekly.co/feed",
            "https://www.cnet.com/rss/news/",
            "https://www.youtube.com/feeds/videos.xml?user=CNETTV",
            "https://news.ycombinator.com/rss",
            "https://lifehacker.com/rss",
            "https://readwrite.com/feed/",
            "https://rss.slashdot.org/Slashdot/slashdotMain",
            "https://stratechery.com/feed/",
            "https://theverge.com/rss/tech/index.xml",
            "https://www.engadget.com/rss.xml",
            "https://www.tomshardware.com/feeds/all",
        }
        self.assertTrue(blocked.isdisjoint(urls))
        self.assertIn(
            "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
            urls,
        )
        self.assertIn(
            "https://www.theverge.com/rss/cyber-security/index.xml",
            urls,
        )

    def test_clustered_newsletters_are_rejected_but_atomic_news_is_allowed(self) -> None:
        clustered_titles = (
            "AI Weekly Issue #517: What happened across the industry",
            "The Download: tricking LLMs, and reviving geothermal plants",
            "Open Model Wars + Google Memoir + Substack Slop Fight",
            "The latest AI news we announced in June 2026",
        )
        for title in clustered_titles:
            self.assertTrue(
                news_pipeline.clustered_content_reasons({"title": title}),
                title,
            )
        self.assertEqual(
            news_pipeline.clustered_content_reasons({
                "title": "Judge denies xAI request to block Minnesota law"
            }),
            [],
        )

    def test_deals_roundup_and_affiliate_price_post_are_promotional(self) -> None:
        roundup = {
            "title": "I'm a Deals Writer, and These Are My Top 10 Tech Deals This Week",
            "rss_text": (
                "We may earn a commission from links on this page. As Lifehacker's "
                "deals writer, I've gathered this week's sales highlights. "
                "Laptop $999.96 Get Deal Get Deal. TV $548.99 Save $451. "
                "Monitor $314.99 at Amazon with 43% off."
            ),
        }
        price_post = {
            "title": "Google Pixel Watch 4 LTE Falls to $349.99",
            "rss_text": (
                "We may earn a commission from links on this page. The watch is "
                "$349.99, originally $449. Get Deal Get Deal at Amazon."
            ),
        }

        self.assertTrue(news_pipeline.promotional_content_reasons(roundup))
        self.assertTrue(news_pipeline.promotional_content_reasons(price_post))

    def test_reg_ad_and_news_about_business_deals_are_not_promotion_signals(self) -> None:
        ordinary_news = {
            "title": "Microsoft changes privacy controls after regulator review",
            "rss_text": (
                "REG AD The regulator said Microsoft's advertising platforms must "
                "offer clearer consent controls. No products are being sold here."
            ),
        }
        corporate_transaction = {
            "title": "Chip company signs $10 billion acquisition deal",
            "rss_text": (
                "The boards approved a corporate acquisition subject to regulatory "
                "review and shareholder approval."
            ),
        }

        self.assertEqual(
            news_pipeline.promotional_content_reasons(ordinary_news),
            [],
        )
        self.assertEqual(
            news_pipeline.promotional_content_reasons(corporate_transaction),
            [],
        )

    def test_ai_cannot_restore_a_locally_detected_promotional_candidate(self) -> None:
        now = news_pipeline.datetime.now(news_pipeline.timezone.utc)
        candidates = [
            {
                "title": "Top 10 Tech Deals This Week",
                "source": "Deals Feed",
                "published": now,
                "rss_text": (
                    "We may earn a commission from links. Laptop $999, TV $500, "
                    "phone $300, watch $200. Get Deal Get Deal."
                ),
                "rough_score": 99,
            },
            {
                "title": "Researchers disclose a major processor vulnerability",
                "source": "Security Feed",
                "published": now,
                "rss_text": "Researchers documented the flaw and mitigations.",
                "rough_score": 80,
            },
        ]
        ai_result = news_pipeline.RankingResult(
            selected=[
                news_pipeline.RankedNews(
                    index=0,
                    score=95,
                    reason="Incorrectly selected promotion.",
                ),
                news_pipeline.RankedNews(
                    index=1,
                    score=85,
                    reason="Substantive security news.",
                ),
            ]
        )
        with (
            patch.object(news_pipeline, "TOP_N", 1),
            patch.object(news_pipeline, "RANKING_RESERVE", 1),
            patch.object(news_pipeline, "structured_ai_call", return_value=ai_result),
        ):
            ranked = news_pipeline.rank_candidates(candidates, "Technology news")

        self.assertEqual([item.index for item in ranked], [1])

    def test_tech_summary_prompt_uses_requested_copy(self) -> None:
        result = news_pipeline.NewsSummary(
            title_en="A factual technology story",
            summary_en=" ".join(["supported"] * 175),
            factual_review=news_pipeline.FactualReview(),
        )
        candidate = {
            "title": "Original technology headline",
            "source": "Example Feed",
            "published": news_pipeline.datetime.now(news_pipeline.timezone.utc),
        }
        with (
            patch.object(news_pipeline, "IS_GAMING", False),
            patch.object(
                news_pipeline,
                "structured_ai_call",
                return_value=result,
            ) as ai_call,
            patch.object(news_pipeline, "validate_narration", return_value={}),
        ):
            news_pipeline.summarize_article(candidate, "Complete supplied article.")

        prompt = ai_call.call_args.args[0]
        self.assertIn(
            "Write an English YouTube tech-news script from the source.",
            prompt,
        )
        self.assertIn("Make it engaging and\nready for ElevenLabs.", prompt)
        self.assertIn("ORIGINAL TITLE:\nOriginal technology headline", prompt)
        self.assertIn("SOURCE:\nExample Feed", prompt)
        self.assertIn("SOURCE TEXT:\nComplete supplied article.", prompt)
        self.assertNotIn("legally cautious", prompt)
        self.assertNotIn("Before returning", prompt)
        self.assertNotIn("Otherwise sound normal", prompt)

    def test_candidate_pool_balances_publishers_and_defers_similar_headlines(self) -> None:
        now = news_pipeline.datetime.now(news_pipeline.timezone.utc)

        def candidate(title: str, source: str, score: int) -> dict:
            return {
                "title": title,
                "source": source,
                "feed_url": f"https://{source}.example/feed.xml",
                "url": f"https://{source}.example/{score}-{len(title)}",
                "published": now,
                "rss_text": "A substantive technology report.",
                "rough_score": score,
            }

        candidates = [
            candidate("OpenAI launches Atlas AI model for developers", "alpha", 100),
            candidate("Alpha publishes another unrelated chip report", "alpha", 99),
            candidate("Alpha announces a third technology product", "alpha", 98),
            candidate("OpenAI unveils Atlas AI model for developers", "beta", 97),
            candidate("Beta reveals a new battery architecture", "beta", 96),
            candidate("Cybersecurity researchers disclose Orion flaw", "gamma", 95),
        ]

        with patch.object(
            news_pipeline, "NEWS_SELECTION_TITLE_SIMILARITY_THRESHOLD", 0.60
        ):
            diversified = news_pipeline.diversify_candidates(candidates, 3)

        self.assertEqual(
            [news_pipeline.candidate_source_key(item) for item in diversified[:3]],
            ["alpha.example", "beta.example", "gamma.example"],
        )
        self.assertEqual(
            diversified[1]["title"],
            "Beta reveals a new battery architecture",
        )

    def test_ai_ranking_is_completed_with_local_reserves(self) -> None:
        candidates = [
            {
                "title": f"Technology story {index}",
                "source": "Test Feed",
                "published": news_pipeline.datetime.now(news_pipeline.timezone.utc),
                "rss_text": "A substantive technology report.",
                "rough_score": 70 - index,
            }
            for index in range(5)
        ]
        ai_result = news_pipeline.RankingResult(
            selected=[
                news_pipeline.RankedNews(index=0, score=90, reason="Best supplied story")
            ]
        )
        with (
            patch.object(news_pipeline, "TOP_N", 2),
            patch.object(news_pipeline, "RANKING_RESERVE", 2),
            patch.object(news_pipeline, "OPENAI_NEWS_SELECTION_MODEL", "selection-model"),
            patch.object(
                news_pipeline,
                "OPENAI_NEWS_SELECTION_REASONING_EFFORT",
                "medium",
            ),
            patch.object(
                news_pipeline, "structured_ai_call", return_value=ai_result
            ) as ai_call,
        ):
            ranked = news_pipeline.rank_candidates(candidates, "Technology news")
        self.assertEqual(len(ranked), 4)
        self.assertEqual(ranked[0].index, 0)
        self.assertEqual({item.index for item in ranked}, {0, 1, 2, 3})
        self.assertEqual(ai_call.call_args.kwargs["openai_model"], "selection-model")
        self.assertEqual(
            ai_call.call_args.kwargs["openai_reasoning_effort"], "medium"
        )

    def test_local_reserve_cannot_outrank_ai_selected_story(self) -> None:
        now = news_pipeline.datetime.now(news_pipeline.timezone.utc)
        candidates = [
            {
                "title": "Recent benchmark omitted by the editor",
                "source": "General hardware feed",
                "published": now,
                "rss_text": "A benchmark rather than a major news event.",
                "rough_score": 100,
            },
            {
                "title": "Major privacy change selected by the editor",
                "source": "Technology news feed",
                "published": now,
                "rss_text": "A platform changed privacy controls for its users.",
                "rough_score": 60,
            },
        ]
        ai_result = news_pipeline.RankingResult(
            selected=[
                news_pipeline.RankedNews(
                    index=1,
                    score=82,
                    reason="Material privacy news.",
                )
            ]
        )
        with (
            patch.object(news_pipeline, "TOP_N", 1),
            patch.object(news_pipeline, "RANKING_RESERVE", 1),
            patch.object(news_pipeline, "structured_ai_call", return_value=ai_result),
        ):
            ranked = news_pipeline.rank_candidates(candidates, "Technology news")

        self.assertEqual([item.index for item in ranked], [1, 0])
        self.assertLess(ranked[1].score, ranked[0].score)

    def test_incomplete_project_reuses_validated_story_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            narration = root / "01_story.txt"
            source = root / "01_story-orijinal.txt"
            narration.write_text("A previously validated narration.\n", encoding="utf-8")
            source.write_text("A previously validated source article.\n", encoding="utf-8")
            project_info = root / "project_info.json"
            project_info.write_text(json.dumps({
                "status": "incomplete",
                "stories": [{
                    "order": 1,
                    "story_id": "story_saved",
                    "title": "Saved technology story",
                    "source_title": "Saved technology story",
                    "source": "Test Feed",
                    "source_url": "https://example.com/saved",
                    "published_utc": "2026-08-01T12:00:00+00:00",
                    "score": 80,
                    "ranking_reason": "Previously selected and validated.",
                    "text": str(narration),
                    "original": str(source),
                    "narration_sha256": sha256_file(narration),
                    "source_sha256": sha256_file(source),
                    "validation": {"factual_review": {}},
                }],
            }), encoding="utf-8")
            candidates: list[dict] = []
            with patch.object(news_pipeline, "PROJECT_INFO_PATH", project_info):
                restored = news_pipeline.load_incomplete_created_items(candidates)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["story_id"], "story_saved")
        self.assertEqual(len(candidates), 1)

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
