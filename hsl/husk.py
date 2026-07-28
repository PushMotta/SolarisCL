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

from .manifest import TASK_RENDER, OutputTask, RenderRop, SceneManifest

# husk emits `ALF_PROGRESS 42%` when run with -Valfred.
_ALF_PROGRESS = re.compile(r"ALF_PROGRESS\s+(\d+)\s*%")
# Frame tokens husk expands in an output path. From `husk --help`:
#   $F, $FF, $F4   current frame number
#   $N             the N'th frame in the sequence
#   <F>, <FF>, <F4>  frame, UDIM style
#   %d, %g, %04d   frame, printf style
# `$FF` and `%g` are float forms whose exact spelling is unconfirmed, and `$N`
# needs the sequence index rather than the frame, so they are matched only to be
# *detected* (see `has_unexpanded_tokens`) and never guessed at.
_FRAME_TOKEN = re.compile(
    r"\$\{F(\d*)\}"        # ${F4}
    r"|\$F(?!F)(\d*)"      # $F, $F4   -- not $FF
    r"|<F(?!F)(\d*)>"      # <F>, <F4> -- not <FF>
    r"|%(0?\d*)d"          # %d, %04d
)
_SEQUENCE_TOKEN = re.compile(r"\$N")
# Anything still token-shaped after expansion: we cannot say what file it means.
_UNEXPANDED = re.compile(r"\$\{?F|\$N|<F|%\d*[dg]")
# `husk --list-renderers` prints one delegate per line, sometimes indented.
_RENDERER_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_:.\-]*)\s*$")

DEFAULT_RENDERERS = [
    "BRAY_HdKarma",
    "BRAY_HdKarmaXPU",
]

# The engine used when the caller does not name one.
#
# "hython" renders the ROP directly, with **no USD export**. That is the safe
# default: exporting a volume-heavy stage bakes the live volumes into the USD
# at tens of GB per frame (see inspector.scan_live_volumes), and the export is
# pure overhead for a single-machine render. Choose "husk" explicitly when you
# want the USD on disk -- for farm submission, AOV filtering or asset relinking,
# all of which operate on the exported USD.
DEFAULT_ENGINE = "hython"


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
    engine: str = DEFAULT_ENGINE         # "husk" or "hython"
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
    # Output paths this job should produce, frame tokens still in place. Used
    # only to catch a render that exits 0 having written nothing; empty means
    # "unknown", and the check is then skipped rather than guessed at.
    expected_outputs: list[str] = field(default_factory=list)
    # Directories to search for unresolved assets (hython engine only -- the
    # husk path relinks the exported USD before the job is ever built).
    relink_dirs: list[str] = field(default_factory=list)
    # Render-settings attributes to override, e.g.
    # {"karma:global:samplesperpixel": "64"} (hython engine only -- the husk
    # path authors them into the USD before the job is built).
    settings_overrides: dict = field(default_factory=dict)
    # Which OutputTask this job is a chunk of, and what that task waits for.
    # Every chunk of one task shares both. Empty ``task_id`` means the job
    # stands alone, which is what the Solaris path has always done -- so the
    # queue behaves exactly as before unless these are set.
    task_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    # What this job cooks. Anything other than a render goes to the inspector's
    # --cook path instead of --render-direct.
    task_kind: str = TASK_RENDER
    # Frame N depends on N-1, so the chunk must be cooked in one ordered call.
    sequential: bool = False

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
        cooking = job.task_kind != TASK_RENDER
        mode = "--cook" if cooking else "--render-direct"
        cmd: list[str] = [exe, "-m", "hsl.inspector",
                          job.hip_file or job.usd_file, mode]
        if job.rop_path:
            cmd += ["--rop", job.rop_path]
        cmd += ["--frame-start", str(job.chunk.start)]
        cmd += ["--frame-count", str(job.chunk.count)]
        if job.chunk.inc != 1:
            cmd += ["--frame-inc", str(job.chunk.inc)]
        if cooking:
            # A cache has no renderer, camera, resolution or render settings.
            # Emitting them would be noise at best and a wrong override at
            # worst, so the cook path takes only what it can actually use.
            if job.sequential:
                cmd += ["--sequential"]
            if job.output:
                cmd += ["--output", job.output]
            cmd += list(job.extra_args)
            return cmd
        if job.renderer:
            cmd += ["--renderer", job.renderer]
        if job.camera:
            cmd += ["--camera", job.camera]
        if job.output:
            cmd += ["--output", job.output]
        if job.resolution:
            cmd += ["--res", str(job.resolution[0]), str(job.resolution[1])]
        for directory in job.relink_dirs:
            cmd += ["--search", directory]
        for key, value in job.settings_overrides.items():
            cmd += ["--set", f"{key}={value}"]
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
        "engine": overrides.get("engine", DEFAULT_ENGINE),
        "hip_file": manifest.hip_path,
        "rop_path": rop.node_path,
        "renderer": rop.renderer or "BRAY_HdKarma",
        "settings_prim": rop.settings_prim or manifest.default_settings_prim,
        "camera": rop.camera or (settings.camera if settings else ""),
        "output": rop.output_override,
    }
    defaults.update({k: v for k, v in overrides.items() if v is not None})

    # What this job should leave on disk, so the runner can tell a silent
    # no-output render from a real one. An explicit output override wins;
    # otherwise the products declared by the settings prim.
    if "expected_outputs" not in overrides:
        chosen = defaults.get("output") or ""
        if chosen:
            defaults["expected_outputs"] = [chosen]
        elif settings:
            defaults["expected_outputs"] = manifest.outputs_for(settings)
        else:
            defaults["expected_outputs"] = []

    if rop.use_frame_range:
        chunks = frame_chunks(rop.frame_start, rop.frame_end,
                              rop.frame_inc, chunk_size)
    else:
        chunks = [FrameChunk(rop.frame_start, 1, 1)]

    return [RenderJob(usd_file=usd_file, chunk=chunk, **defaults)
            for chunk in chunks]


