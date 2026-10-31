import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SetupConfigTests(unittest.TestCase):
    def configure(self, root: Path, *arguments: str):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "assistant.setup_runtime", "--skip-download", *arguments],
                                env=dict(os.environ, CITADEL_DATA_DIR=str(root)), capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((root / "assistant.local.json").read_text(encoding="utf-8"))

    def test_first_install_creates_selected_settings_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            data = self.configure(Path(folder), "--model", "balanced", "--voice", "on")
            self.assertEqual(data["model"], "qwen3:4b-instruct")
            self.assertTrue(data["voice"])

    def test_update_keeps_existing_settings_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = {"model": "user-model", "voice": False, "num_ctx": 10000, "workspace": str(root / "documents")}
            (root / "assistant.local.json").write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(self.configure(root, "--model", "power", "--voice", "on"), original)

    def test_explicit_replacement_changes_model_and_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.configure(root, "--model", "balanced", "--voice", "on")
            updated = self.configure(root, "--model", "fast", "--voice", "off", "--replace-config")
            self.assertEqual(updated["model"], "qwen3:1.7b")
            self.assertFalse(updated["voice"])

    def test_legacy_workspace_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".venv").mkdir()
            (root / "assistant").mkdir()
            (root / "assistant.local.json").write_text('{"model":"legacy-model","voice":true}', encoding="utf-8")
            updated = self.configure(root)
            self.assertEqual(updated["model"], "legacy-model")
            self.assertEqual(Path(updated["workspace"]).resolve(), root.resolve())


if __name__ == "__main__":
    unittest.main()
