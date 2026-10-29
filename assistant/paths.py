"""Separate immutable application files from user-owned settings and work."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = PACKAGE_ROOT.parent
MANIFEST_PATH = INSTALL_ROOT / "citadel.install.json"
IS_BUNDLED = MANIFEST_PATH.is_file() and (INSTALL_ROOT / "runtime" / "python.exe").is_file()
try:
    INSTALL_MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig")) if IS_BUNDLED else {}
except (OSError, ValueError):
    INSTALL_MANIFEST = {}
DATA_DIR_NAME = INSTALL_MANIFEST.get("data_dir_name", "LocalAssistant")
if DATA_DIR_NAME not in {"LocalAssistant", "LocalAssistantPackagingTest"}:
    raise ValueError("Invalid application data directory in installation manifest")
_default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / DATA_DIR_NAME if IS_BUNDLED else PACKAGE_ROOT
DATA_ROOT = Path(os.environ.get("CITADEL_DATA_DIR", str(_default_data))).expanduser().resolve()
ASSET_ROOT = PACKAGE_ROOT / "assets"
PYTHON_EXE = INSTALL_ROOT / "runtime" / "python.exe" if IS_BUNDLED else Path(sys.executable).with_name("python.exe") if sys.platform == "win32" else Path(sys.executable)
PYTHONW_EXE = PYTHON_EXE.with_name("pythonw.exe")
if IS_BUNDLED:
    os.environ.setdefault("HF_HOME", str(DATA_ROOT / "cache" / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(DATA_ROOT / "cache" / "torch"))
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DATA_ROOT / "cache" / "browsers"))


def ensure_data_layout() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if IS_BUNDLED:
        marker = {"product": "CitadelLocalAssistant", "data_root": str(DATA_ROOT), "format": 1}
        (DATA_ROOT / ".citadel-data-root").write_text(json.dumps(marker), encoding="utf-8")
    (DATA_ROOT / "data").mkdir(exist_ok=True)


def installed_uninstaller() -> Path | None:
    if not IS_BUNDLED or sys.platform != "win32":
        return None
    import winreg
    product = "CitadelPackagingTest" if DATA_DIR_NAME == "LocalAssistantPackagingTest" else "CitadelLocalAssistant"
    key_name = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{product}_is1"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_READ | view) as key:
                command = winreg.QueryValueEx(key, "UninstallString")[0].strip()
            path = command[1:command.index('"', 1)] if command.startswith('"') else command
            candidate = Path(path)
            if candidate.parent.resolve() == INSTALL_ROOT.resolve() and re.fullmatch(r"unins\d{3}\.exe", candidate.name, flags=re.I) and candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None
