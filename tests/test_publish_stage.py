from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish_stage import (
    PublishSettings,
    PublishingCopy,
    _local_publishing_fallback,
    archive_thumbnail_asset,
    build_chapters,
    build_description,
    chapter_block,
    prepare_ai_rendered_thumbnail,
)


def publishing_copy() -> PublishingCopy:
    return PublishingCopy.model_validate(
        {
            "title_candidates": [
                {
                    "title": f"Opening One Outlook Email May Run Attacker JavaScript {i}",
                    "rationale": "Grounded and specific title rationale for this episode.",
                    "clarity": 8,
                    "curiosity": 8,
                    "specificity": 9,
                    "factual_risk": 2,
                }
                for i in range(1, 6)
            ],
            "recommended_title": (
                "Opening One Outlook Email May Run Attacker JavaScript 1"
            ),
            "recommended_title_reason": (
                "It describes the supported mechanism while preserving uncertainty."
            ),
            "thumbnail_concepts": [
                {
                    "concept_id": "concept_1",
                    "visual": "A bright generic webmail window with a clear malicious code effect.",
                    "text_options": ["NO CLICK NEEDED", "OPENED = EXPOSED"],
                    "rationale": "It makes the email-opening mechanism readable at phone size.",
                },
                {
                    "concept_id": "concept_2",
                    "visual": "A bright office monitor with a warning over an opened email.",
                    "text_options": ["EMAIL TRAP", "OPEN AND EXPOSED"],
                    "rationale": "It remains factual and creates a single strong focal point.",
                },
            ],
            "recommended_concept_id": "concept_1",
            "recommended_thumbnail_text": "NO CLICK NEEDED",
            "lead_description": (
                "A reported Outlook Web Access attack may execute attacker JavaScript "
                "when a booby-trapped email is opened in an affected environment."
            ),
            "episode_description": (
                "This episode also covers health privacy, age assurance, generated "
                "code policy, and Microsoft Copilot changes."
            ),
            "tags": [f"technology news {i}" for i in range(8)],
            "hashtags": ["#TechNews", "#Cybersecurity", "#Outlook"],
        }
    )