def chunks_for_task(task: OutputTask, chunk_size: int = 0) -> list[FrameChunk]:
    """Frame chunks for a task, refusing to split a sequential one.

    ``chunk_size`` is **ignored** rather than honoured when ``task.sequential``
    is set. A simulation's frame N depends on N-1, so handing chunks to
    separate processes gives each one a cold start at its own boundary: the
    cook does not fail, it writes a wrong result and exits 0.

    Quietly doing the safe thing is the right trade here -- the alternative is
    a flag that looks respected and corrupts caches. Callers that want to tell
    the user their ``--chunk`` was dropped can read ``task.sequential``, which
    is exactly what decided it.
    """
    if not task.use_frame_range:
        return [FrameChunk(task.frame_start, 1, 1)]
    return frame_chunks(task.frame_start, task.frame_end, task.frame_inc,
                        0 if task.sequential else chunk_size)


def jobs_for_task(manifest: SceneManifest, task: OutputTask, *,
                  chunk_size: int = 0, **overrides) -> list[RenderJob]:
    """Build one RenderJob per frame chunk for any cookable node.

    The counterpart to :func:`jobs_for_rop` for work that is not a Solaris
    render ROP. Every chunk carries the task's identity and dependencies, so
    the queue can order them.

    Non-render kinds are pinned to the hython engine: husk consumes USD and
    cannot cook a SOP or advance a solver. Asking for it raises rather than
    silently falling back, which is how the CLI already treats the other
    husk-only options.
    """
    engine = overrides.get("engine") or DEFAULT_ENGINE
    if task.kind != TASK_RENDER and engine != "hython":
        raise ValueError(
            f"{task.node_path} is a '{task.kind}' task and husk cannot cook it "
            f"-- husk only consumes USD. Use engine='hython'."
        )

    defaults = {
        "engine": engine,
        "hip_file": manifest.hip_path,
        "rop_path": task.node_path,
        "task_id": task.node_path,
        "depends_on": list(task.depends_on),
        "expected_outputs": list(task.outputs),
        "task_kind": task.kind,
        "sequential": task.sequential,
    }
    if task.kind != TASK_RENDER:
        # A renderer name is meaningless for a cache or a solver, and
        # RenderJob defaults it to Karma.
        defaults["renderer"] = ""
    defaults.update({k: v for k, v in overrides.items() if v is not None})

    return [RenderJob(chunk=chunk, **defaults)
            for chunk in chunks_for_task(task, chunk_size)]


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

def expand_frame_token(path: str, frame: int, index: Optional[int] = None) -> str:
    """Replace frame tokens in ``path``: ``$F4`` at frame 7 -> ``0007``.

    Covers the spellings husk documents and we can resolve unambiguously --
    ``$F`` / ``$F4`` / ``${F4}``, the UDIM-style ``<F>`` / ``<F4>``, and printf
    ``%d`` / ``%04d``. ``$N`` (the N'th frame *of the sequence*, not the frame
    number) is expanded only when ``index`` is given.

    Tokens whose meaning is not certain -- ``$FF``, ``%g`` -- are deliberately
    left alone rather than guessed at. Use :func:`has_unexpanded_tokens` to find
    out whether the result is a real path or still a template; guessing here
    would invent a filename that no render ever writes.
    """
    def _sub(match) -> str:
        digits = next((g for g in match.groups() if g is not None), "")
        return str(frame).zfill(int(digits) if digits else 1)

    expanded = _FRAME_TOKEN.sub(_sub, path)
    if index is not None:
        expanded = _SEQUENCE_TOKEN.sub(str(index), expanded)
    return expanded


