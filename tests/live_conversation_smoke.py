"""Opt-in actual-model dialogue and opening tests; OS launch itself is intercepted."""
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

from assistant.app import run_turn
from assistant.config import MODEL, OLLAMA_URL
from assistant.memory import Memory
from assistant.ollama_client import OllamaClient

case = sys.argv[1] if len(sys.argv) > 1 else "conversation"
with tempfile.TemporaryDirectory(prefix="citadel-dialogue-proof-") as folder:
    root = Path(folder)
    memory = Memory(root / "memory.sqlite")
    client = OllamaClient(OLLAMA_URL, MODEL)
    messages, events = [], []
    def event(item):
        events.append(item)
        if item["type"] in {"tool_start", "done"}:
            print(item["type"], item.get("name", ""), str(item.get("answer", ""))[:300], flush=True)
    with patch("assistant.app.PROJECT_ROOT", root):
        started = time.monotonic()
        if case == "opening":
            with patch("assistant.tools.os.startfile") as launch:
                answer = run_turn(client, memory, messages, "открой гугл", confirm=lambda *_: (_ for _ in ()).throw(AssertionError("Unnecessary permission")), on_event=event)
            names = [item["name"] for item in events if item["type"] == "tool_start"]
            assert names == ["open_url"], names
            launch.assert_called_once()
            assert "google." in launch.call_args.args[0], launch.call_args
            print("PASS: model requested one URL in OS default browser, without install/search/confirmation", flush=True)
        else:
            answer = run_turn(client, memory, messages, "Название нашего проекта — Северный Маяк 731. Это контекст разговора, запомни его только в этом диалоге. Ответь кратко, инструменты сейчас не нужны.", on_event=event)
            print("FIRST:", answer, flush=True)
            answer = run_turn(client, memory, messages, "Как я назвал наш проект и какой номер указал? Ответь одной строкой.", on_event=event)
            assert "северн" in answer.lower() and "маяк" in answer.lower() and "731" in answer, answer
            print("PASS: actual model retained the earlier user-supplied name and number", flush=True)
        print("ELAPSED:", round(time.monotonic() - started, 2), flush=True)
