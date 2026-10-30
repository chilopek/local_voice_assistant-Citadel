"""Opt-in checks using the installed Ollama model (no fake responses)."""
import argparse
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from assistant.app import run_turn
from assistant.config import MODEL, OLLAMA_URL
from assistant.memory import Memory
from assistant.ollama_client import OllamaClient


parser = argparse.ArgumentParser()
parser.add_argument("case", choices=["file", "web", "research"])
args = parser.parse_args()
events = []


class TimedClient(OllamaClient):
    def chat(self, messages, tools, cancel_event=None, images=None, timeout=120):
        started = time.monotonic()
        try:
            result = super().chat(messages, tools, cancel_event=cancel_event, images=images, timeout=timeout)
            print("CHAT:", {key: result.get(key) for key in ("done", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")}, flush=True)
            return result
        finally:
            print("CHAT_SECONDS:", round(time.monotonic() - started, 2), "TOOLS:", len(tools), flush=True)


def progress(event):
    events.append(event)
    if event["type"] in {"tool_start", "tool_result"}:
        print(event["type"], event.get("name"), str(event.get("arguments", event.get("result", "")))[:500], flush=True)


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    memory = Memory(root / "memory.sqlite")
    client = TimedClient(OLLAMA_URL, MODEL)
    prompt = (
        "Создай в рабочей папке файл agent-proof.txt с текстом «агент проверен», затем прочитай его инструментом и сообщи точный текст. Выполни задачу сейчас."
        if args.case == "file" else
        "найди информацию про ssh" if args.case == "research" else
        "Прочитай официальную страницу https://www.python.org/downloads/latest/ и сообщи номер последней стабильной версии Python, дату релиза и ссылку на источник. Проверь сведения инструментами."
    )
    started = time.monotonic()
    with patch("assistant.tools.WORKSPACE", root), patch("assistant.app.WORKSPACE", root), patch("assistant.app.PROJECT_ROOT", root), patch("assistant.tools.is_inside_workspace", lambda p: p.is_relative_to(root)):
        answer = run_turn(client, memory, [], prompt, confirm=lambda name, arguments: name == "write_file" and arguments.get("path") == "agent-proof.txt", on_event=progress)
    print("ANSWER:", answer, flush=True)
    print("ELAPSED:", round(time.monotonic() - started, 2), "MODEL:", MODEL, flush=True)
    successful = [e["name"] for e in events if e["type"] == "tool_result" and e.get("result", {}).get("ok")]
    if args.case == "file":
        assert (root / "agent-proof.txt").read_text(encoding="utf-8") == "агент проверен"
        assert "write_file" in successful and "read_file" in successful, successful
    elif args.case == "research":
        assert "search_web" in successful, successful
        assert "fetch_url" in successful or "browser_open" in successful, successful
        assert len(answer) > 200 and "http" in answer, answer
        assert "Достигнут лимит шагов" not in answer, answer
        assert "Не удалось сформировать итоговый ответ" not in answer, answer
        assert "Не удалось завершить синтез ответа" not in answer, answer
    else:
        assert "fetch_url" in successful or "browser_open" in successful, successful
        assert "python.org" in answer, answer
print("PASS: real model selected tools and verified the result")
