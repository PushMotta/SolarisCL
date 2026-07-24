"""Everything about driving the ``husk`` binary.

Pure standard library -- no Qt, no ``hou``. Kept free of side effects so the
command construction can be unit tested without Houdini installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .manifest import RenderRop, SceneManifest

# husk emits `ALF_PROGRESS 42%` when run with -Valfred.
_ALF_PROGRESS = re.compile(r"ALF_PROGRESS\s+(\d+)\s*%")
# `husk --list-renderers` prints one delegate per line, sometimes indented.
_RENDERER_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_:.\-]*)\s*$")

DEFAULT_RENDERERS = [
    "BRAY_HdKarma",
    "BRAY_HdKarmaXPU",
]


# --------------------------------------------------------------------------
# Locating husk
# --------------------------------------------------------------------------

def find_husk(explicit: str = "") -> str:
    """Return a path to the husk executable, or "" if it cannot be found.

    Order of preference: explicit argument, ``$HSL_HUSK``, ``$HFS/bin``,
    then whatever is on ``PATH``.
    """
    exe = "husk.exe" if os.name == "nt" else "husk"

    for candidate in (explicit, os.environ.get("HSL_HUSK", "")):
        if candidate and os.path.isfile(candidate):
            return candidate

    hfs = os.environ.get("HFS", "")
    if hfs:
        candidate = os.path.join(hfs, "bin", exe)
        if os.path.isfile(candidate):
            return candidate

    return shutil.which(exe) or ""


def list_renderers(husk_exe: str = "", timeout: float = 30.0) -> list[str]:
    """Ask husk which Hydra delegates are registered.

    Falls back to the known Karma delegates if husk cannot be run, so the UI
    always has something sensible to offer.
    """
    husk_exe = husk_exe or find_husk()
    if not husk_exe:
        return list(DEFAULT_RENDERERS)

    try:
        proc = subprocess.run(
            [husk_exe, "--list-renderers"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return list(DEFAULT_RENDERERS)

    found = []
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        match = _RENDERER_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.lower().startswith(("available", "renderer", "usage")):
            continue
        if name not in found:
            found.append(name)

    return found or list(DEFAULT_RENDERERS)


# --------------------------------------------------------------------------
# Frame ranges
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameChunk:
    """husk thinks in (start, count, increment), not (start, end)."""
    start: int
    count: int
    inc: int = 1

    @property
    def end(self) -> int:
        return self.start + (self.count - 1) * self.inc

    def __str__(self) -> str:
        if self.count == 1:
            return str(self.start)
        step = f"x{self.inc}" if self.inc != 1 else ""
        return f"{self.start}-{self.end}{step}"


def frame_chunks(start: int, end: int, inc: int = 1,
                 chunk_size: int = 0) -> list[FrameChunk]:
    """Split an inclusive frame range into chunks for parallel submission.

    ``chunk_size`` is measured in *frames rendered*, not frame numbers, so an
    increment of 2 over 1-100 with chunk size 10 gives chunks of 10 rendered
    frames each. ``chunk_size <= 0`` means one chunk for the whole range.
    """
    inc = max(1, int(inc))
    if end < start:
        start, end = end, start

    total = (end - start) // inc + 1
    if chunk_size <= 0 or chunk_size >= total:
        return [FrameChunk(start, total, inc)]

    chunks = []
    rendered = 0
    while rendered < total:
        count = min(chunk_size, total - rendered)
        chunks.append(FrameChunk(start + rendered * inc, count, inc))
        rendered += count
    return chunks


# --------------------------------------------------------------------------
# Render job description
# --------------------------------------------------------------------------

@dataclass
class RenderJob:
    """A fully resolved husk or hython invocation, before it is turned into argv."""
    usd_file: str = ""
    engine: str = "husk"                 # "husk" or "hython"
    hip_file: str = ""
    rop_path: str = ""
    renderer: str = "BRAY_HdKarma"
    chunk: FrameChunk = field(default_factory=lambda: FrameChunk(1, 1, 1))
    settings_prim: str = ""
    camera: str = ""
    output: str = ""                     # overrides the product name
    resolution: Optional[tuple[int, int]] = None
    threads: int = 0                     # 0 => husk default (all cores)
    verbosity: str = "3a"
    snapshot_interval: int = 0           # seconds; 0 => off
    alfred_progress: bool = True
    make_output_path: bool = True
    fast_exit: bool = True
    complexity: str = ""                 # veryhigh / high / medium / low
    purpose: str = ""                    # e.g. "render,proxy"
    extra_args: list[str] = field(default_factory=list)
    husk_exe: str = ""
    hython_exe: str = ""

    @property
    def label(self) -> str:
        source = self.hip_file if (self.engine == "hython" and self.hip_file) else self.usd_file
        prefix = f"[{self.engine}] " if self.engine != "husk" else ""
        return f"{prefix}{os.path.basename(source)} [{self.chunk}]"


def build_command(job: RenderJob) -> list[str]:
    """Turn a RenderJob into an argv list. Never shell-quoted -- pass this
    straight to subprocess/QProcess with shell=False."""
    if job.engine == "hython":
        from .bridge import find_hython   # lives in bridge (smart discovery)
        exe = job.hython_exe or find_hython() or "hython"
        cmd: list[str] = [exe, "-m", "hsl.inspector", job.hip_file or job.usd_file, "--render-direct"]
        if job.rop_path:
            cmd += ["--rop", job.rop_path]
        cmd += ["--frame-start", str(job.chunk.start)]
        cmd += ["--frame-count", str(job.chunk.count)]
        if job.chunk.inc != 1:
            cmd += ["--frame-inc", str(job.chunk.inc)]
        if job.renderer:
            cmd += ["--renderer", job.renderer]
        if job.camera:
            cmd += ["--camera", job.camera]
        if job.output:
            cmd += ["--output", job.output]
        if job.resolution:
            cmd += ["--res", str(job.resolution[0]), str(job.resolution[1])]
        cmd += list(job.extra_args)
        return cmd

    exe = job.husk_exe or find_husk() or "husk"
    cmd: list[str] = [exe]

    cmd += ["--renderer", job.renderer]
    cmd += ["--frame", str(job.chunk.start)]
    cmd += ["--frame-count", str(job.chunk.count)]
    if job.chunk.inc != 1:
        cmd += ["--frame-inc", str(job.chunk.inc)]

    if job.settings_prim:
        cmd += ["--settings", job.settings_prim]
    if job.camera:
        cmd += ["--camera", job.camera]
    if job.output:
        cmd += ["--output", job.output]
    if job.resolution:
        cmd += ["--res", str(job.resolution[0]), str(job.resolution[1])]
    if job.threads:
        cmd += ["--threads", str(job.threads)]
    if job.complexity:
        cmd += ["--complexity", job.complexity]
    if job.purpose:
        cmd += ["--purpose", job.purpose]
    if job.snapshot_interval:
        cmd += ["--snapshot", str(job.snapshot_interval)]

    # NB: AOV selection is NOT a husk flag. Which planes a render writes is
    # defined by each UsdRenderProduct's orderedVars in the USD, so AOV editing
    # is done by pointing job.usd_file at an overlay from inspector.filter_usd_aovs
    # (see bridge.filter_aovs). Do not add a --aov/--skip-aov flag here — husk
    # has none and rejects unknown options.

    if job.make_output_path:
        cmd += ["--make-output-path"]
    if job.fast_exit:
        cmd += ["--fast-exit", "1"]

    # husk verbosity is a single token: a numeric level plus optional flag
    # letters, where 'a' switches progress reporting to Alfred style
    # (`ALF_PROGRESS n%`), which is what parse_progress() reads.
    verbosity = job.verbosity or "1"
    if job.alfred_progress and "a" not in verbosity:
        verbosity += "a"
    cmd += ["--verbose", verbosity]

    cmd += list(job.extra_args)
    cmd += [job.usd_file]
    return cmd


def format_command(cmd: Sequence[str]) -> str:
    """Human-readable, copy-pasteable version of an argv list."""
    out = []
    for arg in cmd:
        if any(ch in arg for ch in ' \t"\'$&|<>()'):
            out.append('"%s"' % arg.replace('"', '\\"'))
        else:
            out.append(arg)
    return " ".join(out)


def jobs_for_rop(manifest: SceneManifest, rop: RenderRop, usd_file: str = "",
                 *, chunk_size: int = 0, **overrides) -> list[RenderJob]:
    """Build one RenderJob per frame chunk, seeded from the manifest.

    Any RenderJob field can be forced via keyword, e.g.
    ``jobs_for_rop(m, rop, usd, renderer="BRAY_HdKarmaXPU", resolution=(960, 540))``.
    """
    settings = manifest.resolve_settings(rop)

    defaults = {
        "engine": overrides.get("engine", "husk"),
        "hip_file": manifest.hip_path,
        "rop_path": rop.node_path,
        "renderer": rop.renderer or "BRAY_HdKarma",
        "settings_prim": rop.settings_prim or manifest.default_settings_prim,
        "camera": rop.camera or (settings.camera if settings else ""),
        "output": rop.output_override,
    }
    defaults.update({k: v for k, v in overrides.items() if v is not None})

    if rop.use_frame_range:
        chunks = frame_chunks(rop.frame_start, rop.frame_end,
                              rop.frame_inc, chunk_size)
    else:
        chunks = [FrameChunk(rop.frame_start, 1, 1)]

    return [RenderJob(usd_file=usd_file, chunk=chunk, **defaults)
            for chunk in chunks]


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

def parse_progress(line: str) -> Optional[int]:
    """Extract a 0-100 percentage from a line of husk output, else None."""
    match = _ALF_PROGRESS.search(line)
    return int(match.group(1)) if match else None


def looks_like_error(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in
               ("error:", "fatal", "unable to", "cannot open", "no such file",
                "failed to", "license error"))
