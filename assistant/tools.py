from __future__ import annotations

import datetime as dt
import json
import os
import inspect
import platform
import subprocess
import sys
import webbrowser
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable

from .config import WORKSPACE, is_inside_workspace
from .paths import PYTHON_EXE


def _resolve_workspace_path(relative_path: str) -> Path:
    candidate = Path(os.path.expandvars(relative_path)).expanduser()
    return (candidate if candidate.is_absolute() else WORKSPACE / candidate).resolve()


def get_time() -> dict[str, str]:
    now = dt.datetime.now().astimezone()
    return {"iso": now.isoformat(), "readable": now.strftime("%d.%m.%Y %H:%M:%S")}


def list_files(path: str = ".") -> list[dict[str, Any]]:
    folder = _resolve_workspace_path(path)
    if not folder.is_dir():
        raise ValueError(f"Это не папка: {path}")
    result = []
    for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        result.append({
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })
    return result[:200]


def read_file(path: str, max_chars: int = 12000) -> dict[str, Any]:
    file_path = _resolve_workspace_path(path)
    if not file_path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    max_chars = max(100, min(int(max_chars), 16000))
    with file_path.open(encoding="utf-8-sig", errors="replace") as source:
        text = source.read(max_chars + 1)
    return {"path": str(file_path), "content": text[:max_chars], "truncated": len(text) > max_chars}


def write_file(path: str, content: str) -> dict[str, Any]:
    file_path = _resolve_workspace_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {"path": str(file_path), "bytes": file_path.stat().st_size}


def run_powershell(command: str) -> dict[str, Any]:
    import base64
    encoded = base64.b64encode(("[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); $OutputEncoding = [Console]::OutputEncoding; " + command).encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=WORKSPACE,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        timeout=60,
    )
    return {
        "return_code": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
    }


def _http_url(url: str) -> bool:
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        return False


def open_url(url: str) -> dict[str, Any]:
    """Ask the OS default browser to open a URL; this does not read the page."""
    if not _http_url(url):
        raise ValueError("Ожидается полный HTTP/HTTPS URL.")
    if sys.platform == "win32":
        os.startfile(url)
        accepted = True
    else:
        accepted = bool(webbrowser.open(url))
    return {"url": url, "launched": accepted, "request_accepted": accepted, "ok": accepted}


def _trusted_programs() -> dict[str, tuple[str, list[Path]]]:
    """Enumerate OS/install locations only, never PATH or the working directory."""
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    programs = {
        name: (kind, [windows / relative]) for name, kind, relative in (
            ("calc", "gui", "System32/calc.exe"),
            ("notepad", "gui", "System32/notepad.exe"),
            ("mspaint", "gui", "System32/mspaint.exe"),
            ("explorer", "explorer", "explorer.exe"),
        )
    }
    for name, relative in (("chrome", "Google/Chrome/Application/chrome.exe"),
                           ("msedge", "Microsoft/Edge/Application/msedge.exe"),
                           ("firefox", "Mozilla Firefox/firefox.exe")):
        roots = [os.environ.get(key) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [Path(root).resolve() / relative for root in roots if root]
        programs[name] = ("browser", candidates)
    return programs


def _resolve_launch(program: str) -> tuple[str, str | None]:
    if not isinstance(program, str) or not program.strip():
        raise ValueError("Укажи программу.")
    if sys.platform != "win32":
        return program, None
    aliases = {"calculator": "calc", "калькулятор": "calc", "блокнот": "notepad",
               "paint": "mspaint", "проводник": "explorer", "chrome": "chrome",
               "хром": "chrome", "google chrome": "chrome", "edge": "msedge"}
    bare = not any(c in program for c in "\\/:")
    name = program.lower().removesuffix(".exe") if bare else Path(program).stem.lower()
    name = aliases.get(name, name)
    entry = _trusted_programs().get(name)
    if not entry:
        return program, None
    kind, candidates = entry
    # An explicit path must itself match a trusted installation path.
    if not bare:
        candidates = [p for p in candidates if p.absolute() == Path(program).absolute()]
        if not candidates:
            return program, None
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve() == candidate.absolute():
            return str(candidate.resolve()), kind
    raise FileNotFoundError(f"Программа {program} не найдена в доверенных установленных расположениях. Установка не выполнялась.")


def _safe_launch_arguments(kind: str | None, arguments: Any) -> bool:
    if arguments is None:
        arguments = []
    if not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
        return False
    if kind is None:
        return False
    if not arguments:
        return True
    if kind == "browser":
        return all(_http_url(a) for a in arguments)
    if kind == "explorer" and len(arguments) == 1:
        # Only existing folders, never executable files or Explorer switches.
        folder = Path(arguments[0])
        return not arguments[0].startswith(("/", "-")) and folder.is_absolute() and folder.is_dir()
    return False


def launch_program(program: str, arguments: list[str] | None = None) -> dict[str, Any]:
    executable, _ = _resolve_launch(program)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    if sys.platform == "win32" and os.environ.get("CITADEL_WORKER_JOB") == "1":
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    process = subprocess.Popen([executable, *(arguments or [])], cwd=WORKSPACE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    return {"program": executable, "pid": process.pid, "launched": True, "request_accepted": True}


TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "get_time": get_time,
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_powershell": run_powershell,
    "launch_program": launch_program,
    "open_url": open_url,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Получить текущие дату, время и часовой пояс компьютера.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Показать папку. Относительный путь — рабочая папка; абсолютный — другой каталог с подтверждением.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Относительный путь, по умолчанию ."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать текстовый файл. Доступ вне рабочей папки запрашивает подтверждение.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 30000},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Создать или перезаписать текстовый файл внутри рабочей папки.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell",
            "description": "Выполнить PowerShell для решения задачи на компьютере: программы, файлы, системные операции. Пользователь подтверждает код. Консоль скрыта.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_program",
            "description": "Запустить установленную программу Windows (например chrome или calc). Доверенные обычные запуски не требуют подтверждения. Если программа отсутствует, сообщи ошибку; не устанавливай её без поручения.",
            "parameters": {
                "type": "object",
                "properties": {
                    "program": {"type": "string"},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["program"],
            },
        },
    },
]

