"""Scene manifest schema.

This module is the contract between the two halves of the tool:

  * ``hsl.inspector`` runs under **hython**, opens a .hip, cooks the LOP
    network and emits one of these as JSON.
  * ``hsl.husk`` / ``hsl.ui`` run under **plain Python** and consume it.

It must therefore import nothing beyond the standard library -- no ``hou``,
no ``pxr``, no Qt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from typing import Any, Optional

# 2 added ``SceneManifest.tasks``. The change is additive: a v1 manifest still
# loads (``tasks`` defaults to empty) and nothing gates behaviour on this
# number, so it is here to describe the shape, not to reject anything.
# 3 added ``SceneManifest.inspected_frame`` -- additive likewise.
# 4 added ``SceneManifest.footprint``. Additive too: a v3 manifest loads with
# ``footprint`` None, which reads as "nobody scanned this", not as "empty".
SCHEMA_VERSION = 4


# --------------------------------------------------------------------------
# USD-side description (read from the composed stage)
# --------------------------------------------------------------------------

@dataclass
class RenderVar:
    """A UsdRenderVar -- one AOV."""
    prim_path: str
    source_name: str = ""
    source_type: str = ""        # "raw", "primvar", "lpe", ...
    data_type: str = ""          # "color3f", "float", ...
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.source_name or self.prim_path.rsplit("/", 1)[-1]


@dataclass
class RenderProduct:
    """A UsdRenderProduct -- one output file."""
    prim_path: str
    product_name: str = ""       # the output path husk will write
    product_type: str = "raster"
    ordered_vars: list[str] = field(default_factory=list)   # RenderVar prim paths


@dataclass
class RenderSettings:
    """A UsdRenderSettings prim."""
    prim_path: str
    resolution: Optional[tuple[int, int]] = None
    pixel_aspect_ratio: Optional[float] = None
    aspect_ratio_conform_policy: str = ""
    camera: str = ""                                   # camera prim path
    products: list[str] = field(default_factory=list)  # RenderProduct prim paths
    included_purposes: list[str] = field(default_factory=list)
    instantaneous_shutter: Optional[bool] = None
    # Delegate-specific knobs: karma:*, ri:*, arnold:*, driver:* etc.
    renderer_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Camera:
    """A UsdGeomCamera."""
    prim_path: str
    focal_length: Optional[float] = None
    horizontal_aperture: Optional[float] = None
    vertical_aperture: Optional[float] = None
    near_clip: Optional[float] = None
    far_clip: Optional[float] = None
    f_stop: Optional[float] = None
    focus_distance: Optional[float] = None
    shutter_open: Optional[float] = None
    shutter_close: Optional[float] = None

    @property
    def label(self) -> str:
        return self.prim_path.rsplit("/", 1)[-1]


@dataclass
class MissingAsset:
    """An asset-path attribute on the stage that does not resolve on disk.

    ``attr_path`` is the exact attribute holding the reference (e.g.
    ``/mat/tex.inputs:file``), which is what relinking needs to repath it.
    """
    attr_path: str
    asset_path: str          # the authored, unresolved path
    kind: str = "texture"    # texture / reference / volume / ...

    @property
    def basename(self) -> str:
        return self.asset_path.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class LiveVolume:
    """A volume prim whose field data is live in the stage -- no .vdb to reference.

    Its ``UsdVolOpenVDBAsset`` fields carry an empty ``filePath``, so the data
    is embedded (SOP-imported) rather than pointing at a cache on disk. A USD
    export then *bakes* those voxels into the exported layer -- tens of GB per
    frame observed on a real shot, versus a few MB when a ``.vdb`` is
    referenced. Only the **husk** (USD-export) engine pays this cost; the
    hython-direct engine renders the live data without exporting. Produced by
    ``inspector.scan_live_volumes``.
    """
    prim_path: str                                        # the UsdVolVolume prim
    field_count: int = 0                                  # live OpenVDBAsset fields under it
    field_names: list[str] = field(default_factory=list)  # e.g. ["density", "vel"]

    @property
    def label(self) -> str:
        return self.prim_path.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# Houdini-side description (read from node parameters)
# --------------------------------------------------------------------------

@dataclass
class RenderRop:
    """A USD Render ROP (or equivalent) found in the .hip."""
    node_path: str
    node_type: str
    input_lop: str = ""                 # the LOP whose stage feeds this ROP
    renderer: str = ""                  # e.g. BRAY_HdKarmaXPU
    settings_prim: str = ""             # RenderSettings prim path, may be empty
    camera: str = ""
    frame_start: int = 1
    frame_end: int = 1
    frame_inc: int = 1
    use_frame_range: bool = False       # False => single (current) frame
    output_override: str = ""           # ROP-level output path override
    usd_path: str = ""                  # populated once the stage is exported
    # Everything the inspector managed to read, for debugging / display.
    raw_parms: dict[str, Any] = field(default_factory=dict)

    @property
    def frame_count(self) -> int:
        if not self.use_frame_range:
            return 1
        inc = self.frame_inc or 1
        return max(1, (self.frame_end - self.frame_start) // inc + 1)

    @property
    def label(self) -> str:
        return self.node_path


# --------------------------------------------------------------------------
# Generalised work description -- any ROP, not only Solaris
# --------------------------------------------------------------------------

# What a task produces. Anything the inspector cannot classify stays
# ``unknown`` rather than being guessed into a bucket.
TASK_RENDER = "render"
TASK_CACHE = "cache"
TASK_SIM = "sim"
TASK_UNKNOWN = "unknown"

# Kinds whose frames are *not* independent -- see ``OutputTask.sequential``.
SEQUENTIAL_KINDS = frozenset({TASK_SIM})


@dataclass
class OutputTask:
    """One node that can be cooked to produce files, and when it may run.

    Generalises :class:`RenderRop`. A USD render ROP, a Mantra ROP, a File
    Cache SOP and a DOP simulation are all "cook this node over these frames,
    then check what it wrote" -- only the description differs. ``RenderRop``
    keeps the USD-specific detail; this is the *scheduling* unit, and the only
    thing the queue needs to understand.
    """
    node_path: str
    node_type: str = ""
    kind: str = TASK_UNKNOWN
    frame_start: int = 1
    frame_end: int = 1
    frame_inc: int = 1
    use_frame_range: bool = False       # False => single (current) frame
    # Output path templates, frame tokens ($F4, $SF, ...) still in place.
    outputs: list[str] = field(default_factory=list)
    # True when frame N depends on N-1, so the range must be cooked in one
    # process, in order. Splitting such a task across chunks does not merely
    # run slowly: each process starts cold at its chunk boundary and writes a
    # result that is wrong with no error to say so.
    sequential: bool = False
    # ``node_path`` of every task that must finish before this one starts.
    depends_on: list[str] = field(default_factory=list)
    # Everything the inspector managed to read, for debugging / display.
    raw_parms: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A simulation claiming its frames are independent is not a preference
        # to be respected, it is a bug -- so it cannot be expressed at all.
        if self.kind in SEQUENTIAL_KINDS:
            self.sequential = True

    @property
    def frame_count(self) -> int:
        if not self.use_frame_range:
            return 1
        inc = self.frame_inc or 1
        return max(1, (self.frame_end - self.frame_start) // inc + 1)

    @property
    def label(self) -> str:
        return self.node_path


@dataclass
class SceneFootprint:
    """What a scene *contains* that drives render memory.

    Facts about the scene, each labelled with exactly what it counts. This is
    deliberately **not** a prediction of what a render will need: memory
    depends on the delegate, the Houdini build, texture-cache budgets and BVH
    constants nobody publishes, so a total here would be an invented number
    that someone would then plan a farm around. What it answers instead is
    "what is heavy in this scene, and by how much" -- which is the question an
    artist can actually act on.

    ``None`` means *not measured* -- the scan was skipped, or the value could
    not be read. It never means zero. Counts that are genuinely zero are 0.

    Pairs with the measured history in :mod:`hsl.memlog`: recording what a
    scene contained next to what its render really used is what could one day
    make a *calibrated* estimate possible, from real data rather than recall.
    """
    # -- volumes ----------------------------------------------------------
    volume_count: int = 0
    # Voxels actually stored. VDB is sparse, so this is the active set, not
    # the bounding-box product.
    active_voxels: Optional[int] = None
    # ``active_voxels`` times each grid's own value size: the voxel data
    # itself, uncompressed, with no renderer overhead of any kind included.
    # A lower bound on what volumes cost, never the whole cost.
    voxel_bytes: Optional[int] = None

    # -- textures ---------------------------------------------------------
    texture_count: int = 0
    # Bytes **on disk**, not in memory. A compressed EXR expands when loaded,
    # while a bounded texture cache may never hold all of it at once, so this
    # is neither an upper nor a lower bound on texture memory. It is the one
    # honest number available without loading every file.
    texture_bytes: Optional[int] = None

    # -- geometry ---------------------------------------------------------
    point_count: Optional[int] = None
    prim_count: Optional[int] = None
    # Point-instancer instances, which can be nearly free or ruinous
    # depending on the delegate -- counted, deliberately not weighted.
    instance_count: Optional[int] = None

    # -- framebuffer ------------------------------------------------------
    # The one term that is exact arithmetic rather than a measurement:
    # width * height * channels * bytes-per-channel, summed over products.
    framebuffer_bytes: Optional[int] = None

    # -- provenance -------------------------------------------------------
    scanned: bool = False               # False => nothing here was measured
    # What could not be counted, and why. The channel for "this scene has
    # volumes but their voxel counts were unreadable" -- never a silent gap.
    skipped: list[str] = field(default_factory=list)
    seconds: float = 0.0                # what the scan cost, for the caller

    @property
    def heaviest(self) -> str:
        """Which term dominates, by the bytes actually known. "" if unknown."""
        known = {"volume data": self.voxel_bytes,
                 "textures on disk": self.texture_bytes,
                 "framebuffer": self.framebuffer_bytes}
        known = {k: v for k, v in known.items() if v}
        return max(known, key=known.get) if known else ""


@dataclass
class SceneManifest:
    """Everything the launcher needs to know about one .hip file."""
    schema_version: int = SCHEMA_VERSION
    hip_path: str = ""
    houdini_version: str = ""
    fps: float = 24.0
    stage_start_time_code: Optional[float] = None
    stage_end_time_code: Optional[float] = None
    # The frame the stage was cooked at when this description was taken. A
    # stage whose structure changes over time looks different at another frame
    # (TASKS.md T5) -- consumers can tell *which* moment they are looking at.
    inspected_frame: Optional[float] = None
    default_settings_prim: str = ""     # stage metadata 'renderSettingsPrimPath'
    rops: list[RenderRop] = field(default_factory=list)
    # Every cookable node, render or otherwise. ``rops`` stays the USD-specific
    # view of the subset that are Solaris render ROPs.
    tasks: list[OutputTask] = field(default_factory=list)
    settings: list[RenderSettings] = field(default_factory=list)
    products: list[RenderProduct] = field(default_factory=list)
    vars: list[RenderVar] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    missing_assets: list[MissingAsset] = field(default_factory=list)
    live_volumes: list[LiveVolume] = field(default_factory=list)
    # What the scene contains that drives memory. None until something scans
    # it -- the scan costs real time on a heavy stage, so it is opt-in.
    footprint: Optional[SceneFootprint] = None
    warnings: list[str] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------

    def rop(self, node_path: str) -> Optional[RenderRop]:
        return next((r for r in self.rops if r.node_path == node_path), None)

    def task(self, node_path: str) -> Optional[OutputTask]:
        return next((t for t in self.tasks if t.node_path == node_path), None)

    def tasks_of_kind(self, *kinds: str) -> list[OutputTask]:
        return [t for t in self.tasks if t.kind in kinds]

    def settings_for(self, prim_path: str) -> Optional[RenderSettings]:
        return next((s for s in self.settings if s.prim_path == prim_path), None)

    def product(self, prim_path: str) -> Optional[RenderProduct]:
        return next((p for p in self.products if p.prim_path == prim_path), None)

    def var(self, prim_path: str) -> Optional[RenderVar]:
        return next((v for v in self.vars if v.prim_path == prim_path), None)

    def resolve_settings(self, rop: RenderRop) -> Optional[RenderSettings]:
        """Best guess at which RenderSettings prim a ROP will actually use."""
        for candidate in (rop.settings_prim, self.default_settings_prim):
            if candidate:
                found = self.settings_for(candidate)
                if found:
                    return found
        return self.settings[0] if len(self.settings) == 1 else None

    def outputs_for(self, settings: RenderSettings) -> list[str]:
        """Output file paths declared by a settings prim's products."""
        paths = []
        for prim_path in settings.products:
            product = self.product(prim_path)
            if product and product.product_name:
                paths.append(product.product_name)
        return paths

    def aovs_for(self, settings: RenderSettings) -> list[RenderVar]:
        seen, out = set(), []
        for prim_path in settings.products:
            product = self.product(prim_path)
            if not product:
                continue
            for var_path in product.ordered_vars:
                if var_path in seen:
                    continue
                seen.add(var_path)
                found = self.var(var_path)
                if found:
                    out.append(found)
        return out

    # -- serialisation ----------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=_fallback)

    @classmethod
    def from_json(cls, text: str) -> "SceneManifest":
        return _build(cls, json.loads(text))


def _fallback(obj: Any) -> Any:
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


# Field name -> element dataclass. Name-based lookup rather than parsing type
# annotations, which arrive as strings under ``from __future__ import
# annotations``.
_ELEMENT_TYPES = {
    "rops": RenderRop,
    "tasks": OutputTask,
    "settings": RenderSettings,
    "products": RenderProduct,
    "vars": RenderVar,
    "cameras": Camera,
    "missing_assets": MissingAsset,
    "live_volumes": LiveVolume,
}

# Fields holding **one** nested dataclass rather than a list of them. Without
# this a round trip would hand back a plain dict, and every attribute access
# on it would raise somewhere far from the cause.
_SINGLE_TYPES = {
    "footprint": SceneFootprint,
}


def _build(cls, data: Any):
    """Rebuild nested dataclasses from plain dicts."""
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        inner = _ELEMENT_TYPES.get(f.name)
        single = _SINGLE_TYPES.get(f.name)
        if inner is not None and isinstance(value, list):
            value = [_build(inner, v) for v in value]
        elif single is not None and isinstance(value, dict):
            value = _build(single, value)
        elif f.name == "resolution" and isinstance(value, list):
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)
