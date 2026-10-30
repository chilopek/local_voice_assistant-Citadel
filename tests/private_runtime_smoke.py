"""Run cancellation tests with the staged/installed isolated Python runtime."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

runtime = Path(sys.argv[1]).resolve()
assert (runtime / "python312._pth").is_file(), "Expected private Python 3.12 runtime"
tests = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="citadel-private-runtime-") as folder:
    env = dict(os.environ, CITADEL_DATA_DIR=folder, PYTHONHOME="C:/missing-python-regression", PYTHONPATH="C:/missing-packages-regression")
    env.pop("ASSISTANT_OLLAMA_URL", None)
    for pattern in ("test_sessions.py", "test_context.py"):
        subprocess.run([str(runtime / "python.exe"), "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", str(tests), "-p", pattern, "-v"], env=env, check=True, timeout=90)
print("PASS: private runtime cancels blocked work, accepts subsequent tasks, preserves warm workers and reads context", flush=True)
