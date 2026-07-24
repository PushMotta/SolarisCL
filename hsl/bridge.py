"""Bridge from plain Python to hython.

The launcher never imports ``hou``. It runs :mod:`hsl.inspector` in a
subprocess and reads back a JSON manifest. That keeps the UI responsive, lets
the manifest be cached, and means the launcher can run in whatever Python the
rest of your pipeline uses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from .manifest import SceneManifest


class InspectError(RuntimeError):
    """Raised when hython could not describe the scene."""


def find_hython(explicit: str = "") -> str:
    """Locate hython: explicit path, ``$HSL_HYTHON``, ``$HFS/bin``, then PATH."""
    exe = "hython.exe" if os.name == "nt" else "hython"

    for candidate in (explicit, os.environ.get("HSL_HYTHON", "")):
        if candidate and os.path.isfile(candidate):
            return candidate

    hfs = os.environ.get("HFS", "")
    if hfs:
        candidate = os.path.join(hfs, "bin", exe)
        if os.path.isfile(candidate):
            return candidate

    return shutil.which(exe) or ""


def _package_root() -> str:
    """Directory containing the ``hsl`` package, for PYTHONPATH injection."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def inspect_hip(hip_path: str, *, hython: str = "", export_usd: bool = True,
                usd_dir: str = "", flatten: bool = False, rop: str = "",
                timeout: float = 1800.0,
                env: Optional[dict] = None) -> SceneManifest:
    """Run the inspector under hython and return the parsed manifest.

    Raises :class:`InspectError` with hython's stderr attached on failure --
    a hip that fails to load is the single most common thing to go wrong here,
    and the traceback is what you need to see.
    """
    hip_path = os.path.abspath(hip_path)
    if not os.path.isfile(hip_path):
        raise InspectError(f"No such .hip file: {hip_path}")

    hython_exe = find_hython(hython)
    if not hython_exe:
        raise InspectError(
            "Could not find hython. Set $HFS (source houdini_setup) or pass "
            "an explicit path."
        )

    handle, json_path = tempfile.mkstemp(suffix=".json", prefix="hsl_manifest_")
    os.close(handle)

    cmd = [hython_exe, "-m", "hsl.inspector", hip_path, "--json", json_path]
    if export_usd:
        cmd.append("--export-usd")
    if usd_dir:
        cmd += ["--usd-dir", usd_dir]
    if flatten:
        cmd.append("--flatten")
    if rop:
        cmd += ["--rop", rop]

    run_env = dict(env or os.environ)
    existing = run_env.get("PYTHONPATH", "")
    root = _package_root()
    run_env["PYTHONPATH"] = (root + os.pathsep + existing) if existing else root

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired:
        raise InspectError(f"hython timed out after {timeout:.0f}s loading {hip_path}")
    finally:
        pass

    try:
        with open(json_path) as fh:
            payload = fh.read()
    except OSError:
        payload = ""
    finally:
        try:
            os.unlink(json_path)
        except OSError:
            pass

    if not payload.strip():
        raise InspectError(
            "hython produced no manifest.\n"
            f"exit code: {proc.returncode}\n{proc.stderr[-4000:]}"
        )

    data = json.loads(payload)
    if data.get("error"):
        raise InspectError(data["error"])

    manifest = SceneManifest.from_json(payload)
    return manifest


def cache_path_for(hip_path: str) -> str:
    """Where a cached manifest for this hip lives."""
    base = os.path.splitext(os.path.basename(hip_path))[0]
    digest = str(abs(hash(os.path.abspath(hip_path))))[:8]
    return os.path.join(tempfile.gettempdir(), "hsl", f"{base}_{digest}.json")


def load_cached(hip_path: str) -> Optional[SceneManifest]:
    """Return a cached manifest if it is newer than the .hip, else None."""
    cache = cache_path_for(hip_path)
    try:
        if os.path.getmtime(cache) < os.path.getmtime(hip_path):
            return None
        with open(cache) as fh:
            return SceneManifest.from_json(fh.read())
    except (OSError, ValueError):
        return None


def save_cached(manifest: SceneManifest) -> str:
    cache = cache_path_for(manifest.hip_path)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w") as fh:
        fh.write(manifest.to_json())
    return cache
