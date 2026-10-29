from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from assistant import app
from assistant.memory import Memory
from assistant.tools import execute, needs_confirmation


def call(name: str, arguments: dict | str) -> dict:
    return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}}


class ScriptedModel:
    def __init__(self, responses: list[dict]):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools):
        self.requests.append(list(messages))
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.folder.name) / "memory.sqlite")

    def tearDown(self):
        self.folder.cleanup()

    def test_agent_recovers_after_bad_tool_and_verifies_result(self):
        model = ScriptedModel([
            call("read_file", {"path": "missing.txt"}),
            call("write_file", {"path": "created.txt", "content": "Проверенный результат"}),
            call("read_file", {"path": "created.txt"}),
            {"message": {"role": "assistant", "content": "Файл создан и проверен."}},
        ])
        messages, events = [], []
        with patch("assistant.tools.WORKSPACE", Path(self.folder.name)), patch("assistant.tools.is_inside_workspace", lambda p: p.is_relative_to(self.folder.name)):
            answer = app.run_turn(model, self.memory, messages, "Создай и проверь файл", confirm=lambda *_: True, on_event=events.append)
        self.assertEqual(answer, "Файл создан и проверен.")
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertIn("Проверенный результат", results[2]["result"]["content"])
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_start"], ["read_file", "write_file", "read_file"])

    def test_denial_stops_all_following_actions(self):
        model = ScriptedModel([call("run_powershell", {"command": "Remove-Item important.txt"})])
        with patch("assistant.app.execute") as action:
            result = app.run_turn(model, self.memory, [], "Действие", confirm=lambda *_: False)
        action.assert_not_called()
        self.assertIn("остановлена", result)
        self.assertEqual(len(model.requests), 1)

    def test_stop_during_generation_never_executes_tool(self):
        cancel = threading.Event()
        class CancellingModel:
            def chat(self, *_):
                cancel.set()
                return call("run_python", {"code": "print('should not run')"})
        with patch("assistant.app.execute") as action:
            result = app.run_turn(CancellingModel(), self.memory, [], "Стоп", cancel_event=cancel)
        action.assert_not_called()
        self.assertIn("остановлена", result)

    def test_bad_json_is_feedback_for_model_not_process_crash(self):
        model = ScriptedModel([call("get_time", "{broken"), call("get_time", {}), {"message": {"content": "Проверено"}}])
        messages = []
        app.run_turn(model, self.memory, messages, "Время")
        results = [m for m in messages if m["role"] == "tool"]
        self.assertFalse(json.loads(results[0]["content"])["ok"])
        self.assertEqual(results[1]["tool_name"], "get_time")
        self.assertTrue(json.loads(results[1]["content"])["ok"])

    def test_nonzero_exit_is_reported_as_failure(self):
        model = ScriptedModel([call("run_python", {"code": "raise ValueError('test')"}), {"message": {"content": "Ошибка обнаружена"}}])
        messages = []
        app.run_turn(model, self.memory, messages, "Проверка", confirm=lambda *_: True)
        result = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
        self.assertFalse(result["ok"])
        self.assertIn("ValueError", result["result"]["stderr"])

    def test_external_files_and_browser_writes_need_confirmation(self):
        self.assertTrue(needs_confirmation("read_file", {"path": str(Path(self.folder.name) / "private.txt")}))
        self.assertTrue(needs_confirmation("browser_action", {"action": "click"}))
        self.assertFalse(needs_confirmation("browser_action", {"action": "scroll"}))
        self.assertFalse(needs_confirmation("search_web", {"query": "test"}))

    def test_command_utf8_and_hidden_process(self):
        output = execute("run_powershell", {"command": "Write-Output 'Привет, агент'"})
        self.assertEqual(output["return_code"], 0)
        self.assertIn("Привет, агент", output["stdout"])


if __name__ == "__main__":
    unittest.main()
