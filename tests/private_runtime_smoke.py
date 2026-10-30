"""Run cancellation tests with the staged/installed isolated Python runtime."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import shutil

runtime = Path(sys.argv[1]).resolve()
assert (runtime / "python312._pth").is_file(), "Expected private Python 3.12 runtime"
tests = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="citadel-private-runtime-") as folder:
    env = dict(os.environ, CITADEL_DATA_DIR=folder, PYTHONHOME="C:/missing-python-regression", PYTHONPATH="C:/missing-packages-regression")
    env.pop("ASSISTANT_OLLAMA_URL", None)
    for pattern in ("test_sessions.py", "test_context.py"):
        subprocess.run([str(runtime / "python.exe"), "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", str(tests), "-p", pattern, "-v"], env=env, check=True, timeout=90)
    from assistant.piper_voice import get_piper_path
    piper = get_piper_path()
    if piper.is_file():
        target = Path(folder) / "cache/piper"
        target.mkdir(parents=True, exist_ok=True)
        for asset in (piper, Path(str(piper) + ".json")):
            shutil.copy2(asset, target / asset.name)
        script = "from assistant.voice import VoiceSession; s=VoiceSession(tts_engine='piper',allow_sapi_fallback=False);m,_=s._load_tts();a=m.apply_tts(text='Голос готов.',speaker='irina',sample_rate=22050,put_accent=True,put_yo=True);assert a.size>2000;print('PASS: private runtime generates real Piper speech offline')"
        subprocess.run([str(runtime / "python.exe"), "-B", "-X", "utf8", "-c", script], env=env, check=True, timeout=60)
print("PASS: private runtime cancels blocked work, accepts subsequent tasks, preserves warm workers and reads context", flush=True)
