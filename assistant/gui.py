from __future__ import annotations

import json
import datetime as dt
import queue
import threading
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from . import __version__

from .app import SYSTEM_PROMPT
from .config import MODEL, PROJECT_ROOT, THINK, VOICE_ENABLED, WORKSPACE
from .memory import Memory
from .session import SessionProcess
from .speech import concise_speech
from .paths import installed_uninstaller
from .uninstall import begin_uninstall
from .personal import PersonalScheduler, PersonalStore
from .conversations import Conversations

WAKE_WORD = "цитадель"
HOTKEY = "Ctrl+Alt+C"
MODE_LABELS = {"wake": f"После слова «{WAKE_WORD}»", "always": "Все голосовые команды"}
BG, PANEL, SIDEBAR = "#111318", "#191c23", "#14171d"
TEXT, MUTED, ACCENT = "#e8eaf0", "#929aaa", "#a9c7a5"


class AssistantGUI:
    def __init__(self, root: tk.Tk, *, enable_hotkey: bool = True, warm_voice: bool = True, enable_scheduler: bool = True, enable_history: bool = True) -> None:
        self.root = root
        root.title(f"Citadel {__version__.removesuffix('.0')} — локальный помощник")
        width = min(1220, max(900, root.winfo_screenwidth() - 80))
        height = min(800, max(600, root.winfo_screenheight() - 80))
        root.geometry(f"{width}x{height}")
        root.minsize(900, 600)
        root.configure(bg=BG)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.memory = Memory(PROJECT_ROOT / "data" / "memory.sqlite3")
        self.messages: list[dict[str, Any]] = self._initial_messages()
        self.history = Conversations(PROJECT_ROOT / 'data/conversations.sqlite3',timeout=.2) if enable_history else None
        self._chat_id = None
        self._turn_id = None
        self.suggestions_enabled = threading.Event()
        if self.history and self.history.setting('suggestions',False): self.suggestions_enabled.set()
        self.proactive = None
        self.stop_event: threading.Event | None = None
        self.cancel_event = threading.Event()
        self.closed = threading.Event()
        self.speech_enabled = threading.Event()
        self.worker: SessionProcess | None = None
        self.voice_host: SessionProcess | None = None
        self._voice_ready = False
        self._voice_speech_ready = False
        self._voice_token = uuid.uuid4().hex
        self._pending_listen = False
        self._awaiting_speech = False
        self._agent_running = False
        self._pending_notices: list[dict] = []
        self._notice_speech = False
        self._pending_notice_audio: list[str] = []
        self._notice_toast = None
        self._personal_path = PROJECT_ROOT / 'data/personal.sqlite3'
        self.scheduler = None
        self._voice_setup = None
        self.voice_setup_var = tk.StringVar(value='Выбор модели применяется после перезапуска приложения.')
        self._session_id: str | None = None
        self.hotkey_listener: Any | None = None
        self.mode = "wake"
        self.busy = False
        self.listening = False
        self._session_error = False
        self._pending_confirmation: dict[str, Any] | None = None
        self._confirmation_window: tk.Toplevel | None = None
        self.mode_var = tk.StringVar(value=MODE_LABELS[self.mode])
        self.status_var = tk.StringVar(value="Готов к работе")
        self.voice_status_var = tk.StringVar(value="Голос выключен")
        self.speak_var = tk.BooleanVar(value=VOICE_ENABLED)
        self.speak_var.trace_add("write", self._sync_speech)
        self._sync_speech()
        self._build_style()
        self._build_ui()
        if enable_hotkey:
            self._start_hotkey()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(60, self._drain_events)
        self._welcome()
        if self.history:
            self._restore_dialog(self.history.current())
        self.entry.focus_set()
        if warm_voice and VOICE_ENABLED:
            self._ensure_voice()
        if enable_scheduler:
            self.scheduler = PersonalScheduler(lambda item: self.events.put(('notice', item)), self._personal_path)
            self.scheduler.start()
            from .proactive import ProactiveMonitor
            self.proactive = ProactiveMonitor(lambda text:self.events.put(('suggestion',text)),WORKSPACE,self.suggestions_enabled,lambda:self.busy)
            self.proactive.start()

    def _initial_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": SYSTEM_PROMPT + "\n" + self.memory.prompt_context()}]

    def _sync_speech(self, *_args: Any) -> None:
        if self.speak_var.get():
            self.speech_enabled.set()
        else:
            self.speech_enabled.clear()
        if hasattr(self, "listen_button"):
            if self.speak_var.get():
                self._ensure_voice()
            elif self.voice_host and self._awaiting_speech:
                self.voice_host.send({"command": "cancel", "token": self._voice_token})
                self._awaiting_speech = False
                if self.listening:
                    self.voice_host.send({"command": "listen", "token": self._voice_token, "mode": self._mode_key()})
                elif not self._agent_running:
                    self._finish_session(self.cancel_event, keep_worker=True)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TButton", background="#282e38", foreground=TEXT, borderwidth=0,
                        padding=(14, 10), font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#353e4b"), ("disabled", "#20242c")],
                  foreground=[("disabled", "#626b7a")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#152019")
        style.map("Accent.TButton", background=[("active", "#bfd9bb"), ("disabled", "#303b33")])
        style.configure("Stop.TButton", foreground="#efaaa4")
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, borderwidth=0, padding=12)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, arrowcolor=MUTED, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        style.configure("TCheckbutton", background=SIDEBAR, foreground=MUTED, font=("Segoe UI", 9), padding=(0, 7))
        style.map("TCheckbutton", background=[("active", SIDEBAR)])
        style.configure("Vertical.TScrollbar", background="#333b48", troughcolor=BG, borderwidth=0, arrowsize=10)
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    @staticmethod
    def _label(parent: tk.Widget, text: str, *, color: str = MUTED, size: int = 10, bold: bool = False) -> tk.Label:
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI Semibold" if bold else "Segoe UI", size), anchor="w")

    def _build_ui(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        sidebar = tk.Frame(self.root, bg=SIDEBAR, width=232, padx=20, pady=22)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.pack_propagate(False)
        self._label(sidebar, "◈  Citadel", color=TEXT, size=23, bold=True).pack(anchor="w")
        self._label(sidebar, "ВАШ ЛОКАЛЬНЫЙ ПОМОЩНИК", size=8).pack(anchor="w", pady=(4, 18))
        self.new_button = ttk.Button(sidebar, text="＋  Новая задача", command=self._clear_dialog)
        self.new_button.pack(fill="x")
        bottom = tk.Frame(sidebar, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x")
        self._label(bottom, "Локально · Ollama", color=ACCENT, size=9).pack(anchor="w", pady=(8, 3))
        self._label(bottom, MODEL, size=9).pack(anchor="w")
        self._label(bottom, "ГОЛОС", size=8).pack(anchor="w", pady=(16, 8))
        tk.Label(bottom, textvariable=self.voice_status_var, bg=SIDEBAR, fg=ACCENT,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(0, 5))
        self.mode_box = ttk.Combobox(bottom, textvariable=self.mode_var, values=list(MODE_LABELS.values()),
                                    state="readonly", width=22)
        self.mode_box.pack(fill="x")
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_changed)
        self.listen_button = ttk.Button(bottom, text="Включить микрофон", command=self._toggle_listening)
        self.listen_button.pack(fill="x", pady=(10, 2))
        self.mic_level_var = tk.StringVar(value="Микрофон выключен")
        self.mic_meter = tk.Canvas(bottom, width=184, height=14, bg=SIDEBAR, highlightthickness=0)
        self.mic_meter.pack(fill="x", pady=(10, 3))
        self._mic_segments = [self.mic_meter.create_rectangle(2 + i * 9, 2, 8 + i * 9, 12,
                              fill="#2b313b", outline="") for i in range(20)]
        tk.Label(bottom, textvariable=self.mic_level_var, bg=SIDEBAR, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(0, 3))
        ttk.Checkbutton(bottom, text="Кратко озвучивать ответ", variable=self.speak_var).pack(anchor="w")
        self._label(bottom, HOTKEY + "  ·  микрофон", size=9).pack(anchor="w", pady=(2, 0))
        self.uninstall_button = ttk.Button(bottom, text="Удалить помощника…", command=self._uninstall,
                                           state="normal" if installed_uninstaller() else "disabled")
        self.uninstall_button.pack(fill="x", pady=(16, 0))

        main = ttk.Frame(self.root, padding=(28, 26, 24, 16))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        ttk.Label(header, text="Чем займёмся?", style="Title.TLabel").pack(side="left")
        self.stop_button = ttk.Button(header, text="■  Остановить", style="Stop.TButton", command=self._stop_task, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Button(header, text="Дела и память", command=self._show_personal).pack(side="right", padx=8)
        content = ttk.Frame(main)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        chat = ttk.Frame(content)
        chat.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        self.transcript = tk.Text(chat, wrap="word", state="disabled", bg=BG, fg=TEXT, relief="flat",
                                  borderwidth=0, highlightthickness=0, padx=4, pady=8,
                                  font=("Segoe UI", 11), width=32, selectbackground="#38465a")
        scroll = ttk.Scrollbar(chat, command=self.transcript.yview)
        scroll.pack(side="right", fill="y")
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(fill="both", expand=True)
        self.transcript.tag_configure("speaker", foreground=ACCENT, font=("Segoe UI Semibold", 10), spacing1=12, spacing3=6)
        self.transcript.tag_configure("body", foreground=TEXT, spacing1=2, spacing3=12)
        self.transcript.tag_configure("system", foreground=MUTED, spacing3=12)
        activity_panel = tk.Frame(content, bg=PANEL, width=244, padx=14, pady=16)
        activity_panel.grid(row=0, column=1, sticky="nsew")
        activity_panel.pack_propagate(False)
        self._label(activity_panel, "АКТИВНОСТЬ", size=9, bold=True).pack(anchor="w")
        self._label(activity_panel, "Ход выполнения задачи", size=9).pack(anchor="w", pady=(5, 16))
        self.activity = tk.Text(activity_panel, wrap="word", state="disabled", bg=PANEL, fg=MUTED,
                                relief="flat", highlightthickness=0, font=("Segoe UI", 9), width=20, padx=0, pady=4)
        activity_scroll = ttk.Scrollbar(activity_panel, command=self.activity.yview)
        activity_scroll.pack(side="right", fill="y")
        self.activity.configure(yscrollcommand=activity_scroll.set)
        self.activity.pack(fill="both", expand=True)
        self.activity.tag_configure("title", foreground=ACCENT, spacing1=10, spacing3=5)
        self.activity.tag_configure("detail", spacing3=10)
        composer = ttk.Frame(main, padding=(0, 20, 0, 0))
        composer.grid(row=2, column=0, sticky="ew")
        self.entry = ttk.Entry(composer, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", lambda _event: self._send_text())
        self.send_button = ttk.Button(composer, text="Отправить ↑", style="Accent.TButton", command=self._send_text)
        self.send_button.pack(side="right")
        footer = ttk.Frame(main, padding=(0, 12, 0, 0))
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(footer, text=f"{'Thinking' if THINK else 'Fast'} · {WORKSPACE}",
                  style="Muted.TLabel", wraplength=650).pack(anchor="w", pady=(4, 0))

    def _welcome(self) -> None:
        self._append_message("Citadel", "Опишите задачу обычными словами. Я помогу с браузером, файлами и приложениями Windows.\n\nДействия, требующие разрешения, появятся в окне подтверждения.")
        self._log_activity("Готов к работе", "Здесь появятся шаги и результаты инструментов.")

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard
            self.hotkey_listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+c": self._hotkey_pressed})
            self.hotkey_listener.start()
        except Exception as error:
            self._set_status(f"Горячая клавиша недоступна: {error}")

    def _hotkey_pressed(self) -> None:
        self.events.put(("hotkey", None))

    def _mode_key(self) -> str:
        selected = self.mode_var.get()
        return next((key for key, label in MODE_LABELS.items() if label == selected), "wake")

    def _mode_changed(self, _event: Any = None) -> None:
        if self.listening:
            self._stop_task()
        self._set_status(f"Выбран режим: {self.mode_var.get()}")

    def _toggle_listening(self) -> None:
        if self._pending_listen:
            self._pending_listen = False
            self.listen_button.configure(text="Включить микрофон")
            self.status_var.set("Включение микрофона отменено")
            return
        if self.listening:
            self._stop_listening()
        else:
            self._start_listening()

    def _reserve_session(self, *, listening: bool = False) -> bool:
        # The UI thread owns busy. Reserve BEFORE spawning any worker.
        if self.busy or self.closed.is_set():
            return False
        if self._notice_speech and self.voice_host:
            self.voice_host.send({'command': 'cancel', 'token': self._voice_token})
            self._notice_speech = self._awaiting_speech = False
        self.busy, self.listening = True, listening
        self._session_error = False
        self.cancel_event = threading.Event()
        self.entry.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.new_button.configure(state="disabled")
        self.uninstall_button.configure(state="disabled")
        self.listen_button.configure(state="normal" if listening else "disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Слушаю..." if listening else "Обрабатываю задачу...")
        return True

    def _start_listening(self) -> None:
        if self.busy:
            return
        if not self._voice_ready or (self.speak_var.get() and not self._voice_speech_ready):
            self._pending_listen = True
            self._ensure_voice()
            self.listen_button.configure(text="Отменить включение")
            self.status_var.set("Голос готовится при запуске. Микрофон включится после подготовки.")
            return
        if not self._reserve_session(listening=True):
            return
        mode = self._mode_key()
        self._voice_token = uuid.uuid4().hex
        self.voice_host.send({"command": "listen", "token": self._voice_token, "mode": mode})
        self.listen_button.configure(text="Выключить микрофон")
        self.status_var.set(f"Слушаю: {WAKE_WORD}" if mode == "wake" else "Слушаю...")

    def _stop_listening(self) -> None:
        self._stop_task()

    def _stop_task(self) -> None:
        self._pending_listen = False
        if self._notice_speech:
            self._notice_speech = self._awaiting_speech = False
            self._pending_notice_audio.clear()
            self._voice_token = uuid.uuid4().hex
            if self.voice_host:
                self.voice_host.send({'command':'cancel','token':self._voice_token})
            if not self.busy:
                self.stop_button.configure(state='disabled')
                self.status_var.set('Озвучка напоминания остановлена')
        if not self.busy:
            return
        self.cancel_event.set()
        self._checkpoint_interruption()
        worker, self.worker = self.worker, None
        # Invalidate queued events BEFORE killing. Late results cannot touch the new task.
        self._session_id = None
        self._voice_token = uuid.uuid4().hex
        self._awaiting_speech = False
        self._agent_running = False
        if self.voice_host:
            self.voice_host.send({"command": "cancel", "token": self._voice_token})
        self._resolve_confirmation(False)
        if worker:
            worker.stop()
        self._finish_session(self.cancel_event)
        if self.history and self._chat_id:
            try:
                self.messages = self.history.repair_transcript(self.history.load(self._chat_id)['messages'])
            except (OSError,ValueError):
                self.messages = self.history.repair_transcript(self.messages)
        self._log_activity("Задача остановлена", "Выполнение и озвучка прерваны. Можно отправить новую задачу.")

    def _start_process(self, mode: str, text: str = "") -> None:
        self._agent_running = True
        payload = {"mode": mode, "text": text, "messages": self.messages, "speak": False}
        if self.history and self._chat_id:
            import re
            continuation = bool(re.fullmatch(r'\s*(?:продолжи|продолжай|continue|resume)[.!\s]*', text, re.I))
            task = self.history.begin(self._chat_id, text, self.messages, resume=continuation)
            self._turn_id = task['turn_id']
            payload.update(task, history_path=str(self.history.path))
        if self.worker and not self.worker.stopped.is_set() and self.worker.process and self.worker.process.poll() is None:
            self._session_id = self.worker.id
            self.worker.send({"command": "new_task", "payload": payload})
            return
        worker = SessionProcess(lambda sid, event, data: self.events.put(("worker", (sid, event, data))))
        self.worker, self._session_id = worker, worker.id
        worker.start(payload)

    def _send_text(self) -> None:
        text = self.entry.get().strip()
        if not text or not self._reserve_session():
            return
        # ttk entries cannot be edited while disabled.
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        self.entry.configure(state="disabled")
        self._append_message("Вы", text)
        self._voice_token = uuid.uuid4().hex
        self._start_process("task", text)

    def _ensure_voice(self) -> None:
        speech = bool(self.speak_var.get())
        if self.voice_host and not self.voice_host.stopped.is_set():
            if speech and self._voice_ready and not self._voice_speech_ready:
                self._voice_ready = False
                self.voice_host.send({"command": "warm", "speech": True})
            return
        host = SessionProcess(lambda sid, event, data: self.events.put(("voice_host", (sid, event, data))),
                              module="assistant.voice_host")
        self.voice_host = host
        self.voice_status_var.set("Подготовка голоса при запуске…")
        host.start({"speech": speech})

    def _voice_event(self, sid: str, event: str, data: Any) -> None:
        if not self.voice_host or sid != self.voice_host.id:
            return
        data = data if isinstance(data, dict) else ({"message": str(data)} if event == "error" else {})
        if event == "status":
            self.voice_status_var.set(str(data.get("message", "")))
            return
        if event == "warming":
            self._voice_ready = False
            self.voice_status_var.set("Подготовка голоса при запуске…")
            return
        if event == "ready":
            self._voice_ready = True
            self._voice_speech_ready = self._voice_speech_ready or bool(data.get("speech"))
            self.voice_status_var.set("Голос готов · модели в памяти")
            if self._pending_listen:
                self._pending_listen = False
                self._start_listening()
            return
        if event == "session_done":
            self.voice_host = None
            self._voice_ready = self._voice_speech_ready = False
            self.voice_status_var.set("Голос не готов · повторите подготовку")
            self._awaiting_speech = False
            if self.listening:
                self._stop_task()
            elif self.busy and not self._agent_running:
                self._finish_session(self.cancel_event, keep_worker=True)
            return
        if event == "error":
            if data.get("token") and data["token"] != self._voice_token:
                return
            message = str(data.get("message", "Ошибка голосового режима"))
            self._append_message("Голос", message, system=True)
            self._log_activity("Ошибка голоса", message)
            if data.get("phase") in {"initialization", "capture"} and self.listening:
                self._stop_task()
            return
        if data.get("token") != self._voice_token:
            return
        if event == "mic_level":
            self._set_mic_level(data)
        elif event == 'heard':
            self.voice_status_var.set('Распознано: ' + str(data.get('text',''))[:100])
        elif event == "user" and self.listening and not self._agent_running:
            text = str(data["text"])
            self._append_message("Вы", text)
            self._start_process("task", text)
        elif event == "armed":
            self.status_var.set("Цитадель услышана. Произнесите команду в течение 10 секунд")
        elif event == "listening" and self.listening and not self._agent_running:
            self.status_var.set("Слушаю команду…" if data.get("armed") or self._mode_key() == "always" else "Слушаю слово «цитадель»…")
        elif event == "speaking":
            self.status_var.set("Озвучиваю краткий ответ…")
        elif event == "speech_done":
            self._awaiting_speech = False
            self._notice_speech = False
            if not self.listening and not self._agent_running:
                self._finish_session(self.cancel_event, keep_worker=True)

    def _show_confirmation(self, request: dict[str, Any]) -> None:
        if self.closed.is_set() or self.cancel_event.is_set():
            return
        self._pending_confirmation = request
        window = self._confirmation_window = tk.Toplevel(self.root)
        window.title("Citadel — подтверждение действия")
        window.configure(bg=PANEL, padx=24, pady=24)
        window.geometry("580x400")
        window.minsize(460, 320)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", lambda: self._resolve_confirmation(False))
        self._label(window, "Разрешить действие?", color=TEXT, size=18, bold=True).pack(anchor="w")
        self._label(window, str(request["name"]), color=ACCENT).pack(anchor="w", pady=(12, 10))
        tk.Label(window, text=request.get("reason", "Подтвердите действие, которое может изменить данные."),
                 wraplength=510, justify="left", anchor="w", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10)).pack(fill="x", pady=(0, 12))
        detail_frame = tk.Frame(window, bg=BG)
        detail_frame.pack(fill="both", expand=True)
        details = tk.Text(detail_frame, bg=BG, fg=TEXT, wrap="word", relief="flat", font=("Segoe UI", 10), padx=12, pady=12, height=6)
        scroll = ttk.Scrollbar(detail_frame, command=details.yview)
        scroll.pack(side="right", fill="y")
        details.configure(yscrollcommand=scroll.set)
        details.insert("1.0", self._format_detail(request["arguments"]))
        details.configure(state="disabled")
        details.pack(fill="both", expand=True)
        buttons = tk.Frame(window, bg=PANEL)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Отклонить", command=lambda: self._resolve_confirmation(False)).pack(side="left")
        ttk.Button(buttons, text="Разрешить", style="Accent.TButton", command=lambda: self._resolve_confirmation(True)).pack(side="right")
        # No modal wait/grab: the main Stop button stays accessible.
        window.bind("<Escape>", lambda _event: self._resolve_confirmation(False))
        self.status_var.set("Ожидаю разрешения на действие")
        self._log_activity("Нужно подтверждение", str(request["name"]))

    def _resolve_confirmation(self, answer: bool) -> None:
        request = self._pending_confirmation
        self._pending_confirmation = None
        if self._confirmation_window is not None:
            self._confirmation_window.destroy()
            self._confirmation_window = None
        if request is not None:
            allowed = answer and not self.cancel_event.is_set()
            if self.worker:
                self.worker.send({"command": "confirm", "allowed": allowed})
            self._log_activity("Разрешено" if allowed else "Отклонено", str(request["name"]))

    def _clear_dialog(self) -> None:
        if self.busy:
            return
        self.messages = self._initial_messages()
        if self.history:
            self._chat_id = self.history.create()
            self._turn_id = None
        for widget in (self.transcript, self.activity):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.status_var.set("Готов к новой задаче")
        self._welcome()
        self.entry.focus_set()

    def _checkpoint_interruption(self) -> None:
        if self.history and self._chat_id and self._turn_id:
            import sqlite3
            try:
                self.history.interrupt(self._chat_id, self._turn_id)
            except (OSError,sqlite3.Error):
                import logging
                logging.getLogger(__name__).exception('Не удалось отметить остановку в истории; процесс всё равно будет остановлен')

    def _restore_dialog(self, chat: dict) -> None:
        self._chat_id, self._turn_id = chat['id'], chat['turn_id']
        if chat['status'] == 'running':
            self._checkpoint_interruption()
            chat['status'] = 'interrupted'
        restored = self.history.repair_transcript(chat['messages'])
        self.messages = self._initial_messages() + [m for m in restored if m.get('role') != 'system']
        for message in self.messages[-30:]:
            if message.get('role') in {'user', 'assistant'} and message.get('content') and not message.get('tool_calls'):
                self._append_message('Вы' if message['role'] == 'user' else 'Citadel', str(message['content'])[:12000])
        if chat['status'] in {'interrupted', 'blocked', 'partial'}:
            self._append_message('Сохранённая задача', 'Выполнение было прервано. Проверьте результат последнего действия; отправьте «продолжи», чтобы продолжить.', system=True)
        elif chat['status'] in {'waiting_input', 'waiting_auth'}:
            self.status_var.set('Жду уточнение или завершение входа на сайт')

    def _uninstall(self) -> None:
        if self.busy or self.closed.is_set():
            return
        if not messagebox.askyesno("Удаление Цитадели", "Помощник закроется и запустит штатное удаление.\n\nВ деинсталляторе можно сохранить настройки, историю и файлы.\nОбщие Python, Git и Ollama останутся.\n\nПродолжить?", parent=self.root, default="no"):
            return
        try:
            begin_uninstall()
        except Exception as error:
            messagebox.showerror("Не удалось запустить удаление", str(error), parent=self.root)
            return
        self.close()

    def _append_message(self, speaker: str, text: str, *, system: bool = False) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{speaker}\n", "speaker")
        self.transcript.insert("end", f"{text}\n\n", "system" if system else "body")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    @staticmethod
    def _format_detail(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)

    def _log_activity(self, title: str, detail: str = "") -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", title + "\n", "title")
        if detail:
            self.activity.insert("end", detail[:2500] + ("\n…" if len(detail) > 2500 else "") + "\n", "detail")
        if int(self.activity.index("end-1c").split(".")[0]) > 1500:
            self.activity.delete("1.0", "300.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def _agent_progress(self, event: dict[str, Any]) -> None:
        kind, name, step = event.get("type"), str(event.get("name") or "Инструмент"), event.get("step")
        prefix = f"{step} · " if step is not None else ""
        if kind == "tool_start":
            self._log_activity(prefix + name, self._format_detail(event.get("arguments", {})))
            if not self.cancel_event.is_set():
                self.status_var.set(f"Выполняю: {name}")
        elif kind == "tool_result":
            self._log_activity(prefix + name + " · результат", self._format_detail(event.get("result", "")))
        elif kind == "thinking":
            self._log_activity(prefix + "Обработка задачи")
        elif kind == "done":
            self._log_activity("Цепочка действий завершена")

    def _set_status(self, text: str) -> None:
        self.events.put(("status", text))

    def _finish_session(self, cancel: threading.Event, *, keep_worker: bool = False) -> None:
        if cancel is not self.cancel_event:
            return
        self.busy, self.listening = False, False
        self._agent_running = False
        self._set_mic_level({"active": False})
        self._session_id = None
        if not keep_worker:
            self.worker = None
        self.stop_event = None
        self._resolve_confirmation(False)
        self.entry.configure(state="normal")
        self.send_button.configure(state="normal")
        self.new_button.configure(state="normal")
        self.uninstall_button.configure(state="normal" if installed_uninstaller() else "disabled")
        self.listen_button.configure(text="Включить микрофон", state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Задача остановлена" if cancel.is_set() else
                            "Нужна проверка настроек" if self._session_error else "Готов к работе")
        self.entry.focus_set()

    def _set_mic_level(self, payload: dict[str, Any]) -> None:
        active = bool(payload.get("active")) and self.listening
        level = max(0.0, min(1.0, float(payload.get("level", 0)))) if active else 0.0
        for index, segment in enumerate(self._mic_segments):
            color = ACCENT if index < 13 else "#d8bf79" if index < 17 else "#e68f87"
            self.mic_meter.itemconfigure(segment, fill=color if active and level * 20 > index else "#2b313b")
        if not active:
            text = "Микрофон на паузе" if self.listening else "Микрофон выключен"
        elif payload.get("clipping"):
            text = "Перегрузка · говорите тише"
        else:
            text = ("Есть звук" if level > .12 else "Тишина · слушаю") + f" · {payload.get('dbfs', -96):.0f} дБ"
        self.mic_level_var.set(text)

    def _drain_events(self) -> None:
        if self.closed.is_set():
            return
        if self._pending_confirmation and self.cancel_event.is_set():
            self._resolve_confirmation(False)
        # Bound each batch so Stop remains responsive under heavy tool traffic.
        for _ in range(100):
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == 'notice':
                self._pending_notices.append(payload)
                continue
            if event == 'suggestion':
                if self.suggestions_enabled.is_set() and not self.busy:
                    self._append_message('Citadel · подсказка',str(payload),system=True)
                    self.messages.append({'role':'assistant','content':str(payload)})
                    if self.speak_var.get() and self._voice_ready and self._voice_speech_ready and self.voice_host:
                        self._notice_speech = self._awaiting_speech = True
                        self.stop_button.configure(state='normal')
                        self.voice_host.send({'command':'speak','token':self._voice_token,'text':str(payload)})
                continue
            if event == 'voice_setup':
                sid, kind, data = payload
                if self._voice_setup and sid == self._voice_setup.id and kind in {'status','ready','error'}:
                    data = data if isinstance(data,dict) else {'message':str(data)}
                    self.voice_setup_var.set(data.get('message',''))
                    self._log_activity('Подготовка распознавания',data.get('message',''))
                continue
            if event == "voice_host":
                self._voice_event(*payload)
                continue
            if event == "worker":
                sid, event, payload = payload
                if sid != self._session_id:
                    continue
                if event == "messages":
                    self.messages = payload
                    continue
                if event == "task_done":
                    self._agent_running = False
                    if self.listening:
                        if not self._awaiting_speech and self.voice_host:
                            self.voice_host.send({"command": "resume", "token": self._voice_token})
                    elif not self._awaiting_speech:
                        self._finish_session(self.cancel_event, keep_worker=True)
                    continue
                if event == "session_done":
                    payload = self.cancel_event
            if event == "user":
                self._append_message("Вы", str(payload))
            elif event == "assistant":
                answer = payload.get('text','') if isinstance(payload,dict) else str(payload)
                spoken = payload.get('speech') if isinstance(payload,dict) else None
                self._append_message("Citadel", answer)
                if self.speak_var.get() and self._voice_ready and self._voice_speech_ready and self.voice_host:
                    self._awaiting_speech = True
                    self.voice_host.send({"command": "speak", "token": self._voice_token, "text": spoken['text'] if isinstance(spoken,dict) else concise_speech(answer)})
                elif self.speak_var.get():
                    self._log_activity("Голос готовится при запуске", "Текст ответа уже доступен. Микрофон включается после подготовки локальных моделей.")
            elif event == "status":
                if not (self.busy and self.cancel_event.is_set()):
                    self.status_var.set(str(payload))
            elif event == "error":
                self._session_error = True
                self._append_message("Ошибка", str(payload), system=True)
                self._log_activity("Ошибка", str(payload))
                self.status_var.set("Нужна проверка настроек")
            elif event == "hotkey":
                self._toggle_listening()
            elif event == "agent":
                self._agent_progress(payload)
            elif event == "mic_level":
                self._set_mic_level(payload)
            elif event == "confirm":
                self._show_confirmation(payload)
            elif event == "session_done":
                self._finish_session(payload)
        self._deliver_notice()
        self._poll_id = self.root.after(60, self._drain_events)

    def _deliver_notice(self) -> None:
        if self._agent_running or self._awaiting_speech or self._pending_confirmation or (self.busy and not self.listening):
            return
        if not self.speak_var.get():
            self._pending_notice_audio.clear()
        if self._pending_notice_audio and self._voice_ready and self._voice_speech_ready and self.voice_host:
            self._notice_speech = self._awaiting_speech = True
            self.stop_button.configure(state='normal')
            self.voice_host.send({'command':'speak','token':self._voice_token,'text':self._pending_notice_audio.pop(0),'chime':True})
            return
        if not self._pending_notices:
            return
        item = self._pending_notices.pop(0)
        try:
            if not PersonalStore(self._personal_path).current_delivery(item):
                return
        except Exception as error:
            self._log_activity('Ошибка напоминания', str(error))
            return
        text = item['message']
        self._append_message('Напоминание', text)
        self.messages.append({'role': 'assistant', 'content': f"Уведомление ID {item['id']}: {text}"})
        self._log_activity('Фоновое уведомление', text)
        # Never steal focus or open a modal dialog over the current application.
        if self._notice_toast and self._notice_toast.winfo_exists():
            self._notice_toast.destroy()
        toast = self._notice_toast = tk.Toplevel(self.root)
        toast.title('Цитадель · напоминание')
        toast.configure(bg=PANEL, padx=18, pady=14)
        toast.attributes('-topmost', True)
        tk.Label(toast, text=text[:700], bg=PANEL, fg=TEXT, wraplength=360, justify='left').pack(fill='x')
        row = ttk.Frame(toast)
        row.pack(fill='x', pady=(12, 0))
        if item['kind'] == 'reminder':
            def later():
                PersonalStore(self._personal_path).snooze(item['id'], 600)
                toast.destroy()
            ttk.Button(row, text='Через 10 минут', command=later).pack(side='left')
        ttk.Button(row, text='Закрыть', command=toast.destroy).pack(side='right')
        toast.update_idletasks()
        toast.geometry(f'+{max(0, toast.winfo_screenwidth()-410)}+{max(0, toast.winfo_screenheight()-240)}')
        toast.after(15000, toast.destroy)
        PersonalStore(self._personal_path).acknowledge(item)
        if self.speak_var.get() and self._voice_ready and self._voice_speech_ready and self.voice_host:
            self._notice_speech = self._awaiting_speech = True
            self.stop_button.configure(state='normal')
            self.voice_host.send({'command': 'speak', 'token': self._voice_token, 'text': concise_speech(text),'chime':True})
        elif self.speak_var.get():
            self._pending_notice_audio.append(concise_speech(text))
        elif self.listening and self.voice_host and self._voice_ready:
            self._notice_speech = self._awaiting_speech = True
            self.stop_button.configure(state='normal')
            self.voice_host.send({'command':'speak','token':self._voice_token,'text':'','chime':True})
        else:
            from .chime import play_chime
            play_chime()

    def _show_personal(self) -> None:
        window = tk.Toplevel(self.root)
        window.title('Цитадель · дела и память')
        window.geometry('800x600')
        notebook = ttk.Notebook(window)
        notebook.pack(fill='both', expand=True, padx=16, pady=16)
        reminders = ttk.Frame(notebook, padding=12)
        memories = ttk.Frame(notebook, padding=12)
        notebook.add(reminders, text='Напоминания и наблюдения')
        notebook.add(memories, text='Постоянная память')
        suggestions = tk.BooleanVar(value=self.suggestions_enabled.is_set())
        def change_suggestions():
            if suggestions.get(): self.suggestions_enabled.set()
            else: self.suggestions_enabled.clear()
            if self.history: self.history.set_setting('suggestions',suggestions.get())
        ttk.Checkbutton(reminders,text='Ненавязчивые подсказки: перерыв и мало места на диске',variable=suggestions,command=change_suggestions).pack(anchor='w')
        ttk.Label(reminders,text='Без опроса модели. Подсказки молчат ночью, при бездействии, во время задачи и полноэкранного приложения.',wraplength=650).pack(anchor='w',pady=(0,8))
        from .procedures import Procedures
        procedures = Procedures(self.memory.path.with_name('procedures.sqlite3'))
        learned = ttk.Frame(notebook, padding=12)
        notebook.add(learned, text='Опыт')
        ttk.Label(learned,text='Успешные способы работы помогают выбрать инструменты.\nЭто подсказки: разрешения и результаты проверяются заново.',wraplength=650).pack(anchor='w',pady=8)
        routes = ttk.Treeview(learned,columns=('words','route'),show='headings')
        routes.heading('words',text='Похожая задача')
        routes.heading('route',text='Порядок инструментов')
        routes.pack(fill='both',expand=True)
        route_items = {}
        def refresh_routes():
            route_items.clear(); routes.delete(*routes.get_children())
            for index, item in enumerate(procedures.list()):
                route_items[str(index)] = item['signature']
                routes.insert('','end',iid=str(index),values=(', '.join(item['words']),' → '.join(item['tools'])))
        def forget_route():
            for selected_route in routes.selection(): procedures.forget(route_items[selected_route])
            refresh_routes()
        ttk.Button(learned,text='Забыть выбранный способ',command=forget_route).pack(anchor='w',pady=8)
        ttk.Button(learned,text='Обновить',command=refresh_routes).pack(anchor='w')
        refresh_routes()
        if self.history:
            history_tab = ttk.Frame(notebook, padding=12)
            notebook.add(history_tab, text='История задач')
            chats = ttk.Treeview(history_tab, columns=('status', 'title'), show='headings', height=7)
            chats.heading('status', text='Состояние')
            chats.heading('title', text='Задача')
            chats.column('status', width=125, stretch=False)
            chats.pack(fill='both', expand=True)
            detail = tk.Text(history_tab, height=8, wrap='word', bg=PANEL, fg=TEXT, relief='flat')
            detail.pack(fill='both', expand=True, pady=8)
            statuses = {'running':'Выполняется','complete':'Готово','idle':'Новая','interrupted':'Прервана','cancelled':'Остановлена','blocked':'Нужна помощь','partial':'Частично','waiting_input':'Уточнение','waiting_auth':'Нужен вход'}
            def refresh_history():
                chats.delete(*chats.get_children())
                for chat in self.history.recent():
                    chats.insert('', 'end', iid=chat['id'], values=(statuses.get(chat['status'],chat['status']),chat['title']))
            def show_receipts(_event=None):
                if not chats.selection(): return
                chat = self.history.load(chats.selection()[0])
                lines = ['Задача: '+chat['goal']]
                if chat['plan']: lines.extend(['План:', *chat['plan']])
                for receipt in reversed(self.history.receipts(chat['id'])):
                    lines.append(('✓ ' if receipt['ok'] else '× ')+receipt['tool']+' '+receipt['details'])
                detail.configure(state='normal')
                detail.delete('1.0','end')
                detail.insert('1.0','\n'.join(lines))
                detail.configure(state='disabled')
            def open_history():
                if self.busy or not chats.selection(): return
                chat = self.history.activate(chats.selection()[0])
                self.transcript.configure(state='normal')
                self.transcript.delete('1.0','end')
                self.transcript.configure(state='disabled')
                self._restore_dialog(chat)
                window.destroy()
            def forget_history():
                if self.busy or not chats.selection(): return
                identifier = chats.selection()[0]
                self.history.forget(identifier)
                if identifier == self._chat_id: self._clear_dialog()
                refresh_history()
                detail.configure(state='normal'); detail.delete('1.0','end'); detail.configure(state='disabled')
            chats.bind('<<TreeviewSelect>>', show_receipts)
            buttons = ttk.Frame(history_tab)
            buttons.pack(fill='x')
            ttk.Button(buttons,text='Открыть диалог',command=open_history).pack(side='left')
            ttk.Button(buttons,text='Забыть выбранное',command=forget_history).pack(side='left',padx=8)
            ttk.Button(buttons,text='Обновить',command=refresh_history).pack(side='right')
            ttk.Label(history_tab,text='История хранится локально. Открытие диалога само не запускает действия.',wraplength=650).pack(anchor='w',pady=8)
            refresh_history()
        voice_settings = ttk.Frame(notebook,padding=16)
        notebook.add(voice_settings,text='Голос')
        ttk.Label(voice_settings,text='Распознавание речи',style='Title.TLabel').pack(anchor='w')
        ttk.Label(voice_settings,text='Base — быстрее. Small — точнее, но требует больше памяти и времени.\nМодель скачивается только при нажатии кнопки подготовки.',wraplength=650).pack(anchor='w',pady=12)
        selected = tk.StringVar(value='small')
        ttk.Combobox(voice_settings,textvariable=selected,values=('base','small'),state='readonly').pack(anchor='w')
        def prepare():
            if self._voice_setup and not self._voice_setup.stopped.is_set() and (self._voice_setup.process is None or self._voice_setup.process.poll() is None):
                self.voice_setup_var.set('Подготовка уже выполняется. Дождитесь завершения или отмените её.')
                return
            self._voice_setup = SessionProcess(lambda sid,event,data:self.events.put(('voice_setup',(sid,event,data))),module='assistant.voice_setup')
            self.voice_setup_var.set('Начинаю подготовку модели…')
            self._voice_setup.start({'model':selected.get()})
        def cancel_preparation():
            if self._voice_setup:
                self._voice_setup.stop()
                self._voice_setup = None
                self.voice_setup_var.set('Подготовка остановлена. Если модель уже была проверена и сохранена, выбор применится после перезапуска.')
        ttk.Button(voice_settings,text='Скачать и проверить выбранную модель',command=prepare).pack(anchor='w',pady=(12,4))
        ttk.Button(voice_settings,text='Остановить подготовку',command=cancel_preparation).pack(anchor='w')
        ttk.Label(voice_settings,textvariable=self.voice_setup_var,wraplength=650).pack(anchor='w',pady=12)
        ttk.Label(voice_settings,text='По умолчанию: до 120 секунд, пауза до 1,2 секунды.\nДля особенно длинной диктовки можно увеличить voice_silence_seconds в настройках.',wraplength=650).pack(anchor='w')
        ttk.Label(voice_settings,text='Голос: «Джарвис» — более низкий eugene; обычный — aidar.\nЭто локальные голоса Silero, без клонирования актёра.',wraplength=650).pack(anchor='w',pady=(12,4))
        profile = tk.StringVar(value='jarvis')
        ttk.Combobox(voice_settings,textvariable=profile,values=('jarvis','neutral'),state='readonly').pack(anchor='w')
        def prepare_voice():
            if self._voice_setup and not self._voice_setup.stopped.is_set() and (self._voice_setup.process is None or self._voice_setup.process.poll() is None):
                self.voice_setup_var.set('Дождитесь текущей подготовки или остановите её.')
                return
            self._voice_setup = SessionProcess(lambda sid,event,data:self.events.put(('voice_setup',(sid,event,data))),module='assistant.voice_setup')
            self._voice_setup.start({'profile':profile.get()})
        ttk.Button(voice_settings,text='Подготовить выбранный голос',command=prepare_voice).pack(anchor='w',pady=6)
        model_settings = ttk.Frame(notebook,padding=16)
        notebook.add(model_settings,text='Модель агента')
        ttk.Label(model_settings,text='Модель определяет качество рассуждений',style='Title.TLabel').pack(anchor='w')
        ttk.Label(model_settings,text='Маленькая модель может ошибаться в составных поручениях.\nБолее крупная требует больше RAM/VRAM и может отвечать медленнее.\nЗагрузка начнётся только после нажатия кнопки.',wraplength=700).pack(anchor='w',pady=14)
        agent_model = tk.StringVar(value=MODEL)
        ttk.Combobox(model_settings,textvariable=agent_model,values=tuple(dict.fromkeys([MODEL,'qwen3.5:4b','qwen3.5:9b','qwen3:8b','qwen3:14b'])),width=38).pack(anchor='w')
        def prepare_agent():
            if self._voice_setup and not self._voice_setup.stopped.is_set() and (self._voice_setup.process is None or self._voice_setup.process.poll() is None):
                self.voice_setup_var.set('Дождитесь текущей подготовки или остановите её.')
                return
            self._voice_setup = SessionProcess(lambda sid,event,data:self.events.put(('voice_setup',(sid,event,data))),module='assistant.voice_setup')
            self._voice_setup.start({'agent_model':agent_model.get().strip()})
        ttk.Button(model_settings,text='Загрузить и проверить модель',command=prepare_agent).pack(anchor='w',pady=12)
        ttk.Button(model_settings,text='Остановить подготовку',command=cancel_preparation).pack(anchor='w')
        ttk.Label(model_settings,textvariable=self.voice_setup_var,wraplength=700).pack(anchor='w',pady=12)
        store = PersonalStore(self._personal_path)
        tree = ttk.Treeview(reminders, columns=('time','title'), show='headings', height=8)
        tree.heading('time', text='Когда / тип')
        tree.heading('title', text='Что напомнить')
        tree.column('time', width=160, stretch=False)
        tree.pack(fill='both', expand=True)
        mem = ttk.Treeview(memories, columns=('key','value'), show='headings')
        mem.heading('key', text='Ключ')
        mem.heading('value', text='Предпочтение / заметка')
        mem.pack(fill='both', expand=True)
        def refresh():
            tree.delete(*tree.get_children())
            for item in store.list():
                when = dt.datetime.fromtimestamp(item['due']).astimezone().strftime('%d.%m %H:%M') if item['kind']=='reminder' else 'Наблюдение: '+item['kind']
                tree.insert('', 'end', iid=item['id'], values=(when, item['title']))
            mem.delete(*mem.get_children())
            for index, (key, value) in enumerate(self.memory.get_all().items()):
                mem.insert('', 'end', iid=str(index), values=(key, value))
        def cancel():
            for key in tree.selection():
                store.cancel(key)
            refresh()
        def snooze():
            for key in tree.selection():
                store.snooze(key, 600)
            refresh()
        actions = ttk.Frame(reminders)
        actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='Отменить выбранное', command=cancel).pack(side='left')
        ttk.Button(actions, text='Отложить на 10 минут', command=snooze).pack(side='left', padx=8)
        ttk.Button(actions, text='Обновить', command=refresh).pack(side='right')
        form = ttk.Frame(reminders)
        form.pack(fill='x')
        title = ttk.Entry(form)
        title.pack(side='left', fill='x', expand=True)
        delay = ttk.Spinbox(form, from_=1, to=1440, width=5)
        delay.set('10')
        delay.pack(side='left', padx=6)
        ttk.Label(form, text='мин').pack(side='left')
        def add():
            import time
            try:
                minutes = float(delay.get())
                if not 1 <= minutes <= 1440:
                    raise ValueError('Укажите от 1 до 1440 минут')
                store.add('reminder', title.get(), time.time()+minutes*60)
                title.delete(0,'end')
                refresh()
            except ValueError as error:
                messagebox.showerror('Напоминание', str(error), parent=window)
        ttk.Button(form, text='Добавить', command=add).pack(side='left', padx=6)
        ttk.Label(reminders, text='Работает пока Цитадель открыта. Пропущенные напоминания появятся после запуска.', wraplength=650).pack(anchor='w', pady=(8,0))
        def forget():
            for selection in mem.selection():
                self.memory.delete(mem.item(selection, 'values')[0])
            refresh()
        ttk.Button(memories, text='Забыть выбранное', command=forget).pack(anchor='w', pady=8)
        refresh()

    def close(self) -> None:
        self.closed.set()
        self._checkpoint_interruption()
        if self._voice_setup:
            self._voice_setup.stop()
        if self.scheduler:
            self.scheduler.stop()
        if self.proactive:
            self.proactive.stop()
        self.cancel_event.set()
        self._session_id = None
        if self.voice_host:
            self.voice_host.stop()
            self.voice_host = None
        if self.worker:
            self.worker.stop()
            self.worker = None
        if self.stop_event:
            self.stop_event.set()
        self._resolve_confirmation(False)
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        self.root.after_cancel(self._poll_id)
        self.root.destroy()


def main() -> None:
    from .launcher import main as launch
    raise SystemExit(launch())


if __name__ == "__main__":
    main()
