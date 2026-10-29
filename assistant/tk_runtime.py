"""Locate Tcl/Tk beside the interpreter, including when running inside a venv.

This file is also a standalone installer probe; it has no package dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path
import struct
import sys


def configure_tk_paths(roots: list[Path] | None = None) -> dict[str, str]:
    import _tkinter

    if roots is None:
        roots = [Path(sys.base_prefix), Path(sys.prefix)]
        base_executable = getattr(sys, "_base_executable", None)
        if base_executable:
            roots.append(Path(base_executable).parent)
    configured: dict[str, str] = {}
    for variable, name, version, marker in (
        ("TCL_LIBRARY", "tcl", _tkinter.TCL_VERSION, "init.tcl"),
        ("TK_LIBRARY", "tk", _tkinter.TK_VERSION, "tk.tcl"),
    ):
        for root in roots:
            # Use the version compiled into _tkinter, never a guessed 8.6/9.0 path.
            candidates = [root / "tcl" / f"{name}{version}", root / "lib" / f"{name}{version}"]
            for candidate in candidates:
                if (candidate / marker).is_file():
                    os.environ[variable] = str(candidate)
                    configured[variable] = str(candidate)
                    break
            if variable in configured:
                break
    return configured


def probe() -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    if sys.platform == "win32" and struct.calcsize("P") != 8:
        raise RuntimeError("64-bit Python is required")
    configure_tk_paths()
    import tkinter as tk
    import sqlite3
    import venv

    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        print(f"Runtime OK: Python {sys.version.split()[0]}, Tcl/Tk {root.tk.call('info', 'patchlevel')}, {sys.executable}")
    finally:
        root.destroy()


if __name__ == "__main__":
    probe()
