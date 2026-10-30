from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant import tools
from assistant import permissions


class PermissionTests(unittest.TestCase):
    def test_managed_workspace_in_appdata_allows_new_documents(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder).resolve() / "AppData/Local"
            workspace = local / "LocalAssistant/workspace"
            with patch.object(permissions, "WORKSPACE", workspace), patch.dict(permissions.os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertFalse(permissions.creation_requires_confirmation(workspace / "notes.txt"))
                self.assertTrue(permissions.creation_requires_confirmation(workspace / "Startup/autorun.cmd"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = patch.object(tools, "WORKSPACE", self.root / "workspace")
        self.workspace.start()
        self.addCleanup(self.workspace.stop)

    def test_absolute_reads_and_new_files_outside_workspace_are_automatic(self):
        target = self.root / "documents" / "note.txt"
        self.assertFalse(tools.needs_confirmation("write_file", {"path": str(target)}))
        tools.execute("write_file", {"path": str(target), "content": "hello"})
        for name in ("read_file", "list_files"):
            self.assertFalse(tools.needs_confirmation(name, {"path": str(target)}))
        self.assertEqual(tools.read_file(str(target))["content"], "hello")
        self.assertEqual(tools.list_files(str(target.parent))[0]["name"], target.name)

    def test_overwrite_requires_real_app_flag_and_context_resets(self):
        target = self.root / "note.txt"
        target.write_text("original")
        args = {"path": str(target), "content": "approved"}
        self.assertTrue(tools.needs_confirmation("write_file", args))
        with self.assertRaises(FileExistsError):
            tools.execute("write_file", args)
        with self.assertRaises(TypeError):
            tools.execute("write_file", {**args, "confirmed": True})
        self.assertEqual(target.read_text(), "original")
        tools.execute("write_file", args, confirmed=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "approved")
        with self.assertRaises(FileExistsError):
            tools.write_file(str(target), "unapproved")
        with self.assertRaises(FileExistsError):
            tools.execute("write_file", args, confirmed="true")
        with patch.dict(tools.TOOL_FUNCTIONS, {"write_file": lambda **kw: (_ for _ in ()).throw(RuntimeError("failure"))}):
            with self.assertRaises(RuntimeError):
                tools.execute("write_file", args, confirmed=True)
        with self.assertRaises(FileExistsError):
            tools.write_file(str(target), "unapproved after exception")

    def test_write_check_create_race_does_not_overwrite(self):
        target = self.root / "race.txt"
        args = {"path": str(target), "content": "assistant"}
        self.assertFalse(tools.needs_confirmation("write_file", args))
        real_open = Path.open

        def raced_open(path, mode="r", *a, **kw):
            if path == target and mode == "x":
                with real_open(target, "w") as output:
                    output.write("other process")
            return real_open(path, mode, *a, **kw)

        with patch.object(Path, "open", raced_open), self.assertRaises(FileExistsError):
            tools.execute("write_file", args)
        self.assertEqual(target.read_text(), "other process")

    def test_protected_new_paths_require_confirmation(self):
        for relative in ("Windows/new.txt", "Startup/new.txt", ".config/new.txt",
                         ".ssh/config", "ProgramData/new.txt", "autorun.inf", "desktop.ini"):
            path = self.root / relative
            with self.subTest(path=relative):
                self.assertTrue(tools.needs_confirmation("write_file", {"path": str(path)}))
                self.assertTrue(tools.needs_confirmation("download_file", {"path": str(path)}))
        with self.assertRaises(PermissionError):
            tools.execute("write_file", {"path": str(self.root / "Startup/new.txt"), "content": "x"})
        with patch.dict("os.environ", {"APPDATA": str(self.root / "relocated-config")}):
            self.assertTrue(tools.needs_confirmation("write_file", {"path": str(self.root / "relocated-config/new.txt")}))

    def test_open_ordinary_documents_and_folder_with_os_default(self):
        target = self.root / "note.txt"
        target.touch()
        protected_folder = self.root / "Windows"
        protected_folder.mkdir()
        protected_document = protected_folder / "readme.txt"
        protected_document.touch()
        with patch.object(tools.sys, "platform", "win32"), patch.object(tools.os, "startfile", create=True) as start:
            for path in (target, self.root, protected_folder, protected_document):
                self.assertFalse(tools.needs_confirmation("open_path", {"path": str(path)}))
                self.assertTrue(tools.execute("open_file", {"path": str(path)})["request_accepted"])
            self.assertEqual(start.call_count, 4)

    def test_active_content_shortcuts_and_unknown_handlers_confirm(self):
        for suffix in (".exe", ".ps1", ".py", ".bat", ".js", ".vbs", ".lnk", ".url",
                       ".docm", ".xlsm", ".doc", ".html", ".svg", ".unknown"):
            target = self.root / ("active" + suffix)
            target.touch()
            with self.subTest(suffix=suffix):
                self.assertTrue(tools.needs_confirmation("open_file", {"path": str(target)}))
                with self.assertRaises(PermissionError):
                    tools.execute("open_file", {"path": str(target)})
                self.assertFalse(tools.needs_confirmation("download_file", {"path": str(self.root / ("new" + suffix))}))

    def test_download_new_auto_overwrite_confirm_and_never_execute(self):
        target = self.root / "download.txt"
        args = {"path": str(target), "url": "https://example.com/file"}
        self.assertFalse(tools.needs_confirmation("download_file", args))
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"downloaded")), \
                patch.object(tools.os, "startfile", create=True) as start, \
                patch.object(tools.subprocess, "Popen") as popen:
            self.assertEqual(tools.execute("download_file", args)["bytes"], 10)
            start.assert_not_called()
            popen.assert_not_called()
        self.assertTrue(tools.needs_confirmation("download_file", args))
        with self.assertRaises(PermissionError):
            tools.execute("download_file", args)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"approved")):
            tools.execute("download_file", args, confirmed=True)
        self.assertEqual(target.read_bytes(), b"approved")

    def test_new_downloads_are_only_saved_and_executable_open_requires_approval(self):
        for suffix in (".json", ".zip", ".xml", ".7z"):
            self.assertFalse(tools.needs_confirmation("download_file", {"path": str(self.root / ("data" + suffix))}))
        target = self.root / "installer.exe"
        args = {"path": str(target), "url": "https://example.com/file"}
        self.assertFalse(tools.needs_confirmation("download_file", args))
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"MZcontent")), \
                patch.object(tools.os, "startfile", create=True) as start, \
                patch.object(tools.subprocess, "Popen") as popen:
            tools.execute("download_file", args)
            start.assert_not_called()
            popen.assert_not_called()
        self.assertEqual(target.read_bytes(), b"MZcontent")
        self.assertTrue(tools.needs_confirmation("open_path", {"path": str(target)}))

    def test_download_check_create_race_preserves_existing_file(self):
        target = self.root / "race.txt"
        args = {"path": str(target), "url": "https://example.com/file"}

        def respond(*a, **kw):
            target.write_text("other process")
            return io.BytesIO(b"assistant")

        with patch("urllib.request.urlopen", side_effect=respond), self.assertRaises(FileExistsError):
            tools.execute("download_file", args)
        self.assertEqual(target.read_text(), "other process")

    def test_search_schema_function_and_browser_contract(self):
        self.assertIn("browser_search", {s["function"]["name"] for s in tools.TOOL_SCHEMAS})
        self.assertFalse(tools.needs_confirmation("browser_search", {"query": "weather"}))
        with patch("assistant.browser.BROWSER.call", return_value={"text": "results"}) as call:
            self.assertEqual(tools.execute("browser_search", {"query": "weather"}), {"text": "results"})
        call.assert_called_once_with("search", query="weather", provider="google")

    def test_browser_delegates_grounded_search_and_navigation_vs_danger(self):
        for action in ("fill", "press", "click", "back", "scroll", "tab"):
            args = {"action": action, "element_id": "grounded", "text": "query", "key": "Enter"}
            with patch("assistant.browser.BROWSER.call", return_value={"requires_confirmation": False, "reason": "grounded ordinary navigation/search"}) as call:
                self.assertFalse(tools.needs_confirmation("browser_action", args))
                call.assert_called_once_with("assess", requested_action=action, element_id="grounded",
                                             text="query", key="Enter")
        with patch("assistant.browser.BROWSER.call", return_value={"requires_confirmation": True, "reason": "payment/message/upload"}):
            self.assertTrue(tools.needs_confirmation("browser_action", {"action": "click", "element_id": "pay"}))

    def test_browser_unknown_or_uninspectable_fails_closed(self):
        for result in (None, {}, {"requires_confirmation": 0, "reason": "no"},
                       {"requires_confirmation": False}, {"requires_confirmation": False, "reason": ""}):
            with patch("assistant.browser.BROWSER.call", return_value=result):
                self.assertTrue(tools.needs_confirmation("browser_action", {"action": "click"}))
        with patch("assistant.browser.BROWSER.call", side_effect=RuntimeError("stale DOM")):
            self.assertTrue(tools.needs_confirmation("browser_action", {"action": "fill"}))

    def test_focus_is_auto_other_unknown_effects_confirm(self):
        self.assertFalse(tools.needs_confirmation("desktop_action", {"action": "focus"}))
        for name, args in (("desktop_action", {"action": "click"}), ("desktop_action", {"action": "unknown"}),
                           ("run_python", {}), ("run_powershell", {}), ("install", {}), ("delete_file", {}),
                           ("payment", {}), ("send_message", {}), ("upload", {}), ("unknown", {})):
            self.assertTrue(tools.needs_confirmation(name, args))


if __name__ == "__main__":
    unittest.main()
