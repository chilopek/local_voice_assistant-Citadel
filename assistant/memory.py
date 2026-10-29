from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memories (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def get_all(self) -> dict[str, str]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT key, value FROM memories ORDER BY key").fetchall()
        return {key: value for key, value in rows}

    def set(self, key: str, value: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO memories(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def prompt_context(self) -> str:
        memories = self.get_all()
        if not memories:
            return "Постоянная память пока пуста."
        return "Постоянная память пользователя:\n" + json.dumps(
            memories, ensure_ascii=False, indent=2
        )
