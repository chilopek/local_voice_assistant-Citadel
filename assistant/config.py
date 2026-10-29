from __future__ import annotations

import json
import os
from pathlib import Path
from .paths import DATA_ROOT, IS_BUNDLED, PACKAGE_ROOT


PROJECT_ROOT = DATA_ROOT
OLLAMA_URL = os.environ.get("ASSISTANT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LOCAL_CONFIG_PATH = PROJECT_ROOT / "assistant.local.json"
try:
    LOCAL_CONFIG = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
except (FileNotFoundError, json.JSONDecodeError):
    LOCAL_CONFIG = {}

WORKSPACE = Path(os.environ.get("ASSISTANT_WORKSPACE", LOCAL_CONFIG.get("workspace", str(DATA_ROOT / "workspace" if IS_BUNDLED else PACKAGE_ROOT)))).expanduser().resolve()

MODEL = os.environ.get("ASSISTANT_MODEL", LOCAL_CONFIG.get("model", "qwen3:4b-instruct"))
MAX_STEPS = int(os.environ.get("ASSISTANT_MAX_STEPS", "24"))
_think_override = os.environ.get("ASSISTANT_THINK")
THINK = (
    _think_override.strip().lower() in {"1", "true", "yes", "да"}
    if _think_override is not None
    else bool(LOCAL_CONFIG.get("think", "instruct" not in MODEL.lower()))
)
NUM_PREDICT = max(512, int(os.environ.get("ASSISTANT_NUM_PREDICT", str(LOCAL_CONFIG.get("num_predict", 768)))))
NUM_CTX = max(8192, int(os.environ.get("ASSISTANT_NUM_CTX", str(LOCAL_CONFIG.get("num_ctx", 8192)))))
VOICE_ENABLED = bool(
    os.environ.get("ASSISTANT_VOICE", str(LOCAL_CONFIG.get("voice", False))).strip().lower()
    in {"1", "true", "yes", "да", "on"}
)
VOICE_STT_MODEL = os.environ.get("ASSISTANT_STT_MODEL", LOCAL_CONFIG.get("stt_model", "base"))
VOICE_TTS_MODEL = os.environ.get("ASSISTANT_TTS_MODEL", LOCAL_CONFIG.get("tts_model", "v5_6_ru"))
VOICE_TTS_SPEAKER = os.environ.get("ASSISTANT_TTS_SPEAKER", LOCAL_CONFIG.get("tts_speaker", "aidar"))
VOICE_SAMPLE_RATE = int(os.environ.get("ASSISTANT_VOICE_SAMPLE_RATE", str(LOCAL_CONFIG.get("voice_sample_rate", 16000))))
VOICE_TTS_SAMPLE_RATE = int(os.environ.get("ASSISTANT_TTS_SAMPLE_RATE", str(LOCAL_CONFIG.get("tts_sample_rate", 48000))))
VOICE_INPUT_DEVICE = os.environ.get("ASSISTANT_INPUT_DEVICE", LOCAL_CONFIG.get("input_device", ""))
VOICE_THREADS = int(os.environ.get("ASSISTANT_VOICE_THREADS", str(LOCAL_CONFIG.get("voice_threads", 4))))


def is_inside_workspace(path: Path) -> bool:
    """Разрешаем инструментам работать только внутри выбранной рабочей папки."""
    try:
        path.resolve().relative_to(WORKSPACE)
        return True
    except ValueError:
        return False
