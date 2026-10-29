from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from typing import Any

from .config import MAX_STEPS, MODEL, OLLAMA_URL, THINK, VOICE_ENABLED, WORKSPACE
from .memory import Memory
from .ollama_client import OllamaClient, OllamaError
from .tools import CONFIRMATION_REQUIRED, TOOL_SCHEMAS, execute, format_tool_result
from .voice import VoiceError, VoiceSession


SYSTEM_PROMPT = """Ты — локальный персональный помощник пользователя. Отвечай по-русски, кратко и по делу.
Ты можешь выполнять действия только через предоставленные инструменты.
Не выдумывай результаты инструментов. Если задача требует действия, используй инструмент.
Рабочая папка ограничена каталогом, указанным приложением.
Перед опасными действиями приложение само попросит подтверждение пользователя.
Если инструмент вернул ошибку, объясни её и предложи следующий шаг.
Не показывай внутренние рассуждения пользователю: выдавай только итоговый ответ.
"""


def _tool_call_parts(response: dict[str, Any]) -> list[dict[str, Any]]:
    message = response.get("message", {})
    calls = message.get("tool_calls") or []
    result = []
    for call in calls:
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        result.append({"name": function.get("name", ""), "arguments": arguments})
    return result


def _ask_confirmation(name: str, arguments: dict[str, Any]) -> bool:
    print("\nЗапрошено действие, которое может изменить систему:")
    print(f"  Инструмент: {name}")
    print(f"  Аргументы: {json.dumps(arguments, ensure_ascii=False)}")
    answer = input("Выполнить? [да/нет]: ").strip().lower()
    return answer in {"да", "д", "yes", "y"}


def _clean_answer(content: str) -> str:
    """Убирает reasoning-текст, который старые версии Ollama могут вернуть в content."""
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    if "<think>" in content and "</think>" not in content:
        content = content.split("<think>", 1)[0]
    return content.strip()


def run_turn(client: OllamaClient, memory: Memory, messages: list[dict[str, Any]], user_text: str) -> str:
    messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_STEPS):
        response = client.chat(messages, TOOL_SCHEMAS)
        assistant_message = response.get("message", {})
        messages.append(assistant_message)
        calls = _tool_call_parts(response)
        if not calls:
            return _clean_answer(assistant_message.get("content", "")) or "Готово."

        for call in calls:
            name = call["name"]
            arguments = call["arguments"]
            if name in CONFIRMATION_REQUIRED and not _ask_confirmation(name, arguments):
                result = {"ok": False, "cancelled": True, "message": "Пользователь отменил действие."}
            else:
                try:
                    result = {"ok": True, "result": execute(name, arguments)}
                except Exception as error:  # инструмент должен вернуть ошибку модели, а не уронить процесс
                    result = {"ok": False, "error": str(error)}
            messages.append({"role": "tool", "content": format_tool_result(result)})
    return "Я остановил цепочку: превышено максимальное число шагов."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Локальный помощник")
    parser.add_argument("--voice", action="store_true", help="запустить push-to-talk режим")
    parser.add_argument("--text", action="store_true", help="запустить текстовый режим")
    args = parser.parse_args(argv)

    voice_mode = args.voice or (VOICE_ENABLED and not args.text)
    voice = VoiceSession() if voice_mode else None
    print("Локальный помощник v0.1")
    print(f"Модель: {MODEL}")
    print(f"Thinking: {'включён' if THINK else 'выключен для скорости'}")
    print(f"Рабочая папка: {WORKSPACE}")
    print("Режим: голосовой push-to-talk" if voice_mode else "Режим: текстовый")
    print("Команды: /help, /voice, /text, /clear, /exit\n")

    client = OllamaClient(OLLAMA_URL, MODEL)
    memory = Memory(Path(__file__).resolve().parents[1] / "data" / "memory.sqlite3")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + memory.prompt_context()}
    ]

    while True:
        try:
            if voice_mode:
                if voice is None:
                    voice = VoiceSession()
                user_text = voice.record()
                if not user_text:
                    continue
                print(f"Ты: {user_text}")
            else:
                user_text = input("Ты: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи.")
            return
        except VoiceError as error:
            print(f"Голосовой режим недоступен: {error}")
            print("Переключаюсь в текстовый режим.\n")
            voice_mode = False
            continue
        if not user_text:
            continue
        if user_text in {"/exit", "/quit", "выход"}:
            print("До встречи.")
            return
        if user_text in {"/voice", "голосовой режим", "включи голос"}:
            voice_mode = True
            voice = voice or VoiceSession()
            print("Голосовой режим включён.")
            continue
        if user_text in {"/text", "текстовый режим", "выключи голос"}:
            voice_mode = False
            print("Текстовый режим включён.")
            continue
        if user_text == "/clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + memory.prompt_context()}]
            print("Контекст диалога очищен.")
            continue
        if user_text == "/help":
            print("Команды: /voice, /text, /clear, /exit.")
            print("Например: 'покажи файлы в текущей папке' или 'создай файл notes.txt'.")
            continue
        try:
            answer = run_turn(client, memory, messages, user_text)
            print("Помощник: " + answer)
            if voice_mode and voice is not None:
                voice.speak(answer)
        except VoiceError as error:
            print(f"Ошибка голосового вывода: {error}")
        except OllamaError as error:
            print(f"Ошибка подключения: {error}")
        except Exception as error:
            print(f"Ошибка: {error}")


if __name__ == "__main__":
    main()
