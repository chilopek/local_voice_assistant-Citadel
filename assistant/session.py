"""Killable worker sessions. No inference or blocking I/O runs on the Tk thread."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from typing import Any, Callable

from .paths import PACKAGE_ROOT, PYTHON_EXE


class ProcessJob:
    """Windows owns helper descendants so cancellation also stops browser/audio helpers."""
    def __init__(self, process: subprocess.Popen) -> None:
        self.handle = None
        if sys.platform != "win32":
            return
        from ctypes import wintypes as w
        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(key, ctypes.c_uint64) for key in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK for user-launched apps
        if handle and kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.handle = handle
        elif handle:
            kernel.CloseHandle(handle)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class SessionProcess:
    def __init__(self, emit: Callable[[str, str, Any], None]) -> None:
        self.id = uuid.uuid4().hex
        self.emit = emit
        self.process: subprocess.Popen | None = None
        self.job: ProcessJob | None = None
        self.stopped = threading.Event()
        self._outbound: queue.Queue[dict] = queue.Queue()

    def start(self, payload: dict) -> None:
        # Process creation and initial pipe writes also stay off the UI thread.
        threading.Thread(target=self._run, args=(payload,), daemon=True).start()

    def _run(self, payload: dict) -> None:
        reported_error = False
        try:
            process = self.process = subprocess.Popen(
                [str(PYTHON_EXE), "-B", "-X", "utf8", "-u", "-m", "assistant.worker"],
                cwd=PACKAGE_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32")
            self.job = ProcessJob(process)
            threading.Thread(target=self._stderr, args=(process,), daemon=True).start()
            if self.stopped.is_set():
                self._kill()
            else:
                payload["job_owned"] = bool(self.job.handle)
                self._write(payload)
                threading.Thread(target=self._write_loop, daemon=True).start()
            for line in process.stdout:
                if self.stopped.is_set():
                    break
                try:
                    event = json.loads(line)
                    reported_error = False if event.get("event") == "task_done" else reported_error or event.get("event") == "error"
                    self.emit(self.id, event["event"], event.get("payload"))
                except (ValueError, KeyError, TypeError):
                    continue
            code = process.wait()
            if code and not self.stopped.is_set() and not reported_error:
                self.emit(self.id, "error", f"Процесс помощника завершился с кодом {code}. Подробности: data/worker.log")
        except Exception as error:
            if not self.stopped.is_set():
                self.emit(self.id, "error", str(error))
        finally:
            if self.job:
                self.job.close()
            if self.process:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream:
                        try:
                            stream.close()
                        except OSError:
                            pass
            self.emit(self.id, "session_done", None)

    @staticmethod
    def _stderr(process: subprocess.Popen) -> None:
        from .config import PROJECT_ROOT
        path = PROJECT_ROOT / "data" / "worker.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                for line in process.stderr:
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            pass

    def send(self, message: dict) -> None:
        if not self.stopped.is_set():
            self._outbound.put_nowait(message)

    def _write_loop(self) -> None:
        while not self.stopped.is_set():
            try:
                self._write(self._outbound.get(timeout=.1))
            except queue.Empty:
                if self.process and self.process.poll() is not None:
                    return

    def _write(self, message: dict) -> None:
        process = self.process
        if not process or not process.stdin or self.stopped.is_set():
            return
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self.stopped.set()
        # Never join/wait on Tk. Closing the job is a nonblocking OS cancellation.
        self._kill()

    def _kill(self) -> None:
        if self.job:
            self.job.close()
        if self.process and self.process.poll() is None:
            try:
                if sys.platform == "win32":
                    self.process.kill()
                else:
                    import signal
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
