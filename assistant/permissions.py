"""Effect-based local path policy; no workspace boundary for ordinary files."""
from __future__ import annotations

import os
from pathlib import Path
from .config import WORKSPACE


# Unknown associations are deliberately not treated as document viewers.
DOCUMENT_SUFFIXES = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".log", ".pdf", ".rtf", ".epub",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mkv", ".mov", ".avi",
})


def protected_path(path: Path) -> bool:
    """Resolve aliases before checking OS, startup and executable config roots."""
    path = path.resolve()
    parts = [part.casefold().rstrip(" .") for part in path.parts]
    if ":" in path.name or any(part in {
        "windows", "system32", "syswow64", "program files", "program files (x86)",
        "programdata", "startup", "start menu", "autostart", ".ssh", ".config",
        ".codex", ".vscode", ".git", "site-packages", "dist-packages",
        "windowspowershell", "powershell",
    } for part in parts):
        return True
    if path.name.casefold() in {
        "autorun.inf", "desktop.ini", ".bashrc", ".bash_profile", ".profile",
        ".zshrc", ".zprofile", "profile.ps1", "microsoft.powershell_profile.ps1",
    }:
        return True
    local = os.environ.get("LOCALAPPDATA")
    ordinary_temp = bool(local and path.is_relative_to(Path(local).resolve() / "Temp"))
    ordinary_workspace = path.is_relative_to(WORKSPACE.resolve())
    for variable in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramData", "APPDATA", "LOCALAPPDATA"):
        if ordinary_temp and variable == "LOCALAPPDATA":
            continue
        if ordinary_workspace and variable in {"APPDATA", "LOCALAPPDATA"}:
            continue
        root = os.environ.get(variable)
        if root and path.is_relative_to(Path(root).resolve()):
            return True
    # Also works on non-Windows hosts and for relocated user profiles.
    home = Path.home().resolve()
    return (not ordinary_temp and not ordinary_workspace and path.is_relative_to(home / "AppData")) or any(
        path.is_relative_to(home / relative) for relative in (
            "Library/LaunchAgents", "Library/LaunchDaemons",
        )) or any(path.is_relative_to(Path(root)) for root in (
        "/etc", "/bin", "/sbin", "/usr", "/boot", "/System", "/Library",
    ))


def ordinary_document(path: Path) -> bool:
    # Opening a folder or document does not modify its protected parent.
    return (":" not in path.name and
            (path.is_dir() or path.suffix.casefold() in DOCUMENT_SUFFIXES))


def creation_requires_confirmation(path: Path) -> bool:
    return protected_path(path) or path.exists() or path.is_symlink()


def download_requires_confirmation(path: Path) -> bool:
    # Storage is not execution: opening an executable is assessed separately.
    return creation_requires_confirmation(path)
