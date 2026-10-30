"""On-demand screen text and explicitly approximate network location."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from .config import LOCAL_CONFIG, PROJECT_ROOT

_location_cache: tuple[float, dict] | None = None


def recognize_screen_file(path: str | Path) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("Windows OCR доступен только в Windows")
    shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                             str(Path(__file__).with_name("screen_ocr.ps1")), "-ImagePath", str(Path(path).resolve())],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1800:] or "Windows OCR не ответил")
    return json.loads(result.stdout.lstrip("\ufeff"))


def read_screen() -> dict:
    """Image is attached only by the agent when its model advertises vision."""
    from PIL import ImageGrab
    from .desktop import desktop_snapshot
    folder = PROJECT_ROOT / "data" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    image = ImageGrab.grab()
    # Windows OCR supports a limited image dimension; text remains readable at 2560.
    image.thumbnail((2560, 2560))
    path = folder / "screen-current.png"
    image.save(path)
    result = {"path": str(path), "image_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
              "captured_at": datetime.now().astimezone().isoformat(), "size": list(image.size),
              "note": "Текст ниже распознан OCR, возможны ошибки. Для текстовой модели формы и картинки не распознаны; не выдумывай их содержимое."}
    try:
        result.update(recognize_screen_file(path))
        result["text"] = result.get("text", "")[:14000]
    except Exception as error:
        result["ocr_error"] = str(error)
    try:
        result["windows"] = desktop_snapshot()
    except Exception as error:
        result["windows_error"] = str(error)
    return result


def get_location() -> dict:
    global _location_cache
    manual = LOCAL_CONFIG.get("location")
    if isinstance(manual, str) and manual.strip():
        return {"city": manual.strip(), "source": "user_setting", "approximate": True,
                "note": "Город задан пользователем, координаты неизвестны."}
    if _location_cache and time.monotonic() - _location_cache[0] < 900:
        return dict(_location_cache[1])
    request = Request("https://ipwho.is/", headers={"User-Agent": "Citadel/0.4 (approximate city lookup)"})
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(64000))
        if not payload.get("success") or not payload.get("city"):
            raise ValueError(payload.get("message", "Город неизвестен"))
        result = {key: payload.get(key) for key in ("city", "region", "country", "latitude", "longitude")}
        result.update(source="https://ipwho.is/", approximate=True, accuracy="city", obtained_at=datetime.now().astimezone().isoformat(),
                      note="Примерное местоположение по внешнему IP, не GPS. VPN/провайдер могут указывать другой город. Для точного города уточни у пользователя.")
        _location_cache = (time.monotonic(), result)
        return result
    except Exception as error:
        return {"error": str(error), "source": "IP geolocation", "approximate": True,
                "note": "Не удалось определить город. Спроси город у пользователя; не угадывай по часовому поясу."}
