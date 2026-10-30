"""JSON-lines child process for agent work, microphone and speech synthesis."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from typing import Any


class WakeGate:
    """One utterance containing a wake word, or one following it within 10 seconds."""
    def __init__(self, mode: str, timeout: float = 10) -> None:
        if mode not in {"always", "wake"}:
            raise ValueError("Неизвестный режим микрофона")
        self.mode, self.timeout, self.armed_until = mode, timeout, 0.0

    def accept(self, text: str, now: float | None = None) -> tuple[str, bool]:
        now = time.monotonic() if now is None else now
        text = text.strip()
        if not text:
            return "", False
        if self.mode == "always":
            return text, False
        match = re.search(r"\bцитадель\b", text, re.IGNORECASE)
        if match:
            command = text[match.end():].lstrip(" ,.!?:;—-")
            self.armed_until = 0 if command else now + self.timeout
            return command, not bool(command)
        if now < self.armed_until:
            self.armed_until = 0
            return text, False
        self.armed_until = 0
        return "", False


def main() -> int:
    output = sys.stdout
    sys.stdout = sys.stderr  # Third-party library diagnostics must not corrupt IPC.
    lock = threading.Lock()
    def emit(event: str, payload: Any = None) -> None:
        with lock:
            output.write(json.dumps({"event": event, "payload": payload}, ensure_ascii=False, default=str) + "\n")
            output.flush()
    try:
        payload = json.loads(sys.stdin.readline())
        if payload.get("job_owned"):
            os.environ["CITADEL_WORKER_JOB"] = "1"
        from .app import run_turn
        from .config import MODEL, OLLAMA_URL, PROJECT_ROOT
        from .memory import Memory
        from .ollama_client import OllamaClient
        from .voice import VoiceSession
        client = OllamaClient(OLLAMA_URL, MODEL)
        memory = Memory(PROJECT_ROOT / "data" / "memory.sqlite3")
        messages = payload["messages"]
        cancel = threading.Event()
        confirmation = threading.Event()
        reply = {"allowed": False}
        speech = {"enabled": bool(payload.get("speak"))}
        speech_cancel = threading.Event()
        tasks: queue.Queue[dict] = queue.Queue()
        def commands() -> None:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                    if message.get("command") == "confirm":
                        reply["allowed"] = bool(message.get("allowed"))
                        confirmation.set()
                    elif message.get("command") == "speech":
                        speech["enabled"] = bool(message.get("enabled"))
                        if not speech["enabled"]:
                            speech_cancel.set()
                    elif message.get("command") == "stop":
                        cancel.set()
                    elif message.get("command") == "new_task":
                        tasks.put(message["payload"])
                except (ValueError, TypeError):
                    pass
            cancel.set()
        threading.Thread(target=commands, daemon=True).start()
        voice = VoiceSession(on_status=lambda text: emit("status", text))
        def confirm(name: str, arguments: dict) -> bool:
            reply["allowed"] = False
            confirmation.clear()
            emit("confirm", {"name": name, "arguments": arguments})
            while not confirmation.wait(.1):
                if cancel.is_set():
                    return False
            return reply["allowed"] and not cancel.is_set()
        def answer(text: str) -> None:
            emit("status", "Обрабатываю задачу…")
            result = run_turn(client, memory, messages, text, confirm=confirm,
                              on_event=lambda event: emit("agent", event), cancel_event=cancel)
            if cancel.is_set():
                return
            # Commit conversation before synthesis, so a speech failure loses no answer.
            emit("messages", messages)
            emit("assistant", result)
            if speech["enabled"]:
                try:
                    speech_cancel.clear()
                    if not speech["enabled"]:
                        speech_cancel.set()
                    emit("status", "Озвучиваю ответ…")
                    voice.speak(result, cancel_event=speech_cancel)
                except Exception as error:
                    emit("error", f"Озвучка: {error}")
        while not cancel.is_set():
            messages[:] = payload["messages"]
            speech["enabled"] = bool(payload.get("speak"))
            try:
                if payload.get("mode") == "task":
                    answer(payload["text"])
                else:
                    gate = WakeGate(payload["mode"])
                    voice.prepare()
                    while not cancel.is_set():
                        emit("status", "Слушаю команду…" if gate.mode == "always" or time.monotonic() < gate.armed_until else "Слушаю слово «цитадель»…")
                        heard = voice.capture_utterance(cancel)
                        if cancel.is_set():
                            break
                        command, armed = gate.accept(heard)
                        if armed:
                            emit("status", "Цитадель услышана. Произнесите команду в течение 10 секунд")
                        if command:
                            emit("user", command)
                            try:
                                answer(command)
                            except Exception as error:
                                traceback.print_exc(file=sys.stderr)
                                emit("error", str(error))
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                emit("error", str(error))
            # Keep browser, model handles and speech caches across successful tasks.
            emit("task_done")
            while not cancel.is_set():
                try:
                    payload = tasks.get(timeout=.1)
                    break
                except queue.Empty:
                    pass
        return 0
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit("error", str(error))
        return 1
    finally:
        try:
            from .browser import BROWSER
            BROWSER.close()
        except Exception:
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
