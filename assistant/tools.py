from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from .config import WORKSPACE, is_inside_workspace


def _resolve_workspace_path(relative_path: str) -> Path:
    candidate = (WORKSPACE / relative_path).resolve()
    if not is_inside_workspace(candidate):
        raise ValueError("Путь выходит за пределы рабочей папки помощника.")
    return candidate


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
    text = file_path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(file_path), "content": text[:max_chars], "truncated": len(text) > max_chars}


def write_file(path: str, content: str) -> dict[str, Any]:
    file_path = _resolve_workspace_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {"path": str(file_path), "bytes": file_path.stat().st_size}


def run_powershell(command: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "return_code": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
    }


def launch_program(program: str, arguments: list[str] | None = None) -> dict[str, Any]:
    process = subprocess.Popen([program, *(arguments or [])], cwd=WORKSPACE)
    return {"program": program, "pid": process.pid}


TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "get_time": get_time,
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_powershell": run_powershell,
    "launch_program": launch_program,
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
            "description": "Показать содержимое папки внутри рабочей папки помощника.",
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
            "description": "Прочитать текстовый файл внутри рабочей папки помощника.",
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
            "description": "Выполнить команду PowerShell в рабочей папке. Использовать только по явной просьбе пользователя.",
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
            "description": "Запустить программу Windows. Использовать только по явной просьбе пользователя.",
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

CONFIRMATION_REQUIRED = {"write_file", "run_powershell", "launch_program"}


def execute(name: str, arguments: dict[str, Any]) -> Any:
    if name not in TOOL_FUNCTIONS:
        raise ValueError(f"Неизвестный инструмент: {name}")
    return TOOL_FUNCTIONS[name](**arguments)


def format_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
