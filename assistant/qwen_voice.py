"""Small adapter between Qwen3-TTS and the resident voice host.

Qwen's public API returns a waveform from ``generate_custom_voice`` while the
assistant's audio loop expects the Silero-like ``apply_tts`` method. Keeping the
adapter here makes the rest of the voice pipeline engine-agnostic.
"""
from __future__ import annotations

from typing import Any


DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
DEFAULT_SPEAKER = "Serena"
DEFAULT_INSTRUCT = (
    "Говори по-русски естественно и тепло, как близкая подруга: спокойно, "
    "с живой интонацией, лёгкой иронией и заботой, без дикторской монотонности."
)


class Qwen3Voice:
    sample_rate = 24000

    def __init__(self, model: Any, torch: Any, speaker: str = DEFAULT_SPEAKER) -> None:
        self.model = model
        self.torch = torch
        self.speakers = tuple(model.get_supported_speakers())
        self.sample_rates = (24000, 22050, 16000)
        self.speaker = speaker if speaker in self.speakers else DEFAULT_SPEAKER

    @classmethod
    def load(cls, model_id: str = DEFAULT_MODEL, speaker: str = DEFAULT_SPEAKER,
             *, allow_download: bool = False) -> "Qwen3Voice":
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
        except Exception as error:
            raise RuntimeError("Для Qwen3-TTS нужен пакет qwen-tts и PyTorch.") from error
        if torch.cuda.is_available():
            device_map = "cuda:0"
            dtype = torch.bfloat16
        else:
            device_map = "cpu"
            dtype = torch.float32
        kwargs = {"device_map": device_map, "dtype": dtype}
        if not allow_download:
            kwargs["local_files_only"] = True
        # Runtime is deliberately local-only. The installer calls this with
        # allow_download=True; the resident voice process never does.
        model = Qwen3TTSModel.from_pretrained(model_id or DEFAULT_MODEL, **kwargs)
        return cls(model, torch, speaker)

    def apply_tts(self, *, text: str, speaker: str | None = None,
                  sample_rate: int = 24000, **_: Any) -> Any:
        wavs, rate = self.model.generate_custom_voice(
            text=text,
            language="Russian",
            speaker=speaker if speaker in self.speakers else self.speaker,
            instruct=DEFAULT_INSTRUCT,
        )
        audio = wavs[0]
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        if int(rate) == int(sample_rate):
            return audio
        import numpy as np
        source = np.asarray(audio, dtype=np.float32)
        length = max(1, round(len(source) * int(sample_rate) / int(rate)))
        return np.interp(np.linspace(0, len(source) - 1, length),
                         np.arange(len(source)), source).astype(np.float32)

