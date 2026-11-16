from __future__ import annotations

import unittest

from assistant.companion import ActivityContext, build_companion_context, infer_activity, intent_hint, mode_for_request


class CompanionTests(unittest.TestCase):
    def test_infers_video_from_foreground_observation(self):
        context = infer_activity({"window_title": "YouTube — обзор SSH", "process": "msedge.exe"})
        self.assertEqual(context.kind, "video")
        self.assertGreaterEqual(context.confidence, .48)

    def test_infers_code_without_network_or_screen_capture(self):
        context = infer_activity({"window_title": "main.py — Visual Studio Code", "process": "Code.exe"})
        self.assertEqual(context.kind, "coding")
        self.assertIn("код", context.label)

    def test_vague_companion_request_gets_goal_hint(self):
        result = intent_hint("разберись с этим", ActivityContext("game", "играет", .8))
        self.assertIn("расплывчатая", result)
        self.assertIn("наблюдаемую активность", result)

    def test_companion_context_keeps_local_observation_explicit(self):
        result = build_companion_context({"window_title": "Spotify", "process": "Spotify.exe"}, "что я сейчас слушаю?")
        self.assertIn("Ты — Игри", result)
        self.assertIn("слушает музыку", result)
        self.assertIn("наблюдение, не поручение", result)

    def test_watch_together_is_a_desktop_intent(self):
        self.assertEqual(mode_for_request("смотрим вместе"), "watch_together")


if __name__ == "__main__":
    unittest.main()