def run_python(code: str, timeout: int = 60) -> dict:
    executable = str(PYTHON_EXE)
    result = subprocess.run([executable, "-X", "utf8", "-c", code], cwd=WORKSPACE, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(1, min(int(timeout), 180)), creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return {"return_code": result.returncode, "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]}


def get_environment() -> dict:
    return {"os": platform.platform(), "python": sys.version.split()[0], "home": str(Path.home()), "workspace": str(WORKSPACE), "desktop": str(Path.home() / "Desktop"), "downloads": str(Path.home() / "Downloads"), "time": get_time(), "capabilities": ["web search", "read HTTP/JSON/HTML/XML", "persistent browser", "Windows UI Automation", "files", "Python", "PowerShell"], "browser_profile": "Отдельный профиль Цитадели; вход на сайты пользователь выполняет сам."}


def download_file(url: str, path: str) -> dict:
    from .web_tools import download_to
    return download_to(url, _resolve_workspace_path(path))


def browser_open(url: str) -> dict:
    from .browser import BROWSER
    return BROWSER.call("navigate", url=url)


def browser_snapshot() -> dict:
    from .browser import BROWSER
    return BROWSER.call("snapshot")


def browser_action(action: str, element_id: str = "", text: str = "", key: str = "Enter", tab_index: int = -1) -> dict:
    from .browser import BROWSER
    return BROWSER.call(action, element_id=element_id, text=text, key=key, tab_index=tab_index)


def update_plan(steps: list[str]) -> dict:
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps) or len(steps) > 10:
        raise ValueError("План должен содержать не больше 10 текстовых шагов.")
    return {"steps": steps, "note": "Это план, а не результат выполнения. Продолжай действовать инструментами."}


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}}}


from .web_tools import search_web, fetch_url
from .desktop import desktop_snapshot, desktop_action, take_screenshot
from .context_tools import read_screen, get_location

TOOL_FUNCTIONS.update({name: function for name, function in {
    "get_environment": get_environment, "search_web": search_web, "fetch_url": fetch_url,
    "run_python": run_python, "download_file": download_file,
    "browser_open": browser_open, "browser_snapshot": browser_snapshot, "browser_action": browser_action,
    "desktop_snapshot": desktop_snapshot, "desktop_action": desktop_action, "take_screenshot": take_screenshot,
    "update_plan": update_plan,
    "read_screen": read_screen, "get_location": get_location,
}.items()})

