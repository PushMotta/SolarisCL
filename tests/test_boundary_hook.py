"""The boundary hook is load-bearing — it is the only thing stopping `hou`
from leaking into the layer that has to run without Houdini."""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".claude", "hooks", "boundary_guard.py")
sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))

from boundary_guard import violations  # noqa: E402


class TestViolations(unittest.TestCase):
    def test_houdini_confined_to_inspector(self):
        self.assertTrue(violations("hsl/husk.py", "import hou"))
        self.assertTrue(violations("hsl/bridge.py", "from pxr import UsdRender"))
        self.assertFalse(violations("hsl/inspector.py", "import hou\nfrom pxr import Usd"))

    def test_try_except_guard_is_still_a_violation(self):
        source = "try:\n    import hou\nexcept ImportError:\n    hou = None\n"
        self.assertTrue(violations("hsl/runner.py", source))

    def test_qt_confined_to_ui(self):
        self.assertTrue(violations("hsl/cli.py", "from PySide6.QtWidgets import QApplication"))
        self.assertFalse(violations("hsl/ui.py", "from PySide6.QtCore import Signal"))

    def test_manifest_is_stdlib_only(self):
        self.assertTrue(violations("hsl/manifest.py", "import numpy as np"))
        self.assertFalse(violations("hsl/manifest.py", "import json, os"))

    def test_no_false_positives(self):
        self.assertFalse(violations("hsl/husk.py", "import hounddog"))
        self.assertFalse(violations("hsl/husk.py", "# never import hou here"))
        self.assertFalse(violations("hsl/husk.py", 'HELP = "import hou to inspect"'))

    def test_scoped_to_the_package(self):
        self.assertFalse(violations("scripts/probe.py", "import hou"))
        self.assertFalse(violations("tests/test_core.py", "import hou"))


class TestHookProtocol(unittest.TestCase):
    def _run(self, payload):
        return subprocess.run(
            [sys.executable, HOOK], input=json.dumps(payload),
            capture_output=True, text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": ROOT},
        )

    def test_blocks_with_exit_two_and_explains_on_stderr(self):
        proc = self._run({"tool_input": {
            "file_path": os.path.join(ROOT, "hsl/husk.py"),
            "content": "import hou\n"}})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("AGENTS.md", proc.stderr)

    def test_allows_clean_write(self):
        proc = self._run({"tool_input": {
            "file_path": os.path.join(ROOT, "hsl/husk.py"),
            "content": "import os\n"}})
        self.assertEqual(proc.returncode, 0)

    def test_reads_edit_fragments_not_just_whole_files(self):
        proc = self._run({"tool_input": {
            "file_path": os.path.join(ROOT, "hsl/husk.py"),
            "new_string": "import hou\n"}})
        self.assertEqual(proc.returncode, 2)

    def test_fails_open_on_unparseable_input(self):
        """A guard that crashes the session is worse than one that misses a case."""
        proc = subprocess.run([sys.executable, HOOK], input="not json at all",
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)

    def test_lint_mode_passes_on_the_real_tree(self):
        proc = subprocess.run([sys.executable, HOOK, "--check-tree", ROOT],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
