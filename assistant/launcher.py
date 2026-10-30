"""Windows entry point: record startup failures before importing the GUI."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
from pathlib import Path
import sys
import traceback
from .paths import ASSET_ROOT, DATA_ROOT, INSTALL_MANIFEST, IS_BUNDLED, ensure_data_layout


def show_failure(details: str, log_path: Path) -> None:
    message = f"Не удалось запустить помощника.\n\n{details[-1800:]}\n\nПолный лог: {log_path}"
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "Цитадель — ошибка запуска", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Open and close the GUI to validate startup")
    args = parser.parse_args(argv)
    ensure_data_layout()
    project_root = DATA_ROOT
    log_path = project_root / "assistant-launch.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        log_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LocalAssistant" / "logs" / "assistant-launch.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("assistant.launcher")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.info("Starting: executable=%s version=%s cwd=%s check=%s", sys.executable, sys.version, Path.cwd(), args.check)
    root = None
    callback_failed = False
    mutex = None
    try:
        from .config import WORKSPACE
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        os.chdir(WORKSPACE)
        if IS_BUNDLED and sys.platform == "win32" and not args.check:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            name = INSTALL_MANIFEST.get("mutex_name", "CitadelLocalAssistant")
            mutex = kernel.CreateMutexW(None, False, name)
            if not mutex:
                raise ctypes.WinError(ctypes.get_last_error())
        from .tk_runtime import configure_tk_paths

        logger.info("Tcl/Tk paths: %s", configure_tk_paths())
        import tkinter as tk
        from .gui import AssistantGUI

        root = tk.Tk()

        def report_callback_exception(kind: type[BaseException], error: BaseException, tb: object) -> None:
            nonlocal callback_failed
            callback_failed = True
            details = "".join(traceback.format_exception(kind, error, tb))
            logger.error("GUI callback failed:\n%s", details)
            if not args.check:
                show_failure(details, log_path)

        root.report_callback_exception = report_callback_exception
        app = AssistantGUI(root, enable_hotkey=not args.check, warm_voice=not args.check)
        icon_path = ASSET_ROOT / "citadel.ico"
        if icon_path.exists():
            root.iconbitmap(default=str(icon_path))
        if args.check:
            root.after(400, app.close)
        logger.info("GUI initialized")
        root.mainloop()
        if callback_failed:
            return 1
        logger.info("GUI closed normally")
        if args.check:
            print(f"GUI startup check OK: Python {sys.version.split()[0]}; log={log_path}")
        return 0
    except Exception:
        details = traceback.format_exc()
        logger.error("Startup failed:\n%s", details)
        if args.check:
            if sys.stderr is not None:
                print(details, file=sys.stderr)
        else:
            show_failure(details, log_path)
        return 1
    finally:
        try:
            from .browser import BROWSER
            BROWSER.close()
        except Exception:
            logger.exception("Browser cleanup failed")
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        logger.removeHandler(handler)
        handler.close()
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