def has_unexpanded_tokens(path: str) -> bool:
    """True if ``path`` still holds a token, i.e. it is not a real filename yet."""
    return bool(_UNEXPANDED.search(path))


def has_frame_token(path: str) -> bool:
    """True if ``path`` varies from frame to frame.

    A multi-frame render whose output has no frame token writes every frame to
    the same file, so only the last one survives.
    """
    return bool(_FRAME_TOKEN.search(path) or _SEQUENCE_TOKEN.search(path))


def parse_setting_args(items) -> dict:
    """Turn ``["karma:global:samplesperpixel=64", …]`` into a dict.

    Values stay strings: only the stage knows what type a knob is, so the
    conversion happens there (``inspector._coerce_setting``) rather than being
    guessed from how the text looks.
    """
    settings: dict = {}
    for item in items or []:
        key, sep, value = str(item).partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"Cannot read {item!r} as a setting. Use KEY=VALUE, e.g. "
                f"karma:global:samplesperpixel=64.")
        settings[key] = value.strip()
    return settings


def planned_product_paths(products, output: str) -> list:
    """Where each render product ends up, given an ``--output`` override.

    ``products`` is ``[(prim_path, authored_product_name)]`` in stage order;
    the return is ``[(prim_path, new_path)]``.

    This is the single definition of the rule, shared by the preview and by
    ``inspector.override_product_paths()`` which actually authors it. Two
    copies would drift, and a preview that disagrees with the render is worse
    than no preview.

    * ``output`` naming a **file** gives that exact path to the first product
      and puts the rest alongside it under their own filenames.
    * ``output`` naming a **directory** (trailing separator, or one that exists)
      keeps every product's own filename.
    * A product with no authored name is called after its prim, keeping
      ``output``'s extension.
    """
    as_dir = output.endswith(("/", "\\")) or os.path.isdir(output)
    directory = output if as_dir else (os.path.dirname(output) or ".")
    extension = "" if as_dir else os.path.splitext(output)[1]

    planned = []
    for index, (prim_path, current) in enumerate(products):
        if not as_dir and index == 0:
            new_path = output
        else:
            base = os.path.basename((current or "").replace("\\", "/"))
            if not base:
                base = prim_path.rsplit("/", 1)[-1] + (extension or ".exr")
            new_path = os.path.join(directory, base)
        planned.append((prim_path, new_path.replace(os.sep, "/")))
    return planned


def planned_outputs(manifest, rop, output: str = "", frames=()) -> list:
    """The files a render will actually write — for showing before it starts.

    Returns one entry per product::

        {"product": prim, "template": path, "files": [...], "unresolved": bool}

    ``unresolved`` marks a template holding a token we cannot expand, so the
    caller can say "cannot preview this" instead of inventing a filename.
    An empty list means the scene declares no output path at all — which is
    normal, and means husk/the ROP decides it (real shots often author no
    ``productName``).
    """
    settings = manifest.resolve_settings(rop)
    products = []
    if settings:
        for prim_path in settings.products:
            product = manifest.product(prim_path)
            if product is not None:
                products.append((product.prim_path, product.product_name))

    chosen = output or rop.output_override
    if chosen:
        if products:
            pairs = planned_product_paths(products, chosen)
        elif chosen.endswith(("/", "\\")) or os.path.isdir(chosen):
            # A folder, and nothing in the scene declares a filename to put in
            # it. Naming the files here would be invention, so say only what is
            # known: the destination.
            return [{"product": "(from --output)",
                     "template": chosen.replace(os.sep, "/"),
                     "files": [], "unresolved": False}]
        else:
            pairs = [("(from --output)", chosen.replace(os.sep, "/"))]
    else:
        pairs = [(p, name) for p, name in products if name]

    entries = []
    for prim_path, template in pairs:
        files: list = []
        unresolved = False
        for index, frame in enumerate(frames or (), 1):
            path = expand_frame_token(template, frame, index)
            if has_unexpanded_tokens(path):
                unresolved = True
                break
            # A template with no frame token names one file however many frames
            # are rendered -- listing it once per frame would overstate the job.
            if path not in files:
                files.append(path)
        entries.append({"product": prim_path, "template": template,
                        "files": files, "unresolved": unresolved})
    return entries


def parse_progress(line: str) -> Optional[int]:
    """Extract a 0-100 percentage from a line of husk output, else None."""
    match = _ALF_PROGRESS.search(line)
    return int(match.group(1)) if match else None


def looks_like_error(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in
               ("error:", "fatal", "unable to", "cannot open", "no such file",
                "failed to", "license error"))
