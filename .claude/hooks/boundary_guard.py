#!/usr/bin/env python3
"""PreToolUse guard for the hython / plain-Python boundary.

The whole design of this project rests on `hou` and `pxr` existing in exactly
one module. That invariant is easy to break by accident and expensive to
notice: the tests keep passing on a machine with Houdini installed, and the
package only fails for the people who don't have it.

Reads a Claude Code hook payload on stdin. Exit 2 blocks the tool call and
feeds stderr back to the model; exit 0 allows it.

Usable standalone as a linter too:

    python .claude/hooks/boundary_guard.py --check-tree .
"""

from __future__ import annotations

import json
import os
import re
import sys

# module path (posix, relative to repo root) -> what it is allowed to import
HOUDINI_OWNER = "hsl/inspector.py"
QT_OWNER = "hsl/ui.py"
STDLIB_ONLY = "hsl/manifest.py"

HOUDINI_IMPORT = re.compile(r"^\s*(?:import\s+(hou|pxr)\b|from\s+(hou|pxr)[\s.])", re.M)
QT_IMPORT = re.compile(r"^\s*(?:import\s+(PySide\d|PyQt\d)\b|from\s+(PySide\d|PyQt\d)[\s.])", re.M)
THIRD_PARTY = re.compile(
    r"^\s*(?:import|from)\s+(numpy|pandas|requests|PySide\d|PyQt\d|hou|pxr)\b", re.M)


def violations(rel_path: str, content: str) -> list[str]:
    """Return a list of boundary violations for a file's proposed content."""
    found = []

    if not rel_path.startswith("hsl/") or not rel_path.endswith(".py"):
        return found

    if HOUDINI_IMPORT.search(content) and rel_path != HOUDINI_OWNER:
        found.append(
            f"{rel_path} imports hou/pxr. Only {HOUDINI_OWNER} may do that — "
            f"everything else must stay importable without Houdini installed, "
            f"which is what makes tests/test_core.py runnable. Put the Houdini "
            f"code in {HOUDINI_OWNER} and pass the result through the manifest. "
            f"A `try: import hou / except ImportError` guard is not an "
            f"acceptable workaround."
        )

    if QT_IMPORT.search(content) and rel_path != QT_OWNER:
        found.append(
            f"{rel_path} imports Qt. Only {QT_OWNER} may do that — the CLI and "
            f"the core must work with no GUI toolkit present."
        )

    if rel_path == STDLIB_ONLY and THIRD_PARTY.search(content):
        found.append(
            f"{STDLIB_ONLY} must import from the standard library only. It is "
            f"the contract shared between hython and plain Python, and hython's "
            f"interpreter has its own site-packages."
        )

    return found


def _relative(path: str, root: str) -> str:
    try:
        return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def check_tree(root: str) -> int:
    """Standalone lint mode — walk hsl/ and report violations."""
    failures = 0
    package = os.path.join(root, "hsl")
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(package, name)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        for message in violations(f"hsl/{name}", content):
            print(f"BOUNDARY: {message}", file=sys.stderr)
            failures += 1
    if failures == 0:
        print("Boundary intact: hou/pxr only in hsl/inspector.py, Qt only in hsl/ui.py.")
    return 1 if failures else 0


def main() -> int:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if "--check-tree" in sys.argv:
        index = sys.argv.index("--check-tree")
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else "."
        return check_tree(os.path.abspath(target))

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # Never block on a payload we don't understand.

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not path:
        return 0

    # Write carries the whole file; Edit carries the replacement fragment.
    content = "\n".join(
        str(tool_input.get(key, ""))
        for key in ("content", "new_string", "new_str", "replacement")
    )
    if not content.strip():
        return 0

    messages = violations(_relative(path, os.path.abspath(root)), content)
    if not messages:
        return 0

    for message in messages:
        print(f"Blocked by the project boundary rule (see AGENTS.md):\n{message}",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
