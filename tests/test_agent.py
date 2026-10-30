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
        self.log_root = patch("assistant.app.PROJECT_ROOT", Path(self.folder.name))
        self.log_root.start()
        self.addCleanup(self.log_root.stop)

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

    def test_external_reads_are_allowed_and_unknown_browser_writes_need_confirmation(self):
        self.assertFalse(needs_confirmation("read_file", {"path": str(Path(self.folder.name) / "private.txt")}))
        self.assertTrue(needs_confirmation("browser_action", {"action": "click"}))
        self.assertFalse(needs_confirmation("browser_action", {"action": "scroll"}))
        self.assertFalse(needs_confirmation("search_web", {"query": "test"}))

    def test_command_utf8_and_hidden_process(self):
        output = execute("run_powershell", {"command": "Write-Output 'Привет, агент'"})
        self.assertEqual(output["return_code"], 0)
        self.assertIn("Привет, агент", output["stdout"])

    def test_research_reads_sources_then_forces_grounded_final(self):
        for topic in ("ssh", "миграции птиц"):
            with self.subTest(topic=topic):
                class ResearchModel(ScriptedModel):
                    def chat(self, messages, tools):
                        self.tool_sets.append(tools)
                        return super().chat(messages, tools)
                model = ResearchModel([
                    {"message": {"content": "Ответ по источникам https://one.test и https://two.test"}},
                ])
                model.tool_sets = []
                def execute(name, args):
                    if name == "search_web":
                        return {"results": [{"url": "https://one.test"}, {"url": "https://two.test"}]}
                    return {"url": args["url"], "text": "Проверенные сведения. " * 20}
                with patch("assistant.app.execute", side_effect=execute):
                    answer = app.run_turn(model, self.memory, [], f"найди информацию про {topic}")
                self.assertIn("https://one.test", answer)
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(model.tool_sets[-1], [])
                self.assertIn("только по уже доступным данным", model.requests[-1][0]["content"])

    def test_duplicate_sources_and_empty_reads_do_not_count_as_evidence(self):
        model = ScriptedModel([
            call("search_web", {"query": "topic"}),
            call("fetch_url", {"url": "https://one.test"}),
            call("fetch_url", {"url": "https://one.test", "start": 3500}),
            call("fetch_url", {"url": "https://empty.test"}),
            {"message": {"content": "Частичный ответ"}},
        ])
        def execute(name, args):
            if name == "search_web":
                return {"results": [{"url": "https://one.test"}]}
            return {"url": args["url"], "text": "" if "empty" in args["url"] else "evidence " * 30}
        with patch("assistant.app.execute", side_effect=execute):
            self.assertEqual(app.run_turn(model, self.memory, [], "research topic"), "Частичный ответ")
        self.assertNotIn("Инструменты отключены", model.requests[-1][-1].get("content", ""))

    def test_repeated_failures_force_final_with_original_error_context(self):
        model = ScriptedModel([call("fetch_url", {"url": "https://bad.test"}) for _ in range(2)] +
                              [{"message": {"content": "Источник недоступен: timeout"}}])
        with patch("assistant.app.execute", side_effect=TimeoutError("timeout")) as execute:
            answer = app.run_turn(model, self.memory, [], "найди информацию про topic")
        self.assertEqual(execute.call_count, 3)
        self.assertIn("timeout", answer)
        self.assertIn("timeout", str(model.requests[-1]))

    def test_no_progress_and_step_limit_both_request_final(self):
        for limit, repeats in ((24, 5), (3, 2)):
            with self.subTest(limit=limit):
                model = ScriptedModel([call("get_environment", {}) for _ in range(repeats)] +
                                      [{"message": {"content": "Доступны только сведения об окружении"}}])
                with patch("assistant.app.MAX_STEPS", limit), patch("assistant.app.execute", return_value={"os": "Windows"}):
                    answer = app.run_turn(model, self.memory, [], "Проверка")
                self.assertIn("окружении", answer)
                self.assertIn("Инструменты отключены", str(model.requests[-1]))

    def test_research_timeout_reserves_a_final_request(self):
        model = ScriptedModel([{"message": {"content": "Подтверждённых сведений пока нет"}}])
        with patch("assistant.app.time.monotonic", side_effect=[0, 101, 102]):
            answer = app.run_turn(model, self.memory, [], "research topic")
        self.assertIn("нет", answer)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("время исследования", model.requests[0][0]["content"])

    def test_denial_completes_pending_tool_links_without_final_chat(self):
        response = {"message": {"tool_calls": [
            {"id": "a", "function": {"name": "run_python", "arguments": {"code": "print(1)"}}},
            {"id": "b", "function": {"name": "get_time", "arguments": {}}},
        ]}}
        messages = []
        model = ScriptedModel([response])
        with patch("assistant.app.execute") as execute:
            app.run_turn(model, self.memory, messages, "Действие", confirm=lambda *_: False)
        execute.assert_not_called()
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["a", "b"])
        self.assertEqual(len(model.requests), 1)

    def test_cancel_during_confirmation_does_not_execute(self):
        cancel = threading.Event()
        def confirm(*_):
            cancel.set()
            return True
        model = ScriptedModel([call("run_python", {"code": "print(1)"})])
        with patch("assistant.app.execute") as execute:
            answer = app.run_turn(model, self.memory, [], "Действие", confirm=confirm, cancel_event=cancel)
        execute.assert_not_called()
        self.assertIn("остановлена", answer)

    def test_cancel_in_forced_final_is_strict(self):
        cancel = threading.Event()
        class Model:
            def chat(self, messages, tools, cancel_event=None):
                self.asserted = (tools, cancel_event)
                cancel.set()
                return {"message": {"content": "should not return"}}
        model = Model()
        with patch("assistant.app.MAX_STEPS", 1):
            answer = app.run_turn(model, self.memory, [], "research topic", cancel_event=cancel)
        self.assertEqual(model.asserted, ([], cancel))
        self.assertIn("остановлена", answer)

    def test_model_can_request_another_tool_group(self):
        class Model(ScriptedModel):
            def chat(self, messages, tools):
                self.tools = tools
                return super().chat(messages, tools)
        model = Model([call("select_tools", {"groups": ["files"]}), {"message": {"content": "Готово"}}])
        with patch("assistant.app.execute") as execute:
            app.run_turn(model, self.memory, [], "research topic")
        self.assertEqual(execute.call_args.args[0], "search_web")
        self.assertIn("read_file", {s["function"]["name"] for s in model.tools})

    def test_images_only_reach_next_request_of_vision_capable_client(self):
        for vision in (False, True):
            with self.subTest(vision=vision):
                class Model:
                    supports_vision = vision
                    def __init__(self):
                        self.requests = []
                    def chat(self, messages, tools, cancel_event=None, images=None):
                        self.requests.append(images)
                        if len(self.requests) == 1:
                            return call("read_screen", {})
                        return {"message": {"content": "Прочитан текст экрана"}}
                model = Model()
                screen = {"type": "function", "function": {"name": "read_screen", "parameters": {}}}
                messages = []
                with patch("assistant.app.TOOL_SCHEMAS", app.TOOL_SCHEMAS + [screen]), patch("assistant.app.execute", return_value={"image_base64": "cGl4ZWxz", "ocr_text": "Screen text"}):
                    app.run_turn(model, self.memory, messages, "прочитай экран")
                self.assertEqual(model.requests, [None, ["cGl4ZWxz"] if vision else None])
                result = next(m["content"] for m in messages if m["role"] == "tool")
                self.assertIn("Screen text", result)
                self.assertNotIn("cGl4ZWxz", result)

    def test_screenshot_path_is_encoded_without_requiring_vision_in_old_fakes(self):
        path = Path(self.folder.name) / "screen.png"
        path.write_bytes(b"pixels")
        self.assertEqual(app._screen_images({"screenshot_path": str(path)}), ["cGl4ZWxz"])
        model = ScriptedModel([call("take_screenshot", {}), {"message": {"content": "Снимок сохранён"}}])
        with patch("assistant.app.execute", return_value={"path": str(path)}):
            self.assertIn("сохранён", app.run_turn(model, self.memory, [], "скриншот"))

    def test_cancelled_screen_result_never_journals_base64(self):
        cancel = threading.Event()
        events, messages = [], []
        model = ScriptedModel([call("read_screen", {})])
        def execute(*_):
            cancel.set()
            return {"image_base64": "PRIVATE_PIXELS", "text": "OCR observation"}
        with patch("assistant.app.execute", side_effect=execute):
            answer = app.run_turn(model, self.memory, messages, "прочитай экран", cancel_event=cancel, on_event=events.append)
        self.assertIn("остановлена", answer)
        self.assertNotIn("PRIVATE_PIXELS", json.dumps(events) + json.dumps(messages))
        self.assertNotIn("PRIVATE_PIXELS", (Path(self.folder.name) / "data/agent-events.jsonl").read_text(encoding="utf-8"))

    def test_final_synthesis_instruction_does_not_disable_tools_next_turn(self):
        messages = []
        model = ScriptedModel([{"message": {"content": "Частичный итог"}}])
        with patch("assistant.app.MAX_STEPS", 1):
            app.run_turn(model, self.memory, messages, "research topic")
        self.assertFalse(any("Инструменты отключены" in m.get("content", "") for m in messages))

    def test_location_is_exposed_even_in_research_toolset(self):
        class Model:
            def chat(self, messages, tools):
                self.names = {s["function"]["name"] for s in tools}
                return {"message": {"content": "Готово"}}
        model = Model()
        with patch("assistant.app.execute", return_value={"results": []}):
            app.run_turn(model, self.memory, [], "найди информацию")
        self.assertIn("get_location", model.names)
        self.assertTrue({"search_web", "fetch_url", "get_environment", "select_tools"} <= model.names)
        self.assertFalse({"run_python", "desktop_action", "write_file"} & model.names)

    def test_one_failed_prefetch_source_still_synthesizes_without_more_browsing(self):
        model = ScriptedModel([{"message": {"content": "Подтверждено первым источником https://one.test"}}])
        def execute(name, args):
            if name == "search_web":
                return {"results": [{"url": "https://one.test"}, {"url": "https://two.test"}]}
            if "two" in args["url"]:
                raise TimeoutError("TLS timeout")
            return {"url": args["url"], "text": "Grounded observations. " * 20}
        with patch("assistant.app.execute", side_effect=execute) as action:
            answer = app.run_turn(model, self.memory, [], "найди информацию про протоколы")
        self.assertEqual(action.call_count, 3)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("TLS timeout", str(model.requests[0]))
        self.assertIn("https://one.test", answer)

    def test_failed_final_returns_readable_grounded_excerpts_and_partial_errors(self):
        class Model:
            def chat(self, messages, tools):
                self.tools = tools
                raise app.OllamaError("Истекло время запроса Ollama")
        def execute(name, args):
            if name == "search_web":
                return {"results": [{"url": "https://one.test"}, {"url": "https://two.test"}]}
            if "two" in args["url"]:
                raise TimeoutError("TLS timeout")
            return {"url": args["url"], "title": "Источник", "text": "Содержательное наблюдение, полученное из источника. " * 8}
        model = Model()
        with patch("assistant.app.execute", side_effect=execute):
            answer = app.run_turn(model, self.memory, [], "найди информацию")
        self.assertEqual(model.tools, [])
        self.assertIn("[Источник](https://one.test)", answer)
        self.assertIn("Содержательное наблюдение", answer)
        self.assertIn("TLS timeout", answer)
        self.assertIn("Истекло время запроса", answer)
        self.assertNotIn('"ok"', answer)
        self.assertNotIn('"result"', answer)


if __name__ == "__main__":
    unittest.main()