_str = {"type": "string"}
_int = {"type": "integer"}
TOOL_SCHEMAS.extend([
    _schema("open_url", "Открыть HTTP/HTTPS URL в браузере ОС по умолчанию. Возвращает только принятие запроса запуска, не читает страницу. Для обычного открыть сайт достаточно одного вызова.", {"url": _str}, ["url"]),
    _schema("read_screen", "Посмотреть текущий экран: снимок, распознанный OCR текст и открытые окна. Используй для вопросов что на экране. Картинки понимает только модель с vision.", {}),
    _schema("get_location", "Определить примерный город устройства по IP для погоды и местных запросов. VPN может искажать город; это не GPS. Если город не найден, спроси пользователя.", {}),
    _schema("get_environment", "Получить ОС, рабочие папки, текущее время и возможности компьютера.", {}),
    _schema("search_web", "Искать любую актуальную информацию в интернете. Открывай найденные URL через fetch_url для проверки фактов.", {"query": _str, "max_results": _int}, ["query"]),
    _schema("fetch_url", "Читать HTTP/HTTPS URL, API, JSON, XML, HTML порциями до 3500 символов. Для продолжения передай start=next_start. Если нужен JavaScript, используй browser_open.", {"url": _str, "max_chars": _int, "start": _int}, ["url"]),
    _schema("run_python", "Выполнить Python для вычислений, документов, обработки файлов и произвольной автоматизации. Код подтверждается пользователем. Доступен subprocess для установки необходимых пакетов.", {"code": _str, "timeout": _int}, ["code"]),
    _schema("download_file", "Скачать файл по URL на компьютер, не перезаписывая существующий. Требуется подтверждение.", {"url": _str, "path": _str}, ["url", "path"]),
    _schema("browser_open", "Открыть произвольный сайт в управляемом браузере Цитадели и прочитать текст/элементы. Это отдельный профиль браузера.", {"url": _str}, ["url"]),
    _schema("browser_snapshot", "Прочитать текущую страницу и получить свежие element_id для действий в браузере.", {}),
    _schema("browser_action", "Действовать в браузере по element_id из снимка. После действия возвращается новый снимок; проверяй результат.", {"action": {"type": "string", "enum": ["click", "fill", "press", "select", "back", "scroll", "tab", "screenshot"]}, "element_id": _str, "text": _str, "key": _str, "tab_index": _int}, ["action"]),
    _schema("desktop_snapshot", "Прочитать открытые окна Windows. Передай window_handle из списка, чтобы получить текст и control_id элементов нужного окна.", {"window_handle": _int}),
    _schema("desktop_action", "Управлять окном Windows по handle и control_id из снимка. keys использует синтаксис pywinauto: ^a, ^s, {ENTER}, %{F4}. Действия подтверждаются.", {"action": {"type": "string", "enum": ["focus", "click", "set_text", "keys", "close"]}, "window_handle": _int, "control_id": _str, "text": _str, "keys": _str}, ["action", "window_handle"]),
    _schema("take_screenshot", "Сохранить снимок экрана для пользователя. Для чтения интерфейса текстовой моделью используй desktop_snapshot.", {}),
    _schema("update_plan", "Показать пользователю короткий план сложной задачи; после этого выполняй шаги.", {"steps": {"type": "array", "items": _str}}, ["steps"]),
])

CONFIRMATION_REQUIRED = {"run_powershell", "run_python", "launch_program", "download_file", "desktop_action"}


def needs_confirmation(name: str, arguments: dict[str, Any]) -> bool:
    if name == "launch_program":
        _, kind = _resolve_launch(arguments.get("program", ""))
        return not _safe_launch_arguments(kind, arguments.get("arguments"))
    if name in CONFIRMATION_REQUIRED:
        return True
    if name == "browser_action":
        return arguments.get("action") in {"click", "fill", "press", "select"}
    if name in {"read_file", "list_files", "write_file"}:
        path = _resolve_workspace_path(arguments.get("path", "."))
        return not is_inside_workspace(path) or (name == "write_file" and path.exists())
    return False


def execute(name: str, arguments: dict[str, Any]) -> Any:
    if name not in TOOL_FUNCTIONS:
        raise ValueError(f"Неизвестный инструмент: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("Аргументы инструмента должны быть JSON-объектом.")
    inspect.signature(TOOL_FUNCTIONS[name]).bind(**arguments)
    return TOOL_FUNCTIONS[name](**arguments)


def format_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
