from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from assistant.session import SessionProcess, ProcessJob
from assistant.worker import WakeGate


class WakeTests(unittest.TestCase):
    def test_only_two_modes(self):
        with self.assertRaises(ValueError):
            WakeGate("push")

    def test_wake_in_same_or_following_utterance_and_expires(self):
        gate = WakeGate("wake")
        self.assertEqual(gate.accept("открой браузер", 1), ("", False))
        self.assertEqual(gate.accept("Цитадель, открой браузер", 2), ("открой браузер", False))
        self.assertEqual(gate.accept("цитадель", 3), ("", True))
        self.assertEqual(gate.accept("открой браузер", 4), ("открой браузер", False))
        self.assertEqual(gate.accept("что дальше", 5), ("", False))
        gate.accept("цитадель", 6)
        self.assertEqual(gate.accept("слишком поздно", 17), ("", False))
        self.assertEqual(gate.accept("цитадельная", 18), ("", False))

    def test_always_accepts_without_wake_word(self):
        self.assertEqual(WakeGate("always").accept("найди информацию про SSH"), ("найди информацию про SSH", False))


class SessionTests(unittest.TestCase):
    def test_stop_preserves_the_ready_voice_host_and_unexpected_exit_does_not_lock_ui(self):
        import tkinter as tk
        from unittest.mock import Mock
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        root.withdraw()
        app = AssistantGUI(root, enable_hotkey=False, warm_voice=False)
        host = Mock(id="resident")
        app.voice_host = host
        app._voice_ready = app._voice_speech_ready = True
        try:
            app._reserve_session()
            app._stop_task()
            self.assertIs(app.voice_host, host)
            host.stop.assert_not_called()
            self.assertEqual(host.send.call_args.args[0]["command"], "cancel")
            app._reserve_session()
            app._agent_running = False
            app._awaiting_speech = True
            app._voice_event(host.id, "session_done", None)
            self.assertFalse(app.busy)
            self.assertFalse(app._awaiting_speech)
        finally:
            app.close()

    def test_commands_before_start_are_buffered_without_blocking_tk(self):
        from unittest.mock import Mock
        session = SessionProcess(lambda *_: None)
        started = time.monotonic()
        command = {"command": "speech", "enabled": False}
        session.send(command)
        self.assertLess(time.monotonic() - started, .1)
        self.assertEqual(session._outbound.get_nowait(), command)
        session.process = Mock()
        session.send({"command": "new_task", "payload": {"messages": ["x" * 200000]}})
        session.process.stdin.write.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows job objects")
    def test_cancel_kills_helper_descendants_but_breakaway_apps_survive(self):
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        code = "import sys,subprocess;sys.stdin.readline();helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);app=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB);print(helper.pid,app.pid,flush=True);sys.stdin.readline()"
        parent = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        job = ProcessJob(parent)
        app = helper = None
        try:
            self.assertIsNotNone(job.handle)
            parent.stdin.write("start\n")
            parent.stdin.flush()
            helper_pid, app_pid = map(int, parent.stdout.readline().split())
            helper = kernel.OpenProcess(0x100000 | 1, False, helper_pid)
            app = kernel.OpenProcess(0x100000 | 1, False, app_pid)
            job.close()
            parent.wait(timeout=3)
            self.assertEqual(kernel.WaitForSingleObject(helper, 3000), 0)
            self.assertEqual(kernel.WaitForSingleObject(app, 0), 258)
        finally:
            job.close()
            parent.kill() if parent.poll() is None else None
            parent.wait(timeout=3)
            for handle in (helper, app):
                if handle:
                    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    kernel.TerminateProcess(handle, 0)
                    kernel.CloseHandle(handle)
            parent.stdin.close()
            parent.stdout.close()

    def test_stop_aborts_a_real_blocked_http_request_and_gui_accepts_next_task(self):
        # Exercise real Tk callbacks, child process, OS pipes and Windows job. No Ollama needed.
        entered, release = threading.Event(), threading.Event()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                entered.set()
                release.wait(15)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": "проверенный ответ"}}).encode())
                except OSError:
                    pass
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        root.withdraw()
        app = AssistantGUI(root, enable_hotkey=False)
        try:
            with patch.dict(os.environ, {"ASSISTANT_OLLAMA_URL": f"http://127.0.0.1:{server.server_port}", "ASSISTANT_VOICE": "off"}):
                app.speak_var.set(False)
                app.entry.insert(0, "скажи привет")
                app._send_text()
                old = app.worker
                deadline = time.monotonic() + 12
                while not entered.is_set() and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertTrue(entered.is_set(), "child never reached fake model")
                self.assertIsNotNone(old.job.handle if sys.platform == "win32" else old.process)
                started = time.monotonic()
                app._stop_task()
                self.assertLess(time.monotonic() - started, .5)
                self.assertFalse(app.busy)
                self.assertEqual(str(app.send_button.cget("state")), "normal")
                # A late worker error/completion must never overwrite the next task.
                app.events.put(("worker", (old.id, "error", "STALE ERROR")))
                release.set()
                app.entry.insert(0, "скажи привет снова")
                app._send_text()
                deadline = time.monotonic() + 12
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertFalse(app.busy)
                transcript = app.transcript.get("1.0", "end")
                self.assertNotIn("STALE ERROR", transcript)
                self.assertIn("проверенный ответ", transcript)
                warm = app.worker
                app.entry.insert(0, "ещё один ответ")
                app._send_text()
                deadline = time.monotonic() + 10
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertFalse(app.busy)
                self.assertIs(app.worker, warm, "successful tasks should preserve browser and voice caches")
                self.assertIsNone(warm.process.poll())
                last_dialogue = requests[-1]["messages"]
                self.assertTrue(any(m.get("role") == "user" and "скажи привет снова" in m.get("content", "") for m in last_dialogue))
                self.assertTrue(any(m.get("role") == "assistant" and "проверенный ответ" in m.get("content", "") for m in last_dialogue))
                old.process.wait(timeout=3)
        finally:
            release.set()
            app.close()
            server.shutdown()
            server.server_close()

    def test_stop_before_worker_launch(self):
        session = SessionProcess(lambda *_: None)
        session.stop()
        session.start({"mode": "task", "text": "ignore", "messages": []})
        deadline = time.monotonic() + 5
        while session.process is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNotNone(session.process)
        session.process.wait(timeout=5)

    def test_microphone_meter_ignores_old_sessions_and_resets_on_stop(self):
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        root.withdraw()
        app = AssistantGUI(root, enable_hotkey=False)
        try:
            self.assertTrue(app._reserve_session(listening=True))
            app._session_id = "current"
            app.events.put(("worker", ("current", "mic_level", {"active": True, "level": .7, "dbfs": -18, "clipping": False})))
            root.after_cancel(app._poll_id)
            app._drain_events()
            self.assertIn("Есть звук", app.mic_level_var.get())
            self.assertTrue(any(app.mic_meter.itemcget(item, "fill") != "#2b313b" for item in app._mic_segments))
            app._stop_task()
            app.events.put(("worker", ("current", "mic_level", {"active": True, "level": 1, "dbfs": 0})))
            root.after_cancel(app._poll_id)
            app._drain_events()
            self.assertEqual(app.mic_level_var.get(), "Микрофон выключен")
            self.assertTrue(all(app.mic_meter.itemcget(item, "fill") == "#2b313b" for item in app._mic_segments))
        finally:
            app.close()

    def test_voice_controls_fit_minimum_window_size(self):
        import tkinter as tk
        from assistant.gui import AssistantGUI
        root = tk.Tk()
        app = AssistantGUI(root, enable_hotkey=False)
        try:
            root.geometry("900x600")
            root.update()
            root.update_idletasks()
            bottom = root.winfo_rooty() + root.winfo_height()
            for control in (app.mode_box, app.listen_button, app.mic_meter, app.uninstall_button):
                self.assertGreater(control.winfo_height(), 5)
                self.assertLessEqual(control.winfo_rooty() + control.winfo_height(), bottom - 8)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
