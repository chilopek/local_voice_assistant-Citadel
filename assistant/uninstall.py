"""Launch the registered native uninstaller after the GUI has exited."""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys

from .paths import DATA_ROOT, PYTHONW_EXE, installed_uninstaller


def begin_uninstall() -> subprocess.Popen:
    if installed_uninstaller() is None:
        raise RuntimeError("Штатное удаление доступно после установки через новый Setup.exe.")
    return subprocess.Popen(
        [str(PYTHONW_EXE), "-m", "assistant.uninstall", "--wait-pid", str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def wait_for_parent(pid: int, timeout_seconds: int = 120) -> None:
    if pid <= 0 or pid == os.getpid():
        raise ValueError("Неверный идентификатор завершающегося приложения.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    process = kernel.OpenProcess(0x100000, False, pid)
    if not process:
        if ctypes.get_last_error() == 87:  # Process already exited.
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = kernel.WaitForSingleObject(process, timeout_seconds * 1000)
        if status != 0:
            raise RuntimeError("Помощник ещё завершает работу. Закройте его и повторите удаление через параметры Windows.")
    finally:
        kernel.CloseHandle(process)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        wait_for_parent(args.wait_pid)
        uninstaller = installed_uninstaller()
        if uninstaller is None:
            raise FileNotFoundError("Не найден штатный деинсталлятор.")
        subprocess.Popen([str(uninstaller)], cwd=uninstaller.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    except Exception as error:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        (DATA_ROOT / "uninstall-error.log").write_text(str(error), encoding="utf-8")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, str(error), "Цитадель — удаление", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
