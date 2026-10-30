"""Local Piper adapter tests: synthetic chunks, no models, network or playback."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from assistant.piper_voice import PiperBackend, get_piper_path, _espeak_path


class PiperBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.model = self.root / "ru_RU-irina-medium.onnx"
        self.model.write_bytes(b"ONNX fixture")
        self.config = Path(str(self.model) + ".json")
        self.config.write_text(json.dumps({"audio": {"sample_rate": 22050},
                                           "phoneme_type": "espeak"}), encoding="utf-8")
        self.espeak = self.root / "espeak"
        self.espeak.mkdir()
        (self.espeak / "phontab").write_bytes(b"fixture")
        self.voice = Mock()
        self.loader = Mock(return_value=self.voice)

    def load(self):
        with patch.dict(sys.modules, {
                "piper": SimpleNamespace(PiperVoice=SimpleNamespace(load=self.loader)),
                "piper.voice": SimpleNamespace(ESPEAK_DATA_DIR=self.espeak), "torch": None}):
            return PiperBackend.load(self.model)

    def test_load_is_local_cpu_and_uses_configured_rate_without_torch(self):
        backend = self.load()
        self.assertEqual(backend.speakers, ["irina"])
        self.assertEqual(backend.sample_rates, [22050])
        self.loader.assert_called_once_with(str(self.model), config_path=str(self.config),
                                            use_cuda=False, download_dir=self.root,
                                            espeak_data_dir=self.espeak)

    def test_missing_local_assets_fail_before_importing_piper(self):
        for missing in (self.model, self.config):
            with self.subTest(missing=missing):
                data = missing.read_bytes()
                missing.unlink()
                with patch.dict(sys.modules, {"piper": None}), self.assertRaises(FileNotFoundError):
                    PiperBackend.load(self.model)
                missing.write_bytes(data)

    def test_config_rejects_optional_phonemizer_downloads(self):
        self.config.write_text('{"audio":{"sample_rate":22050},"phoneme_type":"pinyin"}')
        with self.assertRaisesRegex(ValueError, "bundled eSpeak"):
            self.load()
        self.loader.assert_not_called()

    def test_missing_bundled_phonemes_fail_before_native_process_can_exit(self):
        (self.espeak / "phontab").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "phontab"):
            self.load()
        self.loader.assert_not_called()

    def test_float_chunks_concatenate_in_order_and_accept_silero_keywords(self):
        self.voice.synthesize.return_value = [
            SimpleNamespace(audio_float_array=np.array([0.1, -0.2], dtype=np.float64), sample_rate=22050),
            SimpleNamespace(audio_float_array=np.array([0.3], dtype=np.float32), sample_rate=22050)]
        backend = self.load()
        result = backend.apply_tts("Привет. Мир.", speaker="irina", sample_rate=22050,
                                   put_accent=True, put_yo=True)
        np.testing.assert_allclose(result, [0.1, -0.2, 0.3])
        self.assertEqual(result.dtype, np.float32)
        self.voice.synthesize.assert_called_once_with("Привет. Мир.")

    def test_int16_byte_chunks_normalize_to_float32(self):
        self.voice.synthesize.return_value = [SimpleNamespace(
            audio_int16_bytes=np.array([-32768, 0, 16384, 32767], dtype="<i2").tobytes(),
            sample_rate=22050)]
        audio = self.load().apply_tts("Привет")
        np.testing.assert_allclose(audio, [-1, 0, 0.5, 32767 / 32768])
        self.assertEqual(audio.dtype, np.float32)

    def test_empty_text_skips_inference_and_empty_chunks_return_empty(self):
        backend = self.load()
        self.assertEqual(backend.apply_tts(" ").size, 0)
        self.voice.synthesize.assert_not_called()
        self.voice.synthesize.return_value = []
        self.assertEqual(backend.apply_tts("Привет").size, 0)

    def test_wrong_rate_and_invalid_audio_are_rejected(self):
        backend = self.load()
        with self.assertRaisesRegex(ValueError, "22050"):
            backend.apply_tts("Привет", sample_rate=48000)
        for audio in (np.array([np.nan]), np.array([[0.1]])):
            with self.subTest(audio=audio):
                self.voice.synthesize.return_value = [SimpleNamespace(audio_float_array=audio)]
                with self.assertRaisesRegex(ValueError, "invalid mono audio"):
                    backend.apply_tts("Привет")
        self.voice.synthesize.return_value = [SimpleNamespace(audio_float_array=np.zeros(5), sample_rate=16000)]
        with self.assertRaisesRegex(ValueError, "inconsistent sample rates"):
            backend.apply_tts("Привет")

    def test_lazy_inference_error_is_preserved(self):
        def chunks():
            raise RuntimeError("ONNX inference failed")
            yield
        self.voice.synthesize.return_value = chunks()
        with self.assertRaisesRegex(RuntimeError, "ONNX inference failed"):
            self.load().apply_tts("Привет")

    def test_default_cache_path_matches_runtime_data_root(self):
        with patch("assistant.paths.DATA_ROOT", self.root):
            self.assertEqual(get_piper_path(), self.root / "cache/piper/ru_RU-irina-medium.onnx")

    @unittest.skipUnless(sys.platform == "win32", "Windows native path handling")
    def test_unicode_espeak_path_uses_ascii_short_alias(self):
        path = self.root / "фонемы"
        path.mkdir()
        (path / "phontab").write_bytes(b"fixture")
        import ctypes
        def short_path(source, buffer, count):
            buffer.value = "C:\\ASCII\\espeak"
            return len(buffer.value)
        with patch.object(ctypes.windll.kernel32, "GetShortPathNameW", side_effect=short_path):
            self.assertEqual(str(_espeak_path(path)), "C:\\ASCII\\espeak")
        with patch.object(ctypes.windll.kernel32, "GetShortPathNameW", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "short-path alias"):
                _espeak_path(path)


if __name__ == "__main__":
    unittest.main()
