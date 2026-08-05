#!/usr/bin/env python3
"""Build the redistributable zips.

    python scripts/make_release.py                     # dist/hsl-<version>.zip
    python scripts/make_release.py --bundle-python     # + hsl-<version>-win64-full.zip

What goes in is the runtime half only -- the package, the launchers and the
install guide. The test suite, the agent pack, the developer docs and the
build scripts stay out: they are how hsl is *worked on*, not how it is run,
and shipping them only invites someone to run the wrong thing.

``--bundle-python`` additionally builds the **standalone** zip: the same tree
plus a ``python\\`` directory holding the official embeddable CPython with
PySide6 seeded into it. Unzip, double-click ``launch_ui.bat``, done -- no
Python install, no pip, no PATH, nothing touched outside the folder. It needs
the network (one cached fetch from python.org) and a Windows dev machine, and
it refuses to write a zip whose bundled interpreter cannot actually import
PySide6 and hsl -- a broken standalone is a support ticket, not a release.

Everything is nested under a single ``hsl-<version>/`` directory inside the
zip, so extracting it anywhere leaves one tidy folder rather than scattering
files into the current directory.

Standard library only, like everything else here -- no build backend needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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


# --- The bundled-Python ("standalone") build --------------------------------
# Version notes, each deliberate:
#   * 3.11.9 is the last 3.11 with an official embeddable binary (later
#     3.11.x releases are source-only security fixes), and 3.11 is what the
#     dev environment runs the suite under -- the bundle matches what is
#     actually tested.
#   * PySide6-Essentials covers everything hsl/ui.py imports (QtCore, QtGui,
#     QtWidgets); the full PySide6 metapackage would add the Addons wheels --
#     roughly another hundred megabytes -- for nothing.
#   * The PySide6 pin matches the version the UI is verified against on this
#     machine. Bump it deliberately, then re-run check_ui_imports and the
#     bundle build, never as a side effect.
# PySide6 wheels are abi3 (limited API), which is what makes seeding them
# from the dev interpreter into the embeddable one legitimate; the smoke test
# still proves it rather than trusting the tag.

EMBED_VERSION = "3.11.9"
EMBED_URL = ("https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
             .format(v=EMBED_VERSION))
PYSIDE_SPEC = "PySide6-Essentials==6.11.1"

# Checked inside the full zip, on top of REQUIRED.
FULL_REQUIRED = (
    "python/python.exe",
    "python/python311._pth",
    "python/Lib/site-packages/PySide6/__init__.py",
    "python/Lib/site-packages/shiboken6/__init__.py",
)


def patched_pth(text: str) -> str:
    """Rewrite the embeddable build's ``._pth`` file for this layout.

    That file controls ``sys.path`` *completely* -- no registry, no
    ``PYTHONPATH``, no defaults. Three changes, each load-bearing:
    ``Lib\\site-packages`` is where PySide6 is seeded; ``..`` is the app root
    one level above ``python\\``, which is what makes ``-m hsl.cli`` resolve;
    and ``import site`` (shipped commented out) turns the site-packages
    machinery on. Idempotent: patching an already-patched file changes
    nothing, so a cached runtime can be rebuilt safely.
    """
    lines = [line for line in text.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    for entry in ("Lib\\site-packages", ".."):
        if entry not in lines:
            lines.append(entry)
    if "import site" not in lines:
        lines.append("import site")
    return "\n".join(lines) + "\n"


def _download(url: str, dest: str) -> None:
    import urllib.request
    tmp = dest + ".part"
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    os.replace(tmp, dest)


def build_python_runtime(python_dir: str) -> None:
    """Assemble ``python\\``: embeddable CPython with PySide6 seeded in."""
    cache = os.path.join(DIST, "_embed_cache", os.path.basename(EMBED_URL))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    if not os.path.isfile(cache):
        print(f"  fetching {EMBED_URL}")
        _download(EMBED_URL, cache)
    with zipfile.ZipFile(cache) as zf:
        zf.extractall(python_dir)

    pth_names = [n for n in os.listdir(python_dir) if n.endswith("._pth")]
    if len(pth_names) != 1:
        raise SystemExit(
            f"expected exactly one ._pth in the embeddable zip, found: {pth_names}")
    pth = os.path.join(python_dir, pth_names[0])
    with open(pth, "r", encoding="utf-8") as fh:
        text = fh.read()
    with open(pth, "w", encoding="utf-8") as fh:
        fh.write(patched_pth(text))

    print(f"  seeding {PYSIDE_SPEC}")
    site = os.path.join(python_dir, "Lib", "site-packages")
    run = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--target", site,
         "--only-binary=:all:",
         "--python-version", EMBED_VERSION.rsplit(".", 1)[0],
         "--platform", "win_amd64",
         "--quiet", "--no-warn-script-location", PYSIDE_SPEC],
        capture_output=True, text=True)
    if run.returncode != 0:
        raise SystemExit(f"pip could not seed {PYSIDE_SPEC}:\n{run.stderr[-2000:]}")


def smoke_test_bundle(staging: str) -> None:
    """Prove the bundle with its *own* interpreter, not the dev one.

    Anything failing here would fail on the artist's machine identically, so
    the build stops rather than writing the zip.
    """
    exe = os.path.join(staging, "python", "python.exe")
    checks = (
        ("bundled Python runs",
         [exe, "-c", "import sys; print(sys.version)"]),
        ("PySide6 imports",
         [exe, "-c", "import PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets"]),
        ("hsl.ui imports",
         [exe, "-c", "import hsl.ui"]),
        ("hsl CLI answers",
         [exe, "-m", "hsl.cli", "--help"]),
    )
    for label, cmd in checks:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if run.returncode != 0:
            raise SystemExit(
                f"bundle smoke test failed ({label}):\n"
                f"{run.stdout[-1000:]}\n{run.stderr[-2000:]}")
        print(f"  ok: {label}")


def build_full_zip(tag: str) -> str:
    """The standalone zip: the shipped tree plus the bundled runtime."""
    bundle_root = os.path.join(DIST, "_bundle")
    if os.path.isdir(bundle_root):
        shutil.rmtree(bundle_root)
    staging = os.path.join(bundle_root, tag)

    for disk, rel in _members():
        dest = os.path.join(staging, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(disk, dest)

    build_python_runtime(os.path.join(staging, "python"))
    smoke_test_bundle(staging)

    out = os.path.join(DIST, f"{tag}-win64-full.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(staging):
            dirnames.sort()
            for name in sorted(filenames):
                disk = os.path.join(dirpath, name)
                rel = os.path.relpath(disk, staging).replace(os.sep, "/")
                zf.write(disk, f"{tag}/{rel}")

    with zipfile.ZipFile(out) as zf:
        if zf.testzip() is not None:
            raise SystemExit(f"{out} is corrupt")
        inside = {n[len(tag) + 1:] for n in zf.namelist()}
    missing = [n for n in REQUIRED + FULL_REQUIRED if n not in inside]
    if missing:
        raise SystemExit("standalone release is missing: " + ", ".join(missing))
    return out


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


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    bundle_python = "--bundle-python" in args
    unknown = [a for a in args if a != "--bundle-python"]
    if unknown:
        raise SystemExit(f"unknown argument(s): {' '.join(unknown)} "
                         f"(only --bundle-python is understood)")

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

    if bundle_python:
        print("\nstandalone build:")
        full = build_full_zip(tag)
        size = os.path.getsize(full)
        print(f"\nwrote {full}")
        print(f"  {size:,} bytes ({size / (1024 * 1024):.0f} MB)")
        print(f"  extracts to: {tag}/ -- unzip, double-click launch_ui.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
