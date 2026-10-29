from __future__ import annotations

import queue
import re
import tempfile
import wave
from pathlib import Path
from typing import Any

from .config import (
    VOICE_INPUT_DEVICE,
    VOICE_SAMPLE_RATE,
    VOICE_STT_MODEL,
    VOICE_TTS_MODEL,
    VOICE_TTS_SPEAKER,
    VOICE_TTS_SAMPLE_RATE,
    VOICE_THREADS,
)


class VoiceError(RuntimeError):
    """Ошибка загрузки или использования голосового слоя."""


class VoiceSession:
    """Офлайн push-to-talk слой: микрофон -> Whisper -> помощник -> Silero -> динамики."""

    def __init__(self) -> None:
        self._stt: Any | None = None
        self._tts: Any | None = None
        self._torch: Any | None = None

    def _load_stt(self) -> Any:
        if self._stt is not None:
            return self._stt
        try:
            from faster_whisper import WhisperModel

            self._stt = WhisperModel(
                VOICE_STT_MODEL,
                device="cpu",
                compute_type="int8",
                cpu_threads=VOICE_THREADS,
            )
            return self._stt
        except Exception as error:
            raise VoiceError(
                "Не удалось загрузить faster-whisper. "
                "Установите голосовые зависимости через scripts/setup.ps1."
            ) from error

    def _load_tts(self) -> tuple[Any, Any]:
        if self._tts is not None and self._torch is not None:
            return self._tts, self._torch
        try:
            import torch
            from silero import silero_tts

            torch.set_num_threads(VOICE_THREADS)
            model, _ = silero_tts(language="ru", speaker=VOICE_TTS_MODEL)
            model.to(torch.device("cpu"))
            self._tts = model
            self._torch = torch
            return model, torch
        except Exception as error:
            raise VoiceError(
                "Не удалось загрузить Silero TTS. "
                "Установите голосовые зависимости через scripts/setup.ps1."
            ) from error

    @staticmethod
    def _write_wav(raw_audio: bytes, path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(VOICE_SAMPLE_RATE)
            output.writeframes(raw_audio)

    def record(self) -> str | None:
        """Записывает одну фразу. Повторное нажатие Enter останавливает запись."""
        try:
            import sounddevice as sd
        except Exception as error:
            raise VoiceError(
                "Не найден sounddevice. Установите голосовые зависимости через scripts/setup.ps1."
            ) from error

        command = input("Нажмите Enter, скажите команду и нажмите Enter ещё раз (или /text): ").strip()
        if command:
            return command

        chunks: queue.Queue[bytes] = queue.Queue()

        def callback(indata: bytes, frames: int, time: Any, status: Any) -> None:
            if status:
                print(f"[микрофон: {status}]", flush=True)
            chunks.put(bytes(indata))

        try:
            with sd.RawInputStream(
                samplerate=VOICE_SAMPLE_RATE,
                blocksize=8000,
                dtype="int16",
                channels=1,
                device=VOICE_INPUT_DEVICE or None,
                callback=callback,
            ):
                print("Слушаю...", flush=True)
                input()
        except Exception as error:
            raise VoiceError(f"Не удалось открыть микрофон: {error}") from error

        collected: list[bytes] = []
        while not chunks.empty():
            collected.append(chunks.get_nowait())
        raw_audio = b"".join(collected)
        if not raw_audio:
            print("Не удалось получить звук с микрофона.")
            return None

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temporary:
            audio_path = Path(temporary.name)
        try:
            self._write_wav(raw_audio, audio_path)
            segments, _ = self._load_stt().transcribe(
                str(audio_path),
                language="ru",
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            return text or None
        finally:
            audio_path.unlink(missing_ok=True)

    @staticmethod
    def _speech_text(text: str) -> str:
        text = re.sub(r"```.*?```", "код пропущен", text, flags=re.DOTALL)
        text = re.sub(r"[`*_#]", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def speak(self, text: str) -> None:
        text = self._speech_text(text)
        if not text:
            return
        try:
            import sounddevice as sd
        except Exception as error:
            raise VoiceError(
                "Не найден sounddevice. Установите голосовые зависимости через scripts/setup.ps1."
            ) from error

        model, torch = self._load_tts()
        try:
            audio = model.apply_tts(
                text=text[:4000],
                speaker=VOICE_TTS_SPEAKER,
                sample_rate=VOICE_TTS_SAMPLE_RATE,
                put_accent=True,
                put_yo=True,
            )
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            sd.play(audio, VOICE_TTS_SAMPLE_RATE)
            sd.wait()
        except Exception as error:
            raise VoiceError(f"Не удалось озвучить ответ: {error}") from error
