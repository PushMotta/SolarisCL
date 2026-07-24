#!/usr/bin/env python3
"""Wire .claude/commands and .claude/skills to their .agents/ originals.

Both IDEs read the same content; symlinks keep one copy. Git on Windows often
checks symlinks out as plain text files containing the target path, so this
falls back to copying and can be re-run after any edit.

    python scripts/link_shared.py            # symlink, or copy if unsupported
    python scripts/link_shared.py --copy     # force copies
"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = [(".claude/commands", ".agents/workflows"),
         (".claude/skills", ".agents/skills")]


def wire(link_rel: str, target_rel: str, force_copy: bool) -> str:
    link = os.path.join(ROOT, link_rel)
    target = os.path.join(ROOT, target_rel)

    if not os.path.isdir(target):
        return f"skip   {link_rel} — {target_rel} does not exist"

    if os.path.islink(link):
        os.unlink(link)
    elif os.path.isdir(link):
        shutil.rmtree(link)
    elif os.path.exists(link):
        os.unlink(link)

    relative = os.path.relpath(target, os.path.dirname(link))

    if not force_copy:
        try:
            os.symlink(relative, link, target_is_directory=True)
            return f"link   {link_rel} -> {relative}"
        except (OSError, NotImplementedError, AttributeError):
            pass

    shutil.copytree(target, link)
    return f"copy   {link_rel} <- {target_rel}  (re-run after editing .agents/)"


def main() -> int:
    force_copy = "--copy" in sys.argv
    for link_rel, target_rel in PAIRS:
        print(wire(link_rel, target_rel, force_copy))
    if force_copy or os.name == "nt":
        print("\nCopies drift. Edit the .agents/ originals and re-run this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
