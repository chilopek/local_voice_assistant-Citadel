"""Local reminders and requested watches. No model, shell or network in polling."""
from __future__ import annotations

from contextlib import closing
import ctypes
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any
import uuid

from .paths import DATA_ROOT
from .memory import Memory


def personal_path() -> Path:
    return DATA_ROOT / "data" / "personal.sqlite3"


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        raise ValueError("Нужен положительный PID процесса")
    if sys.platform == "win32":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return None
            raise ctypes.WinError(error)
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                raise ctypes.WinError(ctypes.get_last_error())
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


class PersonalStore:
    def __init__(self, path: Path | None = None):
        self.path = path or personal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notices (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                due REAL NOT NULL, repeat REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL DEFAULT '{}',
                lease REAL NOT NULL DEFAULT 0)""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        return closing_transaction(db)

    def add(self, kind: str, title: str, due: float, repeat: float = 0, **payload) -> dict:
        if not title.strip() or len(title) > 1000:
            raise ValueError("Текст должен содержать от 1 до 1000 символов")
        if not math.isfinite(due) or not math.isfinite(repeat) or repeat < 0 or (repeat and repeat < 10):
            raise ValueError("Повтор должен быть не реже 10 секунд")
        due_at = dt.datetime.fromtimestamp(due, dt.timezone.utc).astimezone().isoformat()
        item = dict(id=uuid.uuid4().hex[:12], kind=kind, title=title.strip(), due=due, repeat=repeat)
        with self.connect() as db:
            if db.execute("SELECT count(*) FROM notices WHERE state IN ('pending','delivering')").fetchone()[0] >= 100:
                raise ValueError("Уже есть 100 активных напоминаний/наблюдений")
            db.execute("INSERT INTO notices(id,kind,title,due,repeat,payload) VALUES(:id,:kind,:title,:due,:repeat,:payload)",
                       {**item, "payload": json.dumps(payload)})
        return {**item, "due_at": due_at,
                "note": "Уведомление работает пока Цитадель открыта; пропущенное напоминание появится при следующем запуске."}

    def list(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,kind,title,due,repeat,state FROM notices WHERE state IN ('pending','delivering') ORDER BY due")]

    def cancel(self, notice_id: str) -> dict:
        with self.connect() as db:
            changed = db.execute("UPDATE notices SET state='cancelled' WHERE id=? AND state IN ('pending','delivering')", (notice_id,)).rowcount
        return {"id": notice_id, "cancelled": bool(changed)}

    def snooze(self, notice_id: str, seconds: float) -> dict:
        if not math.isfinite(seconds) or seconds < 1:
            raise ValueError("Задержка должна быть положительной")
        with self.connect() as db:
            changed = db.execute("UPDATE notices SET state='pending',due=?,lease=0 WHERE id=? AND kind='reminder' AND state!='cancelled'",
                                 (time.time() + seconds, notice_id)).rowcount
        return {"id": notice_id, "rescheduled": bool(changed)}

    def poll(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        ready = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM notices WHERE (state='pending' AND due<=?) OR (state='delivering' AND lease<=?)", (now, now)).fetchall()
            for row in rows:
                item = dict(row)
                data = json.loads(item['payload'])
                if item['state'] == 'pending' and item['kind'] != 'reminder':
                    try:
                        if item['kind'] == 'process':
                            if process_identity(data['pid']) == data['identity']:
                                continue
                            message = f"Процесс «{item['title']}» завершился. Код завершения не проверен."
                        else:
                            path = Path(data['path'])
                            if not path.is_file():
                                continue
                            stat = path.stat()
                            signature = [stat.st_size, stat.st_mtime_ns]
                            if data.get('signature') != signature:
                                data.update(signature=signature, stable_since=now)
                                db.execute("UPDATE notices SET payload=? WHERE id=?", (json.dumps(data), item['id']))
                                continue
                            if now - data['stable_since'] < data['stable_seconds']:
                                continue
                            message = f"Файл «{item['title']}» появился и не менялся {data['stable_seconds']:g} секунд: {data['path']}. Это проверка стабильности файла, не проверка целостности загрузки."
                    except OSError as error:
                        message = f"Не удалось продолжить наблюдение «{item['title']}»: {error}"
                    data['message'] = message
                message = data.get('message', item['title'])
                token = uuid.uuid4().hex
                db.execute("UPDATE notices SET state='delivering',lease=?,payload=? WHERE id=?", (now + 300, json.dumps({**data, 'token': token}), item['id']))
                ready.append({**item, 'message': message, 'token': token})
        return ready

    def acknowledge(self, item: dict, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("SELECT * FROM notices WHERE id=? AND state='delivering'", (item['id'],)).fetchone()
            if not row or json.loads(row['payload']).get('token') != item.get('token'):
                return False  # cancelled, rescheduled, or another app owns delivery
            if row['repeat']:
                due = row['due'] + (max(0, math.floor((now-row['due']) / row['repeat']))+1) * row['repeat']
                db.execute("UPDATE notices SET state='pending',due=?,lease=0 WHERE id=?", (due, item['id']))
            else:
                db.execute("UPDATE notices SET state='delivered',lease=0 WHERE id=?", (item['id'],))
        return True

    def current_delivery(self, item: dict) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM notices WHERE id=? AND state='delivering'", (item['id'],)).fetchone()
        return bool(row and json.loads(row['payload']).get('token') == item.get('token'))


class closing_transaction:
    def __init__(self, db):
        self.db = db
    def __enter__(self):
        return self.db
    def __exit__(self, kind, *_):
        try:
            self.db.rollback() if kind else self.db.commit()
        finally:
            self.db.close()


def create_reminder(title: str, delay_seconds: float = 0, due_at: str = "", repeat_seconds: float = 0) -> dict:
    if due_at and delay_seconds:
        raise ValueError("Укажи либо due_at, либо delay_seconds")
    if due_at:
        stamp = dt.datetime.fromisoformat(due_at)
        if stamp.tzinfo is None:
            raise ValueError("due_at требует часовой пояс, например +07:00")
        due = stamp.timestamp()
        if due <= time.time():
            raise ValueError("Время напоминания уже прошло")
    else:
        if not math.isfinite(delay_seconds) or delay_seconds < 1:
            raise ValueError("Задержка должна быть положительной")
        due = time.time() + delay_seconds
    return PersonalStore().add('reminder', title, due, repeat_seconds)


def list_reminders() -> list[dict]:
    return PersonalStore().list()


def cancel_reminder(notice_id: str) -> dict:
    return PersonalStore().cancel(notice_id)


def snooze_reminder(notice_id: str, delay_seconds: float = 600) -> dict:
    return PersonalStore().snooze(notice_id, delay_seconds)


def watch_process(pid: int, title: str) -> dict:
    identity = process_identity(pid)
    if identity is None:
        raise ValueError("Процесс уже завершён или не существует")
    return PersonalStore().add('process', title, time.time(), pid=pid, identity=identity)


def watch_file(path: str, title: str = "", stable_seconds: float = 15) -> dict:
    from .config import WORKSPACE
    target = Path(os.path.expandvars(path)).expanduser()
    target = (target if target.is_absolute() else WORKSPACE / target).resolve()
    if not math.isfinite(stable_seconds) or not 5 <= stable_seconds <= 3600:
        raise ValueError("Стабильность файла: от 5 до 3600 секунд")
    return PersonalStore().add('file', title or target.name, time.time(), path=str(target), stable_seconds=stable_seconds)


def remember_fact(key: str, value: str) -> dict:
    if not key.strip() or len(key) > 100 or not value.strip() or len(value) > 2000:
        raise ValueError("Ключ памяти: 1–100 символов, значение: 1–2000")
    memory = Memory(DATA_ROOT / 'data/memory.sqlite3')
    records = memory.get_all()
    if key.strip() not in records and len(records) >= 50:
        raise ValueError("Память содержит 50 записей. Удалите ненужные в «Дела и память».")
    memory.set(key.strip(), value.strip())
    return {'saved': True, 'key': key.strip(), 'value': value.strip()}


def list_memory() -> dict:
    return Memory(DATA_ROOT / 'data/memory.sqlite3').get_all()


def forget_fact(key: str) -> dict:
    return {'key': key, 'forgotten': Memory(DATA_ROOT / 'data/memory.sqlite3').delete(key)}


def quick_request(text: str) -> tuple[str, dict] | None:
    """Only explicit, unambiguous user requests. Other wording goes to the agent."""
    if re.search(r"\b(?:потом|затем|также)\b|(?:[.;!?]\s+|\s+и\s+)(?:открой|создай|запусти|найди|сохрани|скачай|удали|напомни|запомни)\b", text, re.I):
        return None
    numbers = {'один':1,'одну':1,'два':2,'две':2,'три':3,'четыре':4,'пять':5,'шесть':6,'семь':7,'восемь':8,'девять':9,'десять':10,'пятнадцать':15,'двадцать':20,'тридцать':30,'сорок':40,'пятьдесят':50,'шестьдесят':60}
    quantity = r'(\d+(?:[.,]\d+)?|' + '|'.join(numbers) + ')'
    match = re.fullmatch(r"\s*(?:поставь\s+)?таймер\s+(?:на\s+)?" + quantity + r"\s*(секунд\w*|минут\w*|час\w*)[.!]?\s*", text, re.I)
    reminder = re.fullmatch(r"\s*напомни\s+через\s+" + quantity + r"\s*(секунд\w*|минут\w*|час\w*)\s*[,—:-]?\s+(.+)", text, re.I)
    recurring = re.fullmatch(r"\s*напоминай\s+каждые\s+" + quantity + r"\s*(секунд\w*|минут\w*|час\w*)\s*[,—:-]?\s+(.+)", text, re.I)
    if match or reminder or recurring:
        found = recurring or reminder or match
        unit = found[2].lower()
        amount = numbers[found[1].lower()] if found[1].lower() in numbers else float(found[1].replace(',', '.'))
        seconds = amount * (3600 if unit.startswith('час') else 60 if unit.startswith('минут') else 1)
        return 'create_reminder', {'title': found[3] if reminder or recurring else 'Таймер завершён', 'delay_seconds': seconds, **({'repeat_seconds':seconds} if recurring else {})}
    match = re.fullmatch(r"\s*запомни\s*:\s*(.{1,2000})", text, re.I | re.S)
    if match:
        value = match[1].strip()
        return 'remember_fact', {'key': 'заметка_' + hashlib.sha256(value.encode()).hexdigest()[:10], 'value': value}
    return None


class PersonalScheduler:
    def __init__(self, emit, path: Path | None = None):
        self.emit, self.path = emit, path
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='citadel-reminders')
    def start(self):
        self.thread.start()
    def stop(self):
        self.stop_event.set()
    def run(self):
        import logging
        store = PersonalStore(self.path)
        while not self.stop_event.is_set():
            try:
                for item in store.poll():
                    self.emit(item)
            except Exception:
                logging.getLogger(__name__).exception('Ошибка фоновых напоминаний')
            self.stop_event.wait(1)
