"""Build a relocatable full Python runtime; no Python/pip installation on target."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def stage(destination: Path, refresh: bool = False) -> None:
    destination = destination.resolve()
    if not destination.is_relative_to((ROOT / "dist").resolve()):
        raise ValueError("Payload artifacts must stay inside this project's dist directory")
    marker = destination / ".citadel-payload.json"
    if destination.exists() and any(destination.iterdir()) and (not refresh or not marker.is_file()):
        raise ValueError("Existing payload is not owned by this builder, or --refresh was not provided")
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("Build the runtime using 64-bit Windows Python")
    required_modules = ("torch", "faster_whisper", "silero", "piper", "sounddevice", "pynput", "playwright", "bs4", "ddgs", "pywinauto", "PIL", "psutil")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(f"Build dependencies are missing: {missing}. Install packaging/requirements-win.lock first.")
    base = Path(sys.base_prefix)
    site = Path(sys.prefix) / "Lib" / "site-packages"
    packages = sorted({f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions(path=[str(site)]) if item.metadata.get("Name") != "local-assistant"})
    runtime = destination / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if not refresh:
        required = ["python.exe", "pythonw.exe", "Lib/os.py", "DLLs/_tkinter.pyd", "tcl"]
        if any(not (base / name).exists() for name in required):
            raise RuntimeError(f"Build Python is missing complete stdlib/Tcl support: {base}")
        for file in base.iterdir():
            if file.is_file() and (file.suffix.lower() in {".exe", ".dll"} or file.name.startswith("LICENSE")):
                shutil.copy2(file, runtime / file.name)
        ignore = shutil.ignore_patterns("__pycache__", "site-packages", "test", "tests", "idlelib", "turtledemo")
        shutil.copytree(base / "Lib", runtime / "Lib", ignore=ignore, dirs_exist_ok=True)
        shutil.copytree(base / "DLLs", runtime / "DLLs", dirs_exist_ok=True)
        shutil.copytree(base / "tcl", runtime / "tcl", dirs_exist_ok=True)
    previous_packages = json.loads(marker.read_text(encoding="utf-8")).get("packages", []) if refresh else []
    if not refresh or packages != previous_packages:
        staged_site = runtime / "Lib/site-packages"
        if staged_site.exists():
            # This directory belongs exclusively to this builder. A changed lock
            # must never silently retain missing/stale wheels in --refresh mode.
            if staged_site.is_symlink() or not staged_site.resolve().is_relative_to(destination):
                raise ValueError("Unsafe staged dependency directory")
            shutil.rmtree(staged_site)
        shutil.copytree(site, staged_site,
                        ignore=shutil.ignore_patterns("__pycache__", "__editable__*", "local_assistant-*.dist-info"))
    app = destination / "app"
    shutil.copytree(ROOT / "assistant", app / "assistant", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "assets", app / "assets", dirs_exist_ok=True)
    shutil.copy2(ROOT / "README.md", destination / "README.md")
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    # This isolates the runtime from registry Python installs, user-site and PYTHONPATH.
    (runtime / f"python{version}._pth").write_text(".\nLib\nDLLs\nLib\\site-packages\n..\\app\nimport site\n", encoding="utf-8")
    manifest = {"product": "CitadelLocalAssistant", "version": "0.8.0", "python": sys.version,
                "data_dir_name": "LocalAssistant", "mutex_name": "CitadelLocalAssistant", "format": 1}
    (destination / "citadel.install.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (destination / "bundled-packages.txt").write_text("\n".join(packages) + "\n", encoding="utf-8")
    marker.write_text(json.dumps({"builder": "citadel", "python": sys.version, "packages": packages}), encoding="utf-8")
    print(f"Staged self-contained runtime: {destination}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/payload")
    parser.add_argument("--refresh", action="store_true")
    arguments = parser.parse_args()
    stage(arguments.output, arguments.refresh)
