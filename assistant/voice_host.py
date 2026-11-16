"""Resident audio process: keep model weights when an agent task is cancelled."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import traceback
from typing import Any

from .worker import WakeGate


class ResidentVoice:
    def __init__(self, voice: Any, emit: Any) -> None:
        self.voice, self.emit = voice, emit
        self.commands: queue.Queue[dict] = queue.Queue()
        self.closed = threading.Event()
        self.interrupt = threading.Event()
        self.token = ""
        self.listening = False
        self.waiting = False
        self.gate = WakeGate("wake")
        self.serial = 0
        self.headset = False
        self.capture_stop = threading.Event()
        self.capture_thread: threading.Thread | None = None
        self.capture_mode = "wake"
        self.speaking = False
        self.capture_cooldown_until = 0.0

    def _capture(self):
        try:
            return self.voice.capture_utterance(self.capture_stop, max_seconds=20)
        except TypeError:
            # Small test/integration adapters may expose the old one-argument API.
            return self.voice.capture_utterance(self.capture_stop)

    def _start_listener(self) -> None:
        if self.capture_thread and self.capture_thread.is_alive():
            return
        self.capture_stop.clear()
        self.capture_mode = self.gate.mode
        self.capture_thread = threading.Thread(target=self._listen_loop, daemon=True,
                                               name="citadel-voice-listener")
        self.capture_thread.start()

    def _stop_listener(self) -> None:
        self.capture_stop.set()
        thread, self.capture_thread = self.capture_thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=.7)

    def _listen_loop(self) -> None:
        """Keep STT alive while the agent is thinking or TTS is playing."""
        while self.listening and not self.closed.is_set() and not self.capture_stop.is_set():
            if time.monotonic() < self.capture_cooldown_until:
                self.capture_stop.wait(.05)
                continue
            try:
                self.emit("listening", {"token": self.token,
                                         "armed": time.monotonic() < self.gate.armed_until})
                heard = self._capture()
                if self.capture_stop.is_set() or not self.listening:
                    continue
                if heard:
                    self.emit("heard", {"token": self.token, "text": heard})
                # While TTS is playing, require the wake word even in always mode
                # to prevent the assistant's own voice from becoming a command.
                gate = WakeGate("wake") if self.speaking and self.gate.mode == "always" else self.gate
                text, armed = gate.accept(heard or "")
                if armed:
                    self.emit("armed", {"token": self.token})
                if text:
                    self.capture_cooldown_until = time.monotonic() + .35
                    self.interrupt.set()  # interrupt speech immediately
                    self.emit("user", {"token": self.token, "text": text})
            except Exception as error:
                self.emit("error", {"token": self.token, "message": str(error), "phase": "capture"})
                return

    def speak(self, text: str) -> tuple[str, bool]:
        if self.capture_thread and self.capture_thread.is_alive():
            class PlaybackStop:
                def is_set(inner_self):
                    return self.interrupt.is_set()
            self.speaking = True
            try:
                self.voice.speak(text, cancel_event=PlaybackStop())
            finally:
                self.speaking = False
            return "", False
        if not self.headset or not self.listening:
            self.voice.speak(text, cancel_event=self.interrupt)
            return "", False

        finished = threading.Event()
        spoken_command = []
        wake = WakeGate("wake")

        class PlaybackStop:
            def is_set(inner_self):
                return self.interrupt.is_set() or bool(spoken_command)

        def listen_during_reply():
            try:
                while not finished.is_set() and not self.interrupt.is_set():
                    heard = self.voice.capture_utterance(self.interrupt, max_seconds=20)
                    command, armed = wake.accept(heard or "")
                    if command or armed:
                        spoken_command.append((command, armed))
                        return
            except Exception as error:
                self.emit("status", {"message": f"Микрофон во время ответа недоступен: {error}"})

        listener = threading.Thread(target=listen_during_reply, daemon=True)
        listener.start()
        try:
            self.voice.speak(text, cancel_event=PlaybackStop())
        finally:
            finished.set()
            listener.join()
        return spoken_command[0] if spoken_command and not self.interrupt.is_set() else ("", False)

    def submit(self, command: dict) -> None:
        kind = command.get("command")
        if kind in {"cancel", "listen", "speak", "warm"}:
            self.serial += 1
            command = {**command, "serial": self.serial}
            self.interrupt.set()
        if kind == "close":
            self.closed.set()
            self.interrupt.set()
            self.capture_stop.set()
        self.commands.put(command)

    def prepare(self, speech: bool) -> None:
        # Runtime is local-only; actual downloads belong to installer preparation.
        self.emit("warming", {"speech": speech})
        self.voice.prepare(speech=speech)
        if hasattr(self.voice, "warmup"):
            self.voice.warmup(speech=speech)
        self.emit("ready", {"speech": speech, "stt_instance": id(getattr(self.voice, "_stt", None)),
                            "voice": ("Piper · irina" if getattr(self.voice, "tts_engine", "") == "piper"
                                      else "Silero · " + getattr(self.voice, "tts_speaker", "")) if speech else "",
                            "tts_instance": id(getattr(self.voice, "_tts", None))})

    def run(self, speech: bool, *, prepared: bool = False) -> None:
        try:
            if not prepared:
                self.prepare(speech)
        except Exception as error:
            self.emit("error", {"message": str(error), "token": self.token, "phase": "initialization"})
            return
        while not self.closed.is_set():
            try:
                command = self.commands.get(timeout=.05 if self.listening and not self.waiting else .2)
            except queue.Empty:
                command = None
            if command:
                kind = command.get("command")
                if "serial" in command and command["serial"] != self.serial:
                    continue
                if kind == "cancel":
                    self.token = str(command.get("token", ""))
                    self.listening, self.waiting = False, False
                    self._stop_listener()
                    self.interrupt.clear()
                    continue
                if kind == "inspect":
                    self.emit("state", {"stt_instance": id(getattr(self.voice, "_stt", None)),
                                        "tts_instance": id(getattr(self.voice, "_tts", None))})
                    continue
                if kind == "headset":
                    self.headset = bool(command.get("enabled"))
                    continue
                if kind == "warm":
                    self.interrupt.clear()
                    try:
                        self.prepare(bool(command.get("speech")))
                    except Exception as error:
                        self.emit("error", {"message": str(error), "token": self.token})
                    continue
                if kind == "listen":
                    self.token = command["token"]
                    self.gate = WakeGate(command["mode"])
                    self.listening, self.waiting = True, False
                    self.interrupt.clear()
                    self._start_listener()
                if kind in {"speak", "resume"}:
                    token = str(command.get("token", ""))
                    if self.listening and token != self.token:
                        continue
                    self.token = token
                    self.interrupt.clear()
                    if kind == "speak":
                        self.emit("speaking", {"token": token})
                        try:
                            if command.get('chime'):
                                from .chime import play_chime
                                play_chime()
                                self.interrupt.wait(.35)
                            if self.interrupt.is_set():
                                continue
                            self.speak(command["text"])
                        except Exception as error:
                            self.emit("error", {"message": str(error), "token": token, "phase": "speech"})
                        if not self.interrupt.is_set():
                            self.emit("speech_done", {"token": token})
                        else:
                            self.emit("speech_interrupted", {"token": token})
                            self.interrupt.clear()
                    self.waiting = False
            if self.listening and not self.capture_thread:
                self._start_listener()
        self._stop_listener()


def main() -> int:
    output = sys.stdout
    sys.stdout = sys.stderr
    lock = threading.Lock()
    def emit(event: str, payload: Any = None) -> None:
        if event == "ready":
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
        with lock:
            output.write(json.dumps({"event": event, "payload": payload}, ensure_ascii=False) + "\n")
            output.flush()
    try:
        payload = json.loads(sys.stdin.readline())
        if payload.get("diagnostics"):
            import faulthandler
            faulthandler.dump_traceback_later(15, file=sys.stderr)
        def local_only(event, _arguments):
            if event in {"socket.connect", "http.client.connect"}:
                raise PermissionError("Голосовой процесс работает без сети. Модели подготавливаются установщиком.")
        sys.addaudithook(local_only)
        from .voice import VoiceSession
        service = None
        def level(value):
            emit("mic_level", {**value, "token": service.token if service else ""})
        voice = VoiceSession(on_status=lambda text: emit("status", {"message": text}), on_level=level)
        service = ResidentVoice(voice, emit)
        service.headset = bool(payload.get("headset"))
        # Initialize native numerical libraries before a thread blocks in the
        # Windows CRT stdin reader. This also establishes readiness before jobs.
        try:
            service.prepare(bool(payload.get("speech")))
        except Exception as error:
            emit("error", {"message": str(error), "phase": "initialization"})
            return 1
        def reader():
            for line in sys.stdin:
                try:
                    service.submit(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    continue
            service.submit({"command": "close"})
        threading.Thread(target=reader, daemon=True).start()
        service.run(bool(payload.get("speech")), prepared=True)
        return 0
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit("error", {"message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
