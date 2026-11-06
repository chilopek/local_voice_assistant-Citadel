from __future__ import annotations

from pathlib import Path
import os
import re
import sys
import time
import uuid

from .config import PROJECT_ROOT


_targets: dict[str, tuple[int, tuple]] = {}
_windows: dict[int, int] = {}


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
        windows = [{"handle": w.handle, "title": w.window_text()[:200], "pid": w.process_id()} for w in desktop.windows() if w.is_visible() and w.window_text()][:50]
        _windows.update({int(w['handle']):w['pid'] for w in windows})
        return {"windows": windows}
    window = desktop.window(handle=int(window_handle)).wrapper_object()
    _windows[int(window_handle)] = window.process_id()
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
            text = control.window_text()[:300]
            if info.control_type in {'Edit','Document'}:
                if info.element.CurrentIsPassword:
                    text = '[password field]'
                else:
                    try:
                        text = control.get_value()[:2000]
                    except Exception:
                        try: text = control.iface_text.DocumentRange.GetText(2000)
                        except Exception: pass
            elements.append({"control_id": token, "type": info.control_type, "text": text, "automation_id": info.automation_id, "enabled": control.is_enabled(), "rectangle": str(control.rectangle())})
        except Exception:
            continue
    return {"window_handle": window_handle, "title": window.window_text(), "controls": elements, "note": "Перед действием выбери control_id из этого снимка; при изменении окна получи новый снимок."}


def _observed_window(window_handle: int):
    desktop = _desktop()
    window = desktop.window(handle=int(window_handle)).wrapper_object()
    if _windows.get(int(window_handle))!=window.process_id():
        raise ValueError('Неизвестное или заменённое окно. Получи desktop_snapshot.')
    return desktop, window


def _observed_control(window, window_handle: int, control_id: str):
    recorded = _targets.get(control_id)
    if not recorded or recorded[0]!=int(window_handle):
        raise ValueError('Устаревший control_id. Получи desktop_snapshot.')
    control = next((item for item in [window,*window.descendants()[:250]] if tuple(item.element_info.runtime_id)==recorded[1]),None)
    if control is None:
        raise ValueError('Элемент больше не существует. Получи новый снимок.')
    return control


def foreground_window() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None
    import win32gui
    import win32process
    handle = win32gui.GetForegroundWindow()
    if handle:
        try:
            _, pid = win32process.GetWindowThreadProcessId(handle)
            return handle, pid
        except Exception:
            return None
    return None


