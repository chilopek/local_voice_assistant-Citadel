"""Opt-in real CPU inference, with persistent model caches and WAV-only output.

Run from the project root with python -m tests.live_voice_smoke, or set
PYTHONPATH to the root when invoking this file directly. No audio device is used.
Download first: --download-only --stt. Then validate: --offline --stt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import wave


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--stt", action="store_true")
    parser.add_argument("--sapi-fixture", action="store_true",
                        help="Validate the Windows fallback into a file when Silero is unavailable")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--text", default="Цитадель, найди информацию про SSH")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    os.environ["ASSISTANT_VOICE_THREADS"] = str(args.threads)
    os.environ["ASSISTANT_TTS_MODEL"] = "v5_6_ru"
    os.environ["ASSISTANT_TTS_SPEAKER"] = "aidar"
    os.environ["ASSISTANT_VOICE_SAMPLE_RATE"] = "16000"
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    socket.setdefaulttimeout(30)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"

    # Initialize application cache locations before importing model libraries.
    from assistant import paths  # noqa: F401
    import torch

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    if args.stt:
        from faster_whisper.utils import download_model
        print("RESOLVING Whisper base cache...", flush=True)
        stt_path = download_model("base", local_files_only=args.offline)
        os.environ["ASSISTANT_STT_MODEL"] = stt_path
        print(f"CACHED Whisper base: {stt_path}", flush=True)
    cache = Path(torch.hub.get_dir()) / "silero" / "ru" / "v5_6_ru.pt"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not args.sapi_fixture and not cache.is_file():
        if args.offline:
            raise FileNotFoundError(f"Offline TTS cache missing: {cache}")
        print(f"DOWNLOADING Silero -> {cache}", flush=True)
        from assistant.voice import VoiceSession
        VoiceSession(on_status=lambda message: print(message, flush=True))._download_tts(
            "https://models.silero.ai/models/tts/ru/v5_6_ru.pt", cache)
    if not args.sapi_fixture:
        print(f"CACHED Silero: {cache} ({cache.stat().st_size} bytes)", flush=True)
    if args.download_only:
        print("PASS: downloads complete; no inference performed", flush=True)
        return

    from assistant import voice
    import numpy as np

    session = voice.VoiceSession(on_status=lambda message: print(message, flush=True),
                                 allow_sapi_fallback=False)
    if args.sapi_fixture:
        sapi_fixture(session, args)
        return
    started = time.monotonic()
    model, _ = session._load_tts()
    loaded = time.monotonic()
    speaker, rate = session._tts_settings(model)
    pieces = []
    with torch.inference_mode():
        for text in session._speech_chunks(args.text):
            audio = model.apply_tts(text=text, speaker=speaker, sample_rate=rate,
                                    put_accent=True, put_yo=True)
            pieces.append(np.asarray(audio.detach().cpu().numpy(), dtype=np.float32).reshape(-1))
    audio = np.concatenate(pieces)
    synthesized = time.monotonic()
    assert audio.size > rate * 0.25 and np.isfinite(audio).all(), "Invalid synthesized audio"
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert rms > 0.001, f"Silent synthesis: RMS={rms}"
    output = Path(tempfile.mkdtemp(prefix="citadel-voice-smoke-"))
    wav_path = output / "silero-v5_6_ru-aidar.wav"
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    report = {
        "text": args.text, "tts_model": "v5_6_ru", "speaker": speaker,
        "sample_rate": rate, "duration_seconds": round(audio.size / rate, 3),
        "rms": round(rms, 5), "tts_load_seconds": round(loaded - started, 3),
        "synthesis_seconds": round(synthesized - loaded, 3), "wav": str(wav_path),
        "threads": args.threads, "audio_device_used": False,
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if args.stt:
        # VoiceSession writes 16 kHz PCM. Resample the synthesized fixture only;
        # no microphone, sounddevice, SAPI, or mock inference is involved.
        target_count = round(audio.size * voice.VOICE_SAMPLE_RATE / rate)
        resampled = np.interp(np.arange(target_count) * rate / voice.VOICE_SAMPLE_RATE,
                              np.arange(audio.size), audio)
        raw = (np.clip(resampled, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        start_stt = time.monotonic()
        recognized = session.transcribe_audio(raw)
        report["recognized"] = recognized
        report["stt_seconds"] = round(time.monotonic() - start_stt, 3)
        report["wake_word_exact"] = bool(recognized and "цитадель" in recognized.lower())
        print(json.dumps(report, ensure_ascii=False), flush=True)
        assert recognized, "Real Whisper returned no text for the synthesized fixture"
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: real inference; retained artifacts at {output}", flush=True)


def sapi_fixture(session, args) -> None:
    """Use a file output stream exclusively; never attach a speaker device."""
    import comtypes
    from comtypes.client import CreateObject
    import numpy as np

    output = Path(tempfile.mkdtemp(prefix="citadel-voice-sapi-smoke-"))
    wav_path = output / "sapi-fixture.wav"
    comtypes.CoInitialize()
    stream = speaker = None
    started = time.monotonic()
    try:
        speaker = CreateObject("SAPI.SpVoice", dynamic=True)
        voices = speaker.GetVoices()
        for index in range(voices.Count):
            candidate = voices.Item(index)
            if "419" in candidate.GetAttribute("Language").split(";"):
                speaker.Voice = candidate
                break
        description = speaker.Voice.GetDescription()
        stream = CreateObject("SAPI.SpFileStream", dynamic=True)
        stream.Format.Type = 18  # SAFT16kHz16BitMono
        stream.Open(str(wav_path), 3, False)  # SSFMCreateForWrite
        speaker.AudioOutputStream = stream
        speaker.Speak(args.text, 0)
    finally:
        if stream is not None:
            stream.Close()
        speaker = stream = None
        comtypes.CoUninitialize()
    with wave.open(str(wav_path), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(samples ** 2)))
    assert frames > 4000 and rms > 30, "Empty SAPI file synthesis"
    report = {"engine": "Windows SAPI (fallback; NOT Silero)", "voice": description,
              "text": args.text, "wav": str(wav_path), "duration_seconds": frames / 16000,
              "synthesis_seconds": round(time.monotonic() - started, 3),
              "audio_device_used": False, "rms_pcm16": round(rms, 3)}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if args.stt:
        started = time.monotonic()
        recognized = session.transcribe_audio(raw)
        report.update(recognized=recognized, stt_seconds=round(time.monotonic() - started, 3),
                      wake_word_exact=bool(recognized and "цитадель" in recognized.lower()))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        assert recognized, "Real Whisper returned no text for the SAPI fixture"
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: real fallback inference; retained artifacts at {output}", flush=True)


if __name__ == "__main__":
    main()
