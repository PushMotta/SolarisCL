#!/usr/bin/env python3
"""Import-check hsl/ui.py without PySide6 installed.

Catches missing imports and typo'd widget names, which are otherwise only found
by launching the window. It does not prove the layout is right — nothing here
can do that.

    python scripts/check_ui_imports.py
"""

from __future__ import annotations

import ast
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub(module_name: str) -> types.ModuleType:
    """A module where every attribute is a real, subclassable class."""
    module = types.ModuleType(module_name)
    cache: dict = {}

    def __getattr__(attr):
        if attr not in cache:
            if attr == "Signal":
                cache[attr] = lambda *a, **k: None
            elif attr == "Slot":
                cache[attr] = lambda *a, **k: (lambda fn: fn)
            else:
                cache[attr] = type(attr, (object,),
                                   {"__init__": lambda self, *a, **k: None})
        return cache[attr]

    module.__getattr__ = __getattr__
    return module


def main() -> int:
    try:
        import PySide6  # noqa: F401
        real = True
    except ImportError:
        real = False
        for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui",
                     "PySide6.QtWidgets"):
            sys.modules[name] = _stub(name)
        sys.modules["PySide6.QtCore"].Qt = type(
            "Qt", (object,), {"AlignRight": 0, "Vertical": 0, "SelectRows": 0})

    try:
        import hsl.ui as ui
    except Exception as exc:
        print(f"FAIL  hsl/ui.py does not import: {exc.__class__.__name__}: {exc}")
        return 1

    # Cross-check: every Qt name referenced must be one the module imported.
    source = open(os.path.join(ROOT, "hsl", "ui.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("PySide6")
        for alias in node.names
    }
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    qt_used = {name for name in used
               if name.startswith("Q") or name in ("Signal", "Slot", "Qt")}
    missing = qt_used - imported - set(dir(ui))

    if missing:
        print(f"FAIL  Qt names used but never imported: {sorted(missing)}")
        return 1

    how = "real PySide6" if real else "stubbed Qt"
    print(f"OK    hsl/ui.py imports cleanly and all Qt names resolve ({how}).")
    if not real:
        print("      Layout and runtime behaviour are NOT verified by this check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
