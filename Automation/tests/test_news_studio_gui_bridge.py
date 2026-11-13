from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import news_studio_gui_bridge as bridge


class NewsStudioGuiBridgeTests(unittest.TestCase):
    def test_render_progress_line_becomes_structured_gui_event(self) -> None:
        output_stream = io.StringIO()
        progress = bridge.GuiProgress(44)
        progress.current_stage = "render"
        line = (
            bridge.RENDER_PROGRESS_PREFIX
            + json.dumps(
                {
                    "phase": "visuals",
                    "detail": "Rendered visual segments: 10/61.",
                    "completed": 10,
                    "total": 61,
                }
            )
        )

        with patch.object(bridge, "_transport_out", output_stream):
            progress.write_subprocess_line(line)

        event = json.loads(output_stream.getvalue())
        self.assertEqual(event["type"], "render_progress")
        self.assertEqual(event["stage"], "render")
        self.assertEqual(event["completed"], 10)
        self.assertEqual(event["total"], 61)

    def test_malformed_render_progress_line_remains_visible_as_log(self) -> None:
        output_stream = io.StringIO()
        progress = bridge.GuiProgress(44)
        progress.current_stage = "render"

        with patch.object(bridge, "_transport_out", output_stream):
            progress.write_subprocess_line(bridge.RENDER_PROGRESS_PREFIX + "not-json")

        event = json.loads(output_stream.getvalue())
        self.assertEqual(event["type"], "log")
        self.assertIn("not-json", event["message"])

    def test_choice_round_trip_preserves_options_and_extra_choice_arguments(self) -> None:
        input_stream = io.StringIO(
            json.dumps(
                {
                    "type": "choice_response",
                    "choice": "2",
                    "fast_defaults": False,
                }
            )
            + "\n"
        )
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", input_stream),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(os.environ, {"AUTOMATION_FAST_DEFAULTS": "0"}),
        ):
            selected = bridge.gui_timed_choice(
                "Choose the lead story.",
                {"1": "First", "2": "Second"},
                default="1",
                timeout_seconds=120,
                separator="-",
                detail_items=["ElevenLabs voiceovers: up to 5"],
            )

        self.assertEqual(selected, "2")
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(events[0]["type"], "choice_requested")
        self.assertEqual(events[0]["options"]["2"], "Second")
        self.assertEqual(events[0]["detail_items"], ["ElevenLabs voiceovers: up to 5"])
        self.assertEqual(events[-1]["choice"], "2")

    def test_fast_defaults_select_without_waiting_for_stdin(self) -> None:
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", io.StringIO()),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(os.environ, {"AUTOMATION_FAST_DEFAULTS": "1"}),
        ):
            selected = bridge.gui_timed_choice(
                "Continue?",
                {"1": "Continue", "0": "Stop"},
                default="1",
                timeout_seconds=120,
            )

        self.assertEqual(selected, "1")
        event = json.loads(output_stream.getvalue())
        self.assertEqual(event["type"], "choice_selected")
        self.assertTrue(event["automatic"])

    def test_direct_new_video_selects_create_without_waiting_for_stdin(self) -> None:
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", io.StringIO()),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(
                os.environ,
                {
                    "AUTOMATION_FAST_DEFAULTS": "1",
                    "NEWS_STUDIO_GUI_DIRECT_NEW_VIDEO": "1",
                },
            ),
        ):
            selected = bridge.gui_timed_choice(
                "Choose the production action.",
                {"1": "Create a new video", "2": "Continue"},
                default="1",
                timeout_seconds=120,
            )

        self.assertEqual(selected, "1")
        event = json.loads(output_stream.getvalue())
        self.assertEqual(event["type"], "choice_selected")
        self.assertTrue(event["automatic"])
        self.assertTrue(event["preference"])

    def test_direct_resume_selects_latest_even_when_saved_default_is_create(self) -> None:
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", io.StringIO()),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(
                os.environ,
                {
                    "AUTOMATION_FAST_DEFAULTS": "1",
                    "NEWS_STUDIO_GUI_DIRECT_NEW_VIDEO": "0",
                    "NEWS_STUDIO_GUI_DIRECT_RESUME_LATEST": "1",
                    "NEWS_STUDIO_GUI_USE_DEFAULT_PRODUCTION_ACTION": "true",
                },
            ),
        ):
            selected = bridge.gui_timed_choice(
                "Choose the production action.",
                {"1": "Create a new video", "2": "Continue latest"},
                default="1",
                timeout_seconds=120,
            )

        self.assertEqual(selected, "2")
        event = json.loads(output_stream.getvalue())
        self.assertTrue(event["automatic"])
        self.assertTrue(event["preference"])

    def test_direct_resume_automatically_uses_recommended_resume_target(self) -> None:
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", io.StringIO()),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(
                os.environ,
                {
                    "AUTOMATION_FAST_DEFAULTS": "0",
                    "NEWS_STUDIO_GUI_DIRECT_RESUME_LATEST": "1",
                    "NEWS_STUDIO_GUI_USE_DEFAULT_RESUME_SECTION": "false",
                },
            ),
        ):
            selected = bridge.gui_timed_choice(
                "Choose the section to continue from.",
                {"1": "Rebuild", "u": "Upload completed package"},
                default="u",
                timeout_seconds=120,
            )

        self.assertEqual(selected, "u")
        event = json.loads(output_stream.getvalue())
        self.assertTrue(event["automatic"])
        self.assertTrue(event["preference"])

    def test_indefinite_checkpoint_ignores_fast_defaults_and_emits_folder_action(self) -> None:
        input_stream = io.StringIO(
            json.dumps({"type": "choice_response", "choice": "1"}) + "\n"
        )
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", input_stream),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(os.environ, {"AUTOMATION_FAST_DEFAULTS": "1"}),
        ):
            selected = bridge.gui_timed_choice(
                "Generate the files, then continue.",
                {"1": "Validate and continue", "0": "Stop"},
                default="1",
                timeout_seconds=0,
                wait_indefinitely=True,
                asset_path="C:/Video 33/04_ai_story_videos",
                asset_label="Open folder",
            )

        self.assertEqual(selected, "1")
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(events[0]["type"], "choice_requested")
        self.assertTrue(events[0]["wait_indefinitely"])
        self.assertEqual(events[0]["asset_label"], "Open folder")
        self.assertFalse(events[-1]["automatic"])

    def test_cancel_response_stops_without_selecting_a_default(self) -> None:
        input_stream = io.StringIO(
            json.dumps({"type": "choice_response", "cancel": True}) + "\n"
        )
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", input_stream),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(os.environ, {"AUTOMATION_FAST_DEFAULTS": "0"}),
        ):
            with self.assertRaises(KeyboardInterrupt):
                bridge.gui_timed_choice(
                    "Create a new video?",
                    {"1": "Create", "2": "Continue"},
                    default="1",
                    timeout_seconds=120,
                )
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(events[-1]["type"], "choice_cancelled")

    def test_preview_review_emits_openable_asset_and_waits_for_gui_choice(self) -> None:
        input_stream = io.StringIO(
            json.dumps({"type": "choice_response", "choice": "0"}) + "\n"
        )
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", input_stream),
            patch.object(bridge, "_transport_out", output_stream),
            patch.object(bridge, "_current_video", 34),
            patch.object(bridge.news_studio, "project_dir", return_value=Path("C:/Video 34")),
            patch.dict(
                os.environ,
                {
                    "AUTOMATION_FAST_DEFAULTS": "0",
                    "NEWS_STUDIO_GUI_USE_DEFAULT_PREVIEW": "false",
                },
            ),
        ):
            selected = bridge.gui_timed_choice(
                "Approve the review preview? C:/Video 34/07_effects/preview.mp4",
                {"1": "Approve and render", "0": "Stop before final render"},
                default="1",
                timeout_seconds=120,
            )

        self.assertEqual(selected, "0")
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        request = events[0]
        self.assertEqual(request["type"], "choice_requested")
        self.assertTrue(request["auto_open_asset"])
        self.assertEqual(
            request["asset_path"],
            str(Path("C:/Video 34/07_effects/milestone6_reference_60s.mp4")),
        )
        self.assertFalse(events[-1]["automatic"])

    def test_saved_default_preferences_select_displayed_defaults_without_dialog(self) -> None:
        cases = [
            (
                "Which news should be first and drive the YouTube title and thumbnail?",
                {"1": "Top-ranked story", "2": "Second story"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_LEAD_STORY",
                "1",
            ),
            (
                "Manual video separator checkpoint.",
                {"1": "Wait for manually created separators", "2": "Skip separators"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_SEPARATORS",
                "2",
            ),
            (
                "Will you add AI-generated videos for the story sections?",
                {"1": "Yes", "2": "No"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_AI_VIDEOS",
                "2",
            ),
            (
                "Approve the review preview? C:/Video 34/07_effects/preview.mp4",
                {"1": "Approve and render", "0": "Stop before final render"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_PREVIEW",
                "1",
            ),
            (
                "Review or change the YouTube title before upload.",
                {"1": "Keep current title: Example", "2": "Use candidate: Other"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_TITLE",
                "1",
            ),
            (
                "Upload the video, thumbnail, title, description, and chapters to YouTube?",
                {"1": "Upload to YouTube", "0": "Do not upload"},
                "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_UPLOAD",
                "1",
            ),
        ]
        for prompt, options, preference, default in cases:
            with self.subTest(prompt=prompt):
                output_stream = io.StringIO()
                environment = {
                    "AUTOMATION_FAST_DEFAULTS": "0",
                    preference: "true",
                }
                with (
                    patch.object(bridge, "_transport_in", io.StringIO()),
                    patch.object(bridge, "_transport_out", output_stream),
                    patch.dict(os.environ, environment),
                ):
                    selected = bridge.gui_timed_choice(
                        prompt,
                        options,
                        default=default,
                        timeout_seconds=120,
                    )
                self.assertEqual(selected, default)
                event = json.loads(output_stream.getvalue())
                self.assertEqual(event["type"], "choice_selected")
                self.assertTrue(event["automatic"])
                self.assertTrue(event["preference"])

    def test_progress_emits_stage_wait_and_network_states(self) -> None:
        output_stream = io.StringIO()
        with patch.object(bridge, "_transport_out", output_stream):
            progress = bridge.GuiProgress(32)
            progress.start_stage("news", 0)
            progress.network_waiting(1, 15)
            progress.network_resumed()
            progress.waiting(0)

        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(
            [event["type"] for event in events],
            ["stage_started", "network_waiting", "network_resumed", "stage_waiting"],
        )
        self.assertEqual(events[0]["video_number"], 32)
        self.assertEqual(events[1]["retry_seconds"], 15)

    def test_editable_title_choice_emits_editor_metadata_and_records_custom_text(self) -> None:
        input_stream = io.StringIO(
            json.dumps(
                {
                    "type": "choice_response",
                    "choice": "1",
                    "fast_defaults": False,
                    "custom_text": "Edited Title From GUI Overlay",
                }
            )
            + "\n"
        )
        output_stream = io.StringIO()
        with (
            patch.object(bridge, "_transport_in", input_stream),
            patch.object(bridge, "_transport_out", output_stream),
            patch.dict(
                os.environ,
                {
                    "AUTOMATION_FAST_DEFAULTS": "0",
                    "NEWS_STUDIO_GUI_USE_DEFAULT_YOUTUBE_TITLE": "false",
                },
            ),
        ):
            selected = bridge.gui_timed_choice(
                "Review or change the YouTube title before upload.",
                {
                    "1": "Keep current title: Generated Default Title",
                    "2": "Use candidate: Candidate Two",
                    "c": "Enter a custom YouTube title",
                },
                default="1",
                timeout_seconds=120,
                editable_text="Generated Default Title",
                editable_label="YouTube title",
                editable_max_length=100,
            )

        self.assertEqual(selected, "1")
        self.assertEqual(
            bridge.news_studio.consume_last_choice_custom_text(),
            "Edited Title From GUI Overlay",
        )
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(events[0]["type"], "choice_requested")
        self.assertEqual(events[0]["editable_text"], "Generated Default Title")
        self.assertEqual(events[0]["editable_label"], "YouTube title")
        self.assertEqual(events[0]["editable_max_length"], 100)
        self.assertNotIn("c", events[0]["options"])
        self.assertEqual(
            events[-1]["custom_text"],
            "Edited Title From GUI Overlay",
        )


if __name__ == "__main__":
    unittest.main()
