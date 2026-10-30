"""Synthetic PCM and mocked backends only: no microphone, downloads or playback."""
from array import array
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from assistant import voice
from assistant.voice import VoiceError, VoiceSession, _UtteranceBuffer


def pcm(level=0, seconds=0.05):
    samples = array("h", [level, -level] * (int(voice.VOICE_SAMPLE_RATE * seconds) // 2))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def microphone(blocks, event=None):
    sd = Mock()
    sd.query_devices.return_value = {"name": "Synthetic microphone", "max_input_channels": 1}

    def stream(**kwargs):
        context = MagicMock()

        def enter():
            for block in blocks:
                kwargs["callback"](block, len(block) // 2, None, None)
            if event:
                event.set()
            return context

        context.__enter__.side_effect = enter
        return context

    sd.RawInputStream.side_effect = stream
    return sd


class CaptureTests(unittest.TestCase):
    def test_silence_and_constant_noise_never_produce_utterance(self):
        for level in (0, 80, 700):
            with self.subTest(level=level):
                vad = _UtteranceBuffer(voice.VOICE_SAMPLE_RATE)
                for _ in range(360):
                    self.assertFalse(vad.feed(pcm(level)))
                self.assertEqual(vad.audio(), b"")
                self.assertLessEqual(vad.pre_duration, 0.401)
                self.assertFalse(vad.chunks)

    def test_speech_keeps_onset_and_ends_after_seven_tenths_silence(self):
        vad = _UtteranceBuffer(voice.VOICE_SAMPLE_RATE)
        for _ in range(10):
            vad.feed(pcm(50))
        onset = pcm(1200)
        for _ in range(6):
            self.assertFalse(vad.feed(onset))
        self.assertTrue(vad.started)
        for _ in range(13):
            self.assertFalse(vad.feed(pcm(50)))
        self.assertTrue(vad.feed(pcm(50)))
        self.assertEqual(vad.audio().count(onset), 6)
        self.assertTrue(vad.audio().startswith(pcm(50)))

    def test_single_impulse_does_not_trigger(self):
        vad = _UtteranceBuffer(voice.VOICE_SAMPLE_RATE)
        for block in [pcm(30)] * 6 + [pcm(3000)] + [pcm(30)] * 20:
            vad.feed(block)
        self.assertEqual(vad.audio(), b"")

    def test_noise_floor_tracks_a_changing_background(self):
        vad = _UtteranceBuffer(voice.VOICE_SAMPLE_RATE)
        for level in range(50, 501, 5):
            vad.feed(pcm(level))
        self.assertGreater(vad.noise, 300)
        self.assertEqual(vad.audio(), b"")

    def test_capture_no_speech_skips_stt_and_short_blocks(self):
        sd = microphone([pcm(400)] * 12)
        session = VoiceSession()
        with patch.dict(sys.modules, sounddevice=sd), patch.object(session, "transcribe_audio") as stt:
            raw = session._capture_raw(threading.Event(), start_timeout=0.5)
            self.assertEqual(raw, b"")
            with patch.object(session, "_capture_raw", return_value=raw):
                self.assertIsNone(session.capture_utterance(threading.Event()))
            stt.assert_not_called()
        self.assertLessEqual(sd.RawInputStream.call_args.kwargs["blocksize"], voice.VOICE_SAMPLE_RATE // 10)

    def test_capture_speech_and_silence_delivers_one_utterance(self):
        sd = microphone([pcm(40)] * 6 + [pcm(1500)] * 6 + [pcm(40)] * 14)
        session = VoiceSession()
        with patch.dict(sys.modules, sounddevice=sd), patch.object(session, "transcribe_audio", return_value="команда") as stt:
            self.assertEqual(session.capture_utterance(threading.Event()), "команда")
        self.assertIn(pcm(1500), stt.call_args.args[0])

    def test_stop_during_capture_discards_audio(self):
        event = threading.Event()
        sd = microphone([pcm(40)] * 6 + [pcm(1500)] * 6, event)
        session = VoiceSession()
        with patch.dict(sys.modules, sounddevice=sd), patch.object(session, "transcribe_audio") as stt:
            self.assertIsNone(session.capture_utterance(event))
            stt.assert_not_called()

    def test_stop_between_capture_and_stt(self):
        event = threading.Event()
        session = VoiceSession()

        def capture(*args, **kwargs):
            event.set()
            return pcm(1000)

        with patch.object(session, "_capture_raw", side_effect=capture), patch.object(session, "transcribe_audio") as stt:
            self.assertIsNone(session.capture_until_stop(event))
            stt.assert_not_called()

    def test_missing_device_has_readable_underlying_error(self):
        sd = Mock()
        sd.query_devices.side_effect = RuntimeError("No default input device")
        with patch.dict(sys.modules, sounddevice=sd), self.assertLogs(voice.logger, level="ERROR"):
            with self.assertRaisesRegex(VoiceError, "RuntimeError: No default input device"):
                VoiceSession().capture_utterance(threading.Event())


class RecognitionTests(unittest.TestCase):
    def test_prepare_warms_recognition_without_opening_microphone(self):
        session = VoiceSession()
        with patch.object(session, "_load_stt") as load:
            session.prepare()
        load.assert_called_once_with()

    def test_empty_and_cancelled_audio_never_load_model(self):
        event = threading.Event()
        session = VoiceSession()
        with patch.object(session, "_load_stt") as load:
            self.assertIsNone(session.transcribe_audio(b""))
            self.assertIsNone(session.transcribe_audio(pcm()))
            event.set()
            self.assertIsNone(session.transcribe_audio(pcm(1000), event))
            load.assert_not_called()

    def test_cancel_while_loading_prevents_inference(self):
        event = threading.Event()
        model = Mock()
        session = VoiceSession()

        def load():
            event.set()
            return model

        with patch.object(session, "_load_stt", side_effect=load):
            self.assertIsNone(session.transcribe_audio(pcm(1000), event))
        model.transcribe.assert_not_called()

    def test_int8_load_status_and_single_beam(self):
        factory = Mock()
        factory.return_value.transcribe.return_value = ([SimpleNamespace(text=" команда ")], None)
        statuses = []
        with patch.dict(sys.modules, faster_whisper=SimpleNamespace(WhisperModel=factory)), patch.object(voice, "resolve_whisper_snapshot", return_value=Path("cached-model")):
            session = VoiceSession(on_status=statuses.append)
            self.assertEqual(session.transcribe_audio(pcm(1200)), "команда")
        self.assertEqual(factory.call_args.kwargs["compute_type"], "int8")
        call = factory.return_value.transcribe.call_args
        self.assertEqual(call.kwargs["beam_size"], 1)
        self.assertIn("Цитадель", call.kwargs["initial_prompt"])
        self.assertEqual(str(call.args[0].dtype), "float32")
        self.assertAlmostEqual(float(call.args[0][0]), 1200 / 32768)
        self.assertTrue(any("локальных файлов" in status for status in statuses))

    def test_lazy_segment_exception_is_preserved(self):
        def segments():
            raise RuntimeError("native decoder detail")
            yield

        session = VoiceSession()
        model = Mock()
        model.transcribe.return_value = (segments(), None)
        with patch.object(session, "_load_stt", return_value=model), self.assertLogs(voice.logger, level="ERROR"):
            with self.assertRaisesRegex(VoiceError, "native decoder detail"):
                session.transcribe_audio(pcm(1000))
        self.assertEqual(str(model.transcribe.call_args.args[0].dtype), "float32")

    def test_pcm_input_resamples_to_whispers_required_16khz(self):
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text="команда")], None)
        session = VoiceSession()
        with patch.object(voice, "VOICE_SAMPLE_RATE", 48000), patch.object(session, "_load_stt", return_value=model):
            raw = pcm(1600, seconds=0.3)
            self.assertEqual(session.transcribe_audio(raw), "команда")
        samples = model.transcribe.call_args.args[0]
        self.assertEqual(len(samples), 4800)
        self.assertEqual(str(samples.dtype), "float32")
        self.assertLessEqual(float(abs(samples).max()), 1.0)

    def test_cancellation_from_status_callback_prevents_inference(self):
        event = threading.Event()
        session = VoiceSession(on_status=lambda message: event.set())
        model = Mock()
        with patch.object(session, "_load_stt", return_value=model):
            self.assertIsNone(session.transcribe_audio(pcm(1000), event))
        model.transcribe.assert_not_called()


class SpeechTests(unittest.TestCase):
    def test_markdown_and_urls_are_cleaned(self):
        text = "# Ответ\n- **Читайте** [инструкцию](https://example.org/help).\n> `текст` https://example.org\n```python\nsecret()\n```"
        self.assertEqual(VoiceSession._speech_text(text), "Ответ Читайте инструкцию. текст код пропущен")

    def test_sentences_and_long_words_are_bounded_without_truncation(self):
        text = "Первая фраза. " + "слово " * 1000 + "Конец! " + "я" * 600
        chunks = VoiceSession._speech_chunks(text)
        self.assertEqual(chunks[0], "Первая фраза.")
        self.assertTrue(all(0 < len(chunk) <= 240 for chunk in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), VoiceSession._speech_text(text).replace(" ", ""))

    def backend(self):
        model = Mock(speakers=["aidar", "baya"], sample_rates=[8000, 24000, 48000])
        model.apply_tts.return_value = [0.0, 0.1]
        torch = SimpleNamespace(inference_mode=nullcontext)
        sd = Mock()
        sd.get_stream.return_value = SimpleNamespace(active=False)
        return model, torch, sd

    def test_pre_cancelled_speak_does_not_load_or_play(self):
        event = threading.Event()
        event.set()
        session = VoiceSession()
        with patch.object(session, "_load_tts") as load:
            session.speak("текст", event)
        load.assert_not_called()

    def test_cancel_after_synthesis_prevents_playback(self):
        event = threading.Event()
        model, torch, sd = self.backend()
        model.apply_tts.side_effect = lambda **kwargs: event.set() or [0.0]
        session = VoiceSession()
        with patch.object(session, "_load_tts", return_value=(model, torch)), patch.dict(sys.modules, sounddevice=sd):
            session.speak("Один. Два.", event)
        sd.play.assert_not_called()
        self.assertEqual(model.apply_tts.call_count, 1)

    def test_playback_polling_cancel_stops_and_skips_next_sentence(self):
        event = threading.Event()
        model, torch, sd = self.backend()
        sd.get_stream.side_effect = lambda: event.set() or SimpleNamespace(active=True)
        session = VoiceSession()
        with patch.object(session, "_load_tts", return_value=(model, torch)), patch.dict(sys.modules, sounddevice=sd):
            session.speak("Один. Два.", event)
        self.assertEqual(model.apply_tts.call_count, 1)
        self.assertFalse(sd.play.call_args.kwargs["blocking"])
        sd.stop.assert_called_once()
        sd.wait.assert_not_called()

    def test_stop_method_interrupts_playback(self):
        model, torch, sd = self.backend()
        session = VoiceSession()
        sd.get_stream.side_effect = lambda: session.stop() or SimpleNamespace(active=True)
        with patch.object(session, "_load_tts", return_value=(model, torch)), patch.dict(sys.modules, sounddevice=sd):
            session.speak("Один. Два.")
        self.assertTrue(session._stop_event.is_set())
        self.assertGreaterEqual(sd.stop.call_count, 1)
        self.assertEqual(model.apply_tts.call_count, 1)

    def test_speaker_and_rate_fallbacks_use_actual_model_metadata(self):
        session = VoiceSession()
        with patch.object(voice, "VOICE_TTS_SPEAKER", "missing"), patch.object(voice, "VOICE_TTS_SAMPLE_RATE", 44100):
            self.assertEqual(session._tts_settings(SimpleNamespace(speakers=["baya", "aidar"], sample_rates=[24000, 48000])), ("aidar", 48000))
            self.assertEqual(session._tts_settings(SimpleNamespace(speakers=["baya"], sample_rate=24000)), ("baya", 24000))

    def test_silero_cache_is_explicit_and_cpu_loading_avoids_wrapper(self):
        model = Mock()
        torch = Mock()
        torch.package.PackageImporter.return_value.load_pickle.return_value = model
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "silero" / "ru" / f"{voice.VOICE_TTS_MODEL}.pt"
            cache.parent.mkdir(parents=True)
            cache.touch()
            torch.hub.get_dir.return_value = tmp
            statuses = []
            session = VoiceSession(on_status=statuses.append)
            with patch.dict(sys.modules, torch=torch):
                self.assertEqual(session._load_tts(), (model, torch))
                self.assertEqual(session._load_tts(), (model, torch))
            torch.package.PackageImporter.assert_called_once_with(str(cache))
            torch.hub.download_url_to_file.assert_not_called()
            torch.device.assert_called_with("cpu")
            self.assertTrue(statuses)

    def test_sapi_fallback_is_optional_and_reports_machine_voice(self):
        session = VoiceSession()
        with patch.object(session, "_load_tts", side_effect=VoiceError("Silero failed")), patch.object(session, "_speak_sapi") as sapi, patch.object(voice.os, "name", "nt"), self.assertLogs(voice.logger, level="ERROR"):
            session.speak("Текст.")
            sapi.assert_called_once()
        session = VoiceSession(allow_sapi_fallback=False)
        with patch.object(session, "_load_tts", side_effect=VoiceError("Silero failed")), self.assertLogs(voice.logger, level="ERROR"):
            with self.assertRaisesRegex(VoiceError, "Silero failed"):
                session.speak("Текст.")

    def test_sapi_reports_machine_voice_and_purges_on_cancel(self):
        event = threading.Event()
        speaker = Mock()
        speaker.WaitUntilDone.side_effect = lambda timeout: event.set() or False
        comtypes = Mock()
        client = SimpleNamespace(CreateObject=Mock(return_value=speaker))
        statuses = []
        session = VoiceSession(on_status=statuses.append)
        with patch.dict(sys.modules, {"comtypes": comtypes, "comtypes.client": client}):
            session._speak_sapi(["Первый.", "Второй."], event)
        self.assertIn("машинный голос", statuses[0])
        self.assertEqual(speaker.Speak.call_args_list[0].args, ("Первый.", 1))
        self.assertEqual(speaker.Speak.call_args_list[-1].args, ("", 3))
        self.assertEqual(speaker.Speak.call_count, 2)
        comtypes.CoUninitialize.assert_called_once()


class DownloadTests(unittest.TestCase):
    url = "https://models.silero.ai/models/tts/ru/v5_6_ru.pt"

    def response(self, chunks, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers or {}
        response.read1.side_effect = chunks
        opener = Mock()
        opener.open.return_value = response
        return response, opener

    def test_atomic_official_download_emits_status_and_hash(self):
        response, opener = self.response([b"model-data"], {"Content-Length": "10"})
        statuses = []
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "v5_6_ru.pt"
            with patch.object(voice.urllib.request, "build_opener", return_value=opener):
                VoiceSession(on_status=statuses.append)._download_tts(self.url, cache)
            self.assertEqual(cache.read_bytes(), b"model-data")
            self.assertEqual(list(Path(folder).glob("*.part")), [])
        opener.open.assert_called_once_with(self.url, timeout=15.0)
        response.fp.raw._sock.settimeout.assert_called_once()
        self.assertLessEqual(response.fp.raw._sock.settimeout.call_args.args[0], 15)
        self.assertTrue(any("SHA256" in status for status in statuses))

    def test_read_timeout_cleans_partial_and_preserves_existing_data(self):
        response, opener = self.response([b"partial", TimeoutError("read timed out")])
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "v5_6_ru.pt"
            cache.write_bytes(b"existing-user-model")
            with patch.object(voice.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(TimeoutError, "read timed out"):
                    VoiceSession()._download_tts(self.url, cache)
            self.assertEqual(cache.read_bytes(), b"existing-user-model")
            self.assertEqual(list(Path(folder).glob("*.part")), [])

    def test_total_deadline_shortens_remaining_socket_timeout(self):
        response, opener = self.response([])
        elapsed = [0.0]
        increments = iter([80.0, 10.0])

        def trickle(size):
            elapsed[0] += next(increments)
            return b"data"

        response.read1.side_effect = trickle
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "v5_6_ru.pt"
            with patch.object(voice.urllib.request, "build_opener", return_value=opener), patch.object(voice.time, "monotonic", side_effect=lambda: elapsed[0]):
                with self.assertRaisesRegex(TimeoutError, "90 second deadline"):
                    VoiceSession()._download_tts(self.url, cache)
            self.assertFalse(cache.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual([call.args[0] for call in response.fp.raw._sock.settimeout.call_args_list], [15.0, 10.0])

    def test_empty_and_truncated_downloads_are_not_published(self):
        for chunks, headers in (([b""], {}), ([b"short", b""], {"Content-Length": "100"})):
            response, opener = self.response(chunks, headers)
            with tempfile.TemporaryDirectory() as folder:
                cache = Path(folder) / "v5_6_ru.pt"
                with patch.object(voice.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(ValueError, "Incomplete"):
                        VoiceSession()._download_tts(self.url, cache)
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_untrusted_hosts_and_http_redirects_are_rejected(self):
        for url in ("http://models.silero.ai/model.pt", "https://mirror.example/model.pt",
                    "https://models.silero.ai.evil.example/model.pt", "https://models.silero.ai:8080/model.pt"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "official"):
                    voice._check_model_url(url)
                with self.assertRaisesRegex(ValueError, "official"):
                    voice._OfficialModelRedirect().redirect_request(None, None, 302, "Found", {}, url)

    def test_load_tts_wraps_network_timeout_with_underlying_cause(self):
        torch = Mock()
        session = VoiceSession(allow_download=True)
        with tempfile.TemporaryDirectory() as folder:
            torch.hub.get_dir.return_value = folder
            with patch.dict(sys.modules, torch=torch), patch.object(voice.importlib.util, "find_spec", return_value=None), patch.object(session, "_download_tts", side_effect=TimeoutError("read timed out")), self.assertLogs(voice.logger, level="ERROR"):
                with self.assertRaisesRegex(VoiceError, "TimeoutError: read timed out"):
                    session._load_tts()
            torch.package.PackageImporter.assert_not_called()

    def test_runtime_never_downloads_a_missing_silero_model(self):
        torch = Mock()
        session = VoiceSession()
        with tempfile.TemporaryDirectory() as folder:
            torch.hub.get_dir.return_value = folder
            with patch.dict(sys.modules, torch=torch), patch.object(voice.importlib.util, "find_spec", return_value=None), patch.object(session, "_download_tts") as download, self.assertLogs(voice.logger, level="ERROR"):
                with self.assertRaisesRegex(VoiceError, "Повторите установку"):
                    session._load_tts()
                download.assert_not_called()

    def test_whisper_runtime_is_local_only_and_installer_can_download(self):
        factory = Mock()
        with patch.dict(sys.modules, faster_whisper=SimpleNamespace(WhisperModel=factory)), patch.object(voice, "resolve_whisper_snapshot", side_effect=lambda model, **kwargs: Path(model)) as resolve:
            VoiceSession(stt_model="cached-model").prepare()
            self.assertTrue(factory.call_args.kwargs["local_files_only"])
            self.assertEqual(factory.call_args.args[0], "cached-model")
            resolve.assert_called_with("cached-model", allow_download=False)
            VoiceSession(allow_download=True, stt_model="installer-model").prepare()
            self.assertTrue(factory.call_args.kwargs["local_files_only"])
            resolve.assert_called_with("installer-model", allow_download=True)


class MeterTests(unittest.TestCase):
    def test_level_tracks_rms_clipping_and_resets_on_pause(self):
        updates = []
        session = VoiceSession(on_level=updates.append)
        session._report_level(pcm(0), active=True, force=True)
        session._report_level(pcm(2000), active=True, force=True)
        session._report_level(pcm(32767), active=True, force=True)
        session._report_level(active=False)
        self.assertEqual(updates[0]["level"], 0)
        self.assertGreater(updates[1]["level"], updates[0]["level"])
        self.assertTrue(updates[2]["clipping"])
        self.assertFalse(updates[-1]["active"])
        self.assertEqual(updates[-1]["level"], 0)

    def test_meter_is_throttled_and_capture_always_resets_it(self):
        updates = []
        session = VoiceSession(on_level=updates.append)
        with patch.object(voice.time, "monotonic", return_value=10):
            for _ in range(100):
                session._report_level(pcm(1000), active=True)
        self.assertEqual(len(updates), 1)
        sd = microphone([pcm(40)] * 6 + [pcm(1500)] * 6 + [pcm(40)] * 14)
        with patch.dict(sys.modules, sounddevice=sd):
            session._capture_raw(threading.Event())
        self.assertFalse(updates[-1]["active"])


if __name__ == "__main__":
    unittest.main()
