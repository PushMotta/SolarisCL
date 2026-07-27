"""Locate the data files that ship inside the package.

Standard library only, and deliberately *not* part of ``ui.py``: the icon is a
Qt concern at the point of use, but where it lives on disk is not, and keeping
the lookup here means the test suite can check it on a machine with no PySide6
installed.

Regenerate the icon itself with ``python scripts/make_icon.py``.
"""

from __future__ import annotations

import os

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICON_FILE = "hsl.ico"


def icon_path() -> str:
    """Absolute path to the application icon, or ``""`` if it is not on disk.

    Returning empty rather than raising is deliberate: a missing icon should
    cost the launcher its taskbar picture, not its ability to start. Callers
    pass the result to ``QIcon``, which treats "" as a null icon.
    """
    path = os.path.join(ASSET_DIR, ICON_FILE)
    return path if os.path.isfile(path) else ""