def _close_observed(window_handle: int, title: str, pid: int) -> dict:
    import win32con
    import win32gui
    import win32process

    def still_open() -> bool:
        if not win32gui.IsWindow(window_handle):
            return False
        try:
            return win32process.GetWindowThreadProcessId(window_handle)[1] == pid
        except Exception:
            return False

    if still_open():
        win32gui.PostMessage(window_handle, win32con.WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 1.5
    while still_open() and time.monotonic() < deadline:
        time.sleep(.05)
    closed = not still_open()
    return {"closed": closed, "window_handle": window_handle, "title": title,
            "message": f"Окно «{title}» закрыто." if closed else
            f"Окно «{title}» пока открыто. Проверь, не ждёт ли приложение сохранения или подтверждения."}


def close_window(title: str = "", previous_window: tuple[int, int] | None = None) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("Управление окнами доступно в Windows.")
    import psutil
    from .application_catalog import name_key
    import win32gui

    ignored_pids = {os.getpid(), os.getppid()}
    import pywintypes
    windows = []
    for item in desktop_snapshot()["windows"]:
        try:
            if item["pid"] not in ignored_pids and win32gui.GetClassName(item["handle"]) not in {
                "Progman", "WorkerW", "Shell_TrayWnd",
            }:
                windows.append(item)
        except pywintypes.error:
            continue
    if title:
        key = name_key(title)
        if not key:
            raise ValueError("Укажи название окна.")
        matches = [item for item in windows if key in name_key(item["title"])]
        if not matches:
            for item in windows:
                try:
                    if key == name_key(psutil.Process(item["pid"]).name()):
                        matches.append(item)
                except psutil.Error:
                    continue
    else:
        current = foreground_window()
        preferred = current if current and current[1] not in ignored_pids else previous_window
        matches = [item for item in windows if (item["handle"], item["pid"]) == tuple(preferred or ())]
        if not matches and len(windows) == 1:
            matches = windows
    if len(matches) == 1:
        item = matches[0]
        _, window = _observed_window(item["handle"])
        return _close_observed(item["handle"], item["title"], window.process_id())
    candidates = matches or windows
    options = ", ".join(f"«{item['title']}»" for item in candidates[:5])
    message = f"Какое окно закрыть? Открыты {options}." if options else "Подходящего открытого окна не нашла."
    return {"closed": False, "needs_input": bool(options), "message": message}


def requires_confirmation(action: str, window_handle: int = 0, control_id: str = "") -> bool:
    if action in {"focus", "close"}:
        return False
    if action not in {"invoke", "click", "set_text"} or not window_handle or not control_id:
        return True
    _, window = _observed_window(window_handle)
    control = _observed_control(window, window_handle, control_id)
    info = control.element_info
    if info.element.CurrentIsPassword:
        raise PermissionError("Пароль вводит пользователь.")
    label = control.window_text()
    if re.search(r"купить|оплат|покупк|удал|стереть|выключ|перезагр|отправ|перевести|buy|pay|purchase|delete|remove|shutdown|restart|send|transfer", label, re.I):
        return True
    if action == "set_text":
        return info.control_type != "Edit"
    return info.control_type not in {"TabItem", "ListItem", "TreeItem"} and not re.fullmatch(
        r"открыть меню|настройки|аккаунты|сменить аккаунт|выбрать аккаунт|settings|accounts|switch account|choose an account",
        label.strip(), re.I,
    )


def desktop_action(action: str, window_handle: int, control_id: str = "", text: str = "", keys: str = "") -> dict:
    from .tools import _confirmed_execution
    if not _confirmed_execution.get() and requires_confirmation(action, window_handle, control_id):
        raise PermissionError("Это действие в окне требует подтверждения.")
    _, window = _observed_window(window_handle)
    if action in {"invoke", "click", "set_text"} and not control_id:
        raise ValueError("Выбери control_id элемента из desktop_snapshot.")
    control = _observed_control(window, window_handle, control_id) if control_id else window
    if control_id and control.element_info.element.CurrentIsPassword:
        raise PermissionError("Пароль вводит пользователь.")
    if action == "focus":
        window.set_focus()
    elif action == "invoke":
        from pywinauto.uia_defines import NoPatternInterfaceError
        if not control.is_visible() or not control.is_enabled():
            raise ValueError("Элемент недоступен. Обнови снимок окна.")
        try:
            invoke = control.iface_invoke
        except NoPatternInterfaceError:
            try:
                control.iface_selection_item.Select()
            except NoPatternInterfaceError as error:
                raise ValueError("Элемент не поддерживает активацию через Windows UI Automation.") from error
        else:
            invoke.Invoke()
    elif action == "click":
        control.click_input()
    elif action == "set_text":
        control.set_edit_text(text)
    elif action == "keys":
        window.set_focus()
        control.type_keys(keys, with_spaces=True, pause=0.03)
    elif action == "close":
        return _close_observed(window_handle, window.window_text(), window.process_id())
    else:
        raise ValueError("Доступны focus, invoke, click, set_text, keys, close.")
    return desktop_snapshot(window_handle)


def take_screenshot() -> dict:
    from PIL import ImageGrab
    folder = PROJECT_ROOT / "data" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"desktop-{uuid.uuid4().hex[:8]}.png"
    ImageGrab.grab().save(path)
    return {"path": str(path), "note": "Снимок сохранён для пользователя. Текстовая модель не видит пиксели; используй desktop_snapshot для чтения элементов."}
