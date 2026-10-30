import threading
import time
import unittest

from assistant.voice_host import ResidentVoice


class FakeVoice:
    def __init__(self):
        self.loads = 0
        self.spoken = []
        self.next_text = ""
        self.capturing = threading.Event()

    def prepare(self, speech=False):
        self.loads += 1

    def capture_utterance(self, cancel):
        self.capturing.set()
        if self.next_text:
            result, self.next_text = self.next_text, ""
            return result
        cancel.wait(.2)
        return None

    def speak(self, text, cancel_event=None):
        self.spoken.append(text)


class VoiceHostTests(unittest.TestCase):
    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_models_warm_before_listening_and_survive_cancel_and_next_command(self):
        voice = FakeVoice()
        events = []
        service = ResidentVoice(voice, lambda event, data: events.append((event, data)))
        thread = threading.Thread(target=service.run, args=(True,), daemon=True)
        thread.start()
        try:
            self.wait(lambda: any(event == "ready" for event, _ in events))
            self.assertFalse(voice.capturing.is_set())
            self.assertEqual(voice.loads, 1)
            voice.next_text = "Цитадель, первая команда"
            service.submit({"command": "listen", "token": "one", "mode": "wake"})
            self.wait(lambda: any(event == "user" and data["text"] == "первая команда" for event, data in events))
            service.submit({"command": "cancel", "token": "cancelled"})
            self.wait(lambda: service.token == "cancelled")
            self.assertTrue(thread.is_alive())
            voice.next_text = "вторая команда"
            service.submit({"command": "listen", "token": "two", "mode": "always"})
            self.wait(lambda: any(event == "user" and data["text"] == "вторая команда" for event, data in events))
            service.submit({"command": "speak", "token": "two", "text": "Готово."})
            self.wait(lambda: bool(voice.spoken))
            self.assertEqual(voice.loads, 1)
        finally:
            service.submit({"command": "close"})
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_queued_cancel_prevents_old_speech_from_starting(self):
        voice = FakeVoice()
        service = ResidentVoice(voice, lambda *_: None)
        service.submit({"command": "speak", "token": "old", "text": "stale"})
        service.submit({"command": "cancel", "token": "new"})
        thread = threading.Thread(target=service.run, args=(True,), daemon=True)
        thread.start()
        try:
            self.wait(lambda: service.token == "new")
            self.assertFalse(voice.spoken)
        finally:
            service.submit({"command": "close"})
            thread.join(timeout=3)
