"""Installer cache validation with synthetic backends; no network or audio devices."""
from contextlib import nullcontext
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import MagicMock, Mock, patch

from assistant import setup_runtime, voice_assets


class VoiceAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.snapshot = Path(self.directory.name)
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (self.snapshot / name).write_bytes(b"fixture")
        self.download = Mock(return_value=str(self.snapshot))
        self.stt = Mock()
        self.stt.transcribe.return_value = (iter(()), None)
        self.whisper = Mock(return_value=self.stt)
        self.vad = Mock()
        self.timestamps = Mock(return_value=[])
        self.numpy = SimpleNamespace(
            float32="float32", zeros=Mock(return_value="silence"),
            asarray=Mock(return_value=SimpleNamespace(size=12)),
            isfinite=Mock(return_value=SimpleNamespace(all=lambda: True)),
            any=Mock(return_value=True))
        self.tts = Mock(spec=["apply_tts", "speakers", "sample_rates", "to"])
        self.tts.speakers = ["aidar", "baya"]
        self.tts.sample_rates = [24000, 48000]
        self.tts.apply_tts.return_value = [0.1, 0.2]
        self.torch = SimpleNamespace(inference_mode=nullcontext, set_num_threads=Mock())
        self.session = Mock()
        self.session._load_tts.return_value = (self.tts, self.torch)
        self.session_type = Mock(return_value=self.session)
        self.modules = {
            "faster_whisper": SimpleNamespace(WhisperModel=self.whisper),
            "faster_whisper.utils": SimpleNamespace(download_model=self.download),
            "faster_whisper.vad": SimpleNamespace(get_vad_model=self.vad,
                                                  get_speech_timestamps=self.timestamps),
            "numpy": self.numpy,
        }
        self.config = {"voice": True, "stt_model": "small", "tts_model": "v5_6_ru",
                       "tts_speaker": "baya", "voice_threads": 2, "tts_sample_rate": 24000}
        self.messages = []
        self.addCleanup(patch.stopall)
        from assistant.voice import VoiceSession
        self.real_session = VoiceSession
        patch.dict(sys.modules, self.modules).start()
        patch("assistant.voice.VoiceSession", self.session_type).start()
        self.piper_prepare = patch.object(voice_assets, "prepare_piper_assets",
                                         side_effect=RuntimeError("Piper fixture unavailable")).start()
        # Tests must not inherit the developer machine's voice overrides.
        patch.dict(os.environ, {}).start()
        for key in ("ASSISTANT_STT_MODEL", "ASSISTANT_TTS_MODEL", "ASSISTANT_TTS_SPEAKER",
                    "ASSISTANT_VOICE_THREADS", "ASSISTANT_TTS_SAMPLE_RATE"):
            os.environ.pop(key, None)

    def prepare(self, **kwargs):
        voice_assets.prepare_voice_assets(self.config, self.messages.append, **kwargs)

    def test_cached_assets_load_locally_and_exercise_vad_and_synthesis(self):
        self.prepare()
        self.download.assert_called_once_with("small", local_files_only=True)
        self.whisper.assert_called_once_with(str(self.snapshot), device="cpu", compute_type="int8",
                                             cpu_threads=2, local_files_only=True)
        self.vad.cache_clear.assert_called_once_with()
        self.vad.assert_called_once_with()
        self.timestamps.assert_called_once_with("silence")
        self.stt.transcribe.assert_called_once_with("silence", language="ru", beam_size=1,
                                                    vad_filter=True, condition_on_previous_text=False)
        self.session_type.assert_called_once()
        settings = self.session_type.call_args.kwargs
        self.assertEqual({k: settings[k] for k in ("stt_model", "tts_model", "tts_speaker")},
                         {"stt_model": "small", "tts_model": "v5_6_ru", "tts_speaker": "baya"})
        self.assertFalse(settings["allow_sapi_fallback"])
        self.assertEqual(settings["tts_engine"], "silero")
        self.assertTrue(settings["allow_download"])
        self.tts.apply_tts.assert_called_once_with(text="Проверка голоса.", speaker="baya",
                                                  sample_rate=24000, put_accent=True, put_yo=True)
        self.assertTrue(self.messages[-1].startswith("[voice:ready]"))

    def test_cache_miss_downloads_snapshot_then_loads_local_only(self):
        self.download.side_effect = [FileNotFoundError("not cached"), str(self.snapshot)]
        with self.assertLogs("assistant.voice_assets", level="INFO") as captured:
            self.prepare()
        self.assertEqual([call.kwargs["local_files_only"] for call in self.download.call_args_list],
                         [True, False])
        self.assertTrue(self.whisper.call_args.kwargs["local_files_only"])
        self.assertTrue(any("[voice:download]" in message for message in captured.output))

    def test_public_resolver_defaults_to_cache_only(self):
        self.assertEqual(voice_assets.resolve_whisper_snapshot("small"), self.snapshot)
        self.download.assert_called_once_with("small", local_files_only=True)

    def test_public_resolver_missing_paths_never_contact_hf(self):
        for model in (str(self.snapshot / "missing"), "./missing", "../missing",
                      "C:/missing/model", "C:\\missing\\model", "models/nested/missing", "~/missing"):
            with self.subTest(model=model), self.assertRaises(FileNotFoundError):
                voice_assets.resolve_whisper_snapshot(model, allow_download=True)
        self.download.assert_not_called()

    def test_public_resolver_accepts_hf_repository_identifier(self):
        self.assertEqual(voice_assets.resolve_whisper_snapshot("Systran/faster-whisper-small"), self.snapshot)
        self.download.assert_called_once_with("Systran/faster-whisper-small", local_files_only=True)

    def test_network_failure_is_precise_and_never_reports_ready(self):
        self.download.side_effect = [FileNotFoundError("not cached"), OSError("download unavailable")]
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, r"Whisper \(small\).*download unavailable"):
            self.prepare()
        self.whisper.assert_not_called()
        self.session_type.assert_not_called()
        self.assertTrue(self.messages[-1].startswith("[voice:error]"))

    def test_voice_off_does_not_import_or_load_backends(self):
        self.config["voice"] = False
        self.prepare()
        self.download.assert_not_called()
        self.whisper.assert_not_called()
        self.session_type.assert_not_called()
        self.assertTrue(self.messages[-1].startswith("[voice:skip]"))

    def test_cache_only_miss_never_downloads(self):
        self.download.side_effect = FileNotFoundError("not cached")
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "not cached"):
            self.prepare(allow_download=False)
        self.download.assert_called_once_with("small", local_files_only=True)

    def test_cache_only_passes_no_download_to_silero(self):
        self.prepare(allow_download=False)
        self.assertFalse(self.session_type.call_args.kwargs["allow_download"])

    def test_missing_tokenizer_fails_before_whisper_can_download_fallback(self):
        (self.snapshot / "tokenizer.json").unlink()
        self.config["stt_model"] = str(self.snapshot)
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "tokenizer.json"):
            self.prepare()
        self.whisper.assert_not_called()
        self.download.assert_not_called()

    def test_incomplete_cached_snapshot_is_downloaded_again(self):
        partial = self.snapshot / "partial"
        partial.mkdir()
        self.download.side_effect = [str(partial), str(self.snapshot)]
        self.prepare()
        self.assertEqual(self.download.call_count, 2)

    def test_corrupt_whisper_fails_visibly_without_tts_or_ready(self):
        self.whisper.side_effect = RuntimeError("corrupt model.bin")
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "Whisper.*corrupt model.bin"):
            self.prepare()
        self.session_type.assert_not_called()

    def test_vad_missing_or_onnxruntime_failure_blocks_success(self):
        self.vad.side_effect = RuntimeError("missing silero_vad_v6.onnx / onnxruntime")
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "Whisper VAD.*onnxruntime"):
            self.prepare()
        self.session_type.assert_not_called()

    def test_lazy_transcribe_failure_is_not_ignored(self):
        def segments():
            raise RuntimeError("lazy VAD failure")
            yield
        self.stt.transcribe.return_value = (segments(), None)
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "lazy VAD failure"):
            self.prepare()

    def test_silero_load_and_synthesis_fail_without_sapi(self):
        for target in (self.session._load_tts, self.tts.apply_tts):
            with self.subTest(target=target):
                target.side_effect = RuntimeError("missing dependency")
                with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "Silero failure.*missing dependency"):
                    self.prepare()
                self.assertFalse(self.session_type.call_args.kwargs["allow_sapi_fallback"])
                target.side_effect = None

    def test_empty_silent_and_nonfinite_synthesis_fail(self):
        for size, finite, audible in ((0, True, True), (12, False, True), (12, True, False)):
            with self.subTest(size=size, finite=finite, audible=audible):
                self.numpy.asarray.return_value = SimpleNamespace(size=size)
                self.numpy.isfinite.return_value = SimpleNamespace(all=lambda: finite)
                self.numpy.any.return_value = audible
                with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "synthesis returned"):
                    self.prepare()

    def test_explicit_fresh_config_is_used_after_voice_module_import(self):
        from assistant import voice
        with patch.object(voice, "VOICE_STT_MODEL", "stale-base"), \
                patch.object(voice, "VOICE_TTS_MODEL", "stale-tts"), \
                patch.object(voice, "VOICE_TTS_SPEAKER", "stale-speaker"):
            self.prepare()
        self.assertEqual(self.session_type.call_args.kwargs["tts_model"], "v5_6_ru")
        self.assertEqual(self.session_type.call_args.kwargs["tts_speaker"], "baya")
        self.download.assert_called_once_with("small", local_files_only=True)

    def real_tts_backend(self):
        importer = Mock()
        importer.load_pickle.return_value = self.tts
        self.torch.hub = SimpleNamespace(get_dir=lambda: str(self.snapshot / "torch-hub"))
        self.torch.package = SimpleNamespace(PackageImporter=Mock(return_value=importer))
        self.torch.device = lambda name: name
        patch.dict(sys.modules, {"torch": self.torch}).start()
        patch("assistant.voice.importlib.util.find_spec", return_value=None).start()
        self.session_type.side_effect = self.real_session
        return self.snapshot / "torch-hub" / "silero" / "ru" / "v5_6_ru.pt", importer

    def test_real_session_reuses_official_downloader_and_package_importer(self):
        from assistant import voice
        cache, importer = self.real_tts_backend()
        with patch.object(voice_assets, "_download_silero_tts",
                          side_effect=lambda url, path, status: path.write_bytes(b"fixture")) as downloader, \
                patch.object(voice, "VOICE_TTS_MODEL", "stale-tts"), \
                patch.object(voice, "VOICE_TTS_SPEAKER", "stale-speaker"):
            self.prepare()
        downloader.assert_called_once_with(
            "https://models.silero.ai/models/tts/ru/v5_6_ru.pt", cache, self.messages.append)
        self.torch.package.PackageImporter.assert_called_once_with(str(cache))
        importer.load_pickle.assert_called_once_with("tts_models", "model")
        self.tts.to.assert_called_once_with("cpu")
        self.assertEqual(self.tts.apply_tts.call_args.kwargs["speaker"], "baya")

    def test_real_session_cached_package_never_uses_downloader(self):
        cache, _ = self.real_tts_backend()
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"fixture")
        with patch.object(self.real_session, "_download_tts") as downloader:
            self.prepare(allow_download=False)
        downloader.assert_not_called()
        self.tts.apply_tts.assert_called_once()

    def test_real_session_offline_missing_tts_fails_without_network(self):
        self.real_tts_backend()
        with patch.object(self.real_session, "_download_tts") as downloader:
            with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "Silero TTS.*FileNotFoundError"):
                self.prepare(allow_download=False)
        downloader.assert_not_called()
        self.tts.apply_tts.assert_not_called()

    def test_silero_failure_selects_piper_only_after_successful_preparation(self):
        self.session._load_tts.side_effect = TimeoutError("TLS handshake timed out")
        self.piper_prepare.side_effect = None
        self.prepare()
        self.piper_prepare.assert_called_once_with(self.config, self.messages.append, allow_download=True)
        self.assertEqual(self.config["tts_engine"], "piper")
        self.assertTrue(any(message.startswith("[voice:fallback]") for message in self.messages))
        self.assertIn("piper TTS validated", self.messages[-1])

    def test_existing_piper_skips_silero_entirely(self):
        self.config["tts_engine"] = "piper"
        self.piper_prepare.side_effect = None
        self.prepare(allow_download=False)
        self.session_type.assert_not_called()
        self.piper_prepare.assert_called_once_with(self.config, self.messages.append, allow_download=False)

    def test_failed_fallback_does_not_change_existing_engine_or_choices(self):
        self.config["tts_engine"] = "silero"
        original = dict(self.config)
        self.session._load_tts.side_effect = TimeoutError("TLS handshake timed out")
        with self.assertRaisesRegex(voice_assets.VoiceAssetsError, "TLS handshake timed out.*Piper fixture unavailable"):
            self.prepare()
        self.assertEqual(self.config, original)


class PiperPreparationTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "cache" / voice_assets.PIPER_MODEL_NAME
        self.path.parent.mkdir()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for name in (voice_assets.PIPER_MODEL_NAME, voice_assets.PIPER_MODEL_NAME + ".json", "MODEL_CARD"):
            (self.sources / name).write_bytes(b"fixture")
        self.backend = Mock(sample_rates=[22050])
        self.backend.apply_tts.return_value = np.array([0, 0.25, -0.5, 0.1], dtype=np.float32)
        self.download = Mock(side_effect=lambda **kwargs: str(self.sources / Path(kwargs["filename"]).name))
        self.addCleanup(patch.stopall)
        patch.object(voice_assets, "get_piper_path", return_value=self.path).start()
        patch.object(voice_assets.PiperBackend, "load", return_value=self.backend).start()
        patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(hf_hub_download=self.download)}).start()

    def test_official_assets_copy_atomically_then_synthesize_verified_wav(self):
        result = voice_assets.prepare_piper_assets()
        self.assertIs(result, self.backend)
        self.assertEqual(self.download.call_count, 3)
        for call in self.download.call_args_list:
            self.assertEqual(call.kwargs["repo_id"], "rhasspy/piper-voices")
            self.assertTrue(call.kwargs["filename"].startswith("ru/ru_RU/irina/medium/"))
            self.assertTrue(call.kwargs["local_files_only"])
        self.assertTrue((self.path.parent / "MODEL_CARD").exists())
        self.assertFalse(list(self.path.parent.glob("*.part")))
        with wave.open(str(self.path.parent / "piper-install-check.wav"), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()),
                             (1, 2, 22050, 4))

    def test_download_is_only_after_cache_miss_and_existing_assets_need_no_hf(self):
        def download(**kwargs):
            if kwargs["local_files_only"]:
                raise FileNotFoundError("cache miss")
            return str(self.sources / Path(kwargs["filename"]).name)
        self.download.side_effect = download
        voice_assets.prepare_piper_assets()
        self.assertEqual([call.kwargs["local_files_only"] for call in self.download.call_args_list],
                         [True, False, True, False, True, False])
        self.download.reset_mock()
        voice_assets.prepare_piper_assets(allow_download=False)
        self.download.assert_not_called()

    def test_offline_missing_asset_never_requests_network(self):
        self.download.side_effect = FileNotFoundError("cache miss")
        with self.assertRaisesRegex(FileNotFoundError, "Piper asset not cached"):
            voice_assets.prepare_piper_assets(allow_download=False)
        self.assertEqual(self.download.call_count, 1)
        self.assertTrue(self.download.call_args.kwargs["local_files_only"])
        self.backend.apply_tts.assert_not_called()

    def test_failed_copy_preserves_previous_asset_and_cleans_temporary(self):
        self.path.write_bytes(b"previous")
        with patch.object(voice_assets.shutil, "copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                voice_assets._atomic_copy(self.sources / voice_assets.PIPER_MODEL_NAME, self.path)
        self.assertEqual(self.path.read_bytes(), b"previous")
        self.assertFalse(list(self.path.parent.glob("*.part")))

    def test_synthesis_failure_never_publishes_successful_wav(self):
        self.backend.apply_tts.side_effect = RuntimeError("missing native phonemes")
        with self.assertRaisesRegex(RuntimeError, "missing native phonemes"):
            voice_assets.prepare_piper_assets()
        self.assertFalse((self.path.parent / "piper-install-check.wav").exists())


class ShortSileroDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "v5_6_ru.pt"
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.headers = {"Content-Length": "4"}
        self.response.read1.side_effect = [b"data", b""]
        self.opener = Mock()
        self.opener.open.return_value = self.response

    def test_success_publishes_complete_download_with_remaining_budget(self):
        with patch.object(voice_assets.urllib.request, "build_opener", return_value=self.opener), \
                patch.object(voice_assets.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]):
            voice_assets._download_silero_tts("https://models.silero.ai/models/tts/ru/v5_6_ru.pt", self.cache)
        self.assertEqual(self.cache.read_bytes(), b"data")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15.0)
        self.response.fp.raw._sock.settimeout.assert_called_once_with(15.0)

    def test_total_deadline_cleans_partial_and_preserves_existing(self):
        self.cache.write_bytes(b"previous")
        with patch.object(voice_assets.urllib.request, "build_opener", return_value=self.opener), \
                patch.object(voice_assets.time, "monotonic", side_effect=[0.0, 1.0, 900.1]):
            with self.assertRaisesRegex(TimeoutError, "900 second deadline"):
                voice_assets._download_silero_tts("https://models.silero.ai/models/tts/ru/v5_6_ru.pt", self.cache)
        self.assertEqual(self.cache.read_bytes(), b"previous")
        self.assertFalse(list(self.cache.parent.glob("*.part")))

    def test_incomplete_download_and_untrusted_host_cannot_publish(self):
        self.response.headers = {"Content-Length": "10"}
        with patch.object(voice_assets.urllib.request, "build_opener", return_value=self.opener):
            with self.assertRaisesRegex(ValueError, "Incomplete Silero"):
                voice_assets._download_silero_tts("https://models.silero.ai/models/tts/ru/v5_6_ru.pt", self.cache)
        self.assertFalse(self.cache.exists())
        with self.assertRaisesRegex(ValueError, "official"):
            voice_assets._download_silero_tts("https://untrusted.example/model.pt", self.cache)


