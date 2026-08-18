"""Render memory history.

A tiny JSON-backed store recording what past renders actually *used*, so the
tool can say "last time this shot peaked at 48 GB" instead of predicting.
Stdlib only -- no ``hou``, no ``pxr``, no Qt.

Follows the same tolerant-file pattern as :mod:`hsl.bridge`'s user settings:
missing or corrupt data degrades to nothing rather than raising, and a store
that cannot be written is not an error either. Measurement is a nicety, never
a thing that fails a render.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

LOG_FILE = (
    os.path.expandvars(r"%APPDATA%\hsl\memory.json")
    if os.name == "nt"
    else os.path.expanduser("~/.config/hsl/memory.json")
)

# An unbounded file that grows forever on a busy workstation is a bug -- cap
# both how many samples one scene+ROP can hoard and how many the whole store
# can hold, oldest dropped first either way.
MAX_PER_KEY = 20
MAX_TOTAL = 500


# Schema changes here are **additive only**, the same discipline
# ``hsl/manifest.py`` documents for the scene manifest: every field carries a
# default, ``_load_all`` reads each one with ``.get``, and nothing gates
# behaviour on a version number -- so a store written by an older hsl keeps
# loading and an older hsl keeps loading this one (it ignores what it does not
# know). The additions so far:
#
#   ``peak_rss_is_tree`` -- whether ``peak_rss`` covered the whole process
#   tree. It defaults to False, which is not merely a safe default but the
#   *truthful* reading of an older record: before Job Objects existed here
#   every figure was single-process, so old rows are correctly labelled by the
#   default rather than being silently promoted to a claim they cannot make.


@dataclass
class MemorySample:
    hip_path: str = ""
    rop_path: str = ""
    engine: str = ""
    frames: int = 0                    # frames in the measured chunk
    peak_rss: Optional[int] = None     # bytes, None = not measured
    peak_rss_is_tree: bool = False     # True = whole process tree, False = one process
    peak_vram: Optional[int] = None    # bytes, None = not measured
    vram_sampled: bool = False         # True = polled, so a spike may have been missed
    when: float = 0.0                  # epoch seconds
    houdini: str = ""                  # version string if known


def _key(hip_path: str, rop_path: str) -> str:
    """Normalised (hip, rop) identity used to group samples for the caps.

    Case and relative-vs-absolute spelling of the hip path must not matter --
    the same scene opened two different ways has to land in the same bucket.
    The ROP path is an in-scene node path, not a filesystem path, so it is
    compared literally.
    """
    norm_hip = os.path.normcase(os.path.abspath(hip_path)) if hip_path else ""
    return norm_hip + "|" + (rop_path or "")


def _matches(sample: MemorySample, hip_path: str, rop_path: str) -> bool:
    """True if ``sample`` belongs to ``hip_path`` (and ``rop_path``, if given).

    An empty ``rop_path`` in the query means "any ROP for this scene" -- it
    does not narrow to samples that were themselves recorded with no ROP.
    """
    if hip_path and (os.path.normcase(os.path.abspath(sample.hip_path))
                     != os.path.normcase(os.path.abspath(hip_path))):
        return False
    if rop_path and sample.rop_path != rop_path:
        return False
    return True


def _load_all() -> list[MemorySample]:
    """Every recorded sample, oldest first. Corrupt/missing/unreadable -> []."""
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return []

    if not isinstance(raw, list):
        return []

    samples: list[MemorySample] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            samples.append(MemorySample(
                hip_path=str(entry.get("hip_path", "")),
                rop_path=str(entry.get("rop_path", "")),
                engine=str(entry.get("engine", "")),
                frames=int(entry.get("frames") or 0),
                peak_rss=(int(entry["peak_rss"]) if entry.get("peak_rss") is not None else None),
                # Absent in a store written before whole-tree measurement
                # existed -- and False is what such a row actually was.
                peak_rss_is_tree=bool(entry.get("peak_rss_is_tree", False)),
                peak_vram=(int(entry["peak_vram"]) if entry.get("peak_vram") is not None else None),
                vram_sampled=bool(entry.get("vram_sampled", False)),
                when=float(entry.get("when") or 0.0),
                houdini=str(entry.get("houdini", "")),
            ))
        except (TypeError, ValueError):
            # One malformed row must not take the rest of the history with it.
            continue
    return samples


def _save_all(samples: list[MemorySample]) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            json.dump([asdict(s) for s in samples], fh, indent=2)
    except OSError:
        pass


def record(sample: MemorySample) -> None:
    """Append ``sample`` to the store, then enforce the caps.

    A sample with no measurement at all (``peak_rss`` and ``peak_vram`` both
    ``None``) is not recorded -- an entry that says nothing is noise that only
    dilutes :func:`worst`.
    """
    if sample.peak_rss is None and sample.peak_vram is None:
        return

    samples = _load_all()
    samples.append(sample)

    # Per-key cap: among the samples sharing this (hip, rop) identity, keep
    # only the newest MAX_PER_KEY -- drop the oldest of *that* key, leaving
    # every other key untouched.
    key = _key(sample.hip_path, sample.rop_path)
    keyed_indices = [i for i, s in enumerate(samples)
                     if _key(s.hip_path, s.rop_path) == key]
    if len(keyed_indices) > MAX_PER_KEY:
        drop = set(keyed_indices[:len(keyed_indices) - MAX_PER_KEY])
        samples = [s for i, s in enumerate(samples) if i not in drop]

    # Overall cap: the store as a whole never holds more than MAX_TOTAL,
    # oldest dropped first regardless of key.
    if len(samples) > MAX_TOTAL:
        samples = samples[len(samples) - MAX_TOTAL:]

    _save_all(samples)


def history(hip_path: str = "", rop_path: str = "") -> list[MemorySample]:
    """Samples matching ``hip_path``/``rop_path``, newest first.

    With no ``hip_path`` given, returns everything -- silence is not useful
    when the caller is asking "what do we know at all".
    """
    samples = _load_all()
    if hip_path:
        samples = [s for s in samples if _matches(s, hip_path, rop_path)]
    return list(reversed(samples))


def worst(hip_path: str, rop_path: str = "") -> Optional[MemorySample]:
    """The sample with the highest ``peak_rss`` for this (hip, rop), or None.

    Samples that never measured RSS (``peak_rss is None``, e.g. VRAM-only
    polling) are not candidates -- there is nothing to compare.
    """
    candidates = [s for s in _load_all()
                 if _matches(s, hip_path, rop_path) and s.peak_rss is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.peak_rss)


def forget(hip_path: str = "") -> int:
    """Drop history and return how many samples were removed.

    ``forget()`` with no argument clears the whole store. ``forget(hip)``
    drops every entry for that one scene (any ROP), leaving the rest.
    """
    samples = _load_all()
    if not hip_path:
        _save_all([])
        return len(samples)

    norm_hip = os.path.normcase(os.path.abspath(hip_path))
    keep = [s for s in samples
           if os.path.normcase(os.path.abspath(s.hip_path)) != norm_hip]
    removed = len(samples) - len(keep)
    _save_all(keep)
    return removed
