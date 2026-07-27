#!/usr/bin/env python3
"""Build the redistributable zip: ``dist/hsl-<version>.zip``.

    python scripts/make_release.py

What goes in is the runtime half only -- the package, the launchers and the
install guide. The test suite, the agent pack, the developer docs and the
build scripts stay out: they are how hsl is *worked on*, not how it is run,
and shipping them only invites someone to run the wrong thing.

Everything is nested under a single ``hsl-<version>/`` directory inside the
zip, so extracting it anywhere leaves one tidy folder rather than scattering
files into the current directory.

Standard library only, like everything else here -- no build backend needed.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

# Whole directories, copied recursively.
TREES = ("hsl", "bin")

# Individual files at the top level.
FILES = ("launch_ui.bat", "INSTALL.md", "README.md", "pyproject.toml")

# Never ship these, wherever they turn up.
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
SKIP_SUFFIX = (".pyc", ".pyo", ".orig", ".rej")

# Present in the zip and checked after it is written. If one of these is
# missing the archive is useless, so fail the build rather than ship it.
REQUIRED = (
    "hsl/__init__.py",
    "hsl/cli.py",
    "hsl/ui.py",
    "hsl/inspector.py",
    "hsl/manifest.py",
    "hsl/resources.py",
    "hsl/assets/hsl.ico",
    "bin/hsl",
    "bin/hsl.bat",
    "launch_ui.bat",
    "INSTALL.md",
)


def version() -> str:
    """Read ``version`` out of pyproject.toml.

    Parsed by hand rather than with tomllib: this has to keep working on the
    3.9 the rest of the project targets, and tomllib only arrived in 3.11.
    """
    with open(os.path.join(ROOT, "pyproject.toml"), "r", encoding="utf-8") as fh:
        for line in fh:
            match = re.match(r'^\s*version\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                return match.group(1)
    raise SystemExit("no version found in pyproject.toml")


def _members():
    """Yield (path on disk, path inside the zip) for everything shipped."""
    for tree in TREES:
        base = os.path.join(ROOT, tree)
        if not os.path.isdir(base):
            raise SystemExit(f"missing directory: {tree}")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if name.endswith(SKIP_SUFFIX):
                    continue
                disk = os.path.join(dirpath, name)
                yield disk, os.path.relpath(disk, ROOT).replace(os.sep, "/")

    for name in FILES:
        disk = os.path.join(ROOT, name)
        if not os.path.isfile(disk):
            raise SystemExit(f"missing file: {name}")
        yield disk, name


def main() -> int:
    tag = f"hsl-{version()}"
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"{tag}.zip")

    shipped = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for disk, rel in _members():
            zf.write(disk, f"{tag}/{rel}")
            shipped.append(rel)

    # Read it back. A zip that is missing a file it needs still opens fine,
    # so the only useful check is against the manifest above.
    with zipfile.ZipFile(out) as zf:
        if zf.testzip() is not None:
            raise SystemExit(f"{out} is corrupt")
        inside = {n[len(tag) + 1:] for n in zf.namelist()}
    missing = [name for name in REQUIRED if name not in inside]
    if missing:
        raise SystemExit("release is missing: " + ", ".join(missing))

    for rel in shipped:
        print(f"  {rel}")
    size = os.path.getsize(out)
    print(f"\nwrote {out}")
    print(f"  {len(shipped)} files, {size:,} bytes ({size / 1024:.0f} KB)")
    print(f"  extracts to: {tag}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
