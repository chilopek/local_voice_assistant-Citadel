from __future__ import annotations

from pathlib import Path
import sys
import uuid

from .config import PROJECT_ROOT


_targets: dict[str, tuple[int, tuple]] = {}


def _desktop():
    if sys.platform != "win32":
        raise RuntimeError("Управление рабочим столом доступно только в Windows.")
    import pythoncom
    pythoncom.CoInitialize()
    from pywinauto import Desktop
    return Desktop(backend="uia")


def desktop_snapshot(window_handle: int = 0) -> dict:
    desktop = _desktop()
    if not window_handle:
        return {"windows": [{"handle": w.handle, "title": w.window_text()[:200], "pid": w.process_id()} for w in desktop.windows() if w.is_visible() and w.window_text()][:50]}
    window = desktop.window(handle=int(window_handle)).wrapper_object()
    _targets.clear()
    elements = []
    prefix = uuid.uuid4().hex[:6]
    for control in [window, *window.descendants()[:250]]:
        try:
            if not control.is_visible():
                continue
            info = control.element_info
            token = f"{prefix}_{len(elements)}"
            _targets[token] = (int(window_handle), tuple(info.runtime_id))
            elements.append({"control_id": token, "type": info.control_type, "text": control.window_text()[:300], "automation_id": info.automation_id, "enabled": control.is_enabled(), "rectangle": str(control.rectangle())})
        except Exception:
            continue
    return {"window_handle": window_handle, "title": window.window_text(), "controls": elements, "note": "Перед действием выбери control_id из этого снимка; при изменении окна получи новый снимок."}


def desktop_action(action: str, window_handle: int, control_id: str = "", text: str = "", keys: str = "") -> dict:
    desktop = _desktop()
    window = desktop.window(handle=int(window_handle)).wrapper_object()
    control = window
    if control_id:
        recorded = _targets.get(control_id)
        if not recorded or recorded[0] != int(window_handle):
            raise ValueError("Устаревший control_id. Сначала прочитай desktop_snapshot.")
        control = next((item for item in [window, *window.descendants()[:250]] if tuple(item.element_info.runtime_id) == recorded[1]), None)
        if control is None:
            raise ValueError("Элемент больше не существует. Обнови снимок окна.")
    if action == "focus":
        window.set_focus()
    elif action == "click":
        control.click_input()
    elif action == "set_text":
        control.set_edit_text(text)
    elif action == "keys":
        window.set_focus()
        control.type_keys(keys, with_spaces=True, pause=0.03)
    elif action == "close":
        window.close()
        return {"closed": True, "window_handle": window_handle}
    else:
        raise ValueError("Доступны focus, click, set_text, keys, close.")
    return desktop_snapshot(window_handle)


def take_screenshot() -> dict:
    from PIL import ImageGrab
    folder = PROJECT_ROOT / "data" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"desktop-{uuid.uuid4().hex[:8]}.png"
    ImageGrab.grab().save(path)
    return {"path": str(path), "note": "Снимок сохранён для пользователя. Текстовая модель не видит пиксели; используй desktop_snapshot для чтения элементов."}
