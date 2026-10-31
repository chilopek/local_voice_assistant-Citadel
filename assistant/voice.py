from __future__ import annotations

from array import array
from collections import deque
from contextlib import nullcontext
import importlib.util
import hashlib
import logging
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable
from types import SimpleNamespace

from .config import (
    VOICE_INPUT_DEVICE, VOICE_SAMPLE_RATE, VOICE_STT_MODEL, VOICE_TTS_MODEL,
    VOICE_TTS_SPEAKER, VOICE_TTS_SAMPLE_RATE, VOICE_THREADS, VOICE_TTS_ENGINE,
    VOICE_BEAM_SIZE, VOICE_MAX_SECONDS, VOICE_SILENCE_SECONDS,
)
from .voice_assets import resolve_whisper_snapshot

logger = logging.getLogger(__name__)


class VoiceError(RuntimeError):
    """Ошибка загрузки или использования голосового слоя."""


def _failure(message: str, error: Exception) -> VoiceError:
    logger.error("%s: %s", message, error, exc_info=True)
    return VoiceError(f"{message}: {type(error).__name__}: {error}")


def _check_model_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "models.silero.ai"
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError("Silero models must come from the official models.silero.ai HTTPS host")


class _OfficialModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        _check_model_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _UtteranceBuffer:
    """Energy gate: calibration, bounded pre-roll, sustained onset, silence end.

    This rejects silence and steady background, not every speech-like noise.
    Whisper VAD is the second gate. Feed little-endian mono int16, <=100 ms.
    """

    def __init__(self, sample_rate: int, silence_seconds: float = 0.7) -> None:
        self.sample_rate = sample_rate
        self.silence_seconds = silence_seconds
        self.pre_roll: deque[bytes] = deque()
        self.pre_duration = 0.0
        self.elapsed = 0.0
        self.noise = 80.0
        self.onset = 0.0
        self.voiced = 0.0
        self.silence = 0.0
        self.started = False
        self.chunks: list[bytes] = []

    def feed(self, chunk: bytes) -> bool:
        duration = len(chunk) / (2 * self.sample_rate)
        self.elapsed += duration
        level = VoiceSession._audio_level(chunk)
        if not self.started:
            self.pre_roll.append(chunk)
            self.pre_duration += duration
            while self.pre_duration > 0.4 and len(self.pre_roll) > 1:
                self.pre_duration -= len(self.pre_roll.popleft()) / (2 * self.sample_rate)
            if self.elapsed <= 0.2:
                self.noise = max(30.0, level)
                return False
        loud = level > max(250.0, self.noise * 2.5)
        if not self.started:
            if loud:
                self.onset += duration
                if self.onset + 1e-6 >= 0.15:
                    self.started = True
                    self.voiced = self.onset
                    self.chunks.extend(self.pre_roll)
                    self.pre_roll.clear()
            else:
                self.onset = 0.0
                self.noise = max(30.0, self.noise * 0.95 + level * 0.05)
            return False
        self.chunks.append(chunk)
        if loud:
            self.voiced += duration
            self.silence = 0.0
        else:
            self.silence += duration
        return self.silence + 1e-6 >= self.silence_seconds

    def audio(self) -> bytes:
        return b"".join(self.chunks) if self.started and self.voiced + 1e-6 >= 0.15 else b""


