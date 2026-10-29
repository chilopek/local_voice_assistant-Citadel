from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _tkinter

from assistant.tk_runtime import configure_tk_paths


class TkPathsTests(unittest.TestCase):
    def test_discovers_runtime_libraries_instead_of_inherited_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tcl = root / "tcl" / f"tcl{_tkinter.TCL_VERSION}"
            tk = root / "tcl" / f"tk{_tkinter.TK_VERSION}"
            tcl.mkdir(parents=True)
            tk.mkdir(parents=True)
            (tcl / "init.tcl").touch()
            (tk / "tk.tcl").touch()
            with patch.dict(os.environ, {"TCL_LIBRARY": "missing", "TK_LIBRARY": "missing"}):
                result = configure_tk_paths([root])
                self.assertEqual(result["TCL_LIBRARY"], str(tcl))
                self.assertEqual(result["TK_LIBRARY"], str(tk))
                self.assertEqual(os.environ["TCL_LIBRARY"], str(tcl))

    def test_does_not_select_files_for_different_tcl_version(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / "tcl" / "tcl0.0"
            other.mkdir(parents=True)
            (other / "init.tcl").touch()
            self.assertEqual(configure_tk_paths([root]), {})


if __name__ == "__main__":
    unittest.main()
