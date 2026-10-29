from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def launch(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT), **overrides)
        # A shortcut must work independently of the shell's original directory.
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run(
                [sys.executable, "-m", "assistant.launcher", "--check"],
                cwd=folder, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=30,
            )

    def test_real_window_starts_without_voice_dependencies(self) -> None:
        result = self.launch(ASSISTANT_VOICE="off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GUI startup check OK", result.stdout)

    def test_real_window_starts_with_voice_configured(self) -> None:
        result = self.launch(ASSISTANT_VOICE="on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bad_inherited_tcl_paths_are_repaired(self) -> None:
        result = self.launch(TCL_LIBRARY="C:/missing-tcl/init", TK_LIBRARY="C:/missing-tk/init")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GUI startup check OK", result.stdout)

    def test_import_failure_is_reported_and_logged(self) -> None:
        result = self.launch(ASSISTANT_MAX_STEPS="invalid-regression-test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ValueError", result.stderr)
        self.assertIn("invalid-regression-test", result.stderr)
        log = (ROOT / "assistant-launch.log").read_text(encoding="utf-8-sig")
        self.assertIn("Startup failed:", log)
        self.assertIn("invalid-regression-test", log)

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher")
    def test_windows_powershell_51_wrapper(self) -> None:
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/run.ps1"), "-Check"],
            cwd=ROOT, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, repr(result.stdout + result.stderr))
        self.assertIn(b"GUI startup check OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
