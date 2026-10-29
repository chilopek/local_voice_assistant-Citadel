from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import NUM_CTX, NUM_PREDICT, THINK


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            # Qwen3 otherwise may spend the whole short response budget on thinking.
            "think": THINK,
            "options": {
                "temperature": 0.2,
                "num_predict": NUM_PREDICT,
                "num_ctx": NUM_CTX,
            },
            "keep_alive": "10m",
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as error:
            raise OllamaError(
                f"Не удалось подключиться к Ollama по адресу {self.base_url}. "
                "Запусти `ollama serve` и проверь, что модель установлена."
            ) from error
        except json.JSONDecodeError as error:
            raise OllamaError("Ollama вернула некорректный JSON-ответ.") from error
