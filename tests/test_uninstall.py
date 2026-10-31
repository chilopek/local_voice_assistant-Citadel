from pathlib import Path
import os
import unittest
from unittest.mock import patch

from assistant import uninstall
from assistant.tk_runtime import configure_tk_paths


class UninstallTests(unittest.TestCase):
    def test_source_checkout_does_not_launch_unknown_uninstaller(self):
        with patch.object(uninstall, "installed_uninstaller", return_value=None), patch("subprocess.Popen") as launch:
            with self.assertRaises(RuntimeError):
                uninstall.begin_uninstall()
        launch.assert_not_called()

    def test_installed_app_waits_for_its_own_process(self):
        with patch.object(uninstall, "installed_uninstaller", return_value=Path("unins000.exe")), patch("subprocess.Popen") as launch:
            uninstall.begin_uninstall()
        args = launch.call_args.args[0]
        self.assertEqual(args[1:4], ["-m", "assistant.uninstall", "--wait-pid"])
        self.assertEqual(args[4], str(os.getpid()))

    def test_wait_rejects_self_and_invalid_pid(self):
        for pid in (-1, 0, os.getpid()):
            with self.assertRaises(ValueError):
                uninstall.wait_for_parent(pid)

    def test_rejecting_gui_confirmation_keeps_window_open(self):
        configure_tk_paths()
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        app = AssistantGUI(root, enable_hotkey=False, enable_scheduler=False)
        try:
            with patch("assistant.gui.messagebox.askyesno", return_value=False), patch("assistant.gui.begin_uninstall") as launch:
                app._uninstall()
            launch.assert_not_called()
            self.assertFalse(app.closed.is_set())
        finally:
            app.close()

    def test_gui_closes_after_uninstall_helper_was_started(self):
        configure_tk_paths()
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        app = AssistantGUI(root, enable_hotkey=False, enable_scheduler=False)
        with patch("assistant.gui.messagebox.askyesno", return_value=True), patch("assistant.gui.begin_uninstall") as launch:
            app._uninstall()
        launch.assert_called_once()
        self.assertTrue(app.closed.is_set())


if __name__ == "__main__":
    unittest.main()
