"""Render profile presets manager.

Allows artists to define and apply reusable render profiles such as:
  * Fast Preview (Karma XPU, low complexity)
  * Beauty Final (Karma CPU/XPU, high quality)
  * Matte Pass
  * Turnaround Test
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .husk import RenderJob

PRESETS_FILE = os.path.expanduser("~/.config/hsl/presets.json") if os.name != "nt" else os.path.expandvars(r"%APPDATA%\hsl\presets.json")

DEFAULT_PRESETS: Dict[str, Dict[str, Any]] = {
    "🚀 Fast Preview (Karma XPU)": {
        "renderer": "BRAY_HdKarmaXPU",
        "resolution": [960, 540],
        "threads": 0,
        "complexity": "low",
        "snapshot_interval": 5,
        "verbosity": "3a",
        "extra_args": ["--disable-motionblur"],
    },
    "🎨 Beauty Final (Production)": {
        "renderer": "BRAY_HdKarma",
        "resolution": [1920, 1080],
        "threads": 0,
        "complexity": "high",
        "snapshot_interval": 30,
        "verbosity": "3a",
        "extra_args": [],
    },
    "📐 Matte / Utility Pass": {
        "renderer": "BRAY_HdKarma",
        "resolution": [1920, 1080],
        "threads": 0,
        "complexity": "medium",
        "snapshot_interval": 0,
        "verbosity": "3a",
        # NB: only real husk flags here. Depth-of-field is not a husk switch
        # (it is the karma:global:enable_dof USD setting), so it is not passed.
        "extra_args": ["--disable-motionblur"],
    },
    "🔄 Turnaround Test": {
        "renderer": "BRAY_HdKarmaXPU",
        "resolution": [1280, 720],
        "threads": 0,
        "complexity": "medium",
        "snapshot_interval": 10,
        "verbosity": "3a",
        "extra_args": [],
    },
}


def get_default_presets() -> Dict[str, Dict[str, Any]]:
    """Return all available presets combining defaults and custom user presets."""
    presets = dict(DEFAULT_PRESETS)
    user_presets = load_custom_presets()
    presets.update(user_presets)
    return presets


def load_custom_presets() -> Dict[str, Dict[str, Any]]:
    """Load user defined custom presets from disk."""
    if os.path.isfile(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_custom_preset(name: str, preset_data: Dict[str, Any]) -> None:
    """Save a custom user preset to disk."""
    custom = load_custom_presets()
    custom[name] = preset_data
    try:
        os.makedirs(os.path.dirname(PRESETS_FILE), exist_ok=True)
        with open(PRESETS_FILE, "w", encoding="utf-8") as fh:
            json.dump(custom, fh, indent=2)
    except OSError:
        pass


def apply_preset(job: RenderJob, preset: Dict[str, Any]) -> RenderJob:
    """Apply preset values to a RenderJob."""
    if "renderer" in preset:
        job.renderer = preset["renderer"]
    if "resolution" in preset and preset["resolution"]:
        job.resolution = (preset["resolution"][0], preset["resolution"][1])
    if "threads" in preset:
        job.threads = preset["threads"]
    if "complexity" in preset:
        job.complexity = preset["complexity"]
    if "snapshot_interval" in preset:
        job.snapshot_interval = preset["snapshot_interval"]
    if "verbosity" in preset:
        job.verbosity = preset["verbosity"]
    if "extra_args" in preset:
        job.extra_args = list(preset["extra_args"])
    return job
