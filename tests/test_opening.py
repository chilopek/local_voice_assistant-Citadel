from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant import app, tools
from assistant.memory import Memory


def call(name, arguments):
    return {"message": {"role": "assistant", "tool_calls": [
        {"id": "opening", "function": {"name": name, "arguments": arguments}}]}}


class OpeningTests(unittest.TestCase):
    def setUp(self):
        fixture = patch("assistant.application_catalog.catalog", return_value=[])
        self.catalog = fixture.start()
        self.addCleanup(fixture.stop)
        catalog = patch.object(tools, "catalog", self.catalog)
        catalog.start()
        self.addCleanup(catalog.stop)

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
                for program, args in (("calc", ["/evil"]), (str(Path(folder) / "calc.exe"), [])):
                    self.assertTrue(tools.needs_confirmation("launch_program", {"program": program, "arguments": args}))
                for program in ("powershell.exe", "cmd.exe", "installer.exe"):
                    with self.assertRaises(FileNotFoundError):
                        tools.needs_confirmation("launch_program", {"program": program})

    def test_game_launch_uses_original_registered_shortcut_without_model(self):
        from assistant.application_catalog import Application

        shortcut = r"C:\Users\Gamer\Desktop\VALORANT.lnk"
        self.catalog.return_value = [
            Application("VALORANT", shortcut, "shortcut", True,
                        r"D:\Games\RiotClientServices.exe", "--launch-product=valorant"),
            Application("VALORANT", r"D:\Games\RiotClientServices.exe", "apps_folder"),
        ]
        model = Mock()
        confirm = Mock(side_effect=AssertionError("unexpected confirmation"))
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), \
                patch.object(tools.os, "startfile", create=True) as start:
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), [],
                                  "Открой игру Валорант", confirm=confirm)
        start.assert_called_once_with(shortcut, "open")
        model.chat.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(answer, "Windows приняла запрос на запуск: VALORANT")

    def test_conflicting_shortcuts_ask_which_target_to_launch(self):
        from assistant.application_catalog import Application

        self.catalog.return_value = [
            Application("Studio", r"C:\Start\Studio.lnk", "shortcut", True, r"D:\Studio.exe"),
            Application("Studio", r"C:\Desktop\Studio.lnk", "shortcut", True, r"E:\Studio.exe"),
        ]
        events = []
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), \
                patch.object(tools.os, "startfile", create=True) as start:
            answer = app.run_turn(Mock(), Memory(Path(folder) / "memory.sqlite"), [],
                                  "Открой Studio", on_event=events.append)
        start.assert_not_called()
        self.assertIn("Какой открыть?", answer)
        self.assertTrue(events[-1]["needs_input"])

    def test_compound_request_launches_registered_app_then_keeps_other_tools_available(self):
        from assistant.application_catalog import Application

        shortcut = r"C:\Start\Note Studio.lnk"
        self.catalog.return_value = [Application("Note Studio", shortcut, "shortcut", True,
                                                 r"C:\Program Files\Note Studio\Studio.exe")]
        model = Mock()
        model.chat.side_effect = [call("system_status", {}), {"message": {"content": "Загрузка CPU 25%."}}]
        events = []
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.object(tools.sys, "platform", "win32"), \
                patch.object(tools.os, "startfile", create=True) as start, \
                patch.dict(tools.TOOL_FUNCTIONS, {"system_status": lambda: {"cpu_percent": 25}}):
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), [],
                                  "Открой Note Studio и покажи загрузку процессора", on_event=events.append)
        start.assert_called_once_with(shortcut, "open")
        self.assertEqual([event["name"] for event in events if event["type"] == "tool_result"],
                         ["launch_program", "system_status"])
        self.assertIn("25%", answer)

    def test_registered_script_host_shortcut_requires_confirmation_at_execution(self):
        from assistant.application_catalog import Application

        self.catalog.return_value = [Application("Utility", r"C:\Start\Utility.lnk", "shortcut", True,
                                                 r"C:\Windows\System32\cmd.exe", "/c script.cmd")]
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start:
            self.assertTrue(tools.needs_confirmation("launch_program", {"program": "Utility"}))
            with self.assertRaises(PermissionError):
                tools.execute("launch_program", {"program": "Utility"})
            start.assert_not_called()
            tools.execute("launch_program", {"program": "Utility"}, confirmed=True)
            start.assert_called_once()

    def test_registered_absolute_shortcut_does_not_need_confirmation(self):
        from assistant.application_catalog import Application

        shortcut = r"C:\Users\Gamer\Desktop\VALORANT.lnk"
        self.catalog.return_value = [Application("VALORANT", shortcut, "shortcut", True,
                                                 r"D:\Games\RiotClientServices.exe")]
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start:
            arguments = {"program": shortcut}
            self.assertFalse(tools.needs_confirmation("launch_program", arguments))
            tools.execute("launch_program", arguments)
        start.assert_called_once_with(shortcut, "open")

    def test_system_load_is_measured_without_asking_model(self):
        model = Mock()
        status = {"cpu_percent": 17, "ram_percent": 30, "ram_available_bytes": 4 * 1024**3,
                  "ram_total_bytes": 8 * 1024**3, "disk_active_percent": None, "disk_io": {},
                  "disks": [], "nvidia_gpus": []}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "PROJECT_ROOT", Path(folder)), \
                patch.dict(tools.TOOL_FUNCTIONS, {"system_status": lambda: status}):
            answer = app.run_turn(model, Memory(Path(folder) / "memory.sqlite"), [],
                                  "Посмотри нагруженность системы", confirm=Mock(side_effect=AssertionError))
        self.assertIn("17%", answer)
        self.assertIn("30%", answer)
        model.chat.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows process flags")
    def test_shell_launch_in_worker_breaks_away_from_cancelled_job(self):
        from assistant.application_catalog import Application

        shortcut = r"C:\Start\Studio.lnk"
        self.catalog.return_value = [Application("Studio", shortcut, "shortcut", True)]
        with patch.dict(tools.os.environ, {"CITADEL_WORKER_JOB": "1"}), \
                patch.object(tools.subprocess, "run", return_value=Mock(returncode=0)) as run:
            result = tools.launch_program("Studio")
        self.assertTrue(result["request_accepted"])
        self.assertEqual(run.call_args.args[0][-1], shortcut)
        self.assertTrue(run.call_args.kwargs["creationflags"] & tools.subprocess.CREATE_BREAKAWAY_FROM_JOB)

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
        self.assertEqual({s["function"]["name"] for s in requests[0][1]}, {"open_url", "launch_program", "find_apps", "select_tools"})
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
