"""Discover application launch entries registered with the Windows shell."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import unicodedata


@dataclass(frozen=True)
class Application:
    name: str
    target: str
    source: str
    shell: bool = False
    executable: str = ""
    arguments: str = ""
    directory: str = ""


class AmbiguousApplication(ValueError):
    """Several distinct launch entries have the same name."""


def name_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"\.(?:exe|lnk|url)$", "", value)
    return "".join(character for character in value if character.isalnum() or character in "+#")


_LATIN = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                  ("a","b","v","g","d","e","yo","zh","z","i","y","k","l","m","n","o","p",
                   "r","s","t","u","f","kh","ts","ch","sh","shch","","y","","e","yu","ya")))


def _local_path(value: str) -> Path | None:
    path = Path(os.path.expandvars(value.strip().strip('"')))
    if path.is_absolute() and not str(path).startswith(("\\\\", "//")):
        return path
    return None


def _shortcuts(client):
    from pywintypes import com_error
    from win32com.shell import shell, shellcon

    shortcuts = client.Dispatch("WScript.Shell")
    for folder_id, nested in ((shellcon.CSIDL_PROGRAMS, True),
                              (shellcon.CSIDL_COMMON_PROGRAMS, True),
                              (shellcon.CSIDL_DESKTOPDIRECTORY, False),
                              (shellcon.CSIDL_COMMON_DESKTOPDIRECTORY, False)):
        try:
            root = Path(shell.SHGetFolderPath(0, folder_id, 0, 0))
        except (OSError, com_error):
            continue
        for folder, directories, files in os.walk(root):
            parent = Path(folder)
            directories[:] = [name for name in directories if nested
                              and not (parent / name).is_symlink()
                              and not (parent / name).is_junction()]
            for name in files:
                path = parent / name
                if path.is_symlink():
                    continue
                if path.suffix.lower() == ".url":
                    yield Application(path.stem, str(path), "shortcut", shell=True)
                if path.suffix.lower() == ".lnk":
                    try:
                        link = shortcuts.CreateShortcut(str(path))
                        executable = _local_path(link.TargetPath)
                        if executable and executable.suffix.lower() == ".exe" and executable.is_file():
                            yield Application(path.stem, str(path), "shortcut", True,
                                              str(executable), link.Arguments, link.WorkingDirectory)
                    except (OSError, com_error):
                        continue


def _shell_apps(client):
    namespace = client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
    if namespace is None:
        return
    for item in namespace.Items():
        target = str(item.Path)
        path = _local_path(target)
        if path and path.suffix.lower() == ".exe" and path.is_file():
            yield Application(str(item.Name), str(path), "apps_folder", executable=str(path))
        elif re.fullmatch(r"[\w.{}!@+-]+", target):
            yield Application(str(item.Name), "shell:AppsFolder\\" + target, "apps_folder", shell=True)


def _app_paths():
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths",
                                    access=winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            name = winreg.EnumKey(root, index)
                            with winreg.OpenKey(root, name) as key:
                                value, value_type = winreg.QueryValueEx(key, "")
                            if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                                continue
                            path = _local_path(value)
                            if path and path.suffix.lower() == ".exe" and path.is_file():
                                yield Application(Path(name).stem, str(path), "app_paths", executable=str(path))
                        except OSError:
                            continue
            except OSError:
                continue


def catalog() -> list[Application]:
    if sys.platform != "win32":
        return []
    import pythoncom
    import win32com.client

    entries = []
    pythoncom.CoInitialize()
    try:
        for source in (_shortcuts, _shell_apps):
            try:
                entries.extend(source(win32com.client))
            except (OSError, pythoncom.com_error):
                continue
    finally:
        pythoncom.CoUninitialize()
    entries.extend(_app_paths())
    return list(dict.fromkeys(entries))


def resolve_application(query: str) -> Application | None:
    key = name_key(query)
    if not key:
        return None
    entries = catalog()
    matches = [entry for entry in entries if name_key(entry.name) == key]
    if not matches:
        latin = "".join(_LATIN.get(character, character) for character in key)
        if latin != key:
            matches = [entry for entry in entries if name_key(entry.name) == latin]
    if not matches:
        matches = [entry for entry in entries if entry.executable
                   and name_key(Path(entry.executable).stem) == key]
    for source in ("shortcut", "apps_folder", "app_paths"):
        candidates = {}
        for entry in matches:
            if entry.source == source:
                identity = (os.path.normcase(entry.executable or entry.target),
                            entry.arguments, os.path.normcase(entry.directory))
                candidates.setdefault(identity, entry)
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if candidates:
            choices = ", ".join(f"{item.name} ({item.target})" for item in list(candidates.values())[:5])
            raise AmbiguousApplication(f"Найдено несколько вариантов «{query}». Какой открыть? {choices}")
    return None


def find_apps(query: str, limit: int = 12) -> list[dict]:
    key = name_key(query)
    if not key:
        raise ValueError("Укажи название приложения.")
    results = []
    seen = set()
    for entry in catalog():
        if key not in name_key(entry.name) and key not in name_key(Path(entry.executable).stem):
            continue
        identity = (entry.name.casefold(), os.path.normcase(entry.executable or entry.target))
        if identity not in seen:
            seen.add(identity)
            results.append({"name": entry.name, "source": entry.source})
    return results[:max(1, min(limit, 30))]