class PublishStageTests(unittest.TestCase):
    def test_inconsistent_ai_recommendations_are_repaired_locally(self) -> None:
        payload = publishing_copy().model_dump(mode="json")
        payload["recommended_title"] = "A separate title the model forgot to list"
        payload["recommended_concept_id"] = "concept_3"
        payload["recommended_thumbnail_text"] = "UNLISTED HEADLINE"
        payload["tags"][-1] = payload["tags"][0].upper()

        repaired = PublishingCopy.model_validate(payload)

        self.assertIn(
            repaired.recommended_title,
            [item.title for item in repaired.title_candidates],
        )
        concept = next(
            item
            for item in repaired.thumbnail_concepts
            if item.concept_id == repaired.recommended_concept_id
        )
        self.assertIn(repaired.recommended_thumbnail_text, concept.text_options)
        self.assertEqual(len(repaired.tags), 8)

    def test_oversized_thumbnail_option_is_shortened_before_cross_field_repair(self) -> None:
        payload = publishing_copy().model_dump(mode="json")
        payload["thumbnail_concepts"][0]["text_options"][0] = (
            "Claude Reached Production"
        )
        payload["recommended_thumbnail_text"] = "Claude Reached Production"

        repaired = PublishingCopy.model_validate(payload)
        concept = next(
            item
            for item in repaired.thumbnail_concepts
            if item.concept_id == repaired.recommended_concept_id
        )

        self.assertEqual(
            repaired.recommended_thumbnail_text,
            "Claude Reached Production",
        )
        self.assertLessEqual(len(repaired.recommended_thumbnail_text), 42)
        self.assertIn(repaired.recommended_thumbnail_text, concept.text_options)
        self.assertTrue(all(len(option) <= 42 for option in concept.text_options))

    def test_local_copy_fallback_is_complete_and_grounded(self) -> None:
        project = {
            "stories": [
                {
                    "order": index,
                    "title": title,
                    "source_title": title,
                }
                for index, title in enumerate(
                    [
                        "Claude Published Malicious Code and Attacked Three Companies",
                        "New York Questions Prediction Markets",
                        "Researchers Discover a Browser Security Flaw",
                        "A New AI Model Reaches Developers",
                        "A Major Software Update Changes Privacy Controls",
                    ],
                    start=1,
                )
            ]
        }
        lead = project["stories"][0]
        narration = (
            "Anthropic said its Claude system was involved in a reported security "
            "incident affecting three companies, according to the supplied report."
        )

        copy = _local_publishing_fallback(project, lead, narration)

        self.assertEqual(len(copy.title_candidates), 5)
        self.assertIn("Claude", copy.recommended_title)
        self.assertIn(
            copy.recommended_thumbnail_text,
            copy.thumbnail_concepts[0].text_options,
        )
        self.assertGreaterEqual(len(copy.tags), 8)

    def test_ai_rendered_thumbnail_is_cached_without_second_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lead = {
                "title": "Outlook email attack",
                "source_title": "Outlook Web Access email attack",
            }
            planned = {"images": [{"visual_concept": "Opened malicious email"}]}
            calls = []

            def generator(prompt, settings):
                calls.append(prompt)
                return b"fake-1280x720-png", {"request_id": "request_1", "usage": {}}

            with patch(
                "publish_stage.probe_image",
                return_value={"width": 1280, "height": 720},
            ):
                first = prepare_ai_rendered_thumbnail(
                    output_dir=root,
                    lead=lead,
                    planned=planned,
                    narration="Opening the message may run attacker JavaScript.",
                    publishing_copy=publishing_copy(),
                    headline="OUTLOOK EMAIL TRAP",
                    settings=PublishSettings(),
                    confirm_cost=True,
                    generator=generator,
                )
                second = prepare_ai_rendered_thumbnail(
                    output_dir=root,
                    lead=lead,
                    planned=planned,
                    narration="Opening the message may run attacker JavaScript.",
                    publishing_copy=publishing_copy(),
                    headline="OUTLOOK EMAIL TRAP",
                    settings=PublishSettings(),
                    confirm_cost=False,
                    generator=lambda *_: self.fail("cache should prevent generation"),
                )

            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertIn("OUTLOOK EMAIL TRAP", calls[0])
            self.assertIn("LEFT 46%", calls[0])

    def test_chapters_are_embedded_verbatim_in_description(self) -> None:
        project = {
            "stories": [
                {
                    "order": index,
                    "story_id": f"story_{index}",
                    "title": f"Story {index}",
                    "source_title": f"Source {index}",
                    "source_url": f"https://example.com/{index}",
                }
                for index in range(1, 4)
            ]
        }
        timing = {
            "stories": [
                {
                    "story_id": f"story_{index}",
                    "narration": {"start": start},
                }
                for index, start in enumerate((0.0, 99.824, 199.462), start=1)
            ]
        }
        chapters = build_chapters(project, timing)
        description = build_description(publishing_copy(), project, chapters)

        self.assertEqual(chapter_block(chapters).splitlines()[1], "1:39 Story 2")
        self.assertIn(f"CHAPTERS\n{chapter_block(chapters)}\n\n", description)
        self.assertNotIn("SOURCES", description)

    def test_final_thumbnail_is_archived_with_prompt_and_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final = root / "thumbnail_final.png"
            background = root / "background.png"
            final.write_bytes(b"final-thumbnail")
            background.write_bytes(b"bright-background")
            thumbnails = {
                "final_file": str(final),
                "alignment_template": {
                    "template_id": "tech_news_left_headline_v1",
                    "text_rendering": "local_overlay",
                },
            }
            lead = {
                "story_id": "story_1",
                "title": "Outlook attack",
                "source_title": "Source title",
                "source_url": "https://example.com/story",
            }
            with patch("publish_stage.PROJECTS_ROOT", root):
                archived = archive_thumbnail_asset(
                    video_number=5,
                    thumbnails=thumbnails,
                    publishing_copy=publishing_copy(),
                    lead=lead,
                    background=background,
                    background_prompt="Use a bright, factual, right-weighted composition.",
                    thumbnail_text="NO CLICK NEEDED",
                )

            self.assertTrue(Path(archived["file"]).is_file())
            metadata = Path(archived["metadata_file"]).read_text(encoding="utf-8")
            self.assertIn("tech_news_left_headline_v1", metadata)
            self.assertIn("bright, factual", metadata)


if __name__ == "__main__":
    unittest.main()
