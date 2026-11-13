from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from file_utils import sha256_file
from publish_stage import (
    PublishSettings,
    PublishingCopy,
    _lead_story,
    _local_publishing_fallback,
    _publishing_prompt,
    archive_thumbnail_asset,
    build_chapters,
    build_description,
    chapter_block,
    prepare_ai_rendered_thumbnail,
    prepare_stock_thumbnail_background,
    thumbnail_generation_prompt,
    update_publish_title,
    validate_custom_youtube_title,
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
    def test_stock_thumbnail_background_uses_selected_lead_media_without_ai(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "selected_stock.png"
            source.write_bytes(b"selected-stock-image")
            output_dir = root / "09_publish"
            output_dir.mkdir()
            media_dir = root / "05_visuals"
            media_dir.mkdir()
            (media_dir / "media_plan.json").write_text(
                json.dumps(
                    {
                        "quality": {"status": "passed"},
                        "stories": [
                            {
                                "story_id": "story_1",
                                "windows": [
                                    {
                                        "window_id": "window_1",
                                        "start": 0,
                                        "duration": 5,
                                        "selected_asset": {
                                            "absolute_path": str(source),
                                            "sha256": sha256_file(source),
                                            "kind": "image",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def render(command: list[str], _: str) -> None:
                Path(command[-1]).write_bytes(b"stock-thumbnail-background")

            with (
                patch("publish_stage._run", side_effect=render) as run,
                patch(
                    "publish_stage.probe_image",
                    return_value={"width": 1280, "height": 720},
                ),
            ):
                result = prepare_stock_thumbnail_background(
                    video_dir=root,
                    output_dir=output_dir,
                    lead={"story_id": "story_1"},
                )

            self.assertEqual(result.name, "thumbnail_stock_background.png")
            self.assertTrue(result.is_file())
            self.assertIn(str(source), run.call_args.args[0])
            manifest = json.loads(
                (output_dir / "thumbnail_stock_background.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["generation_provider"],
                "stock_or_archive_media",
            )

    def test_editorial_lead_may_have_a_lower_ai_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            narration = root / "lead.txt"
            source = root / "lead-original.txt"
            narration.write_text("Selected lead narration.", encoding="utf-8")
            source.write_text("Selected lead source article.", encoding="utf-8")
            project = {
                "lead_story_selection_required": True,
                "lead_story_selection": {
                    "status": "selected",
                    "selected_story_id": "story_low_score",
                },
                "stories": [
                    {
                        "story_id": "story_low_score",
                        "order": 1,
                        "score": 70,
                        "text": str(narration),
                        "original": str(source),
                    },
                    {"story_id": "story_high_score", "order": 2, "score": 99},
                ],
            }
            storyboard = {
                "stories": [
                    {
                        "story_id": "story_low_score",
                        "editorial_lead": True,
                        "images": [],
                    }
                ]
            }

            lead, planned, lead_narration, lead_source = _lead_story(
                project,
                storyboard,
            )

        self.assertEqual(lead["story_id"], "story_low_score")
        self.assertTrue(planned["editorial_lead"])
        self.assertEqual(lead_narration, "Selected lead narration.")
        self.assertEqual(lead_source, "Selected lead source article.")

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
            "Claude Reached",
        )
        self.assertLessEqual(len(repaired.recommended_thumbnail_text), 22)
        self.assertIn(repaired.recommended_thumbnail_text, concept.text_options)
        self.assertTrue(all(len(option) <= 22 for option in concept.text_options))

    def test_verbose_editorial_title_is_trimmed_without_breaking_recommendation(self) -> None:
        payload = publishing_copy().model_dump(mode="json")
        verbose = (
            "Google Is Building a Vast New Face Recognition Database for Advertising"
        )
        payload["title_candidates"][0]["title"] = verbose
        payload["recommended_title"] = verbose

        repaired = PublishingCopy.model_validate(payload)

        self.assertLessEqual(len(repaired.recommended_title), 65)
        self.assertIn(
            repaired.recommended_title,
            [item.title for item in repaired.title_candidates],
        )

    def test_prompts_demand_distinct_short_hooks_and_breathing_room(self) -> None:
        project = {"stories": [{"order": 1, "title": "Google face data"}]}
        lead = {
            "order": 1,
            "title": "Google face data",
            "source_title": "Google seeks additional face data",
            "score": 9,
            "ranking_reason": "High audience impact.",
        }
        planned = {"images": []}
        copy_prompt = _publishing_prompt(
            project,
            lead,
            planned,
            "Google requested face data from users.",
            "Google requested face data from users for a supported feature.",
        )
        image_prompt = thumbnail_generation_prompt(
            lead=lead,
            planned=planned,
            narration="Google requested face data from users.",
            publishing_copy=publishing_copy(),
            headline="YOUR FACE DATA",
        )

        self.assertIn("5 to 14 words", copy_prompt)
        self.assertIn("Use sentence case", copy_prompt)
        self.assertIn("conversational mini-story", copy_prompt)
        self.assertIn("Use one deliberate ellipsis", copy_prompt)
        self.assertIn("at most 1 or 2 candidates", copy_prompt)
        self.assertIn("symbol/icon only", copy_prompt)
        self.assertIn("no slash, vertical bar", copy_prompt)
        self.assertIn("Did [Company] just [Consequence]...?", copy_prompt)
        self.assertIn("Never fabricate\n  first-person reporting", copy_prompt)
        self.assertIn("DeepSeek is back... and Silicon Valley is terrified", copy_prompt)
        self.assertIn("Claude is definitely not conscious...", copy_prompt)
        self.assertIn("1 to 4 word phrase", copy_prompt)
        self.assertIn("YOUR FACE DATA", copy_prompt)
        self.assertIn("recognizable key person on the right", copy_prompt)
        self.assertIn("public face of the primary mentioned company", copy_prompt)
        self.assertIn("not a fixed\n  series pose", copy_prompt)
        self.assertIn("broad pose vocabulary", copy_prompt)
        self.assertIn("Never default every person", copy_prompt)
        self.assertIn("company/product logo near the", copy_prompt)
        self.assertIn("one dominant word or compact payoff phrase", copy_prompt)
        self.assertIn("#231F20", copy_prompt)
        self.assertIn("Never tighten tracking", image_prompt)
        self.assertIn("Do not impose", image_prompt)
        self.assertNotIn("LEFT 46%", image_prompt)
        self.assertIn("completely new composition", image_prompt)
        self.assertIn("No previous", image_prompt)
        self.assertIn("story image is supplied", image_prompt)
        self.assertIn("phone-size readability", image_prompt)
        self.assertIn("recognizable key person on the right", image_prompt)
        self.assertIn("neutral brand-associated visual anchor", image_prompt)
        self.assertIn("POSE DIVERSITY", image_prompt)
        self.assertIn("reference set's full range", image_prompt)
        self.assertIn("Hands may remain\nvisible and expressive", image_prompt)
        self.assertIn("do not hide them by default", image_prompt)
        self.assertIn("never invent a prop", image_prompt)
        self.assertIn("official logo or wordmark", image_prompt)
        self.assertIn("standalone symbol/icon only", image_prompt)
        self.assertIn("never repeat the brand in smaller text", image_prompt)
        self.assertIn("flat vivid signal red", image_prompt)
        self.assertIn("#F21F3D", image_prompt)
        self.assertIn("#FFD000", image_prompt)
        self.assertIn("Choose\nthe emphasis color from the approved story context", image_prompt)
        self.assertIn("Do not\ndefault to red", image_prompt)
        self.assertIn("#F4EFD8", image_prompt)
        self.assertIn("tight, subtle lower-right drop shadow", image_prompt)
        self.assertIn("Do not use a\ndeep shadow", image_prompt)
        self.assertIn("Panton Rust Black Base", image_prompt)
        self.assertIn("same Panton Rust Black Base upper-line", image_prompt)
        self.assertIn("do not switch a cream line to a pixel", image_prompt)
        self.assertIn("#231F20 (RGB 35, 31, 32)", image_prompt)
        self.assertIn("no vignette", image_prompt)
        self.assertIn('Never add\n"THE CODE REPORT"', image_prompt)

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
            self.assertIn("headline on the left", calls[0])
            self.assertNotIn("LEFT 46%", calls[0])

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
            with patch("publish_stage.ASSETS_ROOT", root / "Assets"):
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

    def test_update_publish_title_synchronizes_all_publish_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory) / "Video 42"
            publish_dir = video_dir / "09_publish"
            publish_dir.mkdir(parents=True)
            archive_meta = Path(directory) / "archived_thumbnail.metadata.json"
            archive_meta.write_text(
                json.dumps({"youtube_title": "Original Title"}),
                encoding="utf-8",
            )
            (publish_dir / "title.txt").write_text("Original Title", encoding="utf-8")
            (publish_dir / "publish_package.json").write_text(
                json.dumps(
                    {
                        "recommended_title": "Original Title",
                        "title_candidates": [
                            {"title": "Original Title"},
                            {"title": "Candidate Two Title"},
                        ],
                        "quality": {
                            "recommended_title_within_100_characters": True,
                            "recommended_title_is_candidate": True,
                        },
                        "grounding": {"recommended_title_characters": 14},
                        "thumbnail_asset_archive": {
                            "metadata_file": str(archive_meta),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (publish_dir / "youtube_upload_manifest.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "snippet": {
                                "title": "Original Title",
                                "description": "Episode description",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (video_dir / "state.json").write_text(
                json.dumps(
                    {
                        "stages": {
                            "publish": {
                                "status": "PUBLISH_READY",
                                "title": "Original Title",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("publish_stage.project_dir", return_value=video_dir):
                updated = update_publish_title(
                    42,
                    "  Custom Edited YouTube Title Before Upload  ",
                )

            self.assertTrue(updated["changed"])
            self.assertEqual(updated["previous_title"], "Original Title")
            self.assertEqual(
                updated["title"],
                "Custom Edited YouTube Title Before Upload",
            )
            self.assertEqual(
                (publish_dir / "title.txt").read_text(encoding="utf-8"),
                "Custom Edited YouTube Title Before Upload",
            )
            manifest = json.loads(
                (publish_dir / "youtube_upload_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["metadata"]["snippet"]["title"],
                "Custom Edited YouTube Title Before Upload",
            )
            package = json.loads(
                (publish_dir / "publish_package.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                package["recommended_title"],
                "Custom Edited YouTube Title Before Upload",
            )
            self.assertFalse(package["quality"]["recommended_title_is_candidate"])
            self.assertEqual(
                package["grounding"]["recommended_title_characters"],
                len("Custom Edited YouTube Title Before Upload"),
            )
            state = json.loads(
                (video_dir / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["stages"]["publish"]["title"],
                "Custom Edited YouTube Title Before Upload",
            )
            archived = json.loads(archive_meta.read_text(encoding="utf-8"))
            self.assertEqual(
                archived["youtube_title"],
                "Custom Edited YouTube Title Before Upload",
            )

    def test_validate_custom_youtube_title_rejects_empty_and_overlong_titles(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            validate_custom_youtube_title("   ")
        with self.assertRaisesRegex(ValueError, "100 characters or fewer"):
            validate_custom_youtube_title("A" * 101)
        self.assertEqual(
            validate_custom_youtube_title("New Chip Architecture | Tech Reports"),
            "New Chip Architecture",
        )


if __name__ == "__main__":
    unittest.main()
