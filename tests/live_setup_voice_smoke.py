"""Opt-in real installer preparation, then offline TTS-to-STT without devices."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from assistant.piper_voice import get_piper_path

with tempfile.TemporaryDirectory(prefix="citadel-voice-install-proof-") as folder:
    root = Path(folder)
    cache = root / "cache/piper"
    cache.mkdir(parents=True)
    model = get_piper_path()
    for asset in (model, Path(str(model) + ".json"), model.parent / "MODEL_CARD"):
        shutil.copy2(asset, cache / asset.name)
    (root / "assistant.local.json").write_text(json.dumps({"model": "unused", "voice": True, "stt_model": "base", "tts_engine": "silero"}), encoding="utf-8")
    env = dict(os.environ, CITADEL_DATA_DIR=folder)
    subprocess.run([sys.executable, "-B", "-X", "utf8", "-u", "-m", "assistant.setup_runtime", "--skip-agent-assets"], env=env, check=True, timeout=180)
    settings = json.loads((root / "assistant.local.json").read_text(encoding="utf-8"))
    assert settings["tts_engine"] in {"silero", "piper"}
    script = '''
import sys
def audit(event,args):
    if event in {'socket.connect','http.client.connect'}:
        raise AssertionError('Unexpected network during voice runtime: '+event)
sys.addaudithook(audit)
import numpy as np
from assistant.voice import VoiceSession
s=VoiceSession(allow_sapi_fallback=False)
s.prepare(speech=True)
m,_=s._load_tts()
speaker,rate=s._tts_settings(m)
for utterance,wake in [('Цитадель, открой калькулятор.',True),('Открой калькулятор.',False),('Цитадель.',True)]:
    audio=m.apply_tts(text=utterance,speaker=speaker,sample_rate=rate,put_accent=True,put_yo=True)
    if hasattr(audio,'detach'): audio=audio.detach().cpu().numpy()
    assert len(audio)>rate*.2
    audio=np.interp(np.arange(round(len(audio)*16000/rate))*rate/16000,np.arange(len(audio)),audio)
    text=s.transcribe_audio((np.clip(audio,-1,1)*32767).astype('<i2').tobytes())
    print('RECOGNIZED:',text,flush=True)
    assert text and ('цитадель' in text.lower())==wake,(utterance,text)
print('PASS: actual local synthesis and Whisper recognition work without network, microphone or speakers',flush=True)
'''
    subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script], env=env, check=True, timeout=90)
    print("PASS: setup persisted a validated natural voice engine:", settings["tts_engine"], flush=True)
