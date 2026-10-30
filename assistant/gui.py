from __future__ import annotations

import json
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

WAKE_WORD = "цитадель"
HOTKEY = "Ctrl+Alt+C"
MODE_LABELS = {"wake": f"После слова «{WAKE_WORD}»", "always": "Все голосовые команды"}
BG, PANEL, SIDEBAR = "#111318", "#191c23", "#14171d"
TEXT, MUTED, ACCENT = "#e8eaf0", "#929aaa", "#a9c7a5"


class AssistantGUI:
    def __init__(self, root: tk.Tk, *, enable_hotkey: bool = True, warm_voice: bool = True) -> None:
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
        self.entry.focus_set()
        if warm_voice and VOICE_ENABLED:
            self._ensure_voice()

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
        if not self.busy:
            return
        self.cancel_event.set()
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
        self._log_activity("Задача остановлена", "Выполнение и озвучка прерваны. Можно отправить новую задачу.")

    def _start_process(self, mode: str, text: str = "") -> None:
        self._agent_running = True
        payload = {"mode": mode, "text": text, "messages": self.messages, "speak": False}
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
        for widget in (self.transcript, self.activity):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.status_var.set("Готов к новой задаче")
        self._welcome()
        self.entry.focus_set()

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
                self._append_message("Citadel", str(payload))
                if self.speak_var.get() and self._voice_ready and self._voice_speech_ready and self.voice_host:
                    self._awaiting_speech = True
                    self.voice_host.send({"command": "speak", "token": self._voice_token, "text": concise_speech(str(payload))})
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
        self._poll_id = self.root.after(60, self._drain_events)

    def close(self) -> None:
        self.closed.set()
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
