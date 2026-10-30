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

    def submit(self, command: dict) -> None:
        kind = command.get("command")
        if kind in {"cancel", "listen", "speak", "warm"}:
            self.serial += 1
            command = {**command, "serial": self.serial}
            self.interrupt.set()
        if kind == "close":
            self.closed.set()
            self.interrupt.set()
        self.commands.put(command)

    def prepare(self, speech: bool) -> None:
        # Runtime is local-only; actual downloads belong to installer preparation.
        self.emit("warming", {"speech": speech})
        self.voice.prepare(speech=speech)
        if hasattr(self.voice, "warmup"):
            self.voice.warmup(speech=speech)
        self.emit("ready", {"speech": speech, "stt_instance": id(getattr(self.voice, "_stt", None)),
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
                    self.interrupt.clear()
                    continue
                if kind == "inspect":
                    self.emit("state", {"stt_instance": id(getattr(self.voice, "_stt", None)),
                                        "tts_instance": id(getattr(self.voice, "_tts", None))})
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
                if kind in {"speak", "resume"}:
                    token = str(command.get("token", ""))
                    if self.listening and token != self.token:
                        continue
                    self.token = token
                    self.interrupt.clear()
                    if kind == "speak":
                        self.emit("speaking", {"token": token})
                        try:
                            self.voice.speak(command["text"], cancel_event=self.interrupt)
                        except Exception as error:
                            self.emit("error", {"message": str(error), "token": token, "phase": "speech"})
                        if not self.interrupt.is_set():
                            self.emit("speech_done", {"token": token})
                    self.waiting = False
            if not self.listening or self.waiting or self.interrupt.is_set():
                continue
            token = self.token
            try:
                self.emit("listening", {"token": token, "armed": time.monotonic() < self.gate.armed_until})
                heard = self.voice.capture_utterance(self.interrupt)
                if self.interrupt.is_set():
                    continue
                text, armed = self.gate.accept(heard or "")
                if armed:
                    self.emit("armed", {"token": token})
                if text:
                    self.waiting = True
                    self.emit("user", {"token": token, "text": text})
            except Exception as error:
                self.listening = False
                self.emit("error", {"token": token, "message": str(error), "phase": "capture"})


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
