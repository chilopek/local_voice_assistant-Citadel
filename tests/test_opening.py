from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant import app, tools
from assistant.memory import Memory


def call(name, arguments):
    return {"message": {"role": "assistant", "tool_calls": [
        {"id": "opening", "function": {"name": name, "arguments": arguments}}]}}


class OpeningTests(unittest.TestCase):
    def test_default_browser_once_without_managed_browser(self):
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start, \
                patch("assistant.browser.BROWSER.call") as managed, patch.object(tools.webbrowser, "open") as other:
            result = tools.execute("open_url", {"url": "https://www.google.com"})
        start.assert_called_once_with("https://www.google.com")
        managed.assert_not_called()
        other.assert_not_called()
        self.assertTrue(result["request_accepted"])
        self.assertTrue(result["launched"])
        self.assertNotIn("text", result)

    def test_http_only_and_failures_are_honest(self):
        with patch.object(tools.os, "startfile", create=True) as start:
            for url in ("file:///C:/x.exe", "javascript:alert(1)", "ftp://host", "https://", "https://host\n--flag", "https://host\\x"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    tools.open_url(url)
            start.assert_not_called()
        with patch.object(tools.sys, "platform", "linux"), patch.object(tools.webbrowser, "open", return_value=False):
            self.assertFalse(tools.open_url("http://example.com")["ok"])
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", side_effect=OSError("no handler"), create=True):
            with self.assertRaises(OSError):
                tools.open_url("https://example.com")

    def test_trusted_resolution_shared_by_confirmation_and_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder).resolve() / "System32/calc.exe"
            exe.parent.mkdir()
            exe.touch()
            with patch.object(tools.sys, "platform", "win32"), \
                    patch.object(tools, "_trusted_programs", return_value={"calc": ("gui", [exe])}), \
                    patch.object(tools.subprocess, "Popen") as popen:
                popen.return_value.pid = 123
                self.assertFalse(tools.needs_confirmation("launch_program", {"program": "calc.exe"}))
                self.assertFalse(tools.needs_confirmation("launch_program", {"program": str(exe)}))
                result = tools.launch_program("calc.exe")
                self.assertEqual(popen.call_args.args[0], [str(exe.resolve())])
                self.assertEqual(result["pid"], 123)
                for program, args in (("calc", ["/evil"]), (str(Path(folder) / "calc.exe"), []),
                                      ("powershell.exe", []), ("cmd.exe", []), ("installer.exe", [])):
                    self.assertTrue(tools.needs_confirmation("launch_program", {"program": program, "arguments": args}))

    def test_browser_flags_and_explorer_files_still_confirm(self):
        with tempfile.TemporaryDirectory() as folder:
            chrome, explorer = Path(folder).resolve() / "chrome.exe", Path(folder).resolve() / "explorer.exe"
            chrome.touch()
            explorer.touch()
            with patch.object(tools.sys, "platform", "win32"), patch.object(tools, "_trusted_programs", return_value={
                    "chrome": ("browser", [chrome]), "explorer": ("explorer", [explorer])}):
                for args in ([], ["https://example.com"], ["http://example.com"]):
                    self.assertFalse(tools.needs_confirmation("launch_program", {"program": "chrome", "arguments": args}))
                for args in (["--remote-debugging-port=9222"], ["--load-extension=C:/evil"], ["file:///C:/evil"], ["--app=https://example.com"], "https://example.com"):
                    self.assertTrue(tools.needs_confirmation("launch_program", {"program": "chrome", "arguments": args}))
                self.assertFalse(tools.needs_confirmation("launch_program", {"program": "explorer", "arguments": [folder]}))
                self.assertTrue(tools.needs_confirmation("launch_program", {"program": "explorer", "arguments": [str(chrome)]}))
        for name in ("run_python", "run_powershell", "download_file", "desktop_action"):
            self.assertTrue(tools.needs_confirmation(name, {}))

    def test_missing_chrome_does_not_launch_or_install(self):
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools, "_trusted_programs", return_value={"chrome": ("browser", [])}), \
                patch.object(tools.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(FileNotFoundError, "Установка не выполнялась"):
                tools.launch_program("chrome")
            popen.assert_not_called()

    def test_system_gui_allowlist_and_missing_app_turn(self):
        with patch.object(tools.sys, "platform", "win32"), patch.object(Path, "is_file", return_value=True):
            for program in ("calc", "notepad", "mspaint", "explorer"):
                with self.subTest(program=program):
                    self.assertFalse(tools.needs_confirmation("launch_program", {"program": program}))
        requests = []
        class Model:
            def chat(self, messages, schemas):
                requests.append((list(messages), schemas))
                return call("launch_program", {"program": "chrome"}) if len(requests) == 1 else {"message": {"content": "Chrome не найден. Установка не выполнялась."}}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), \
                patch.object(tools, "_trusted_programs", return_value={"chrome": ("browser", [])}), \
                patch.object(tools.subprocess, "Popen") as popen:
            confirm = Mock(side_effect=AssertionError("unexpected confirmation"))
            answer = app.run_turn(Model(), Memory(Path(folder) / "memory.sqlite"), [], "открой Хром", confirm=confirm)
        confirm.assert_not_called()
        popen.assert_not_called()
        self.assertIn("не найден", answer)
        self.assertEqual(len(requests), 1)

    def test_open_route_is_compact_generic_and_finishes_after_one_action(self):
        for text in ("открой гугл", "откройGoogle", "открой Хром", "открой https://example.com", "запусти калькулятор"):
            self.assertEqual(app._task_tools(text), (False, {"opening"}))
        self.assertEqual(app._task_tools("открой сайт и нажми вход"), (False, {"browser"}))
        self.assertEqual(app._task_tools("открой Google и найди погоду"), (False, {"browser"}))
        requests = []
        class Model:
            def chat(self, messages, schemas):
                requests.append((list(messages), schemas))
                return call("open_url", {"url": "https://www.google.com"}) if len(requests) == 1 else {"message": {"content": "Запрос открытия принят."}}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start:
            confirm = Mock(side_effect=AssertionError("unexpected confirmation"))
            messages = []
            app.run_turn(Model(), Memory(Path(folder) / "memory.sqlite"), messages, "открой гугл", confirm=confirm)
        start.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual({s["function"]["name"] for s in requests[0][1]}, {"open_url", "launch_program", "select_tools"})
        self.assertEqual(len(requests), 1)
        self.assertEqual([m["tool_name"] for m in messages if m["role"] == "tool"], ["open_url"])
        self.assertTrue(json.loads(next(m["content"] for m in messages if m["role"] == "tool"))["ok"])
        self.assertIn("Ссылка передана основному браузеру", messages[-1]["content"])

    def test_two_urls_in_one_batch_finish_without_second_chat_or_expansion(self):
        urls = ["https://www.google.com", "https://www.youtube.com"]
        response = {"message": {"role": "assistant", "tool_calls": [
            {"id": str(i), "function": {"name": "open_url", "arguments": {"url": url}}} for i, url in enumerate(urls)] + [
            {"id": "expand", "function": {"name": "select_tools", "arguments": {"groups": ["python"]}}}]}}
        model = Mock()
        model.chat.side_effect = [response, AssertionError("second chat")]
        messages = []
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start:
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), messages, "открой Google и YouTube")
        self.assertEqual([c.args[0] for c in start.call_args_list], urls)
        model.chat.assert_called_once()
        self.assertTrue(all(url in answer for url in urls))
        self.assertEqual(answer.count("Ссылка передана основному браузеру"), 2)
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["0", "1", "expand"])
        self.assertTrue(json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "expand"))["skipped"])

    def test_failed_open_does_not_prevent_next_open_or_claim_success(self):
        response = {"message": {"tool_calls": [
            {"id": str(i), "function": {"name": "open_url", "arguments": {"url": url}}}
            for i, url in enumerate(["https://failed.test", "https://opened.test"])]}}
        model = Mock()
        model.chat.side_effect = [response, AssertionError("second chat")]
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), \
                patch.object(tools.os, "startfile", side_effect=[OSError("no handler"), None], create=True) as start:
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), [], "открой два сайта")
        self.assertEqual(start.call_count, 2)
        model.chat.assert_called_once()
        self.assertIn("no handler", answer)
        self.assertIn("Ссылка передана основному браузеру: https://opened.test", answer)
        self.assertNotIn("Ссылка передана основному браузеру: https://failed.test", answer)

    def test_program_success_receipt_uses_actual_tool_output(self):
        model = Mock()
        model.chat.side_effect = [call("launch_program", {"program": "calc"}), AssertionError("second chat")]
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(app, "needs_confirmation", return_value=False), \
                patch.object(app, "execute", return_value={"program": "C:/Windows/System32/calc.exe", "pid": 42, "request_accepted": True, "launched": True}):
            messages = []
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), messages, "запусти калькулятор")
        model.chat.assert_called_once()
        self.assertEqual(answer, "Программа запущена: C:/Windows/System32/calc.exe")
        self.assertEqual(messages[-1], {"role": "assistant", "content": answer})

    def test_managed_browser_unchanged(self):
        with patch("assistant.browser.BROWSER.call", return_value={"text": "page"}) as managed:
            self.assertEqual(tools.browser_open("https://example.com"), {"text": "page"})
        managed.assert_called_once_with("navigate", url="https://example.com", visible=False)


if __name__ == "__main__":
    unittest.main()
