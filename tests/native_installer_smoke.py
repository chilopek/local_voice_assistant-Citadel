"""Real install/uninstall checks using only the isolated Inno TestBuild."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import winreg


REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CitadelPackagingTest_is1"


def registered() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY):
            return True
    except FileNotFoundError:
        return False


def run(arguments: list[str], *, env: dict | None = None) -> None:
    result = subprocess.run(arguments, env=env, capture_output=True, timeout=600)
    if result.returncode:
        raise AssertionError(f"Command failed ({result.returncode}): {arguments[0]}\n{result.stdout!r}\n{result.stderr!r}")


parser = argparse.ArgumentParser()
parser.add_argument("installer", type=Path)
args = parser.parse_args()
installer = args.installer.resolve()
if installer.name != "LocalAssistantSetupTest.exe":
    raise RuntimeError("Only a TestBuild installer may be used by this test")
data = (Path(os.environ["LOCALAPPDATA"]) / "LocalAssistantPackagingTest").resolve()
if data.exists() or registered():
    raise RuntimeError("Existing packaging test data/installation found; refusing to overwrite it")
env = dict(os.environ)
env.pop("CITADEL_DATA_DIR", None)
env["PYTHONHOME"] = r"C:\missing-test-python"
env["PYTHONPATH"] = r"C:\missing-test-packages"
base_python = Path(sys.base_prefix) / "python.exe"
with tempfile.TemporaryDirectory(prefix="citadel-native-integration-") as folder:
    parent = Path(folder)
    install = parent / "app-install"
    outside = parent / "outside-data"
    outside.mkdir()
    outside_file = outside / "must-survive.txt"
    outside_file.write_text("not owned by the assistant", encoding="utf-8")
    params = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SKIPRUNTIMESETUP=1", f"/DIR={install}", f"/LOG={parent / 'install.log'}"]
    run(params, env=env)
    assert registered(), "Windows Apps uninstall entry missing"
    manifest = json.loads((install / "citadel.install.json").read_text(encoding="utf-8-sig"))
    assert manifest["data_dir_name"] == "LocalAssistantPackagingTest", manifest
    assert (install / "unins000.exe").is_file()
    run([str(install / "Citadel.exe"), "--check"], env=env)
    python = install / "runtime/python.exe"
    run([str(python), "-B", "-c", "import tkinter,torch,faster_whisper,playwright,piper; from assistant.paths import installed_uninstaller; assert installed_uninstaller() is not None"], env=env)
    # Exercise Stop, real child pipes/job cleanup and subsequent tasks using the
    # installed private runtime, including under poisoned system Python settings.
    run([str(python), "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", str(Path(__file__).resolve().parent), "-p", "test_sessions.py", "-v"], env=env)
    assert (install / "app/assistant/screen_ocr.ps1").is_file()
    assert (data / ".citadel-data-root").is_file()
    document = data / "workspace/keep-document.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("user document", encoding="utf-8")
    config = data / "assistant.local.json"
    config.write_text('{"model":"test","voice":false}', encoding="utf-8")
    unknown = install / "unknown-root-file.txt"
    unknown.write_text("not an installed payload file", encoding="utf-8")
    print("PASS: install, private Python/Tcl/voice imports, GUI startup, registered uninstaller", flush=True)
    run([str(install / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=env)
    assert not registered()
    assert not (install / "runtime").exists()
    assert not (install / "app").exists()
    assert document.read_text(encoding="utf-8") == "user document"
    assert config.exists() and unknown.exists() and outside_file.exists()
    print("PASS: default uninstall preserves data and unknown root files", flush=True)
    run(params, env=env)
    import _winapi
    if hasattr(_winapi, "CreateJunction"):
        _winapi.CreateJunction(str(outside), str(data / "outside-junction"))
    run([str(install / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PURGEDATA=1"], env=env)
    assert not registered() and not data.exists()
    assert unknown.exists() and outside_file.exists()
    assert base_python.exists()
    print("PASS: explicit data purge removes only owned data, junction target and shared Python survive", flush=True)
print("PASS: complete native installer lifecycle")
