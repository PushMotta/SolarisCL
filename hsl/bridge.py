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


SETTINGS_FILE = os.path.expanduser("~/.config/hsl/settings.json") if os.name != "nt" else os.path.expandvars(r"%APPDATA%\hsl\settings.json")


def load_user_settings() -> dict:
    """Load user settings JSON file if it exists."""
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_user_setting(key: str, value: str) -> None:
    """Save a single setting key to user settings JSON file."""
    data = load_user_settings()
    data[key] = value
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def list_hython_installations() -> list[tuple[str, str]]:
    """Discover all installed Houdini hython executables on the system.

    Returns a list of (label, path) tuples sorted by version (newest first).
    """
    import glob
    import re
    results: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    def add(label: str, path: str):
        norm = os.path.normpath(path).lower()
        if os.path.isfile(path) and norm not in seen_paths:
            seen_paths.add(norm)
            results.append((label, os.path.abspath(path)))

    # 1. HSL_HYTHON env var
    hsl_env = os.environ.get("HSL_HYTHON", "")
    if hsl_env and os.path.isfile(hsl_env):
        add(f"HSL_HYTHON ({os.path.basename(os.path.dirname(os.path.dirname(hsl_env)))})", hsl_env)

    # 2. $HFS/bin/hython
    exe = "hython.exe" if os.name == "nt" else "hython"
    hfs = os.environ.get("HFS", "")
    if hfs:
        cand = os.path.join(hfs, "bin", exe)
        if os.path.isfile(cand):
            add(f"$HFS ({os.path.basename(hfs)})", cand)

    # 3. System PATH
    in_path = shutil.which(exe)
    if in_path:
        add(f"System PATH ({in_path})", in_path)

    # 4. Standard installation directories
    raw_matches: list[str] = []
    if os.name == "nt":
        raw_matches = glob.glob("C:/Program Files/Side Effects Software/Houdini*/bin/hython.exe")
    elif sys.platform == "darwin":
        raw_matches = glob.glob("/Applications/Houdini/Houdini*/bin/hython")
    else:
        raw_matches = glob.glob("/opt/hfs*/bin/hython")

    def version_key(p: str):
        match = re.search(r"Houdini\s*([\d.]+)|hfs([\d.]+)", p, re.IGNORECASE)
        if match:
            parts = match.group(1) or match.group(2)
            try:
                return [int(x) for x in parts.split(".")]
            except ValueError:
                pass
        return []

    sorted_matches = sorted(raw_matches, key=version_key, reverse=True)

    for p in sorted_matches:
        match = re.search(r"Houdini\s*([\d.]+)|hfs([\d.]+)", p, re.IGNORECASE)
        ver_str = (match.group(1) or match.group(2)) if match else os.path.basename(p)
        add(f"Houdini {ver_str}", p)

    return results


def find_hython(explicit: str = "") -> str:
    """Locate hython: explicit path, saved setting, ``$HSL_HYTHON``, ``$HFS/bin``, PATH, or standard install dirs."""
    exe = "hython.exe" if os.name == "nt" else "hython"

    saved = load_user_settings().get("hython_path", "")
    for candidate in (explicit, saved, os.environ.get("HSL_HYTHON", "")):
        if candidate and os.path.isfile(candidate):
            return candidate

    installs = list_hython_installations()
    if installs:
        return installs[0][1]

    return ""


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


def filter_aovs(usd_in: str, enabled_var_paths, *, hython: str = "",
                usd_out: str = "", timeout: float = 300.0,
                env: Optional[dict] = None) -> str:
    """Return a USD that renders only ``enabled_var_paths`` RenderVars.

    husk has no AOV-selection flag — output planes come from each product's
    ``orderedVars`` in USD. So this runs :func:`hsl.inspector.filter_usd_aovs`
    under hython to author a thin overlay over ``usd_in``. Only call it for a
    strict subset; if every AOV is enabled, render ``usd_in`` directly.

    Raises :class:`InspectError` on failure rather than falling back to the
    unfiltered USD, so a dropped selection never turns into a silently wrong
    render.
    """
    if not usd_in:
        return usd_in

    hython_exe = find_hython(hython)
    if not hython_exe:
        raise InspectError(
            "Could not find hython to filter AOVs. Set $HFS or pass a path."
        )

    if not usd_out:
        base, ext = os.path.splitext(usd_in)
        usd_out = base + ".aovs" + (ext or ".usd")

    cmd = [hython_exe, "-m", "hsl.inspector", "--filter-aovs",
           "--usd-in", usd_in, "--usd-out", usd_out,
           "--keep", ",".join(enabled_var_paths)]

    run_env = dict(env or os.environ)
    root = _package_root()
    existing = run_env.get("PYTHONPATH", "")
    run_env["PYTHONPATH"] = (root + os.pathsep + existing) if existing else root

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)
    except subprocess.SubprocessError as exc:
        raise InspectError(f"AOV filter failed to run under hython: {exc}")

    if proc.returncode != 0 or not os.path.isfile(usd_out):
        raise InspectError(
            "AOV filter did not produce an overlay USD.\n"
            f"exit code: {proc.returncode}\n{proc.stderr[-2000:]}"
        )
    return usd_out


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
