"""Installer-time voice cache preparation, without microphone or playback access."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from collections.abc import MutableMapping
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import time
import threading
import urllib.request
import wave
from typing import Any, Callable, Mapping

from .piper_voice import (PiperBackend, get_piper_path, PIPER_REPOSITORY,
                          PIPER_SUBDIRECTORY, PIPER_MODEL_NAME)

logger = logging.getLogger(__name__)


@contextmanager
def _download_status(label: str):
    stopped = threading.Event()
    started = time.monotonic()
    def heartbeat():
        while not stopped.wait(10):
            logger.info("[voice:download] %s: downloading, %d seconds elapsed", label, time.monotonic() - started)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=.1)


class VoiceAssetsError(RuntimeError):
    """A selected voice asset could not be prepared or validated."""


def _status(on_status: Callable[[str], None] | None, event: str, message: str) -> None:
    message = f"[voice:{event}] {message}"
    logger.info(message)
    if on_status is not None:
        on_status(message)


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output)
        if temporary.stat().st_size == 0:
            raise ValueError(f"Empty model asset: {source}")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_audio(audio: Any, name: str) -> Any:
    import numpy as np
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    audio = np.asarray(audio)
    if audio.size == 0 or not np.isfinite(audio).all() or not np.any(audio):
        raise ValueError(f"{name} synthesis returned empty, silent or non-finite audio")
    return audio


def prepare_piper_assets(config: Mapping[str, Any] | None = None,
                         on_status: Callable[[str], None] | None = None, *,
                         allow_download: bool = True) -> PiperBackend:
    """Prepare the official Irina ONNX, config and attribution, then synthesize WAV."""
    path = get_piper_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    names = (PIPER_MODEL_NAME, PIPER_MODEL_NAME + ".json", "MODEL_CARD")
    for name in names:
        target = path.parent / name
        if target.is_file() and target.stat().st_size > 0:
            _status(on_status, "piper", f"Cached {target.name}")
            continue
        from huggingface_hub import hf_hub_download
        arguments = dict(repo_id=PIPER_REPOSITORY, filename=f"{PIPER_SUBDIRECTORY}/{name}",
                         cache_dir=str(path.parent / "hub"))
        try:
            source = hf_hub_download(**arguments, local_files_only=True)
        except (OSError, ValueError):
            if not allow_download:
                raise FileNotFoundError(f"Piper asset not cached: {target}")
            _status(on_status, "piper:download", f"{PIPER_REPOSITORY}/{arguments['filename']}")
            with _download_status(f"Piper {name}"):
                source = hf_hub_download(**arguments, local_files_only=False)
        _atomic_copy(Path(source), target)
    _status(on_status, "piper", "Loading local Irina ONNX (CPU, bundled eSpeak)")
    backend = PiperBackend.load(path)
    rate = backend.sample_rates[0]
    _status(on_status, "piper:synthesis", f"Validating Irina synthesis and WAV ({rate} Hz)")
    audio = _validate_audio(backend.apply_tts(text="Проверка естественного голоса.",
                                            speaker="irina", sample_rate=rate), "Piper")
    import numpy as np
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    if not pcm or not any(pcm):
        raise ValueError("Piper synthesis quantized to silent PCM")
    fixture = path.parent / "piper-install-check.wav"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".wav", delete=False) as output:
            temporary = Path(output.name)
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(pcm)
        with wave.open(str(temporary), "rb") as check:
            if (check.getnchannels() != 1 or check.getsampwidth() != 2
                    or check.getframerate() != rate or check.getnframes() == 0
                    or check.readframes(check.getnframes()) != pcm):
                raise ValueError("Piper synthesis WAV verification failed")
        temporary.replace(fixture)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _status(on_status, "piper:ready", f"Irina validated locally; fixture: {fixture}; no playback")
    return backend


def _download_silero_tts(url: str, cache: Path,
                         on_status: Callable[[str], None] | None = None) -> None:
    """15-second network inactivity limit; allow a slow transfer up to 15 minutes."""
    from .voice import _check_model_url, _OfficialModelRedirect
    _check_model_url(url)
    deadline = time.monotonic() + 900.0
    last_status = deadline - 900.0
    temporary = None
    digest = hashlib.sha256()
    received = 0
    _status(on_status, "silero:download", "Official Silero HTTPS; inactivity timeout 15 s, transfer limit 15 min")
    try:
        with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            opener = urllib.request.build_opener(_OfficialModelRedirect())
            with opener.open(url, timeout=15.0) as response:
                expected = response.headers.get("Content-Length")
                expected = int(expected) if expected is not None else None
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Silero primary download exceeded its 900 second deadline")
                    response.fp.raw._sock.settimeout(min(15.0, remaining))
                    chunk = response.read1(64 * 1024)
                    now = time.monotonic()
                    if now >= deadline:
                        raise TimeoutError("Silero primary download exceeded its 900 second deadline")
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if now - last_status >= 3:
                        _status(on_status, "silero:download", f"{received / 1048576:.1f} MB received" + (f" / {expected / 1048576:.1f} MB" if expected else ""))
                        last_status = now
                    if expected is not None and received >= expected:
                        break
        if not received or (expected is not None and received != expected):
            raise ValueError(f"Incomplete Silero download: received {received}, expected {expected}")
        temporary.replace(cache)
        _status(on_status, "silero:download", f"Cached {received} bytes; SHA256 {digest.hexdigest()}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _setting(config: Mapping[str, Any], key: str, environment: str, default: Any) -> Any:
    # Read the installer configuration now: assistant.config may predate its write.
    return os.environ.get(environment, config.get(key, default))


def _complete_snapshot(path: Path) -> Path:
    # local_files_only does NOT suppress Whisper's tokenizer fallback download.
    # Require it before constructing the model, including for custom local models.
    for name in ("config.json", "model.bin", "tokenizer.json"):
        asset = path / name
        if not asset.is_file() or asset.stat().st_size == 0:
            raise FileNotFoundError(f"Whisper asset missing or empty: {asset}")
    return path


def resolve_whisper_snapshot(model: str, allow_download: bool = False) -> Path:
    """Return a complete local snapshot, never fetching a missing local path.

    Bare model aliases and ``owner/repository`` are HF identifiers. Absolute,
    explicitly relative, Windows-style and deeper paths are local directories.
    A cached tokenizer is mandatory to prevent Whisper's online fallback.
    """
    path = Path(model).expanduser()
    local_path = (path.is_absolute() or PureWindowsPath(model).drive != ""
                  or model.startswith((".", "~", "/", "\\")) or "\\" in model
                  or ("/" in model and not re.fullmatch(r"[\w.-]+/[\w.-]+", model)))
    if path.is_dir() or local_path:
        return _complete_snapshot(path)

    from faster_whisper.utils import download_model

    try:
        snapshot = _complete_snapshot(Path(download_model(model, local_files_only=True)))
        logger.info("[voice:cached] Whisper %s: %s", model, snapshot)
        return snapshot
    except (OSError, ValueError):
        if not allow_download:
            raise
    logger.info("[voice:download] Whisper %s: downloading selected Hugging Face snapshot", model)
    with _download_status(f"Whisper {model}"):
        return _complete_snapshot(Path(download_model(model, local_files_only=False)))


def _prepare_silero_assets(stt_model: str, tts_model: str, speaker: str,
                           threads: int, rate: int, allow_download: bool,
                           on_status: Callable[[str], None] | None) -> None:
    from .voice import VoiceSession
    session = VoiceSession(on_status=lambda message: _status(on_status, "tts", message),
                           allow_sapi_fallback=False, allow_download=allow_download,
                           stt_model=stt_model, tts_model=tts_model, tts_speaker=speaker,
                           tts_engine="silero")
    if allow_download:
        session._download_tts = lambda url, path: _download_silero_tts(url, path, on_status)
    model, torch = session._load_tts()
    torch.set_num_threads(threads)
    speakers = getattr(model, "speakers", None)
    if speakers and speaker not in speakers:
        speaker = "aidar" if "aidar" in speakers else next(iter(speakers))
        _status(on_status, "tts", f"Configured speaker unavailable; using {speaker}")
    rates = getattr(model, "sample_rates", None)
    if rates is None:
        rates = getattr(model, "sample_rate", (8000, 24000, 48000))
    rates = [rates] if isinstance(rates, int) else list(rates)
    rates = [int(value) for value in rates if int(value) > 0]
    if rates and rate not in rates:
        rate = 48000 if 48000 in rates else max(rates)
    _status(on_status, "synthesis", f"Validating Silero synthesis ({speaker}, {rate} Hz)")
    with torch.inference_mode():
        audio = model.apply_tts(text="Проверка голоса.", speaker=speaker,
                                sample_rate=rate, put_accent=True, put_yo=True)
    _validate_audio(audio, "Silero")


def prepare_voice_assets(config: Mapping[str, Any],
                         on_status: Callable[[str], None] | None = None, *,
                         allow_download: bool = True) -> None:
    """Cache the configured models and exercise the real CPU inference backends.

    Uses the runtime's standard HF/torch caches and trusted Silero downloader.
    ``allow_download=False`` validates an existing cache without downloading.
    The setup CLI's --skip-download skips this function altogether.
    """
    def status(event: str, message: str) -> None:
        _status(on_status, event, message)

    if not config.get("voice", False):
        status("skip", "Voice disabled; no voice assets requested")
        return

    stage = "configuration"
    try:
        stt_model = str(_setting(config, "stt_model", "ASSISTANT_STT_MODEL", "base"))
        tts_model = str(_setting(config, "tts_model", "ASSISTANT_TTS_MODEL", "v5_6_ru"))
        speaker = str(_setting(config, "tts_speaker", "ASSISTANT_TTS_SPEAKER", "aidar")) or "aidar"
        threads = int(_setting(config, "voice_threads", "ASSISTANT_VOICE_THREADS", 4))
        rate = int(_setting(config, "tts_sample_rate", "ASSISTANT_TTS_SAMPLE_RATE", 48000))

        stage = f"Whisper ({stt_model})"
        status("whisper", f"Preparing {stt_model} (CPU, int8)")
        from faster_whisper import WhisperModel

        snapshot = resolve_whisper_snapshot(stt_model, allow_download=allow_download)
        stt = WhisperModel(str(snapshot), device="cpu", compute_type="int8",
                           cpu_threads=threads, local_files_only=True)

        stage = "Whisper VAD (bundled ONNX / onnxruntime CPU)"
        status("vad", "Loading bundled Silero VAD with onnxruntime CPU")
        import numpy as np
        from faster_whisper.vad import get_vad_model, get_speech_timestamps

        # Clear the memoized wrapper so an earlier load cannot hide a broken asset.
        get_vad_model.cache_clear()
        get_vad_model()  # Constructs onnxruntime.InferenceSession from bundled files.
        get_speech_timestamps(np.zeros(16000, dtype=np.float32))
        segments, _ = stt.transcribe(np.zeros(16000, dtype=np.float32), language="ru",
                                     beam_size=1, vad_filter=True,
                                     condition_on_previous_text=False)
        list(segments)  # Transcription is lazy; consume it to validate the VAD path.
        del stt

        engine = str(config.get("tts_engine", "silero")).lower()
        if engine not in {"silero", "piper"}:
            raise ValueError(f"Unknown configured TTS engine: {engine}")
        if engine == "silero":
            stage = f"Silero TTS (ru/{tts_model}, speaker={speaker})"
            status("tts", f"Preparing ru/{tts_model}, speaker={speaker}")
            try:
                _prepare_silero_assets(stt_model, tts_model, speaker, threads, rate,
                                       allow_download, on_status)
            except Exception as silero_error:
                status("fallback", f"Silero unavailable: {type(silero_error).__name__}: {silero_error}; "
                       "preparing local neural Piper Irina (not Windows SAPI)")
                stage = f"Piper TTS fallback after Silero failure ({silero_error})"
                prepare_piper_assets(config, on_status, allow_download=allow_download)
                engine = "piper"
        else:
            stage = "Piper TTS (Irina)"
            prepare_piper_assets(config, on_status, allow_download=allow_download)
        if isinstance(config, MutableMapping):
            config["tts_engine"] = engine
        status("ready", f"Whisper {stt_model}, VAD and {engine} TTS validated; no playback")
    except Exception as error:
        message = f"{stage}: {type(error).__name__}: {error}"
        status("error", message)
        raise VoiceAssetsError(f"Voice assets preparation failed: {message}") from error