class VoiceSession:
    """Utterances for the caller's always-listen or wake-word loop.

    Owner sequences capture -> agent -> speak -> capture. Terminate the owner's
    subprocess to interrupt native inference; Python events cancel between calls.
    """

    def __init__(self, on_status: Callable[[str], None] | None = None, *,
                 allow_sapi_fallback: bool = True, allow_download: bool = False,
                 stt_model: str | None = None, tts_model: str | None = None,
                 tts_speaker: str | None = None,
                 tts_engine: str | None = None,
                 on_level: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._stt: Any | None = None
        self._tts: Any | None = None
        self._torch: Any | None = None
        self._tts_key: tuple[str, str] | None = None
        self._on_status = on_status
        self._allow_sapi_fallback = allow_sapi_fallback
        self.allow_download = allow_download
        self.stt_model = stt_model if stt_model is not None else VOICE_STT_MODEL
        self.tts_model = tts_model if tts_model is not None else VOICE_TTS_MODEL
        self.tts_speaker = tts_speaker if tts_speaker is not None else VOICE_TTS_SPEAKER
        self.tts_engine = tts_engine if tts_engine is not None else VOICE_TTS_ENGINE
        self._on_level = on_level
        self._last_level_at = 0.0
        self._stop_event = threading.Event()
        self._stt_warmed = False
        self._tts_warmed = False

    def _status(self, message: str) -> None:
        logger.info(message)
        if self._on_status:
            self._on_status(message)

    def _report_level(self, raw: bytes = b"", *, active: bool, force: bool = False) -> None:
        if not self._on_level:
            return
        now = time.monotonic()
        if active and not force and now - self._last_level_at < 0.08:
            return
        self._last_level_at = now
        rms = self._audio_level(raw) if active else 0.0
        dbfs = max(-96.0, 20 * math.log10(max(rms, 0.5) / 32768))
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        self._on_level({"active": active, "level": max(0.0, min(1.0, (dbfs + 60) / 60)) if active else 0.0,
                        "dbfs": round(dbfs, 1) if active else None,
                        "clipping": any(abs(sample) >= 32700 for sample in samples) if active else False})

    def prepare(self, *, speech: bool = False) -> None:
        """Warm recognition before opening the microphone, reporting load status."""
        self._load_stt()
        if speech:
            self._load_tts()

    def warmup(self, *, speech: bool = False) -> None:
        """Initialize VAD, encoder and synthesis kernels without any audio device."""
        import numpy as np
        if not self._stt_warmed:
            self._status("Проверка распознавания при запуске…")
            stt = self._load_stt()
            samples = np.zeros(16000, dtype=np.float32)
            segments, _ = stt.transcribe(samples, language="ru", beam_size=1, vad_filter=True)
            list(segments)
            stt.encode(stt.feature_extractor(samples))
            self._stt_warmed = True
        if speech and not self._tts_warmed:
            self._status("Проверка озвучки при запуске…")
            model, context = self._load_tts()
            speaker, rate = self._tts_settings(model)
            with context.inference_mode():
                audio = model.apply_tts(text="Готово.", speaker=speaker, sample_rate=rate,
                                        put_accent=True, put_yo=True)
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            if not np.asarray(audio).size or not np.isfinite(audio).all():
                raise VoiceError("Проверка озвучки не дала корректный звук")
            self._tts_warmed = True

    def _load_stt(self) -> Any:
        if self._stt is not None:
            return self._stt
        self._status("Подготовка Whisper из локальных файлов…")
        try:
            from faster_whisper import WhisperModel
            snapshot = resolve_whisper_snapshot(self.stt_model, allow_download=self.allow_download)
            self._stt = WhisperModel(str(snapshot), device="cpu", compute_type="int8",
                                     cpu_threads=VOICE_THREADS, local_files_only=True)
            return self._stt
        except Exception as error:
            raise _failure("Whisper не готов. Повторите установку с подготовкой голоса", error) from error

    def _load_tts(self) -> tuple[Any, Any]:
        if self.tts_engine == "piper":
            try:
                from .piper_voice import PiperBackend, get_piper_path
                path = get_piper_path()
                key = ("piper", str(path))
                if self._tts is None or self._tts_key != key:
                    self._status("Подготовка Piper из локальных файлов…")
                    self._tts = PiperBackend.load(path)
                    self._torch = SimpleNamespace(inference_mode=nullcontext)
                    self._tts_key = key
                return self._tts, self._torch
            except Exception as error:
                raise _failure("Не удалось загрузить Piper. Повторите подготовку голоса в установщике", error) from error
        if self.tts_engine != "silero":
            raise VoiceError(f"Неизвестный движок голоса: {self.tts_engine}")
        # Avoid silero's wrapper: undeclared OmegaConf dependency and cwd writes.
        # Packaged ru models need only torch and their own declared dependencies.
        key = ("ru", self.tts_model)
        if self._tts is not None and self._torch is not None and self._tts_key == key:
            return self._tts, self._torch
        self._status("Подготовка Silero из локальных файлов…")
        try:
            import torch
            if not re.fullmatch(r"v[3-5][\w]*_ru", key[1]):
                raise ValueError(f"Неподдерживаемая пакетная модель русского TTS: {key[1]}")
            torch.set_num_threads(VOICE_THREADS)
            cache = Path(torch.hub.get_dir()) / "silero" / key[0] / f"{key[1]}.pt"
            cache.parent.mkdir(parents=True, exist_ok=True)
            source = cache
            if not cache.exists():
                spec = importlib.util.find_spec("silero")
                installed = (Path(spec.origin).parent / "model" / cache.name
                             if spec and spec.origin else None)
                if installed and installed.is_file():
                    source = installed
                else:
                    if not self.allow_download:
                        raise FileNotFoundError("Модель Silero отсутствует. Повторите установку с подготовкой голоса; во время команды скачивание отключено.")
                    self._download_tts(
                        f"https://models.silero.ai/models/tts/{key[0]}/{cache.name}", cache)
            model = torch.package.PackageImporter(str(source)).load_pickle("tts_models", "model")
            model.to(torch.device("cpu"))
            self._tts, self._torch, self._tts_key = model, torch, key
            return model, torch
        except Exception as error:
            raise _failure("Не удалось загрузить Silero TTS", error) from error

    def _download_tts(self, url: str, cache: Path) -> None:
        """Official HTTPS only; 15s socket timeout, 90s transfer deadline.

        read1 performs at most one underlying read. Reset its CPython HTTPS
        socket timeout to the remaining budget so slow trickle responses cannot
        extend the deadline. Never publish empty or truncated cache entries.
        """
        _check_model_url(url)
        deadline = time.monotonic() + 90.0
        self._status("Скачивание Silero: таймаут чтения 15 с, общий срок 90 с...")
        temporary: Path | None = None
        received = 0
        digest = hashlib.sha256()
        last_status = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(dir=cache.parent, prefix=cache.stem + "-",
                                             suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                opener = urllib.request.build_opener(_OfficialModelRedirect())
                with opener.open(url, timeout=15.0) as response:
                    expected = response.headers.get("Content-Length")
                    expected = int(expected) if expected is not None else None
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Silero download exceeded its 90 second deadline")
                        if self._stop_event.is_set():
                            raise InterruptedError("Silero download stopped")
                        response.fp.raw._sock.settimeout(min(15.0, remaining))
                        chunk = response.read1(64 * 1024)
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Silero download exceeded its 90 second deadline")
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        digest.update(chunk)
                        now = time.monotonic()
                        if now - last_status >= 5.0:
                            self._status(f"Скачивание Silero: {received / 1048576:.1f} МБ")
                            last_status = now
                        if expected is not None and received >= expected:
                            break
            if received == 0 or (expected is not None and received != expected):
                raise ValueError(f"Incomplete Silero download: received {received}, expected {expected}")
            temporary.replace(cache)
            self._status(f"Silero скачан: {received} байт; SHA256 {digest.hexdigest()}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_wav(raw_audio: bytes, path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(VOICE_SAMPLE_RATE)
            output.writeframes(raw_audio)

    @staticmethod
    def _audio_level(raw_audio: bytes) -> float:
        samples = array("h")
        samples.frombytes(raw_audio)
        if sys.byteorder != "little":
            samples.byteswap()
        return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0

    def _capture_raw(self, stop_event: threading.Event, *, wait_for_speech: bool = True,
                     max_seconds: float = VOICE_MAX_SECONDS, start_timeout: float = 3.0,
                     silence_seconds: float = VOICE_SILENCE_SECONDS) -> bytes:
        # Compatibility argument wait_for_speech: all capture now requires speech
        # and closes on silence; stopping discards the pending utterance.
        if stop_event.is_set():
            return b""
        try:
            import sounddevice as sd
            device: Any = VOICE_INPUT_DEVICE if VOICE_INPUT_DEVICE != "" else None
            if isinstance(device, str) and device.isdecimal():
                device = int(device)
            info = sd.query_devices(device, "input")
            if info["max_input_channels"] < 1:
                raise ValueError(f"Устройство {info['name']} не имеет микрофонного входа")
            sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=VOICE_SAMPLE_RATE)
        except Exception as error:
            raise _failure(f"Микрофон недоступен (устройство {VOICE_INPUT_DEVICE or 'по умолчанию'})", error) from error
        chunks: queue.Queue[bytes] = queue.Queue(maxsize=40)

        def callback(indata: Any, frames: int, timing: Any, status: Any) -> None:
            if status:
                logger.warning("Микрофон: %s", status)
            try:
                chunks.put_nowait(bytes(indata))
            except queue.Full:
                pass

        vad = _UtteranceBuffer(VOICE_SAMPLE_RATE, silence_seconds)
        started_at = time.monotonic()
        self._status("Слушаю...")
        try:
            with sd.RawInputStream(samplerate=VOICE_SAMPLE_RATE,
                                   blocksize=max(1, VOICE_SAMPLE_RATE // 20), dtype="int16",
                                   channels=1, device=device, callback=callback):
                self._report_level(active=True, force=True)
                while not stop_event.is_set():
                    elapsed = time.monotonic() - started_at
                    if elapsed >= max_seconds or (not vad.started and elapsed >= start_timeout):
                        if vad.started and elapsed >= max_seconds:
                            raise VoiceError(f"Фраза длиннее {max_seconds:g} секунд. Команда не выполнена; разделите её на части.")
                        break
                    try:
                        chunk = chunks.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    self._report_level(chunk, active=True)
                    if vad.feed(chunk):
                        break
                    if vad.elapsed >= max_seconds or (not vad.started and vad.elapsed >= start_timeout):
                        if vad.started and vad.elapsed >= max_seconds:
                            raise VoiceError(f"Фраза длиннее {max_seconds:g} секунд. Команда не выполнена; разделите её на части.")
                        break
        except Exception as error:
            raise _failure("Не удалось записать микрофон", error) from error
        finally:
            self._report_level(active=False, force=True)
        return b"" if stop_event.is_set() else vad.audio()

    def transcribe_audio(self, raw_audio: bytes, stop_event: threading.Event | None = None) -> str | None:
        if not raw_audio or (stop_event is not None and stop_event.is_set()):
            return None
        if self._audio_level(raw_audio) < 100.0:
            return None
        try:
            import numpy as np

            model = self._load_stt()
            if stop_event is not None and stop_event.is_set():
                return None
            # Capture is already mono PCM at Whisper's 16 kHz input rate. Pass
            # normalized samples directly, avoiding WAV I/O and PyAV-version
            # incompatibilities in faster-whisper's file decoder.
            samples = np.frombuffer(raw_audio, dtype="<i2").astype(np.float32) / 32768.0
            if VOICE_SAMPLE_RATE != 16000:
                samples = np.interp(
                    np.arange(round(len(samples) * 16000 / VOICE_SAMPLE_RATE)) * VOICE_SAMPLE_RATE / 16000,
                    np.arange(len(samples)), samples).astype(np.float32)
            self._status("Распознавание речи...")
            if stop_event is not None and stop_event.is_set():
                return None
            segments, _ = model.transcribe(samples, language="ru", beam_size=VOICE_BEAM_SIZE,
                                           temperature=0, vad_filter=True,
                                           vad_parameters={"min_silence_duration_ms": 600, "speech_pad_ms": 400},
                                           condition_on_previous_text=True,
                                           initial_prompt="Цитадель. Открой браузер. Поставь таймер. Запомни заметку.",
                                           hotwords="Цитадель")
            parts = []
            for segment in segments:
                if stop_event is not None and stop_event.is_set():
                    return None
                parts.append(segment.text.strip())
            return " ".join(parts).strip() or None
        except Exception as error:
            raise _failure("Не удалось распознать речь", error) from error

    def capture_until_stop(self, stop_event: threading.Event, max_seconds: float = 30.0) -> str | None:
        """Compatibility alias for utterance capture, without keyboard gating."""
        return self.capture_utterance(stop_event, max_seconds)

    def capture_utterance(self, stop_event: threading.Event, max_seconds: float = VOICE_MAX_SECONDS) -> str | None:
        raw = self._capture_raw(stop_event, max_seconds=max_seconds)
        if stop_event.is_set() or not raw:
            return None
        return self.transcribe_audio(raw, stop_event)

    def record(self) -> str | None:
        """CLI compatibility: listen for one utterance, without push-to-talk."""
        return self.capture_utterance(threading.Event())

    @staticmethod
    def _speech_text(text: str) -> str:
        text = re.sub(r"```.*?```", " код пропущен ", text, flags=re.DOTALL)
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"https?://\S+|www\.\S+", " ", text)
        text = re.sub(r"<[^>]*>", " ", text)
        text = re.sub(r"(?m)^\s*(?:[-+*>#]+|\d+[.)])\s*", "", text)
        text = re.sub(r"[`*_#~|]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _speech_chunks(cls, text: str, limit: int = 240) -> list[str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        chunks: list[str] = []
        for sentence in re.split(r"(?<=[.!?…])\s+", cls._speech_text(text)):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit + 1)
                cut = cut if cut > 0 else limit
                chunks.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                chunks.append(sentence)
        return chunks

    def _tts_settings(self, model: Any) -> tuple[str, int]:
        speakers = getattr(model, "speakers", None)
        speaker = self.tts_speaker or "aidar"
        if speakers and speaker not in speakers:
            speaker = "aidar" if "aidar" in speakers else next(iter(speakers))
            self._status(f"Голос модели недоступен, выбран {speaker}")
        rates = getattr(model, "sample_rates", None)
        if rates is None:
            rates = getattr(model, "sample_rate", (8000, 24000, 48000))
        rates = [rates] if isinstance(rates, int) else list(rates)
        rates = [int(rate) for rate in rates if int(rate) > 0]
        rate = VOICE_TTS_SAMPLE_RATE
        if rates and rate not in rates:
            rate = 48000 if 48000 in rates else max(rates)
            self._status(f"Частота модели: {rate} Гц")
        return speaker, rate

    def stop(self) -> None:
        self._stop_event.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            logger.debug("Остановка аудио недоступна", exc_info=True)

    def _cancelled(self, cancel_event: threading.Event | None) -> bool:
        return self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set())

    def _speak_sapi(self, chunks: list[str], cancel_event: threading.Event | None) -> None:
        # comtypes is small and already used by pywinauto. Never spawn a helper
        # process: it could keep speaking after the owner subprocess is killed.
        import comtypes
        from comtypes.client import CreateObject

        self._status("Резервная озвучка Windows SAPI: используется системный машинный голос")
        comtypes.CoInitialize()
        speaker = None
        try:
            speaker = CreateObject("SAPI.SpVoice", dynamic=True)
            for chunk in chunks:
                if self._cancelled(cancel_event):
                    return
                speaker.Speak(chunk, 1)  # SVSFlagsAsync
                while not speaker.WaitUntilDone(50):
                    if self._cancelled(cancel_event):
                        return
        finally:
            try:
                if speaker is not None:
                    speaker.Speak("", 3)  # Async | PurgeBeforeSpeak
            finally:
                speaker = None
                comtypes.CoUninitialize()

    def speak(self, text: str, cancel_event: threading.Event | None = None) -> None:
        self._stop_event.clear()
        chunks = self._speech_chunks(text)
        if not chunks or self._cancelled(cancel_event):
            return
        pending = chunks
        try:
            model, torch = self._load_tts()
            import sounddevice as sd
            speaker, sample_rate = self._tts_settings(model)
            for index, chunk in enumerate(chunks):
                pending = chunks[index:]
                if self._cancelled(cancel_event):
                    return
                with torch.inference_mode():
                    audio = model.apply_tts(text=chunk, speaker=speaker, sample_rate=sample_rate,
                                            put_accent=True, put_yo=True)
                if self._cancelled(cancel_event):
                    return
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                try:
                    sd.play(audio, sample_rate, blocking=False)
                    while sd.get_stream().active:
                        if self._cancelled(cancel_event):
                            return
                        self._stop_event.wait(0.05)
                finally:
                    sd.stop()
        except Exception as error:
            failure = _failure(f"Не удалось озвучить ответ {self.tts_engine.title()}", error)
            if self._cancelled(cancel_event):
                return
            if self._allow_sapi_fallback and os.name == "nt":
                self._status(str(failure))
                try:
                    self._speak_sapi(pending, cancel_event)
                    return
                except Exception as fallback_error:
                    raise _failure(f"{failure}; Windows SAPI также недоступен", fallback_error) from fallback_error
            raise failure from error
