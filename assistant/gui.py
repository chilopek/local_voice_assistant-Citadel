from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .app import SYSTEM_PROMPT, run_turn
from .config import MODEL, OLLAMA_URL, THINK, VOICE_ENABLED, WORKSPACE
from .memory import Memory
from .ollama_client import OllamaClient, OllamaError
from .voice import VoiceError, VoiceSession


WAKE_WORD = "цитадель"
HOTKEY = "Ctrl+Alt+C"
MODE_LABELS = {
    "wake": f"Ключевое слово «{WAKE_WORD}»",
    "always": "Всегда слушать",
    "push": "Push-to-talk",
}


class AssistantGUI:
    def __init__(self, root: tk.Tk, *, enable_hotkey: bool = True) -> None:
        self.root = root
        self.root.title("Цитадель — локальный помощник")
        self.root.geometry("900x650")
        self.root.minsize(720, 520)
        self.root.configure(bg="#0f172a")

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.client = OllamaClient(OLLAMA_URL, MODEL)
        self.memory = Memory(Path(__file__).resolve().parents[1] / "data" / "memory.sqlite3")
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n" + self.memory.prompt_context()}
        ]
        self.voice = VoiceSession()
        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.hotkey_listener: Any | None = None
        self.mode = "wake" if VOICE_ENABLED else "push"
        self.busy = False

        self.mode_var = tk.StringVar(value=MODE_LABELS[self.mode])
        self.status_var = tk.StringVar(value="Готов к работе")
        self.speak_var = tk.BooleanVar(value=VOICE_ENABLED)

        self._build_style()
        self._build_ui()
        if enable_hotkey:
            self._start_hotkey()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain_events)
        self._append_message("Цитадель", "Готова. Выберите режим и нажмите Ctrl+Alt+C или кнопку микрофона.")

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0f172a")
        style.configure("TLabel", background="#0f172a", foreground="#e2e8f0", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground="#f8fafc")
        style.configure("Muted.TLabel", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 10))
        style.configure("Accent.TButton", background="#2563eb", foreground="white")
        style.map("Accent.TButton", background=[("active", "#1d4ed8")])
        style.configure("TCombobox", padding=5)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Цитадель", style="Title.TLabel").pack(side="left")
        info = f"Ollama: {MODEL}  •  {'thinking on' if THINK else 'fast mode'}"
        ttk.Label(header, text=info, style="Muted.TLabel").pack(side="right", pady=8)

        self.transcript = tk.Text(
            outer,
            wrap="word",
            state="disabled",
            bg="#111827",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            padx=16,
            pady=14,
            font=("Segoe UI", 11),
        )
        self.transcript.pack(fill="both", expand=True)
        self.transcript.tag_configure("speaker", foreground="#60a5fa", font=("Segoe UI Semibold", 10))
        self.transcript.tag_configure("body", foreground="#e5e7eb", spacing3=8)
        self.transcript.tag_configure("system", foreground="#94a3b8", spacing3=8)

        controls = ttk.Frame(outer, padding=(0, 14, 0, 0))
        controls.pack(fill="x")
        self.entry = ttk.Entry(controls)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry.bind("<Return>", lambda _event: self._send_text())
        ttk.Button(controls, text="Отправить", style="Accent.TButton", command=self._send_text).pack(side="left")

        options = ttk.Frame(outer, padding=(0, 10, 0, 0))
        options.pack(fill="x")
        ttk.Label(options, text="Режим:").pack(side="left")
        self.mode_box = ttk.Combobox(
            options,
            textvariable=self.mode_var,
            values=list(MODE_LABELS.values()),
            state="readonly",
            width=28,
        )
        self.mode_box.pack(side="left", padx=(8, 14))
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_changed)
        self.listen_button = ttk.Button(options, text="Запустить микрофон", command=self._toggle_listening)
        self.listen_button.pack(side="left")
        ttk.Checkbutton(options, text="Озвучивать ответы", variable=self.speak_var).pack(side="left", padx=14)
        ttk.Button(options, text="Очистить диалог", command=self._clear_dialog).pack(side="right")

        footer = ttk.Frame(outer, padding=(0, 12, 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, text=f"Горячая клавиша: {HOTKEY}  •  Рабочая папка: {WORKSPACE}", style="Muted.TLabel").pack(side="right")

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard

            self.hotkey_listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+c": self._hotkey_pressed})
            self.hotkey_listener.start()
        except Exception as error:
            self._set_status(f"Горячая клавиша недоступна: {error}")

    def _hotkey_pressed(self) -> None:
        self.root.after(0, self._toggle_listening)

    def _mode_key(self) -> str:
        selected = self.mode_var.get()
        return next((key for key, label in MODE_LABELS.items() if label == selected), "wake")

    def _mode_changed(self, _event: Any = None) -> None:
        if self.worker and self.worker.is_alive():
            self._stop_listening()
        self._set_status(f"Выбран режим: {self.mode_var.get()}")

    def _toggle_listening(self) -> None:
        if self.worker and self.worker.is_alive():
            self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self) -> None:
        if self.busy:
            self._set_status("Подождите, текущий запрос ещё выполняется")
            return
        self.stop_event = threading.Event()
        mode = self._mode_key()
        target = {
            "wake": self._wake_loop,
            "always": self._always_loop,
            "push": self._push_once,
        }[mode]
        self.worker = threading.Thread(target=target, args=(self.stop_event,), daemon=True)
        self.worker.start()
        self.listen_button.configure(text="Остановить микрофон")
        self._set_status(
            f"Слушаю: {WAKE_WORD}" if mode == "wake" else "Слушаю..."
        )

    def _stop_listening(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.listen_button.configure(text="Запустить микрофон")
        self._set_status("Останавливаю микрофон...")

    def _push_once(self, stop_event: threading.Event) -> None:
        try:
            text = self.voice.capture_until_stop(stop_event)
            if text:
                self._process_voice_text(text, "push")
        except VoiceError as error:
            self.events.put(("error", str(error)))
        finally:
            self.events.put(("listening_done", None))

    def _wake_loop(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                text = self.voice.capture_utterance(stop_event)
                if not text:
                    continue
                normalized = text.lower().replace("ё", "е")
                if not re.search(rf"\b{re.escape(WAKE_WORD)}\b", normalized):
                    continue
                command = re.sub(rf"\b{re.escape(WAKE_WORD)}\b[:,.!]?\s*", "", text, count=1, flags=re.IGNORECASE).strip()
                if command:
                    self._process_voice_text(command, "wake")
                else:
                    self.events.put(("status", "Ключевое слово услышано, слушаю команду"))
                    self.voice.speak("Слушаю.")
        except VoiceError as error:
            self.events.put(("error", str(error)))
        finally:
            self.events.put(("listening_done", None))

    def _always_loop(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                text = self.voice.capture_utterance(stop_event)
                if text:
                    self._process_voice_text(text, "always")
        except VoiceError as error:
            self.events.put(("error", str(error)))
        finally:
            self.events.put(("listening_done", None))

    def _process_voice_text(self, text: str, _mode: str) -> None:
        self.events.put(("user", text))
        self._answer_worker(text)

    def _send_text(self) -> None:
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self.events.put(("user", text))
        threading.Thread(target=self._answer_worker, args=(text,), daemon=True).start()

    def _answer_worker(self, text: str) -> None:
        self.busy = True
        self.events.put(("busy", True))
        try:
            answer = run_turn(self.client, self.memory, self.messages, text, confirm=self._confirm_action)
            self.events.put(("assistant", answer))
            if self.speak_var.get():
                self.voice.speak(answer)
        except VoiceError as error:
            self.events.put(("error", f"Голосовой вывод: {error}"))
        except OllamaError as error:
            self.events.put(("error", f"Ollama: {error}"))
        except Exception as error:
            self.events.put(("error", str(error)))
        finally:
            self.busy = False
            self.events.put(("busy", False))

    def _confirm_action(self, name: str, arguments: dict[str, Any]) -> bool:
        answer: list[bool] = [False]
        done = threading.Event()

        def show_dialog() -> None:
            answer[0] = messagebox.askyesno(
                "Требуется подтверждение",
                f"Выполнить действие?\n\n{name}\n{arguments}",
                parent=self.root,
            )
            done.set()

        self.root.after(0, show_dialog)
        done.wait()
        return answer[0]

    def _clear_dialog(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + self.memory.prompt_context()}]
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._append_message("Система", "Контекст диалога очищен.", system=True)

    def _append_message(self, speaker: str, text: str, *, system: bool = False) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{speaker}\n", "speaker")
        self.transcript.insert("end", f"{text}\n\n", "system" if system else "body")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.events.put(("status", text))

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "user":
                self._append_message("Вы", str(payload))
            elif event == "assistant":
                self._append_message("Цитадель", str(payload))
            elif event == "status":
                self.status_var.set(str(payload))
            elif event == "error":
                self._append_message("Ошибка", str(payload), system=True)
                self.status_var.set("Нужна проверка настроек")
            elif event == "busy":
                self.entry.configure(state="disabled" if payload else "normal")
            elif event == "listening_done":
                self.listen_button.configure(text="Запустить микрофон")
                if not self.stop_event or not self.stop_event.is_set():
                    self.status_var.set("Готов к работе")
        self.root.after(100, self._drain_events)

    def close(self) -> None:
        self._stop_listening()
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        self.root.destroy()


def main() -> None:
    from .launcher import main as launch

    raise SystemExit(launch())


if __name__ == "__main__":
    main()
