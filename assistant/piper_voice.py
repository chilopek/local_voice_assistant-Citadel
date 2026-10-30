"""Local-only Piper adapter with the small synthesis API used by VoiceSession."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PIPER_REPOSITORY = "rhasspy/piper-voices"
PIPER_SUBDIRECTORY = "ru/ru_RU/irina/medium"
PIPER_MODEL_NAME = "ru_RU-irina-medium.onnx"


def _espeak_path(path: Path) -> Path:
    """eSpeak's Windows C file API requires an ASCII path, unlike ONNX Runtime."""
    if not (path / "phontab").is_file():
        raise FileNotFoundError(f"Bundled Piper eSpeak data missing: {path / 'phontab'}")
    if os.name == "nt" and not str(path).isascii():
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        get_short_path = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short_path.restype = ctypes.c_uint32
        length = get_short_path(str(path), buffer, len(buffer))
        if not length or length >= len(buffer) or not buffer.value.isascii():
            raise RuntimeError("Piper eSpeak needs an ASCII package path or Windows short-path alias")
        return Path(buffer.value)
    return path


def get_piper_path() -> Path:
    from .paths import DATA_ROOT
    return DATA_ROOT / "cache" / "piper" / PIPER_MODEL_NAME


class PiperBackend:
    def __init__(self, voice: Any, sample_rate: int) -> None:
        self._voice = voice
        self.speakers = ["irina"]
        self.sample_rates = [sample_rate]

    @classmethod
    def load(cls, model_path: str | Path) -> "PiperBackend":
        """Load only local files. No downloader, CUDA or optional phonemizer."""
        path = Path(model_path)
        config_path = Path(str(path) + ".json")
        for asset in (path, config_path):
            if not asset.is_file() or asset.stat().st_size == 0:
                raise FileNotFoundError(f"Piper asset missing or empty: {asset}")
        settings = json.loads(config_path.read_text(encoding="utf-8"))
        rate = int(settings["audio"]["sample_rate"])
        if rate <= 0:
            raise ValueError("Piper sample rate must be positive")
        # Irina uses the bundled eSpeak phonemizer, which needs no extra models.
        if settings.get("phoneme_type", "espeak") != "espeak":
            raise ValueError("This Piper backend requires the bundled eSpeak phonemizer")
        from piper import PiperVoice
        from piper.voice import ESPEAK_DATA_DIR
        espeak_data = _espeak_path(Path(ESPEAK_DATA_DIR))
        voice = PiperVoice.load(str(path), config_path=str(config_path), use_cuda=False,
                                download_dir=path.parent, espeak_data_dir=espeak_data)
        return cls(voice, rate)

    def apply_tts(self, text: str, speaker: str | None = None,
                  sample_rate: int | None = None, put_accent: bool = True,
                  put_yo: bool = True) -> Any:
        """Return mono float32 samples; single-voice Piper ignores Silero flags."""
        import numpy as np
        if sample_rate is not None and sample_rate != self.sample_rates[0]:
            raise ValueError(f"Piper requires {self.sample_rates[0]} Hz, got {sample_rate}")
        if not text.strip():
            return np.empty(0, dtype=np.float32)
        parts = []
        for chunk in self._voice.synthesize(text):
            if getattr(chunk, "sample_rate", self.sample_rates[0]) != self.sample_rates[0]:
                raise ValueError("Piper returned inconsistent sample rates")
            audio = getattr(chunk, "audio_float_array", None)
            if audio is None:
                audio = np.frombuffer(chunk.audio_int16_bytes, dtype="<i2").astype(np.float32) / 32768.0
            else:
                audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim != 1 or not np.isfinite(audio).all():
                raise ValueError("Piper returned invalid mono audio")
            if audio.size:
                parts.append(audio)
        return np.concatenate(parts).astype(np.float32, copy=False) if parts else np.empty(0, dtype=np.float32)
