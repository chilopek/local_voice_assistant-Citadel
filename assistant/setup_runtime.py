"""Prepare only external model assets; Python and packages are already bundled."""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from assistant.paths import DATA_ROOT, ensure_data_layout


def choose_model(mode: str) -> str:
    modes = {"fast": "qwen3:1.7b-instruct", "balanced": "qwen3:4b-instruct", "power": "qwen3:8b-instruct"}
    if mode in modes:
        return modes[mode]
    if mode != "auto":
        return mode
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return modes["power" if status.total >= 16 * 1024**3 else "balanced"]


def find_ollama() -> str | None:
    candidates = [shutil.which("ollama.exe"),
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"),
                  str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Ollama/ollama.exe")]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def run(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    logging.info("Run dependency installer: %s", command[0])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    logging.info("Dependency installer exit %s\n%s\n%s", result.returncode, result.stdout[-4000:], result.stderr[-4000:])
    return result


def install_ollama() -> str:
    winget = shutil.which("winget.exe")
    if winget:
        run([winget, "install", "--id", "Ollama.Ollama", "-e", "--accept-source-agreements", "--accept-package-agreements"])
        found = find_ollama()
        if found:
            return found
    # The official installer is executed only after a valid Ollama signature check.
    with tempfile.TemporaryDirectory(prefix="citadel-ollama-") as folder:
        installer = Path(folder) / "OllamaSetup.exe"
        logging.info("Download official Ollama installer")
        with urllib.request.urlopen("https://ollama.com/download/OllamaSetup.exe", timeout=90) as response, installer.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        quoted = str(installer).replace("'", "''")
        code = f"$s=Get-AuthenticodeSignature -LiteralPath '{quoted}'; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Ollama'){{exit 1}}"
        encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
        shell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        if run([shell, "-NoProfile", "-EncodedCommand", encoded], timeout=60).returncode:
            raise RuntimeError("Не пройдена проверка подписи установщика Ollama. Установите Ollama с официального сайта и повторите настройку.")
        result = run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        if result.returncode not in {0, 3010}:
            raise RuntimeError(f"Установщик Ollama вернул {result.returncode}")
    found = find_ollama()
    if not found:
        raise RuntimeError("После установки не найден ollama.exe")
    return found


def tags() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
        return json.load(response).get("models", [])


def prepare_model(model: str) -> None:
    try:
        models = tags()
    except Exception:
        ollama = find_ollama() or install_ollama()
        with (DATA_ROOT / "ollama-service.log").open("ab") as output:
            subprocess.Popen([ollama, "serve"], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(45):
            time.sleep(1)
            try:
                models = tags()
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Ollama не ответила на 127.0.0.1:11434. Подробности в ollama-service.log")
    if any(item.get("name") in {model, model + ":latest"} for item in models):
        logging.info("Model already present: %s", model)
        return
    request = urllib.request.Request("http://127.0.0.1:11434/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
    previous = 0.0
    logging.info("Downloading model %s (Ollama resumes interrupted downloads)", model)
    with urllib.request.urlopen(request, timeout=900) as response:
        for line in response:
            item = json.loads(line)
            if item.get("error"):
                raise RuntimeError(item["error"])
            if time.monotonic() - previous >= 2:
                logging.info("%s %s / %s", item.get("status"), item.get("completed", ""), item.get("total", ""))
                previous = time.monotonic()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="auto")
    parser.add_argument("--voice", choices=["on", "off"], default="on")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()
    ensure_data_layout()
    log_path = DATA_ROOT / "setup-runtime.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    try:
        path = DATA_ROOT / "assistant.local.json"
        if path.exists() and not args.replace_config:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
            # Legacy installs used their whole directory as the workspace.
            if "workspace" not in config and (DATA_ROOT / ".venv").is_dir() and (DATA_ROOT / "assistant").is_dir():
                config["workspace"] = str(DATA_ROOT)
                path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            config = {"model": choose_model(args.model), "voice": args.voice == "on", "think": False, "num_ctx": 8192,
                      "num_predict": 768, "stt_model": "base" if args.model == "fast" else "small", "tts_model": "v5_6_ru", "tts_speaker": "aidar"}
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("Configuration ready; model=%s, voice=%s", config["model"], config.get("voice"))
        if not args.skip_download:
            prepare_model(config["model"])
            from assistant.browser import main as prepare_browser
            prepare_browser()
        logging.info("Setup completed")
        return 0
    except Exception:
        logging.exception("Runtime setup failed")
        print(f"Настройка не завершена. Подробный лог: {log_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
