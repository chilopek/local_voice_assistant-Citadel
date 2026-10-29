from __future__ import annotations

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get("ASSISTANT_WORKSPACE", PROJECT_ROOT)).expanduser().resolve()
OLLAMA_URL = os.environ.get("ASSISTANT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LOCAL_CONFIG_PATH = PROJECT_ROOT / "assistant.local.json"
try:
    LOCAL_CONFIG = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
except (FileNotFoundError, json.JSONDecodeError):
    LOCAL_CONFIG = {}

MODEL = os.environ.get("ASSISTANT_MODEL", LOCAL_CONFIG.get("model", "qwen3:4b-instruct"))
MAX_STEPS = int(os.environ.get("ASSISTANT_MAX_STEPS", "8"))
_think_override = os.environ.get("ASSISTANT_THINK")
THINK = (
    _think_override.strip().lower() in {"1", "true", "yes", "да"}
    if _think_override is not None
    else bool(LOCAL_CONFIG.get("think", "instruct" not in MODEL.lower()))
)
NUM_PREDICT = int(os.environ.get("ASSISTANT_NUM_PREDICT", str(LOCAL_CONFIG.get("num_predict", 512 if THINK else 192))))
NUM_CTX = int(os.environ.get("ASSISTANT_NUM_CTX", str(LOCAL_CONFIG.get("num_ctx", 4096))))


def is_inside_workspace(path: Path) -> bool:
    """Разрешаем инструментам работать только внутри выбранной рабочей папки."""
    try:
        path.resolve().relative_to(WORKSPACE)
        return True
    except ValueError:
        return False