class SetupVoiceTests(unittest.TestCase):
    def run_setup(self, *arguments, existing=None, failure=None, choose_piper=False):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        if existing is not None:
            (root / "assistant.local.json").write_text(json.dumps(existing), encoding="utf-8")
        def prepare(config):
            if failure:
                raise failure
            if choose_piper:
                config["tts_engine"] = "piper"
        with patch.object(setup_runtime, "DATA_ROOT", root), \
                patch.object(setup_runtime, "ensure_data_layout"), \
                patch.object(setup_runtime, "prepare_voice_assets", side_effect=prepare) as voice, \
                patch.object(setup_runtime, "prepare_model") as agent, \
                patch.dict(sys.modules, {"assistant.browser": SimpleNamespace(main=Mock())}), \
                patch.object(sys, "argv", ["setup_runtime", "--model", "balanced", *arguments]), \
                patch.object(logging, "basicConfig"), \
                patch.object(logging, "FileHandler"), \
                self.assertLogs(level="INFO") as captured:
            result = setup_runtime.main()
            browser = sys.modules["assistant.browser"].main
        return result, voice, agent, browser, captured.output

    def test_voice_always_prepared_when_only_agent_assets_skipped(self):
        result, voice, agent, browser, logs = self.run_setup("--skip-agent-assets")
        self.assertEqual(result, 0)
        self.assertTrue(voice.call_args.args[0]["voice"])
        agent.assert_not_called()
        browser.assert_not_called()
        self.assertTrue(any("Setup completed" in message for message in logs))

    def test_skip_download_skips_every_asset_even_with_voice_on(self):
        result, voice, agent, browser, _ = self.run_setup("--skip-download", "--voice", "on")
        self.assertEqual(result, 0)
        for operation in (voice, agent, browser):
            operation.assert_not_called()

    def test_existing_voice_off_stays_off_and_other_assets_prepare(self):
        result, voice, agent, browser, _ = self.run_setup(
            "--voice", "on", existing={"model": "custom-agent", "voice": False})
        self.assertEqual(result, 0)
        voice.assert_not_called()
        agent.assert_called_once_with("custom-agent")
        browser.assert_called_once_with()

    def test_preserved_voice_choices_are_passed_directly(self):
        original = {"model": "custom-agent", "voice": True, "stt_model": "tiny",
                    "tts_model": "v4_ru", "tts_speaker": "xenia", "custom": 123}
        result, voice, _, _, _ = self.run_setup("--skip-agent-assets", existing=original)
        self.assertEqual(result, 0)
        voice.assert_called_once_with(original)
        self.assertEqual(json.loads((Path(self.directory.name) / "assistant.local.json").read_text()), original)

    def test_voice_failure_prevents_success_and_optional_agent_work(self):
        result, voice, agent, browser, logs = self.run_setup(
            failure=voice_assets.VoiceAssetsError("Silero v5_6_ru download unavailable"))
        self.assertEqual(result, 1)
        voice.assert_called_once()
        agent.assert_not_called()
        browser.assert_not_called()
        self.assertFalse(any("Setup completed" in message for message in logs))
        self.assertTrue(any("Silero v5_6_ru download unavailable" in message for message in logs))

    def test_validated_piper_choice_is_persisted_and_other_choices_preserved(self):
        original = {"model": "custom-agent", "voice": True, "tts_model": "v5_6_ru",
                    "tts_speaker": "aidar", "tts_engine": "silero"}
        result, _, _, _, _ = self.run_setup("--skip-agent-assets", existing=original, choose_piper=True)
        self.assertEqual(result, 0)
        saved = json.loads((Path(self.directory.name) / "assistant.local.json").read_text())
        self.assertEqual(saved, dict(original, tts_engine="piper"))

    def test_failed_piper_preparation_does_not_persist_engine_change(self):
        original = {"model": "custom-agent", "voice": True, "tts_engine": "silero"}
        result, _, _, _, _ = self.run_setup("--skip-agent-assets", existing=original,
                                           failure=RuntimeError("Piper synthesis failed"))
        self.assertEqual(result, 1)
        saved = json.loads((Path(self.directory.name) / "assistant.local.json").read_text())
        self.assertEqual(saved, original)


if __name__ == "__main__":
    unittest.main()
