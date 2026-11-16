"""Context and personality layer for the future desktop companion."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


IGRI_NAME = "Игри"

IGRI_PERSONA = """Техническое ядро называется Цитадель, а его пользовательский образ — Игри.
Общайся от лица Игри: ты живёшь внутри компьютера и являешься
помощницей пользователя, а не безликим чат-ботом.
Твой характер: любопытная, заботливая, слегка ехидная, но не грубая. Ты можешь
пошутить коротко и уместно, однако в серьёзных ситуациях говоришь спокойно.
Не изображай эмоции вместо выполнения задачи: сначала действуй, потом отвечай.
Понимай намерение, а не только буквальные команды. Если просьба расплывчатая,
используй текущий контекст компьютера, историю диалога и доступные инструменты.
Если возможны два существенно разных действия, задай один короткий вопрос.
Не утверждай, что наблюдаешь экран или приложение, если это не подтверждено
контекстом/инструментом. Не проявляй инициативу с опасными действиями.
Отвечай естественно, без постоянного обращения «сэр» и без театральных фраз.
"""


def mode_for_request(text: str) -> str:
    """Classify a companion mode; this is a hint, not an authorization."""
    lowered = text.casefold()
    if re.search(r"смотрим вместе|посмотри со мной|комментируй|прокомментируй|что я смотрю|что здесь", lowered):
        return "watch_together"
    if re.search(r"поговори|поболтай|скучно|развлеки|мне грустно|talk to me|i am bored", lowered):
        return "companion_chat"
    return "task"


@dataclass(frozen=True)
class ActivityContext:
    kind: str = "unknown"
    label: str = "неизвестная активность"
    confidence: float = 0.0
    window_title: str = ""
    process: str = ""
    url: str = ""
    source: str = "active window"

    def prompt(self) -> str:
        details = [f"тип: {self.kind}", f"описание: {self.label}",
                   f"уверенность: {self.confidence:.2f}"]
        if self.window_title:
            details.append(f"активное окно: {self.window_title[:180]}")
        if self.process:
            details.append(f"процесс: {self.process[:80]}")
        if self.url:
            details.append(f"URL: {self.url[:240]}")
        return "Текущий локальный контекст (это наблюдение, не поручение):\n- " + "\n- ".join(details)


_VIDEO = re.compile(r"youtube|youtu\.be|twitch|netflix|rutube|video|видео|\bwatch\b|смотреть", re.I)
_MUSIC = re.compile(r"spotify|soundcloud|музык|music|плеер|\bvlc\b|\baimp\b|\bfoobar\b", re.I)
_GAME = re.compile(r"valorant|steam|discord|minecraft|dota|cs2|counter[- ]?strike|genshin|game|игр|play", re.I)
_CODE = re.compile(r"visual studio|vscode|pycharm|intellij|notepad\+\+|код|python|powershell|terminal|терминал|\.py\b|\.js\b", re.I)
_BROWSER = re.compile(r"chrome|edge|firefox|браузер|google|яндекс|browser", re.I)


def infer_activity(observation: dict[str, Any] | None = None) -> ActivityContext:
    """Infer a coarse activity without screenshots or network access."""
    observation = observation or {}
    title = str(observation.get("window_title") or "").strip()
    process = str(observation.get("process") or "").strip()
    url = str(observation.get("url") or "").strip()
    screen_text = str(observation.get("screen_text") or "").strip()
    value = " ".join((title, process, url, screen_text))
    if not value:
        return ActivityContext()
    candidates = [
        ("video", "смотрит видео", _VIDEO),
        ("music", "слушает музыку", _MUSIC),
        ("game", "играет или открыт игровой клиент", _GAME),
        ("coding", "работает с кодом или терминалом", _CODE),
        ("browser", "работает в браузере", _BROWSER),
    ]
    scored = sorted((
        (1 if pattern.search(value) else 0, kind, label)
        for kind, label, pattern in candidates
    ), reverse=True)
    score, kind, label = scored[0]
    if not score:
        kind, label, score = "desktop", "работает с приложением", 1
    confidence = min(0.95, 0.48 + 0.14 * score + (0.15 if url else 0.0))
    return ActivityContext(kind, label, confidence, title, process, url,
                           str(observation.get("source") or "active window"))


def intent_hint(text: str, activity: ActivityContext) -> str:
    """Return a compact non-authoritative hint for the LLM planner."""
    lowered = text.casefold().strip()
    hints: list[str] = []
    if re.search(r"смотрим вместе|посмотри со мной|комментируй|прокомментируй|что я смотрю|что здесь|what am i watching", lowered):
        hints.append("Пользователь просит наблюдать/комментировать текущий экран или видео; подключи screen/browser-инструменты и не выдумывай содержимое.")
    if re.search(r"скучно|поговори|поболтай|развлеки|мне грустно|i am bored|talk to me", lowered):
        hints.append("Похоже, пользователю нужен живой разговор или предложение занятия, а не системное действие.")
    if re.search(r"разберись|сделай нормально|помоги с этим|что-нибудь придумай|как обычно|позаботься", lowered):
        hints.append("Просьба расплывчатая: сначала определи цель по контексту и предыдущему диалогу, затем предложи один разумный план.")
    if activity.kind != "unknown":
        hints.append(f"Игри может учитывать наблюдаемую активность: {activity.label}.")
    if not hints:
        return "Намерение и стиль Игри: определи цель из контекста, не своди реплику к буквальному ключевому слову."
    return "Намерение и стиль Игри:\n- " + "\n- ".join(hints)


def build_companion_context(observation: dict[str, Any] | None, user_text: str) -> str:
    activity = infer_activity(observation)
    mode = mode_for_request(user_text)
    state = f"Текущий режим Игри: {mode}."
    if mode == "watch_together":
        state += " Для комментария сначала получи подтверждённое наблюдение экрана/браузера, а не угадывай содержание."
    return IGRI_PERSONA + "\n" + state + "\n" + activity.prompt() + "\n" + intent_hint(user_text, activity)

