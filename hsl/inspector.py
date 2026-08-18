"""Scene introspection. **Must be run under hython**, not system Python.

    hython -m hsl.inspector /path/shot.hip --json /tmp/shot.json --export-usd

The design principle here is *probe, don't assume*. Parameter names on
``usdrender_rop`` have drifted across Houdini 19.0 -> 20.5, and third-party
delegates add their own. So every parameter read goes through :func:`_parm`,
which tries a list of candidate names and quietly returns a default. The
authoritative information comes from the composed USD stage, which is stable;
node parameters are treated as hints for pre-filling the UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any, Iterable, Optional

from .manifest import (
    TASK_CACHE, TASK_RENDER, TASK_SIM, TASK_UNKNOWN, Camera, LiveVolume,
    MissingAsset, OutputTask, RenderProduct, RenderRop, RenderSettings,
    RenderVar, SceneFootprint, SceneManifest,
)
# Plain-Python, no Houdini: the output-naming rule, shared with the UI/CLI
# preview so the two can never disagree about where a render lands.
from .husk import parse_setting_args as _parse_settings, planned_product_paths

try:
    import hou
except ImportError:  # pragma: no cover - only importable inside hython
    hou = None

try:
    from pxr import Sdf, Usd, UsdGeom, UsdRender, UsdVol
except ImportError:  # pragma: no cover
    Sdf = Usd = UsdGeom = UsdRender = UsdVol = None


# Node types that can drive a husk render. Versioned type names
# (``usdrender_rop::3.0``) are normalised by stripping at "::".
RENDER_ROP_TYPES = {"usdrender_rop", "usdrender", "karma"}

# --- Non-Solaris work -----------------------------------------------------
# Everything below was probed on **21.0.729 and 22.0.368**, which agreed
# exactly; see docs/UNVERIFIED.md for the transcript and how to re-run it.
#
# Driver-context types that write files, and what each produces. Every one of
# these exposes ``.render()``.
TASK_ROP_KINDS = {
    "geometry": TASK_CACHE,
    "rop_geometry": TASK_CACHE,
    "alembic": TASK_CACHE,
    "filmboxfbx": TASK_CACHE,
    "channel": TASK_CACHE,
    "dop": TASK_SIM,
    "ifd": TASK_RENDER,          # Mantra
    "opengl": TASK_RENDER,
    "comp": TASK_RENDER,
    "baketexture": TASK_RENDER,
    "karma": TASK_RENDER,
    "usdrender_rop": TASK_RENDER,
    "usdrender": TASK_RENDER,
}

# SOP-level cache nodes. These have **no** ``.render()`` of their own -- they
# wrap a ROP that does. See ``_cookable_node``.
SOP_CACHE_KINDS = {"filecache": TASK_CACHE}

# Where each type keeps its output path. Read through ``_parm``, so a name
# that moved between builds falls back rather than raising.
TASK_OUTPUT_PARMS = {
    "geometry": ("sopoutput",),
    "rop_geometry": ("sopoutput",),
    "filecache": ("file", "sopoutput"),
    "dop": ("dopoutput",),
    "alembic": ("filename",),
    "ifd": ("vm_picture",),
    "opengl": ("picture",),
    "comp": ("copoutput",),
    "baketexture": ("vm_uvoutputpicture1",),
}

# Parameters that mean "this node carries state from one frame to the next".
# ``cachesim`` defaults to 1 on filecache::2.0, so a stock File Cache SOP is
# treated as sequential -- deliberately conservative: a wrongly parallelised
# cache is silently wrong, a needlessly serial one is merely slower.
SEQUENTIAL_PARMS = ("cachesim", "initsim")
# NB: there is no USD_ROP_TYPES list. `export_usd()` creates its own temporary
# `usd_rop` rather than looking for one in the scene, so a set of candidate
# export-ROP names had nothing to match against (docs/TASKS.md T4).

# Attribute namespaces worth capturing off a RenderSettings prim.
DELEGATE_NAMESPACES = ("karma", "ri", "arnold", "cycles", "driver", "husk")


# --------------------------------------------------------------------------
# Defensive parameter access
# --------------------------------------------------------------------------

def _type_name(node) -> str:
    return node.type().name().split("::", 1)[0]


def _parm(node, names, default=None):
    """Evaluate the first parameter in ``names`` that exists on ``node``."""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        parm = node.parm(name)
        if parm is None:
            parm = node.parmTuple(name)
        if parm is None:
            continue
        try:
            return parm.eval()
        except Exception:
            continue
    return default


def _set_parm(node, name, value) -> bool:
    parm = node.parm(name)
    if parm is None:
        return False
    try:
        parm.set(value)
        return True
    except Exception:
        return False


def _force_parm(node, name, value) -> bool:
    """Set a parameter that may carry a **default expression**, and check it took.

    Houdini ships ``usd_rop``'s ``f1`` / ``f2`` as the expressions ``$FSTART`` /
    ``$FEND``. ``parm.set()`` does not beat an expression: the raw value stays
    ``$FSTART``, the parm keeps evaluating to the playbar -- and nothing raises,
    so :func:`_set_parm` reports ``True``. The miss is invisible on both channels
    this module watches, which is how ``export_usd(frame_range=...)`` came to
    cover the whole playbar however narrow a range was asked for (UNVERIFIED.md
    C8; the same defect as K4 on the File Cache SOP).

    ``deleteAllKeyframes()`` removes the expression -- verified on 22.0.368 for
    ``f1``/``f2``/``f3``, and harmless on the parms that carry none. The set is
    then *read back*, so a build where this stops working reports ``False`` and
    the caller's warning fires instead of exporting the wrong frames in silence.
    """
    parm = node.parm(name)
    if parm is None:
        return False
    try:
        parm.deleteAllKeyframes()
    except Exception:
        pass                      # no expression to clear, or no such method
    if not _set_parm(node, name, value):
        return False
    try:
        actual = parm.eval()
    except Exception:
        return True               # cannot read it back; the set did not raise
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    try:
        return abs(float(actual) - float(value)) < 1e-6
    except (TypeError, ValueError):
        return True


def _apply_override(node, names, value, label: str, warnings: list) -> bool:
    """Set the first parameter in ``names`` that exists; record it if none do.

    ``_set_parm`` returns False when the parameter is absent. Discarding that
    return is how an override turns into a **silent no-op**: the render succeeds
    and quietly uses the scene's own value instead of the one that was asked
    for. Every override goes through here so that never happens unnoticed.
    """
    for name in names:
        if _set_parm(node, name, value):
            return True
    warnings.append(
        f"{node.path()}: no parameter {' / '.join(names)} on this build, so "
        f"{label}={value!r} was NOT applied; the scene's own value is used."
    )
    return False


def _flatten_token(node) -> str:
    """Return the usd_rop ``savestyle`` token that flattens into one layer.

    ``savestyle`` is a *string* menu, so ``parm.set()`` accepts an unknown
    token without raising and the ROP then ignores it silently — which is
    exactly how the old ``"flattenall"`` guess turned ``--flatten`` into a
    no-op. So choose a token that is genuinely in the menu rather than trusting
    a remembered spelling. ``flattenalllayers`` is confirmed present on Houdini
    21.0.729 and 22.0.368; ``flattenstage`` is the fuller-flatten fallback.
    """
    parm = node.parm("savestyle")
    if parm is None:
        return ""
    try:
        tokens = parm.parmTemplate().menuItems()
    except Exception:
        tokens = ()
    for token in ("flattenalllayers", "flattenstage"):
        if token in tokens:
            return token
    return ""


# --------------------------------------------------------------------------
# USD stage walking
# --------------------------------------------------------------------------

def _attr(prim, name):
    attr = prim.GetAttribute(name)
    if not attr or not attr.HasAuthoredValue():
        return None
    try:
        return attr.Get()
    except Exception:
        return None


def _delegate_settings(prim) -> dict[str, Any]:
    """Collect namespaced delegate knobs (karma:global:samplesperpixel etc)."""
    out: dict[str, Any] = {}
    for attr in prim.GetAttributes():
        name = attr.GetName()
        if ":" not in name:
            continue
        if not name.split(":", 1)[0] in DELEGATE_NAMESPACES:
            continue
        if not attr.HasAuthoredValue():
            continue
        try:
            value = attr.Get()
        except Exception:
            continue
        if value is None:
            continue
        # Coerce Gf/Vt types into something json can hold.
        if hasattr(value, "__len__") and not isinstance(value, str):
            try:
                value = list(value)
            except Exception:
                value = str(value)
        elif not isinstance(value, (int, float, bool, str)):
            value = str(value)
        out[name] = value
    return out


def _time_code(frame) -> Any:
    """``Usd.TimeCode(frame)``, or ``None`` when there is nothing to build it from.

    Every scan that reads an attribute takes its time code through here, so
    "read at the frame the manifest describes" has exactly one spelling.
    """
    if frame is None or Usd is None:
        return None
    try:
        return Usd.TimeCode(float(frame))
    except Exception:
        return None


def _attr_value(attr, time_code=None):
    """An attribute's value at ``time_code`` -- never a bare ``Get()``.

    Houdini authors volume fields (and plenty else) as **time samples with no
    default value**: measured on the production shot, ``filePath``,
    ``fieldName`` and ``fieldDataType`` are all ``HasAuthoredValue() == True``
    yet all return ``None`` from a plain ``Get()``, because the one sample sits
    at frame 1074 and there is no default underneath it. Reading at the wrong
    moment therefore does not raise and does not warn -- it quietly answers
    "empty", which is a different fact from "not authored" and led to exactly
    the wrong conclusion (see ``docs/UNVERIFIED.md`` N8 and N10).

    ``Get(timeCode)`` falls back to the default value when an attribute has no
    samples, so passing a frame is never worse than not passing one.
    """
    if attr is None or not attr:
        return None
    if time_code is not None:
        try:
            return attr.Get(time_code)
        except Exception:
            pass
    try:
        return attr.Get()
    except Exception:
        return None


def _is_live_asset_path(path: str) -> bool:
    """True when an asset path names live Houdini data rather than a file.

    An ``op:`` path is a reference back into the running session -- the SOP
    that holds the voxels -- so there is nothing on disk for a USD export to
    reference and the data bakes into the exported layer. An empty path is the
    same situation with no reference authored at all. Everything else names a
    file, whether or not the resolver can currently find it.
    """
    return not path or path.startswith("op:")


def _first_target(rel) -> str:
    if not rel:
        return ""
    targets = rel.GetTargets()
    return str(targets[0]) if targets else ""


def _targets(rel) -> list[str]:
    return [str(t) for t in rel.GetTargets()] if rel else []


def walk_stage(stage) -> tuple[list[RenderSettings], list[RenderProduct],
                               list[RenderVar], list[Camera]]:
    """Extract render-relevant prims from a composed stage."""
    settings: list[RenderSettings] = []
    products: list[RenderProduct] = []
    render_vars: list[RenderVar] = []
    cameras: list[Camera] = []

    for prim in stage.Traverse():
        path = str(prim.GetPath())

        if prim.IsA(UsdRender.Settings):
            node = UsdRender.Settings(prim)
            resolution = node.GetResolutionAttr().Get()
            settings.append(RenderSettings(
                prim_path=path,
                resolution=tuple(int(v) for v in resolution) if resolution is not None else None,
                pixel_aspect_ratio=node.GetPixelAspectRatioAttr().Get(),
                aspect_ratio_conform_policy=node.GetAspectRatioConformPolicyAttr().Get() or "",
                camera=_first_target(node.GetCameraRel()),
                products=_targets(node.GetProductsRel()),
                included_purposes=[str(p) for p in (node.GetIncludedPurposesAttr().Get() or [])],
                instantaneous_shutter=_attr(prim, "instantaneousShutter"),
                renderer_settings=_delegate_settings(prim),
            ))

        elif prim.IsA(UsdRender.Product):
            node = UsdRender.Product(prim)
            products.append(RenderProduct(
                prim_path=path,
                product_name=str(node.GetProductNameAttr().Get() or ""),
                product_type=str(node.GetProductTypeAttr().Get() or "raster"),
                ordered_vars=_targets(node.GetOrderedVarsRel()),
            ))

        elif prim.IsA(UsdRender.Var):
            node = UsdRender.Var(prim)
            render_vars.append(RenderVar(
                prim_path=path,
                source_name=str(node.GetSourceNameAttr().Get() or ""),
                source_type=str(node.GetSourceTypeAttr().Get() or ""),
                data_type=str(node.GetDataTypeAttr().Get() or ""),
            ))

        elif prim.IsA(UsdGeom.Camera):
            node = UsdGeom.Camera(prim)
            cameras.append(Camera(
                prim_path=path,
                focal_length=node.GetFocalLengthAttr().Get(),
                horizontal_aperture=node.GetHorizontalApertureAttr().Get(),
                vertical_aperture=node.GetVerticalApertureAttr().Get(),
                near_clip=(node.GetClippingRangeAttr().Get() or [None, None])[0],
                far_clip=(node.GetClippingRangeAttr().Get() or [None, None])[1],
                f_stop=node.GetFStopAttr().Get(),
                focus_distance=node.GetFocusDistanceAttr().Get(),
                shutter_open=node.GetShutterOpenAttr().Get(),
                shutter_close=node.GetShutterCloseAttr().Get(),
            ))

    return settings, products, render_vars, cameras


def scan_missing_assets(stage, time_code=None) -> list[MissingAsset]:
    """Asset-path attributes on the stage whose value does not resolve on disk.

    Every asset-valued attribute (texture ``inputs:file``, volume filenames,
    etc.) carries an ``Sdf.AssetPath`` with a ``resolvedPath``. An authored path
    with an **empty** ``resolvedPath`` is one the resolver could not find — a
    missing texture. This is exactly what husk would fail on mid-render, caught
    here at read time instead. Deduplicated on (attribute, path).

    ``time_code`` is the frame the manifest describes, and it is load-bearing:
    an asset authored **only** as a time sample -- which is how Houdini writes
    an animated reference -- reads as ``None`` at the default time code, so a
    missing file would simply never be reported. Under-reporting, not
    over-reporting, but the render still dies mid-frame on the thing preflight
    was meant to catch (``docs/UNVERIFIED.md`` N10).

    ``op:`` paths are skipped: they are live Houdini data, not files, so
    "unresolved" carries no meaning there. On 22.0.368 the resolver hands the
    ``op:`` string back as its own ``resolvedPath`` and they never reach this
    branch anyway -- the skip is what stops a build that answers differently
    from filling ``manifest.missing_assets`` with every volume in the scene.
    """
    missing: list[MissingAsset] = []
    seen: set = set()
    tc = _time_code(time_code)

    def _check(attr, asset):
        if asset is None:
            return
        path = getattr(asset, "path", "") or ""
        resolved = getattr(asset, "resolvedPath", "") or ""
        if not path or resolved or path.startswith("op:"):
            return
        key = (str(attr.GetPath()), path)
        if key in seen:
            return
        seen.add(key)
        missing.append(MissingAsset(attr_path=str(attr.GetPath()), asset_path=path))

    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            type_name = attr.GetTypeName()
            if type_name == Sdf.ValueTypeNames.Asset:
                _check(attr, _attr_value(attr, tc))
            elif type_name == Sdf.ValueTypeNames.AssetArray:
                for asset in (_attr_value(attr, tc) or []):
                    _check(attr, asset)

    return missing


def _owning_volume(prim) -> str:
    """Path of the nearest ``UsdVolVolume`` ancestor of a field prim.

    Solaris authors OpenVDB field prims as children of the volume prim, so the
    parent is normally the volume; walking up is just robustness for fields
    authored elsewhere. Falls back to the field prim's parent, then itself.
    """
    node = prim.GetParent()
    while node and node.IsValid() and not node.IsPseudoRoot():
        if UsdVol is not None and node.IsA(UsdVol.Volume):
            return str(node.GetPath())
        node = node.GetParent()
    parent = prim.GetParent()
    if parent and parent.IsValid() and not parent.IsPseudoRoot():
        return str(parent.GetPath())
    return str(prim.GetPath())


def scan_live_volumes(stage, time_code=None) -> list[LiveVolume]:
    """Volumes whose OpenVDB fields have no on-disk ``.vdb`` -- they bake on export.

    A ``UsdVolOpenVDBAsset`` whose ``filePath`` is **empty**, or is an ``op:``
    reference back into the running session, carries no file on disk: its voxels
    are live in the composed stage (SOP-imported). Exporting such a stage *bakes*
    the volume into the exported layer -- tens of GB per frame on a real shot,
    versus a few MB when a ``.vdb`` is referenced. The husk (USD-export) path
    pays that cost every frame; the hython-direct engine renders the live data
    without exporting.

    A field whose ``filePath`` names a **file** (even one that fails to resolve)
    is **not** a bake -- that is a missing VDB, reported by
    :func:`scan_missing_assets` instead. Results are grouped by the owning
    ``UsdVolVolume`` prim so a caller can say "N live volumes".

    ``time_code`` is the frame the manifest describes and both halves of the
    verdict depend on it. Houdini authors these attributes as time samples with
    no default value, so at the default time code every ``filePath`` reads
    empty -- which made a perfectly ordinary ``.vdb`` sequence look like a live
    volume and had ``inspect(allow_volume_bake=False)`` refuse a safe export.
    Reading at the frame fixes that; the ``op:`` test is what keeps a genuinely
    SOP-imported volume flagged now that its path is no longer invisible.
    Both directions matter: the first is a refused export, the second is tens
    of GB written to disk (``docs/UNVERIFIED.md`` N10).
    """
    if UsdVol is None:
        return []

    order: list[str] = []
    fields_by_volume: dict[str, list[str]] = {}
    tc = _time_code(time_code)

    for prim in stage.Traverse():
        if not prim.IsA(UsdVol.OpenVDBAsset):
            continue
        asset = UsdVol.OpenVDBAsset(prim)
        value = _attr_value(asset.GetFilePathAttr(), tc)
        path = (getattr(value, "path", "") or "") if value is not None else ""
        if not _is_live_asset_path(path):
            continue                      # references a real .vdb -- cheap to export

        # `fieldName` is authored as a time sample with no default, so it too
        # reads empty unless a frame is passed -- and on a SOP-imported field
        # the prim *name* is the reliable carrier ("density", "vel") in any
        # case. The fallback is load-bearing, not defensive: without it a
        # warning would name its fields as empty strings.
        field_name = str(_attr_value(asset.GetFieldNameAttr(), tc)
                         or prim.GetName())

        owner = _owning_volume(prim)
        if owner not in fields_by_volume:
            fields_by_volume[owner] = []
            order.append(owner)
        fields_by_volume[owner].append(field_name)

    return [
        LiveVolume(prim_path=owner,
                   field_count=len(fields_by_volume[owner]),
                   field_names=fields_by_volume[owner])
        for owner in order
    ]


# --------------------------------------------------------------------------
# Scene footprint -- what the scene *contains* that drives render memory
# --------------------------------------------------------------------------
#
# Facts, each labelled with exactly what it counts. Deliberately **not** a
# prediction: see ``manifest.SceneFootprint``'s docstring for why a total would
# be a number someone then sizes a farm around.
#
# Everything here is opt-in (``inspect(footprint=True)``) because two of the
# four parts cost real time on a heavy stage -- measured per part in
# ``docs/UNVERIFIED.md`` section N.

# Bytes per voxel for the OpenVDB grid types Houdini names through
# ``hou.VDB.vdbType()``. Used only for a **file-backed** grid, where the USD
# side authors no ``fieldDataType`` (measured: the Volume LOP leaves that token
# empty, while a SOP-imported field authors it). The widths are arithmetic on
# the type's own name -- ``Vec3f`` is three 32-bit floats -- not a recalled API.
# ``hou.vdbType`` exposes exactly these members plus ``PointData``,
# ``PointIndex`` and ``Invalid``, which have no fixed per-voxel width and are
# therefore recorded in ``skipped`` rather than guessed.
VDB_TYPE_BYTES = {
    "Float": 4, "Double": 8, "Int32": 4, "Int64": 8, "Bool": 1,
    "Vec3f": 12, "Vec3d": 24, "Vec3i": 12,
}

# The marker Houdini puts in an ``op:`` asset path between the node path and
# the resolver's own arguments.
_SDF_FORMAT_ARGS = ":SDF_FORMAT_ARGS:"

# Suffixes Houdini appends to the node path inside an ``op:`` asset path.
# Longest first, so ``.sop.volumes`` is not half-stripped to ``.sop``.
_OP_PATH_SUFFIXES = (".sop.volumes", ".sop.geo", ".sop", ".volumes")


def _value_type_shape(type_name) -> Optional[tuple]:
    """``(channels, bytes_per_channel)`` for a USD type name, or ``None``.

    Both numbers are **measured off the USD library itself** rather than read
    from a table someone typed from memory: the type's array form exposes the
    buffer protocol, whose ``itemsize`` is the element width and whose shape
    says how many elements make up one value. So ``color3f`` answers ``(3, 4)``
    and ``half`` answers ``(1, 2)`` because that is what the build in front of
    us says, not because this file claims to know.

    That matters most for the framebuffer term, where the tempting shortcut is
    to assume every AOV is four floats. A ``half`` beauty pass and a ``float``
    depth pass are 2 and 4 bytes per channel, and a cryptomatte is not three
    channels -- guessing would silently double or halve the answer.

    ``None`` means the type was not recognised (``token`` and ``string`` have
    no buffer form, and an unknown name does not resolve at all). Callers must
    record that in ``skipped``; they must never fall back to a default width.
    """
    if Sdf is None or not type_name:
        return None
    try:
        value_type = Sdf.ValueTypeNames.Find(str(type_name))
    except Exception:
        return None
    if not value_type:
        return None
    try:
        empty = value_type.arrayType.defaultValue
        view = memoryview(type(empty)(1))
        # shape is (1,) for a scalar, (1, 3) for color3f, (1, 4, 4) for
        # matrix4d -- so the channel count is the product of everything past
        # the leading element count.
        channels = 1
        for dim in view.shape[1:]:
            channels *= int(dim)
        return int(channels), int(view.itemsize)
    except Exception:
        return None


def _op_node_path(asset_path: str) -> str:
    """The Houdini node path inside an ``op:`` asset path, or ``""``.

    A SOP-imported volume does not carry an empty ``filePath`` in every case --
    measured on 22.0.368, both ``sopimport`` and ``sopcreate`` author a live
    reference that points straight back at the SOP::

        op:/obj/vol_src/vdbfrompolygons1.sop.volumes:SDF_FORMAT_ARGS:...

    which is the route to a real active-voxel count for live volumes: the node
    it names has already been cooked by the stage cook, so asking it costs
    fractions of a millisecond.
    """
    if not asset_path or not asset_path.startswith("op:"):
        return ""
    body = asset_path[3:].split(_SDF_FORMAT_ARGS, 1)[0]
    for suffix in _OP_PATH_SUFFIXES:
        if body.endswith(suffix):
            return body[: -len(suffix)]
    return body


def _vdb_grids(geometry) -> dict:
    """``{grid name: (active voxel count, bytes per voxel or None)}``.

    Reads ``hou.VDB.activeVoxelCount()``, which is the **sparse** count -- the
    voxels actually stored -- and not the bounding-box product. On the probe
    fixture the two are 29 999 and 45x45x45 = 91 125, so the distinction is not
    academic: taking the bounding box would have overstated that grid by 3x.
    """
    grids: dict = {}
    for prim in geometry.prims():
        counter = getattr(prim, "activeVoxelCount", None)
        if counter is None:
            continue                      # not a VDB primitive
        try:
            count = int(counter())
        except Exception:
            continue
        try:
            name = str(prim.attribValue("name"))
        except Exception:
            name = ""
        width = None
        try:
            # hou.vdbType.Float -> "Float"
            width = VDB_TYPE_BYTES.get(str(prim.vdbType()).rsplit(".", 1)[-1])
        except Exception:
            width = None
        grids[name] = (count, width)
    return grids


class _FootprintScan:
    """Accumulates footprint facts across one or more composed stages.

    ``inspect()`` cooks a stage per render ROP and those stages overlap, so
    every term is deduplicated by the identity of the thing counted -- prim
    path for volumes and geometry, resolved file path for textures. Counting a
    shared texture once per ROP would inflate the only number here that is
    meant to be exact about disk.

    Each ``scan()`` call reuses a stage the caller **already cooked**. That is
    the whole reason this is affordable on a heavy shot: the cold LOP cook
    dominates everything (~95 s on the production shot measured in
    ``docs/UNVERIFIED.md``), and the footprint adds no cook of its own.

    ``time_code`` is the frame the description is being taken at, and it is
    **load-bearing rather than a refinement**. Houdini authors volume fields as
    *time samples*: on the production shot every one of ``filePath``,
    ``fieldName`` and ``fieldDataType`` carries a single sample at frame 1074
    and **no default value**, so a plain ``attr.Get()`` returns ``None`` and
    the field looks like it has no data at all. Reading at the frame turns four
    "uncountable" fields into four real voxel counts. ``Get(timeCode)`` falls
    back to the default value when an attribute has no samples, so passing a
    frame is never worse than not passing one.
    """

    def __init__(self, count_vdb_files: bool = True, time_code=None):
        self.count_vdb_files = count_vdb_files
        self._tc = _time_code(time_code)

        # `*_ran` says the part executed at least once. It is what separates
        # "measured, and the answer is none" (0) from "nobody looked" (None) --
        # a distinction the whole dataclass rests on.
        self.volumes_ran = False
        self.geometry_ran = False
        self.framebuffer_ran = False

        self.volumes: set = set()             # UsdVolVolume prim paths
        self.fields: set = set()              # OpenVDBAsset prim paths
        self.active_voxels = 0
        self.voxel_bytes = 0
        self.fields_counted = 0               # fields that yielded a count
        self.voxel_width_missing = 0          # counted, but value size unknown

        self.textures: dict = {}              # resolved path -> size in bytes
        self.textures_ran = False
        self.texture_unstattable = 0
        self.volume_files: set = set()        # .vdb refs, counted as volumes

        self.gprims: set = set()
        self.point_count = 0
        self.point_arrays_failed = 0
        self.instancers: set = set()
        self.instance_count = 0
        self.instancer_reads_failed = 0

        self.settings_seen: set = set()
        self.framebuffer_bytes = 0
        self.vars_sized = 0
        self.unknown_var_types: set = set()
        self.settings_without_resolution: set = set()

        self.live_fields_uncounted: set = set()
        self.unresolved_vdbs: set = set()
        self.vdb_files_skipped: set = set()
        self.vdb_files_failed: set = set()
        self.seconds = 0.0
        self.part_seconds: dict = {"volumes": 0.0, "textures": 0.0,
                                   "geometry": 0.0, "framebuffer": 0.0}

        # Cached per-node and per-file grid tables, so a stage referencing the
        # same SOP or the same .vdb from several fields pays once.
        self._sop_grids: dict = {}
        self._file_grids: dict = {}
        self._reader = None                   # temporary geo/file SOP pair

    # -- reading ----------------------------------------------------------

    def _value(self, attr):
        """An attribute's value at the inspected frame.

        Everything the scan reads goes through here, and through the same
        :func:`_attr_value` the live-volume and missing-asset scans use, so the
        three can never disagree about which moment they are describing.
        """
        return _attr_value(attr, self._tc)

    # -- volumes ----------------------------------------------------------

    def _sop_grid_table(self, node_path: str) -> dict:
        if node_path in self._sop_grids:
            return self._sop_grids[node_path]
        table: dict = {}
        node = hou.node(node_path) if hou is not None else None
        if node is not None:
            try:
                table = _vdb_grids(node.geometry())
            except Exception:
                table = {}
        self._sop_grids[node_path] = table
        return table

    def _file_grid_table(self, path: str) -> dict:
        """Grid table for a ``.vdb`` on disk, read through a temporary File SOP.

        There is **no ``pyopenvdb`` under hython** (checked on 22.0.368), so a
        .vdb cannot be opened directly from Python. A File SOP can, and its
        ``activeVoxelCount()`` matches the writing SOP exactly -- but it is a
        real file read: measured at roughly 320-350 MB/s (a 140 MB grid took
        0.39 s), and it holds that grid in memory while it is counted. That is
        why this part is separately switchable through ``count_vdb_files``.
        """
        key = os.path.normcase(path)
        if key in self._file_grids:
            return self._file_grids[key]
        table: dict = {}
        try:
            if self._reader is None:
                parent = hou.node("/obj")
                if parent is None:
                    raise RuntimeError("no /obj to build a reader in")
                container = parent.createNode("geo", "hsl_tmp_vdb_probe")
                self._reader = (container, container.createNode("file"))
            container, reader = self._reader
            reader.parm("file").set(path)
            table = _vdb_grids(reader.geometry())
        except Exception:
            self.vdb_files_failed.add(path)
            table = {}
        self._file_grids[key] = table
        return table

    def close(self) -> None:
        """Drop the temporary File SOP, releasing whatever grid it still holds."""
        if self._reader is None:
            return
        container, reader = self._reader
        try:
            reader.parm("file").set("")     # release the last grid's memory
        except Exception:
            pass
        try:
            container.destroy()
        except Exception:
            pass
        self._reader = None

    def _scan_volumes(self, stage) -> None:
        if UsdVol is None:
            return
        self.volumes_ran = True
        for prim in stage.Traverse():
            if prim.IsA(UsdVol.Volume):
                self.volumes.add(str(prim.GetPath()))
            if not prim.IsA(UsdVol.OpenVDBAsset):
                continue
            path = str(prim.GetPath())
            if path in self.fields:
                continue
            self.fields.add(path)

            asset = UsdVol.OpenVDBAsset(prim)
            value = self._value(asset.GetFilePathAttr())
            raw = (getattr(value, "path", "") or "") if value is not None else ""
            resolved = ((getattr(value, "resolvedPath", "") or "")
                        if value is not None else "")

            field_name = str(self._value(asset.GetFieldNameAttr())
                             or prim.GetName())

            # The grid's own value size. Authored on SOP-imported fields;
            # measured empty on a Volume LOP, where the .vdb itself answers.
            data_type = str(self._value(asset.GetFieldDataTypeAttr()) or "")
            shape = _value_type_shape(data_type)
            width = (shape[0] * shape[1]) if shape else None

            node_path = _op_node_path(raw)
            if node_path:
                # Live, but referenced: `op:` points back at the SOP holding
                # the grid, which the stage cook has already cooked.
                table = self._sop_grid_table(node_path)
            elif resolved and os.path.isfile(resolved):
                self.volume_files.add(os.path.normcase(os.path.abspath(resolved)))
                if not self.count_vdb_files:
                    self.vdb_files_skipped.add(resolved)
                    continue
                table = self._file_grid_table(resolved)
            elif raw:
                # Authored but the resolver could not find it: a missing VDB,
                # which scan_missing_assets reports as such. Nothing to count.
                self.unresolved_vdbs.add(path)
                continue
            else:
                # A live field with no on-disk .vdb and no op: reference back
                # to a SOP -- the flavour scan_live_volumes() reports. Nothing
                # here can count it, so say so rather than contribute 0.
                self.live_fields_uncounted.add(path)
                continue

            entry = table.get(field_name)
            if entry is None and len(table) == 1:
                # A single-grid file whose grid name differs from the USD
                # field name (a Volume LOP names the prim, not the grid).
                entry = next(iter(table.values()))
            if entry is None:
                self.live_fields_uncounted.add(path)
                continue

            count, file_width = entry
            self.active_voxels += count
            self.fields_counted += 1
            per_voxel = width if width is not None else file_width
            if per_voxel is None:
                self.voxel_width_missing += 1
            else:
                self.voxel_bytes += count * per_voxel

    # -- textures ---------------------------------------------------------

    def _add_texture(self, prim, attr, asset) -> None:
        if asset is None:
            return
        path = getattr(asset, "path", "") or ""
        resolved = getattr(asset, "resolvedPath", "") or ""
        if not path or not resolved:
            return                          # unresolved: a missing asset
        if resolved.startswith("op:") or path.startswith("op:"):
            return                          # live Houdini data, not a file
        # A volume's own .vdb is counted in the volume terms; letting it also
        # land here would double it into `heaviest` and make a cached sim look
        # like a texture problem.
        if UsdVol is not None and prim.IsA(UsdVol.OpenVDBAsset) \
                and attr.GetName() == "filePath":
            return
        key = os.path.normcase(os.path.abspath(resolved))
        if key in self.textures:
            return
        try:
            self.textures[key] = os.path.getsize(resolved)
        except OSError:
            # Resolved but unreadable -- a permission or a stale mount. Not a
            # missing asset (the resolver found it), so it is its own gap.
            self.texture_unstattable += 1

    def _scan_textures(self, stage) -> None:
        if Sdf is None:
            return
        self.textures_ran = True
        for prim in stage.Traverse():
            for attr in prim.GetAttributes():
                type_name = attr.GetTypeName()
                if type_name == Sdf.ValueTypeNames.Asset:
                    self._add_texture(prim, attr, self._value(attr))
                elif type_name == Sdf.ValueTypeNames.AssetArray:
                    for asset in (self._value(attr) or []):
                        self._add_texture(prim, attr, asset)

    # -- geometry ---------------------------------------------------------

    def _scan_geometry(self, stage) -> None:
        """Points, geometry prims and instances authored on the stage.

        ``prim_count`` is **renderable geometry prims** -- meshes, curves,
        point clouds and the quadrics -- not the total number of prims on the
        stage, and not polygons. Volumes and point instancers are excluded
        because they have their own terms, so the three never overlap.

        This is the part that has to materialise arrays: USD offers no way to
        ask an attribute how long it is without fetching it, so a point count
        costs reading the points. That is the second-most expensive part of
        the scan after opening .vdb files.
        """
        if UsdGeom is None:
            return
        self.geometry_ran = True
        for prim in stage.Traverse():
            path = str(prim.GetPath())

            if prim.IsA(UsdGeom.PointInstancer):
                if path in self.instancers:
                    continue
                self.instancers.add(path)
                # Instances are counted, deliberately **not** weighted by what
                # they instance: the same million instances are nearly free on
                # one delegate and ruinous on another, and a weighting here
                # would be an invented constant.
                indices = self._value(
                    UsdGeom.PointInstancer(prim).GetProtoIndicesAttr())
                if indices is None:
                    self.instancer_reads_failed += 1
                else:
                    self.instance_count += len(indices)
                continue

            if not prim.IsA(UsdGeom.Gprim):
                continue
            # A UsdVolVolume **is** a Gprim (checked on 22.0.368 -- Volume,
            # Mesh, Points, Sphere and BasisCurves all answer IsA(Gprim) true,
            # while PointInstancer does not). Counting it here as well as in
            # volume_count would report one volume twice under two headings,
            # so the three geometry terms are kept a clean partition:
            # geometry prims, volumes, instancers.
            if UsdVol is not None and prim.IsA(UsdVol.Volume):
                continue
            if path in self.gprims:
                continue
            self.gprims.add(path)

            if not prim.IsA(UsdGeom.PointBased):
                continue                    # a Sphere/Cube has no point array
            points = self._value(UsdGeom.PointBased(prim).GetPointsAttr())
            if points is None:
                self.point_arrays_failed += 1
            else:
                self.point_count += len(points)

    # -- framebuffer ------------------------------------------------------

    def _scan_framebuffer(self, stage) -> None:
        if UsdRender is None:
            return
        self.framebuffer_ran = True
        for prim in stage.Traverse():
            if not prim.IsA(UsdRender.Settings):
                continue
            path = str(prim.GetPath())
            if path in self.settings_seen:
                continue
            self.settings_seen.add(path)

            settings = UsdRender.Settings(prim)
            resolution = self._value(settings.GetResolutionAttr())
            if resolution is None or len(resolution) < 2:
                self.settings_without_resolution.add(path)
                continue
            width, height = int(resolution[0]), int(resolution[1])

            for product_path in _targets(settings.GetProductsRel()):
                product_prim = stage.GetPrimAtPath(product_path)
                if not product_prim or not product_prim.IsValid():
                    continue
                for var_path in _targets(
                        UsdRender.Product(product_prim).GetOrderedVarsRel()):
                    var_prim = stage.GetPrimAtPath(var_path)
                    if not var_prim or not var_prim.IsValid():
                        continue
                    data_type = str(
                        self._value(UsdRender.Var(var_prim).GetDataTypeAttr()) or "")
                    shape = _value_type_shape(data_type)
                    if shape is None:
                        # An unrecognised width would be a guess multiplied by
                        # a few million pixels. Record it instead.
                        self.unknown_var_types.add(
                            f"{var_path} ({data_type or 'no dataType'})")
                        continue
                    channels, per_channel = shape
                    self.framebuffer_bytes += width * height * channels * per_channel
                    self.vars_sized += 1

    # -- driver -----------------------------------------------------------

    def scan(self, stage) -> None:
        """Add one composed stage's contents to the running totals."""
        for name, part in (("volumes", self._scan_volumes),
                           ("textures", self._scan_textures),
                           ("geometry", self._scan_geometry),
                           ("framebuffer", self._scan_framebuffer)):
            start = time.time()
            part(stage)
            self.part_seconds[name] += time.time() - start
            self.seconds += time.time() - start

    def result(self) -> SceneFootprint:
        """Freeze the totals into a :class:`SceneFootprint`.

        Every gap becomes a sentence in ``skipped``. ``None`` is used only for
        "not measured" -- a term that was measured and came to nothing stays 0,
        because "this scene has no volumes" and "nobody counted the volumes"
        are answers a farm decision turns on.
        """
        skipped: list = []

        if self.unresolved_vdbs:
            skipped.append(
                f"{len(self.unresolved_vdbs)} OpenVDB field(s) name a .vdb the "
                f"resolver could not find, so their voxels are not counted: "
                f"{_sample(self.unresolved_vdbs)}. These are missing assets -- "
                f"see manifest.missing_assets."
            )
        if self.live_fields_uncounted:
            skipped.append(
                f"{len(self.live_fields_uncounted)} OpenVDB field(s) carry no "
                f"on-disk .vdb and no op: reference back to a SOP, so their "
                f"active voxels could not be counted: "
                f"{_sample(self.live_fields_uncounted)}. Their voxels are NOT "
                f"included in the volume figures above."
            )
        if self.vdb_files_skipped:
            skipped.append(
                f"{len(self.vdb_files_skipped)} on-disk .vdb file(s) were not "
                f"opened because VDB file counting was switched off, so their "
                f"active voxels are not included: "
                f"{_sample(self.vdb_files_skipped)}."
            )
        if self.vdb_files_failed:
            skipped.append(
                f"{len(self.vdb_files_failed)} .vdb file(s) could not be read "
                f"to count their voxels: {_sample(self.vdb_files_failed)}."
            )
        if self.voxel_width_missing:
            skipped.append(
                f"{self.voxel_width_missing} volume field(s) were counted in "
                f"active voxels but their grid value size was not recognised, "
                f"so they contribute to the voxel count and NOT to the byte "
                f"figure."
            )
        if self.texture_unstattable:
            skipped.append(
                f"{self.texture_unstattable} asset file(s) resolved but could "
                f"not be measured on disk (permission, or a stale mount), so "
                f"their bytes are missing from the texture total."
            )
        if self.volume_files:
            skipped.append(
                f"{len(self.volume_files)} .vdb file(s) referenced as volume "
                f"data are counted in the volume figures, not in the texture "
                f"figures, so the two never double-count the same bytes."
            )
        if self.point_arrays_failed:
            skipped.append(
                f"{self.point_arrays_failed} geometry prim(s) had a points "
                f"attribute that could not be read, so their points are "
                f"missing from the point count."
            )
        if self.instancer_reads_failed:
            skipped.append(
                f"{self.instancer_reads_failed} point instancer(s) would not "
                f"report their instance count, so their instances are missing "
                f"from the instance total."
            )
        if self.settings_without_resolution:
            skipped.append(
                f"{len(self.settings_without_resolution)} render settings "
                f"prim(s) author no resolution, so their products contribute "
                f"nothing to the framebuffer figure: "
                f"{_sample(self.settings_without_resolution)}."
            )
        if self.unknown_var_types:
            skipped.append(
                f"{len(self.unknown_var_types)} render var(s) have a data type "
                f"this build could not size, so they are missing from the "
                f"framebuffer figure: {_sample(self.unknown_var_types)}."
            )
        if self.framebuffer_ran and not self.settings_seen:
            skipped.append(
                "The stage declares no UsdRenderSettings prim, so there was no "
                "resolution to size a framebuffer from. That is why the "
                "framebuffer figure is 'not measured' rather than zero."
            )
        if len(self.settings_seen) > 1:
            skipped.append(
                f"The framebuffer figure sums all {len(self.settings_seen)} "
                f"render settings prims on the stage, not one render: "
                f"{_sample(self.settings_seen)}. A single render pays only its "
                f"own settings prim's share."
            )

        # The None/0 ladder, applied the same way to every term:
        #   never ran            -> None   (nobody looked)
        #   ran, found nothing   -> 0      (measured, and the answer is none)
        #   ran, found some but  -> None   (there IS something here and it
        #   could count none of it         could not be counted; 0 would lie)
        if not self.volumes_ran:
            voxels = voxel_bytes = None
        elif not self.fields:
            voxels, voxel_bytes = 0, 0
        elif not self.fields_counted:
            voxels = voxel_bytes = None
        else:
            voxels, voxel_bytes = self.active_voxels, self.voxel_bytes

        if not self.framebuffer_ran or not self.settings_seen:
            framebuffer = None
        elif not self.vars_sized:
            framebuffer = None
        else:
            framebuffer = self.framebuffer_bytes

        return SceneFootprint(
            volume_count=len(self.volumes),
            active_voxels=voxels,
            voxel_bytes=voxel_bytes,
            texture_count=len(self.textures),
            texture_bytes=(sum(self.textures.values())
                           if self.textures_ran else None),
            point_count=self.point_count if self.geometry_ran else None,
            prim_count=len(self.gprims) if self.geometry_ran else None,
            instance_count=self.instance_count if self.geometry_ran else None,
            framebuffer_bytes=framebuffer,
            scanned=True,
            skipped=skipped,
            seconds=round(self.seconds, 3),
        )


def _sample(items, limit: int = 3) -> str:
    """Up to ``limit`` of ``items``, sorted, with a count of the remainder."""
    ordered = sorted(items)
    shown = ", ".join(ordered[:limit])
    extra = len(ordered) - limit
    return shown + (f" (+{extra} more)" if extra > 0 else "")


# --------------------------------------------------------------------------
# Node walking
# --------------------------------------------------------------------------

def find_render_rops(roots: Iterable[str] = ("/stage", "/out")) -> list:
    found = []
    for root_path in roots:
        root = hou.node(root_path)
        if root is None:
            continue
        for node in root.allSubChildren():
            if _type_name(node) in RENDER_ROP_TYPES:
                found.append(node)
    return found


# --------------------------------------------------------------------------
# Non-Solaris work: caches, simulations and the other ROP contexts
# --------------------------------------------------------------------------

def _parm_raw(node, names, default: str = "") -> str:
    """The *unexpanded* string of a parameter.

    ``_parm`` evaluates, which turns ``geo.$F4.bgeo.sc`` into the filename for
    whatever frame happens to be current -- so an output path read that way
    would claim every frame writes to frame 1's file. Output templates have to
    keep their tokens; the runner expands them per frame itself.
    """
    if isinstance(names, str):
        names = (names,)
    for name in names:
        parm = node.parm(name)
        if parm is None:
            continue
        for reader in ("unexpandedString", "rawValue"):
            try:
                return getattr(parm, reader)()
            except Exception:
                continue
    return default


def _inside_cache_node(node) -> bool:
    """True for the ROP *inside* a File Cache SOP, which is not its own task."""
    parent = node.parent()
    while parent is not None:
        if _type_name(parent) in SOP_CACHE_KINDS:
            return True
        parent = parent.parent()
    return False


def find_output_tasks(roots: Iterable[str] = ("/out", "/stage", "/obj")) -> list:
    """Every node in the scene that can be cooked to produce files.

    Wider than :func:`find_render_rops`: caches and simulations too. ``/obj``
    is included because a File Cache SOP lives inside a geometry object rather
    than in ``/out``.
    """
    found = []
    for root_path in roots:
        root = hou.node(root_path)
        if root is None:
            continue
        for node in root.allSubChildren():
            name = _type_name(node)
            if name not in TASK_ROP_KINDS and name not in SOP_CACHE_KINDS:
                continue
            if _inside_cache_node(node):
                continue        # implementation detail of the SOP that owns it
            found.append(node)
    return found


def _cookable_node(node, warnings: list):
    """The node whose ``render()`` actually does the work, or None.

    A File Cache SOP has no ``render()`` of its own. Probed on 21.0.729 and
    22.0.368: it wraps a ``render`` node of type ``rop_geometry`` that does,
    and driving *that* honours an explicit frame range -- whereas pressing the
    SOP's own ``execute`` button ignores one and cooks whatever ``$FSTART`` and
    ``$FEND`` evaluate to, which would quietly cache the wrong frames.
    """
    if hasattr(node, "render"):
        return node
    inner = node.node("render")
    if inner is not None and hasattr(inner, "render"):
        return inner
    for child in node.children():
        if hasattr(child, "render"):
            return child
    warnings.append(
        f"{node.path()}: nothing inside it can be cooked -- skipped. Point hsl "
        f"at the ROP that writes this instead."
    )
    return None


def _task_kind(node) -> str:
    name = _type_name(node)
    kind = TASK_ROP_KINDS.get(name) or SOP_CACHE_KINDS.get(name)
    return kind or TASK_UNKNOWN


def _is_sequential(node) -> bool:
    """True when the node carries state from one frame to the next."""
    return any(bool(_parm(node, name, 0)) for name in SEQUENTIAL_PARMS)


def _task_dependencies(node, task_paths) -> list:
    """Task node paths that must finish before ``node`` can cook.

    Walks the input chain, following ``fetch`` nodes to their ``source`` parm
    and passing straight through anything that is not itself a task -- a merge
    produces nothing, so it is traversed rather than reported. Stops at the
    first task on each branch: whatever *that* depends on is its own business.
    """
    found, seen = set(), set()
    queue = [n for n in node.inputs() if n is not None]
    while queue:
        current = queue.pop()
        path = current.path()
        if path in seen:
            continue
        seen.add(path)

        if _type_name(current) == "fetch":
            target = _parm(current, "source", "")
            source = hou.node(target) if target else None
            if source is not None:
                queue.append(source)
            continue
        if path in task_paths:
            found.add(path)
            continue
        queue.extend(n for n in current.inputs() if n is not None)
    return sorted(found)


def describe_task(node, task_paths, warnings: list) -> OutputTask:
    """Read one cookable node into an :class:`OutputTask`."""
    trange = _parm(node, "trange", 0)
    f1 = _parm(node, "f1", None)
    f2 = _parm(node, "f2", None)
    f3 = _parm(node, "f3", 1)
    if f1 is None:
        f1 = hou.frame()
    if f2 is None:
        f2 = f1

    outputs = []
    template = _parm_raw(node, TASK_OUTPUT_PARMS.get(_type_name(node), ()))
    if template:
        outputs.append(template)
    else:
        warnings.append(
            f"{node.path()}: could not read an output path, so hsl cannot "
            f"check whether it wrote anything."
        )

    try:
        start, end, inc = int(f1), int(f2), int(f3 or 1)
    except (TypeError, ValueError):
        warnings.append(f"{node.path()}: unreadable frame range; using frame 1.")
        start, end, inc = 1, 1, 1

    return OutputTask(
        node_path=node.path(),
        node_type=node.type().name(),
        kind=_task_kind(node),
        frame_start=start,
        frame_end=end,
        frame_inc=inc or 1,
        use_frame_range=bool(trange),
        outputs=outputs,
        sequential=_is_sequential(node),
        depends_on=_task_dependencies(node, task_paths),
    )


def scan_tasks(warnings: list) -> list:
    """Describe every cookable node in the loaded scene."""
    nodes = find_output_tasks()
    paths = {n.path() for n in nodes}
    return [describe_task(n, paths, warnings) for n in nodes]


def describe_rop(node, warnings: list[str]) -> RenderRop:
    """Read a render ROP's parameters into a RenderRop."""
    inputs = [n for n in node.inputs() if n is not None]
    trange = _parm(node, "trange", 0)

    # Houdini's trange: 0 = current frame, 1 = frame range, 2 = range (strict).
    use_range = bool(trange)
    f1 = _parm(node, "f1", None)
    f2 = _parm(node, "f2", None)
    f3 = _parm(node, "f3", 1)

    if f1 is None:
        f1 = hou.frame()
    if f2 is None:
        f2 = f1

    raw = {}
    for name in ("trange", "f1", "f2", "f3", "rendersettings", "camera",
                 "renderer", "engine", "outputimage", "picture", "lopoutput",
                 "husk_threads", "husk_verbosity", "husk_snapshot",
                 "husk_makedir", "husk_alfprogress", "savetodirectory",
                 "override_camera", "res_mode", "resolutionx", "resolutiony"):
        value = _parm(node, name, None)
        if value is not None:
            raw[name] = value

    rop = RenderRop(
        node_path=node.path(),
        node_type=_type_name(node),
        input_lop=inputs[0].path() if inputs else "",
        renderer=str(_parm(node, ("renderer", "husk_renderer", "engine"), "") or ""),
        settings_prim=str(_parm(node, ("rendersettings", "settingsprim",
                                       "rendersettingsprim"), "") or ""),
        # `override_camera` is the real camera-override parm on the usdrender
        # LOP ROP (confirmed a String path on Houdini 21.0.729 and 22.0.368,
        # where it is the *only* camera-like parm). The rest are fallbacks for
        # other builds. Ordered newest/most-correct first.
        camera=str(_parm(node, ("override_camera", "camera",
                                "override_camera_path", "cameraprim"), "") or ""),
        frame_start=int(round(f1)),
        frame_end=int(round(f2)),
        frame_inc=max(1, int(round(f3 or 1))),
        use_frame_range=use_range,
        output_override=str(_parm(node, ("outputimage", "picture",
                                         "override_outputimage"), "") or ""),
        raw_parms=raw,
    )

    if not rop.renderer:
        warnings.append(
            f"{node.path()}: no renderer parameter found; defaulting to Karma CPU."
        )
    return rop


# Set once if this build's LopNode.stage() refuses a `frame` keyword, so the
# fallback is reported to the caller exactly once instead of per ROP.
_FRAME_KWARG_MISSING = False


def _stage_at(lop, frame: Optional[float], warnings: list):
    """The LOP's composed stage, cooked at ``frame`` when one is given.

    **There is no ``LopNode.stageAtFrame()``.** That is an easy name to
    half-remember and it does not exist on either build here -- probed on
    21.0.729 and 22.0.368, whose only stage-ish methods are ``stage``,
    ``editableStage``, ``uneditableStage``, ``stagePrimStats`` and
    ``isMostRecentStageLock``. The real mechanism is a keyword on ``stage()``
    itself::

        stage(self, output_index=-1, apply_viewport_overrides=False,
              ignore_errors=False, use_last_cook_context_options=True,
              apply_post_layers=True, frame=None, context_options={})

    documented as "A frame number can be provided to return the result of
    cooking the LOP node at a particular frame", and confirmed by cooking a
    switch LOP driven by ``$F > 5``: frames 1 and 5 composed one branch, 6 and
    10 the other, and repeating the calls in either order returned the same
    answer each time -- a cached cook is not reused across frames.

    The explicit argument also **beats** ``hou.setFrame()``: with the playbar
    parked on frame 1, ``stage(frame=10)`` still returned frame 10's
    composition (and vice versa). ``hou.setFrame()`` is still used alongside it
    by :func:`inspect`, because node *parameters* evaluate at the global frame
    and the manifest reads plenty of those; the keyword is what makes the stage
    itself unambiguous.

    On an older build with no such keyword the call raises ``TypeError``; that
    degrades to moving the global frame instead, and says so, rather than
    silently describing the wrong moment.
    """
    if frame is None:
        return lop.stage()
    try:
        return lop.stage(frame=float(frame))
    except TypeError:
        global _FRAME_KWARG_MISSING
        if not _FRAME_KWARG_MISSING:
            _FRAME_KWARG_MISSING = True
            warnings.append(
                "This Houdini build's LopNode.stage() takes no 'frame' keyword "
                "(it is present on 21.0.729 and 22.0.368), so the per-frame "
                "cook falls back to setting the global frame first. That is "
                "usually equivalent but cannot be guaranteed on a build hsl "
                "has never seen."
            )
        hou.setFrame(float(frame))
        return lop.stage()


def stage_for(node, warnings: list[str], frame: Optional[float] = None):
    """Cook a LOP (or a ROP's input LOP) and return its composed stage.

    ``frame`` cooks at that moment instead of the current one -- a stage whose
    structure changes over time is a different stage at a different frame, and
    describing it from one arbitrary moment is TASKS.md T5.
    """
    candidates = [node] + [n for n in node.inputs() if n is not None]
    for candidate in candidates:
        if not isinstance(candidate, hou.LopNode):
            continue
        try:
            stage = _stage_at(candidate, frame, warnings)
        except hou.Error as exc:
            at = "" if frame is None else f" at frame {frame:g}"
            warnings.append(f"{candidate.path()}: cook failed{at} -- {exc}")
            continue
        if stage is not None:
            return stage, candidate
    return None, None


def _settings_prim_paths(stage) -> set:
    """The set of UsdRenderSettings prim paths on a composed stage."""
    return {str(p.GetPath()) for p in stage.Traverse()
            if p.IsA(UsdRender.Settings)}


def _cross_range_frames(stage, rop) -> Optional[tuple]:
    """The two frames the cross-range check compares, and where they came from.

    First choice is the stage's own authored time code range -- the stage
    saying how long it lasts. But a Solaris network authors **none** by
    default: a ``rendersettings -> switch -> usdrender_rop`` network probed on
    22.0.368 reported ``GetStartTimeCode() == GetEndTimeCode() == 0.0`` and
    ``HasAuthoredTimeCodeRange() == False``, with the playbar on 1-10, until a
    Configure Layer LOP set its ``starttime`` / ``endtime`` parms (that is the
    real parm spelling -- there is no ``starttimecode`` on that node).

    So a stage-only trigger would skip the check on most scenes, which is
    exactly the under-reporting T5 is about. The ROP's own authored frame range
    is the fallback: it is the range actually being rendered, so it is the
    range over which a structural change would matter.
    """
    start = stage.GetStartTimeCode()
    end = stage.GetEndTimeCode()
    if start is not None and end is not None and start != end:
        return float(start), float(end), "the stage's time code range"
    if rop.use_frame_range and rop.frame_start != rop.frame_end:
        return (float(rop.frame_start), float(rop.frame_end),
                "the ROP's frame range (the stage authors no time code range)")
    return None


def _check_settings_drift(node, rop, stage, inspected_frame: Optional[float],
                          inspected_paths: set, warnings: list) -> None:
    """Warn when the RenderSettings prim *set* differs across the frame range.

    This is the whole point of T5: a manifest describes one moment, and a
    switch, a prune or a stage variant driven by ``$F`` can make another moment
    a different scene entirely. Rather than silently describing the first
    moment, compare the range's endpoints and say so.

    **Cost:** one extra LOP cook per endpoint that is not the frame already
    inspected -- so usually *one*, since inspecting frame 1 of a 1-100 range
    reuses that walk for the start. Measured on a real 167 MB Karma shot
    (22.0.368), a warm re-cook at another frame ran 10-12 s against a 94.9 s
    cold first cook: roughly an eighth, not a repeat. A ROP with ``trange = 0``
    has no range to compare and pays nothing -- on that shot two single-frame
    thumbnail ROPs, one of them a 248 s cook, were skipped entirely.

    It is paid unconditionally where it does apply. The alternative is a
    manifest that is confidently wrong about which scene is being rendered.

    The global frame is restored afterwards: the fallback path inside
    :func:`_stage_at` moves it, and every parameter read after this point would
    otherwise evaluate somewhere the caller did not ask for.
    """
    window = _cross_range_frames(stage, rop)
    if window is None:
        return
    start, end, source = window

    restore = hou.frame()
    try:
        by_frame: dict = {}
        for frame in (start, end):
            if inspected_frame is not None and frame == float(inspected_frame):
                by_frame[frame] = inspected_paths
                continue
            other, _lop = stage_for(node, warnings, frame=frame)
            if other is None:
                warnings.append(
                    f"{node.path()}: could not cook a stage at frame {frame:g}, "
                    f"so hsl cannot tell whether the render settings change "
                    f"over {source}."
                )
                return
            by_frame[frame] = _settings_prim_paths(other)
    finally:
        if hou.frame() != restore:
            hou.setFrame(restore)

    first, last = by_frame[start], by_frame[end]
    if first == last:
        return

    def _show(paths) -> str:
        return ", ".join(sorted(paths)) if paths else "(none)"

    at = ("" if inspected_frame is None
          else f" This manifest describes frame {inspected_frame:g}.")
    warnings.append(
        f"{node.path()}: the RenderSettings prims are not the same across "
        f"{source} -- at frame {start:g} the stage has {_show(first)}, at frame "
        f"{end:g} it has {_show(last)}. A single-frame description cannot cover "
        f"both.{at} Re-inspect with --frame to read another moment."
    )


# --------------------------------------------------------------------------
# USD export
# --------------------------------------------------------------------------

def _flatten_node_path(node_path: str) -> str:
    """``/stage/a/rop`` -> ``stage_a_rop`` -- the export basename."""
    return node_path.strip("/").replace("/", "_")


def _export_usd_names(node_paths) -> tuple:
    """Assign each ROP node path a **unique** export filename.

    Flattening a node path to a filename is not injective: ``/stage/a_b/rop``
    and ``/stage/a/b_rop`` are two different ROPs that both become
    ``stage_a_b_rop.usd``. Left alone, the second export overwrites the first,
    both manifest entries point at the survivor, and asking for the first ROP
    renders the second ROP's scene with no error anywhere (UNVERIFIED.md C7).

    Only names that actually collide are changed, and the suffix is a digest of
    the node's own path -- so an ordinary scene keeps byte-identical filenames,
    and the same scene always produces the same names. A counter or a PID would
    make the filename depend on discovery order or on which process ran, which
    is exactly what a farm submission holding a ``usd_path`` cannot tolerate.

    Returns ``(names, collisions)``: ``names`` maps node path -> filename, and
    ``collisions`` lists the ``(basename, [node paths])`` groups that needed
    disambiguating, so the caller can warn about them.
    """
    groups: dict = {}
    for path in node_paths:
        groups.setdefault(_flatten_node_path(path), []).append(path)

    names: dict = {}
    taken = set()
    for base, paths in groups.items():
        if len(paths) == 1:
            names[paths[0]] = base + ".usd"
            taken.add(names[paths[0]])

    collisions = []
    for base in sorted(groups):
        paths = groups[base]
        if len(paths) == 1:
            continue
        for path in sorted(paths):
            digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
            # Widen only if the short form would clash with another export in
            # the same pass; 8 hex chars is plenty for the ROPs in one scene.
            for width in (8, 16, 40):
                candidate = f"{base}_{digest[:width]}.usd"
                if candidate not in taken:
                    break
            names[path] = candidate
            taken.add(candidate)
        collisions.append((base, sorted(paths)))
    return names, collisions


def export_usd(rop_node, out_path: str, frame_range=None,
               flatten: bool = False, warnings: Optional[list] = None) -> str:
    """Write the ROP's input stage to disk so husk can render it.

    Uses a temporary ``usd_rop`` rather than ``Usd.Stage.Export()``. That
    matters: ``Export()`` serialises the stage as cooked at a single time, so
    animation, motion blur samples and per-frame value clips are silently lost.
    The USD ROP re-cooks per frame and handles all of that.

    ``frame_range`` is applied through :func:`_force_parm`, which clears the
    ``$FSTART``/``$FEND`` expressions the ROP ships with and reads the value
    back. Without that the range is ignored and the whole playbar is exported
    (UNVERIFIED.md C8).
    """
    warnings = warnings if warnings is not None else []
    parent = rop_node.parent()
    inputs = [n for n in rop_node.inputs() if n is not None]
    source = inputs[0] if inputs else rop_node

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    tmp = parent.createNode("usd_rop", "hsl_tmp_export")
    try:
        tmp.setInput(0, source)

        # T1: if `lopoutput` is named differently on this build, the ROP writes
        # to its own default path and husk would then render a stale or missing
        # file. Refuse rather than return a path that was never written to.
        if not _set_parm(tmp, "lopoutput", out_path):
            warnings.append(
                f"{rop_node.path()}: the temporary usd_rop has no 'lopoutput' "
                f"parameter on this Houdini build, so the export destination "
                f"could not be set. Aborting the export instead of writing to "
                f"an unknown location."
            )
            return ""
        # Absent on some builds and harmless either way -- not worth a warning.
        _set_parm(tmp, "enableoutputprocessor_simplerelativepaths", False)

        if flatten:
            token = _flatten_token(tmp)
            if token:
                _set_parm(tmp, "savestyle", token)
            else:
                warnings.append(
                    f"{rop_node.path()}: usd_rop exposes no recognised flatten "
                    f"savestyle token; --flatten had no effect."
                )

        if frame_range:
            start, end, inc = frame_range
            # One self-contained file (fileperframe=0) keeps the husk command
            # simple. A missing parm here means the wrong frames get exported,
            # so say so rather than exporting a single frame in silence.
            #
            # These go through _force_parm, not _set_parm: f1/f2 arrive as the
            # expressions $FSTART/$FEND, which a plain set() does not beat (C8).
            # Every export used to cover the whole playbar as a result.
            for name, value in (("trange", 1), ("f1", start), ("f2", end),
                                ("f3", inc), ("fileperframe", 0)):
                if not _force_parm(tmp, name, value):
                    warnings.append(
                        f"{rop_node.path()}: usd_rop's '{name}' parameter could "
                        f"not be set to {value!r} on this build; the exported "
                        f"range may not be {start}-{end}x{inc} as requested."
                    )
        elif not _force_parm(tmp, "trange", 0):
            warnings.append(
                f"{rop_node.path()}: usd_rop has no 'trange' parameter; the "
                f"export may cover the ROP's whole range, not the current frame."
            )

        tmp.render(verbose=False)
    except hou.Error as exc:
        warnings.append(f"USD export failed for {rop_node.path()}: {exc}")
        return ""
    finally:
        try:
            tmp.destroy()
        except Exception:
            pass

    return out_path if os.path.exists(out_path) else ""


def filter_usd_aovs(usd_in: str, usd_out: str, keep_paths,
                    warnings: Optional[list] = None) -> str:
    """Write a thin overlay USD that keeps only ``keep_paths`` RenderVars.

    husk has **no flag** to select which AOVs are written — a render's output
    planes are defined entirely by each ``UsdRenderProduct``'s ``orderedVars``
    relationship (``husk --mask`` is a stage *population* mask; ``--mplay-monitor``
    only affects the interactive display). So AOV editing is done in USD.

    Rather than re-cook or re-export the heavy stage, this authors an *overlay*
    layer at ``usd_out`` that sublayers ``usd_in`` and overrides every product's
    ``orderedVars`` to the chosen subset. husk renders ``usd_out`` and composes
    the original underneath — fast, and the source export is left untouched so
    the selection can be changed again cheaply.
    """
    warnings = warnings if warnings is not None else []
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    keep = set(keep_paths or [])
    os.makedirs(os.path.dirname(os.path.abspath(usd_out)) or ".", exist_ok=True)

    # Sublayer the full export by a path relative to the overlay, so husk
    # resolves it wherever the pair lives.
    overlay = Sdf.Layer.CreateNew(usd_out)
    sub_rel = os.path.relpath(os.path.abspath(usd_in),
                              os.path.dirname(os.path.abspath(usd_out)))
    overlay.subLayerPaths.append(sub_rel.replace(os.sep, "/"))

    stage = Usd.Stage.Open(overlay)          # overlay is the root / edit layer
    if stage is None:
        warnings.append(f"AOV filter: could not open {usd_in}")
        return ""

    edited = 0
    for prim in stage.Traverse():
        if not prim.IsA(UsdRender.Product):
            continue
        rel = UsdRender.Product(prim).GetOrderedVarsRel()
        current = [str(t) for t in rel.GetTargets()]
        if not current:
            continue
        kept = [t for t in current if t in keep]
        if kept != current:
            rel.SetTargets([Sdf.Path(t) for t in kept])
            edited += 1

    overlay.Save()
    if edited == 0:
        warnings.append("AOV filter: no product orderedVars changed "
                        "(selection already matched the scene).")
    return usd_out if os.path.exists(usd_out) else ""


def override_product_paths(usd_in: str, usd_out: str, output: str,
                           warnings: Optional[list] = None) -> str:
    """Overlay that redirects **every** RenderProduct's output, not just the first.

    husk's ``-o/--output`` overrides only the *first* product (confirmed in
    ``husk --help``). On a multi-product shot -- beauty + cryptomatte + depth --
    that silently leaves every other product writing to wherever the scene
    pointed it, which is the kind of half-applied override that is only noticed
    after the render.

    So the redirect is done in USD, the same overlay trick as the AOV filter:

    * ``output`` naming a **directory** (trailing separator, or an existing dir)
      keeps each product's own filename and moves it into that directory.
    * ``output`` naming a **file** gives that exact path to the first product,
      and puts the others alongside it under their own filenames.

    A product with no authored ``productName`` -- which happens: the SandBurst
    products were all empty, the path coming from the ROP instead -- is named
    after its prim, keeping ``output``'s extension.
    """
    warnings = warnings if warnings is not None else []
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")
    if not output:
        return ""

    os.makedirs(os.path.dirname(os.path.abspath(usd_out)) or ".", exist_ok=True)

    overlay = Sdf.Layer.CreateNew(usd_out)
    sub_rel = os.path.relpath(os.path.abspath(usd_in),
                              os.path.dirname(os.path.abspath(usd_out)))
    overlay.subLayerPaths.append(sub_rel.replace(os.sep, "/"))

    stage = Usd.Stage.Open(overlay)
    if stage is None:
        warnings.append(f"Output override: could not open {usd_in}")
        return ""

    # Collect in stage order, then apply the shared naming rule. The rule lives
    # in hsl.husk so the UI/CLI preview and this overlay cannot disagree about
    # where a render lands.
    prims: dict = {}
    products: list = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdRender.Product):
            continue
        attr = UsdRender.Product(prim).GetProductNameAttr()
        path = str(prim.GetPath())
        prims[path] = prim
        products.append((path, str((attr.Get() if attr else "") or "")))

    assigned: dict = {}
    for prim_path, new_path in planned_product_paths(products, output):
        if new_path in assigned:
            warnings.append(
                f"Output override: {prim_path} and {assigned[new_path]} both "
                f"resolve to {new_path}; they would overwrite each other. Point "
                f"--output at a directory instead of a file, or rename the products."
            )
        assigned[new_path] = prim_path
        UsdRender.Product(prims[prim_path]).CreateProductNameAttr(new_path)

    overlay.Save()
    if not products:
        warnings.append("Output override: the stage declares no RenderProducts.")
        return ""
    return usd_out if os.path.exists(usd_out) else ""


def _basename_index(search_dirs) -> dict:
    """Map lower-case basename -> first absolute path found under search_dirs."""
    index: dict = {}
    for directory in search_dirs or []:
        if not directory or not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                index.setdefault(name.lower(), os.path.join(root, name))
    return index


def _coerce_setting(text, current):
    """Turn a command-line string into the type the attribute already holds.

    USD attributes are typed, and husk has no flag to set them, so these are
    authored by hand -- which makes the type our problem. Writing an int knob
    as a string is exactly the silently-wrong edit this project refuses, so the
    type is taken from the value already on the stage rather than guessed from
    how the text looks ("1" is a perfectly good int, float, bool or string).
    """
    if not isinstance(text, str):
        return text
    if isinstance(current, bool):          # before int: bool is an int subclass
        lowered = text.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected a true/false value, got {text!r}")
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    if isinstance(current, str):
        return text
    raise ValueError(f"cannot set a {type(current).__name__} from the command line")


def _author_settings_opinions(stage, overrides: dict, layer,
                              settings_prim: str = "",
                              warnings: Optional[list] = None) -> dict:
    """Author ``over`` opinions for render-settings attributes onto ``layer``.

    Works for either engine: husk gets a layer that sublayers the exported USD,
    hython gets a standalone layer sublayered into the LOP network. Only
    attributes that already exist on the settings prim can be set -- an absent
    one has no discoverable type, and inventing it would author a knob the
    delegate may not read. Those are reported, not guessed.
    """
    warnings = warnings if warnings is not None else []
    applied: list = []
    skipped: list = []

    targets = [p for p in stage.Traverse() if p.IsA(UsdRender.Settings)]
    if settings_prim:
        targets = [p for p in targets if str(p.GetPath()) == settings_prim]
    if not targets:
        warnings.append(
            f"No UsdRenderSettings prim{' at ' + settings_prim if settings_prim else ''} "
            f"to override.")
        return {"applied": applied, "skipped": skipped}

    for prim in targets:
        for key, raw in overrides.items():
            attr = prim.GetAttribute(key)
            if not attr:
                skipped.append({"key": key, "why": "not present on this settings prim"})
                continue
            current = attr.Get()
            if current is None:
                skipped.append({"key": key, "why": "has no value to take a type from"})
                continue
            try:
                value = _coerce_setting(raw, current)
            except (ValueError, TypeError) as exc:
                skipped.append({"key": key, "why": str(exc)})
                continue

            prim_path = prim.GetPath()
            Sdf.CreatePrimInLayer(layer, prim_path)
            prim_spec = layer.GetPrimAtPath(prim_path)
            prim_spec.specifier = Sdf.SpecifierOver
            attr_spec = layer.GetAttributeAtPath(attr.GetPath())
            if attr_spec is None:
                attr_spec = Sdf.AttributeSpec(prim_spec, key, attr.GetTypeName())
            attr_spec.default = value
            applied.append({"prim": str(prim_path), "key": key,
                            "old": current, "new": value})

    for entry in skipped:
        warnings.append(f"Render setting {entry['key']}: {entry['why']}.")
    return {"applied": applied, "skipped": skipped}


def override_render_settings(usd_in: str, usd_out: str, overrides: dict,
                             settings_prim: str = "",
                             warnings: Optional[list] = None) -> dict:
    """Overlay over ``usd_in`` that changes render-settings attributes.

    husk exposes only a fixed handful of overrides (``--camera``, ``--output``,
    ``--res``, ``--complexity`` …) and has **no** flag to set an arbitrary
    ``karma:*`` knob. They are ordinary USD attributes on the settings prim, so
    they are set the same way the AOV filter and the output redirect are: a thin
    overlay husk renders in place of the raw export.
    """
    warnings = warnings if warnings is not None else []
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")
    if not overrides:
        return {"usd_out": "", "applied": [], "skipped": []}

    os.makedirs(os.path.dirname(os.path.abspath(usd_out)) or ".", exist_ok=True)
    if os.path.exists(usd_out):
        os.remove(usd_out)

    overlay = Sdf.Layer.CreateNew(usd_out)
    sub_rel = os.path.relpath(os.path.abspath(usd_in),
                              os.path.dirname(os.path.abspath(usd_out)))
    overlay.subLayerPaths.append(sub_rel.replace(os.sep, "/"))

    stage = Usd.Stage.Open(overlay)
    if stage is None:
        warnings.append(f"Render-setting override: could not open {usd_in}")
        return {"usd_out": "", "applied": [], "skipped": []}

    result = _author_settings_opinions(stage, overrides, overlay,
                                       settings_prim, warnings)
    overlay.Save()
    result["usd_out"] = usd_out if (os.path.exists(usd_out) and result["applied"]) else ""
    return result


def author_settings_overlay(stage, overrides: dict, out_path: str,
                            settings_prim: str = "",
                            warnings: Optional[list] = None) -> dict:
    """Standalone render-settings overlay for a **live** stage (hython engine).

    Same idea as :func:`author_relink_overlay`: ``render_direct`` has no
    exported USD to overlay, so the opinions go into their own layer and a
    Sublayer LOP composes them into the network.
    """
    warnings = warnings if warnings is not None else []
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")
    if not overrides:
        return {"out": "", "applied": [], "skipped": []}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    layer = Sdf.Layer.CreateNew(out_path)
    result = _author_settings_opinions(stage, overrides, layer,
                                       settings_prim, warnings)
    layer.Save()
    result["out"] = out_path if (os.path.exists(out_path) and result["applied"]) else ""
    return result


def _author_asset_opinion(layer, attr, value_type, value) -> None:
    """Write a single ``over`` opinion for ``attr`` into ``layer``."""
    prim_path = attr.GetPath().GetPrimPath()
    Sdf.CreatePrimInLayer(layer, prim_path)
    prim_spec = layer.GetPrimAtPath(prim_path)
    prim_spec.specifier = Sdf.SpecifierOver

    attr_spec = layer.GetAttributeAtPath(attr.GetPath())
    if attr_spec is None:
        attr_spec = Sdf.AttributeSpec(prim_spec, attr.GetName(), value_type)
    attr_spec.default = value


def author_relink_overlay(stage, search_dirs, out_path: str,
                          warnings: Optional[list] = None) -> dict:
    """Author a standalone layer of repath opinions for a **live** stage.

    :func:`relink_assets` overlays an exported USD *file*, which the husk engine
    has and the hython engine does not -- ``render_direct`` renders the LOP
    network's own composed stage. So this writes an opinions-only layer (``over``
    prims carrying just the fixed asset paths) that can be sublayered back into
    the network by a Sublayer LOP, which composes **stronger** than the incoming
    stage (verified on 22.0.368).

    Returns ``{"out", "relinked": [...], "still_missing": [...]}``.
    """
    warnings = warnings if warnings is not None else []
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    index = _basename_index(search_dirs)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    layer = Sdf.Layer.CreateNew(out_path)
    relinked: list = []
    still_missing: list = []

    def _find(path: str) -> str:
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        return index.get(base.lower(), "")

    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            type_name = attr.GetTypeName()

            if type_name == Sdf.ValueTypeNames.Asset:
                asset = attr.Get()
                path = (getattr(asset, "path", "") or "") if asset else ""
                resolved = (getattr(asset, "resolvedPath", "") or "") if asset else ""
                if not path or resolved:
                    continue
                new = _find(path)
                if new:
                    _author_asset_opinion(layer, attr, Sdf.ValueTypeNames.Asset,
                                          Sdf.AssetPath(new))
                    relinked.append({"attr": str(attr.GetPath()), "old": path, "new": new})
                else:
                    still_missing.append({"attr": str(attr.GetPath()), "path": path})

            elif type_name == Sdf.ValueTypeNames.AssetArray:
                assets = list(attr.Get() or [])
                new_list, changed = [], False
                for asset in assets:
                    path = getattr(asset, "path", "") or ""
                    resolved = getattr(asset, "resolvedPath", "") or ""
                    if path and not resolved:
                        new = _find(path)
                        if new:
                            new_list.append(Sdf.AssetPath(new))
                            relinked.append({"attr": str(attr.GetPath()),
                                             "old": path, "new": new})
                            changed = True
                            continue
                        still_missing.append({"attr": str(attr.GetPath()), "path": path})
                    new_list.append(asset)
                if changed:
                    _author_asset_opinion(layer, attr, Sdf.ValueTypeNames.AssetArray,
                                          Sdf.AssetPathArray(new_list))

    layer.Save()
    if not relinked:
        warnings.append(
            "Relink: nothing was repathed — no unresolved asset had a "
            "same-named file under the search directories.")
    return {
        "out": out_path if os.path.exists(out_path) else "",
        "relinked": relinked,
        "still_missing": still_missing,
    }


def relink_assets(usd_in: str, usd_out: str, search_dirs) -> dict:
    """Author an overlay that repaths unresolved assets found under search_dirs.

    For each missing asset (empty ``resolvedPath``), the file with the same
    basename found under ``search_dirs`` (first match wins) is authored onto an
    overlay over ``usd_in`` — the same non-destructive overlay approach as the
    AOV filter, so the export is untouched and husk/hython render the repathed
    stage. Returns ``{"usd_out", "relinked": [...], "still_missing": [...]}``.
    """
    if Usd is None or Sdf is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    index = _basename_index(search_dirs)
    os.makedirs(os.path.dirname(os.path.abspath(usd_out)) or ".", exist_ok=True)

    overlay = Sdf.Layer.CreateNew(usd_out)
    sub_rel = os.path.relpath(os.path.abspath(usd_in),
                              os.path.dirname(os.path.abspath(usd_out)))
    overlay.subLayerPaths.append(sub_rel.replace(os.sep, "/"))

    stage = Usd.Stage.Open(overlay)
    if stage is None:
        return {"usd_out": "", "relinked": [], "still_missing": []}

    relinked: list = []
    still_missing: list = []

    def _find(path: str) -> str:
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        return index.get(base.lower(), "")

    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            type_name = attr.GetTypeName()
            if type_name == Sdf.ValueTypeNames.Asset:
                asset = attr.Get()
                path = getattr(asset, "path", "") or "" if asset else ""
                resolved = getattr(asset, "resolvedPath", "") or "" if asset else ""
                if not path or resolved:
                    continue
                new = _find(path)
                if new:
                    attr.Set(Sdf.AssetPath(new))
                    relinked.append({"attr": str(attr.GetPath()), "old": path, "new": new})
                else:
                    still_missing.append({"attr": str(attr.GetPath()), "path": path})
            elif type_name == Sdf.ValueTypeNames.AssetArray:
                assets = list(attr.Get() or [])
                new_list = []
                changed = False
                for asset in assets:
                    path = getattr(asset, "path", "") or ""
                    resolved = getattr(asset, "resolvedPath", "") or ""
                    if path and not resolved:
                        new = _find(path)
                        if new:
                            new_list.append(Sdf.AssetPath(new))
                            relinked.append({"attr": str(attr.GetPath()), "old": path, "new": new})
                            changed = True
                            continue
                        still_missing.append({"attr": str(attr.GetPath()), "path": path})
                    new_list.append(asset)
                if changed:
                    attr.Set(new_list)

    overlay.Save()
    return {
        "usd_out": usd_out if os.path.exists(usd_out) else "",
        "relinked": relinked,
        "still_missing": still_missing,
    }


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

def inspect(hip_path: str, export: bool = False,
            usd_dir: str = "", flatten: bool = False,
            rop_filter: str = "", allow_volume_bake: bool = True,
            export_frames: Optional[tuple] = None,
            frame: Optional[float] = None,
            footprint: bool = False,
            footprint_vdb_files: bool = True) -> SceneManifest:
    """Load a .hip and describe every render ROP in it.

    When ``export`` is requested and a ROP's stage contains live volumes (see
    :func:`scan_live_volumes`), the export is **skipped** unless
    ``allow_volume_bake`` is true -- baking tens of GB per frame is almost never
    what the caller wants, and the hython-direct engine avoids it entirely. The
    skip is recorded in ``manifest.warnings`` so the caller can explain it.

    ``export_frames`` is a ``(start, end, inc)`` that narrows the export to the
    frames actually being rendered. Without it the export covers the ROP's whole
    authored range, which on a heavy scene is most of the cost -- exporting 1-240
    to render frame 12.

    ``frame`` describes the scene as it is **at that frame** rather than at
    whatever frame the .hip happens to open on. It moves the global frame (so
    parameter expressions on ``$F`` evaluate there, which the task scan and the
    ROP frame parms depend on) *and* is passed to each stage cook, so the two
    cannot disagree. Either way ``manifest.inspected_frame`` records the moment
    the description was taken at -- a consumer should never have to guess.

    ``footprint`` additionally counts what the scene *contains* that drives
    render memory -- volumes, textures, geometry and the framebuffer -- into
    ``manifest.footprint``. It is **off by default** because it costs real time
    on a heavy stage: the walk itself is cheap, but reading a point array or
    opening a .vdb is not. It reuses the stage each ROP has already cooked and
    never cooks one of its own, so it adds nothing to the dominant cost.

    ``footprint_vdb_files`` (on by default, and meaningless unless ``footprint``
    is set) controls the one part that reads files: counting the active voxels
    of an on-disk ``.vdb`` means opening it, at roughly 320-350 MB/s, and
    holding that grid in memory while it is counted. Switch it off on a shot
    with very large caches -- the uncounted files are then listed in
    ``footprint.skipped`` rather than silently reading as zero.
    """
    if hou is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    warnings: list[str] = []

    hou.hipFile.load(hip_path, suppress_save_prompt=True,
                     ignore_load_warnings=True)

    # Before anything is read: parameters evaluate at the global frame, so this
    # has to happen ahead of describe_rop()/scan_tasks(), not just the cooks.
    if frame is not None:
        hou.setFrame(float(frame))
    inspected_frame = float(frame) if frame is not None else float(hou.frame())

    manifest = SceneManifest(
        hip_path=os.path.abspath(hip_path),
        houdini_version=".".join(str(v) for v in hou.applicationVersion()),
        fps=hou.fps(),
        inspected_frame=inspected_frame,
    )

    discovered = find_render_rops()
    rop_nodes = ([n for n in discovered if n.path() == rop_filter]
                 if rop_filter else list(discovered))
    if not rop_nodes:
        warnings.append("No USD Render ROPs found in /stage or /out.")

    usd_dir = usd_dir or os.path.join(
        tempfile.gettempdir(), "hsl",
        os.path.splitext(os.path.basename(hip_path))[0],
    )

    # Export filenames are derived from *every* ROP in the scene, not just the
    # --rop subset, so `--rop X` writes the same file a full pass would (C7).
    export_names, name_collisions = _export_usd_names(
        [n.path() for n in discovered])
    if export:
        selected = {n.path() for n in rop_nodes}
        for base, paths in name_collisions:
            if not selected.intersection(paths):
                continue
            mapping = "; ".join(f"{p} -> {export_names[p]}" for p in paths)
            joined = (" and ".join(paths) if len(paths) == 2
                      else ", ".join(paths))
            warnings.append(
                f"Export filename collision: {joined} all flatten to "
                f"'{base}.usd'. Only one file would have survived and the other "
                f"ROPs would have rendered its stage instead of their own, so "
                f"each is exported to a distinct, path-derived filename "
                f"({mapping})."
            )

    seen_settings: dict[str, RenderSettings] = {}
    seen_products: dict[str, RenderProduct] = {}
    seen_vars: dict[str, RenderVar] = {}
    seen_cameras: dict[str, Camera] = {}
    seen_assets: dict[tuple, MissingAsset] = {}
    seen_volumes: dict[str, LiveVolume] = {}
    # Fed the stage each ROP has already cooked, so the footprint costs no
    # extra cook -- which is what makes it affordable on a heavy shot. It reads
    # at `inspected_frame`, because Houdini authors volume fields as time
    # samples with no default value: at the wrong time code they read as empty.
    scan = (_FootprintScan(count_vdb_files=footprint_vdb_files,
                           time_code=inspected_frame)
            if footprint else None)

    for node in rop_nodes:
        rop = describe_rop(node, warnings)

        stage_volumes: list[LiveVolume] = []
        stage, lop = stage_for(node, warnings, frame=frame)
        if stage is not None:
            if not rop.input_lop and lop is not None:
                rop.input_lop = lop.path()

            manifest.stage_start_time_code = stage.GetStartTimeCode()
            manifest.stage_end_time_code = stage.GetEndTimeCode()
            try:
                manifest.default_settings_prim = str(
                    stage.GetMetadata("renderSettingsPrimPath") or ""
                )
            except Exception:
                pass

            settings, products, render_vars, cameras = walk_stage(stage)
            for item in settings:
                seen_settings.setdefault(item.prim_path, item)
            for item in products:
                seen_products.setdefault(item.prim_path, item)
            for item in render_vars:
                seen_vars.setdefault(item.prim_path, item)
            for item in cameras:
                seen_cameras.setdefault(item.prim_path, item)
            # Both scans read at `inspected_frame` for the same reason the
            # footprint does: Houdini authors asset attributes as time samples
            # with no default value, so at the default time code a cached .vdb
            # sequence reads as a live volume and a missing texture reads as
            # nothing at all (docs/UNVERIFIED.md N10).
            for asset in scan_missing_assets(stage, time_code=inspected_frame):
                seen_assets.setdefault((asset.attr_path, asset.asset_path), asset)
            stage_volumes = scan_live_volumes(stage, time_code=inspected_frame)
            for volume in stage_volumes:
                seen_volumes.setdefault(volume.prim_path, volume)

            if scan is not None:
                # A footprint that cannot be counted must not cost the caller
                # the manifest it actually asked for.
                try:
                    scan.scan(stage)
                except Exception as exc:            # noqa: BLE001
                    warnings.append(
                        f"{node.path()}: the footprint scan failed on this "
                        f"stage ({exc}), so its contents are missing from "
                        f"manifest.footprint."
                    )

            # Everything above describes one moment. Say so out loud when
            # another moment would have looked different (TASKS.md T5).
            _check_settings_drift(node, rop, stage, inspected_frame,
                                  {s.prim_path for s in settings}, warnings)
        else:
            warnings.append(f"{node.path()}: could not obtain a USD stage.")

        if export:
            if stage_volumes and not allow_volume_bake:
                # Live volumes bake tens of GB/frame into the export; skip it
                # rather than fill the disk. The hython engine renders the live
                # data with no export at all.
                fields = ", ".join(sorted({f for v in stage_volumes
                                           for f in v.field_names}))
                detail = f" ({fields})" if fields else ""
                warnings.append(
                    f"{node.path()}: skipped USD export -- {len(stage_volumes)} "
                    f"live volume(s){detail} have no on-disk VDB at frame "
                    f"{inspected_frame:g} (OpenVDBAsset.filePath is empty or an "
                    f"op: reference to a SOP) and would bake tens of GB/frame "
                    f"into the export. Render with --engine hython (no export), "
                    f"cache the volumes to .vdb, or pass --allow-volume-bake to "
                    f"export anyway."
                )
            else:
                usd_name = export_names.get(
                    node.path(), _flatten_node_path(node.path()) + ".usd")
                # Export only what is being rendered when the caller said so.
                frame_range = export_frames or (
                    (rop.frame_start, rop.frame_end, rop.frame_inc)
                    if rop.use_frame_range else None)
                rop.usd_path = export_usd(node, os.path.join(usd_dir, usd_name),
                                          frame_range=frame_range,
                                          flatten=flatten, warnings=warnings)

        manifest.rops.append(rop)

    manifest.settings = list(seen_settings.values())
    manifest.products = list(seen_products.values())
    manifest.vars = list(seen_vars.values())
    manifest.cameras = list(seen_cameras.values())
    manifest.missing_assets = list(seen_assets.values())
    manifest.live_volumes = list(seen_volumes.values())
    if scan is not None:
        try:
            manifest.footprint = scan.result()
        except Exception as exc:                    # noqa: BLE001
            warnings.append(f"Could not summarise the scene footprint: {exc}")
        finally:
            # Releases the temporary File SOP and whatever VDB it still holds.
            scan.close()
    # Caches, sims and the non-Solaris ROPs. Failing to describe these must not
    # cost the caller its render manifest, which is what it actually asked for.
    try:
        manifest.tasks = scan_tasks(warnings)
    except Exception as exc:                       # noqa: BLE001
        warnings.append(f"Could not scan cookable tasks: {exc}")
    manifest.warnings = warnings
    return manifest


def _sublayer_into_network(target_rop, layer_path: str, name: str,
                           warnings: list) -> bool:
    """Compose ``layer_path`` into the ROP's input with a Sublayer LOP.

    A Sublayer LOP's file sits **stronger** than the incoming stage (verified on
    22.0.368), which is what makes this work: the overlay's opinions win over
    the scene's. A weaker one would leave the original values in place and the
    render would look fine while ignoring every override.
    """
    inputs = [n for n in target_rop.inputs() if n is not None]
    if not inputs:
        warnings.append(
            f"{target_rop.path()}: has no input LOP, so the {name} overlay "
            f"could not be composed in.")
        return False

    sub = target_rop.parent().createNode("sublayer", name)
    if not _set_parm(sub, "filepath1", layer_path):
        warnings.append(f"Sublayer LOP has no 'filepath1' parameter; {name} skipped.")
        try:
            sub.destroy()
        except Exception:
            pass
        return False

    sub.setInput(0, inputs[0])
    target_rop.setInput(0, sub)
    return True


def _direct_overlay_path(name: str) -> str:
    """Return a process-scoped path for a hython-direct USD overlay.

    Parallel chunks run in separate hython processes. Including the PID keeps
    their relink/settings layers from overwriting one another while each ROP is
    still rendering from its own overlay.
    """
    return os.path.join(
        tempfile.gettempdir(), "hsl", f"{name}_direct_{os.getpid()}.usda",
    )


def _insert_relink_layer(target_rop, search_dirs, warnings: list) -> int:
    """Repath the ROP's unresolved assets by sublayering a relink overlay in.

    Returns the number of assets repathed. The overlay is composed into the
    network with a Sublayer LOP, which sits *stronger* than the incoming stage,
    so the fixed paths win over the scene's broken ones.
    """
    stage, _lop = stage_for(target_rop, warnings)
    if stage is None:
        warnings.append(f"{target_rop.path()}: no stage to relink.")
        return 0

    overlay_path = _direct_overlay_path("relink")
    result = author_relink_overlay(stage, search_dirs, overlay_path, warnings)
    if not result["out"] or not result["relinked"]:
        return 0

    if not _sublayer_into_network(target_rop, result["out"], "hsl_relink", warnings):
        return 0
    if result["still_missing"]:
        warnings.append(f"{len(result['still_missing'])} asset(s) are still "
                        f"unresolved after the relink.")
    return len(result["relinked"])


def render_direct(hip_path: str, rop_path: str = "", frame_start: Optional[int] = None,
                  frame_count: int = 1, frame_inc: int = 1, renderer: str = "",
                  camera: str = "", output: str = "", res: Optional[tuple[int, int]] = None,
                  relink_from=None, settings_overrides: Optional[dict] = None) -> int:
    """Render a LOP ROP directly in hython without exporting USD to disk.

    This is the default engine (``husk.DEFAULT_ENGINE``), so its progress
    reporting has to be real: frames are rendered **one call at a time** and an
    ``ALF_PROGRESS`` line is emitted after each. ``RopNode.render()`` blocks and
    reports nothing while it runs, so rendering a whole range in one call can
    only ever print 0% and then 100% -- which reads as a hung render.

    The trade-off is one ``render()`` call per frame instead of one per range.
    Progress within a single frame is still not available on this path (husk
    gets that from Karma's own ``ALF_PROGRESS``); a one-frame job therefore
    still goes 0% -> 100%.
    """
    if hou is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    hou.hipFile.load(hip_path, suppress_save_prompt=True, ignore_load_warnings=True)

    rop_nodes = find_render_rops()
    if rop_path:
        rop_nodes = [n for n in rop_nodes if n.path() == rop_path]
    if not rop_nodes:
        sys.stderr.write(f"No matching render ROP found in {hip_path}\n")
        return 1

    target_rop = rop_nodes[0]

    # Relink before anything else: it rewires the ROP's input, and everything
    # below reads from that. Unlike the husk path there is no exported USD to
    # overlay, so the repaths are sublayered straight into the network.
    if relink_from:
        relink_warnings: list[str] = []
        count = _insert_relink_layer(target_rop, relink_from, relink_warnings)
        for message in relink_warnings:
            sys.stderr.write(f"warning: {message}\n")
        sys.stderr.write(f"Relinked {count} asset(s) into the LOP network.\n")
        sys.stderr.flush()

    # Karma knobs are USD attributes on the settings prim, so they are overridden
    # the same way — an overlay sublayered into the network. husk has no flag for
    # these either; this is the only route on both engines.
    if settings_overrides:
        setting_warnings: list[str] = []
        stage, _lop = stage_for(target_rop, setting_warnings)
        if stage is None:
            setting_warnings.append("no stage to apply render-setting overrides to.")
        else:
            overlay_path = _direct_overlay_path("settings")
            result = author_settings_overlay(stage, settings_overrides, overlay_path,
                                             warnings=setting_warnings)
            if result["out"] and _sublayer_into_network(
                    target_rop, result["out"], "hsl_settings", setting_warnings):
                for entry in result["applied"]:
                    sys.stderr.write(f"  {entry['key']}: {entry['old']!r} -> "
                                     f"{entry['new']!r}\n")
                sys.stderr.write(f"Applied {len(result['applied'])} render-setting "
                                 f"override(s).\n")
        for message in setting_warnings:
            sys.stderr.write(f"warning: {message}\n")
        sys.stderr.flush()

    # Every override reports whether it actually landed. `resolutionx` /
    # `resolutiony` in particular are unconfirmed on usdrender_rop -- if they
    # are absent the render silently used the scene resolution before this.
    overrides: list[str] = []
    if renderer:
        _apply_override(target_rop, ("renderer", "husk_renderer", "engine"),
                        renderer, "renderer", overrides)
    if camera:
        _apply_override(target_rop, ("override_camera", "camera"),
                        camera, "camera", overrides)
    if output:
        _apply_override(target_rop, ("outputimage", "picture"),
                        output, "output", overrides)
    if res:
        _apply_override(target_rop, ("resolutionx",), res[0], "resolution x", overrides)
        _apply_override(target_rop, ("resolutiony",), res[1], "resolution y", overrides)
    for message in overrides:
        sys.stderr.write(f"warning: {message}\n")
    sys.stderr.flush()

    if frame_start is not None:
        frames = [frame_start + i * frame_inc for i in range(max(1, frame_count))]
        _set_parm(target_rop, "trange", 1)
    else:
        frames = []

    total = len(frames) or 1
    try:
        sys.stdout.write("ALF_PROGRESS 0%\n")
        sys.stdout.flush()

        if frames:
            for index, frame in enumerate(frames, 1):
                # Set the frame parms *and* pass an explicit range, so the two
                # cannot disagree if this build honours only one of them.
                _set_parm(target_rop, "f1", frame)
                _set_parm(target_rop, "f2", frame)
                _set_parm(target_rop, "f3", 1)
                target_rop.render(frame_range=(frame, frame, 1), verbose=True)
                sys.stdout.write("ALF_PROGRESS %d%%\n" % int(index * 100 / total))
                sys.stdout.flush()
        else:
            target_rop.render(verbose=True)
            sys.stdout.write("ALF_PROGRESS 100%\n")
            sys.stdout.flush()
        return 0
    except hou.Error as exc:
        sys.stderr.write(f"Direct render failed on {target_rop.path()}: {exc}\n")
        return 1


def cook_task(hip_path: str, node_path: str, frame_start: Optional[int] = None,
              frame_count: int = 1, frame_inc: int = 1,
              sequential: bool = False, output: str = "") -> int:
    """Cook a cache, a simulation or a non-Solaris ROP under hython.

    Progress mirrors :func:`render_direct`: one ``render()`` call per frame
    with an ``ALF_PROGRESS`` line after each, so the queue's bars move.

    **Except when the task is sequential.** Frame N of a simulation depends on
    N-1, and separate ``render()`` calls do not carry solver state across them
    -- so the whole range goes in a single call instead, which can only report
    0% and then 100%. A truthful pair of numbers beats a smooth bar over a
    corrupted cache.
    """
    if hou is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    hou.hipFile.load(hip_path, suppress_save_prompt=True, ignore_load_warnings=True)

    node = hou.node(node_path)
    if node is None:
        sys.stderr.write(f"No node at {node_path} in {hip_path}\n")
        return 1

    warnings: list[str] = []
    target = _cookable_node(node, warnings)
    if output:
        # Prefer the node the user pointed at -- on a File Cache SOP that is
        # the parameter they can see. _apply_override records it if neither
        # node has the parameter, rather than letting the override vanish.
        names = TASK_OUTPUT_PARMS.get(_type_name(node), ())
        if names and not _apply_override(node, names, output, "output", warnings):
            if target is not None:
                _apply_override(target, TASK_OUTPUT_PARMS.get(_type_name(target), ()),
                                output, "output", warnings)
    for message in warnings:
        sys.stderr.write(f"warning: {message}\n")
    sys.stderr.flush()
    if target is None:
        return 1

    frames = ([] if frame_start is None
              else [frame_start + i * frame_inc for i in range(max(1, frame_count))])

    try:
        sys.stdout.write("ALF_PROGRESS 0%\n")
        sys.stdout.flush()

        if frames and not sequential:
            total = len(frames)
            for index, frame in enumerate(frames, 1):
                target.render(frame_range=(frame, frame, 1), verbose=False)
                sys.stdout.write("ALF_PROGRESS %d%%\n" % int(index * 100 / total))
                sys.stdout.flush()
        else:
            if frames:
                target.render(frame_range=(frames[0], frames[-1], frame_inc),
                              verbose=False)
            else:
                target.render(verbose=False)
            sys.stdout.write("ALF_PROGRESS 100%\n")
            sys.stdout.flush()
        return 0
    except hou.Error as exc:
        sys.stderr.write(f"Cook failed on {node.path()}: {exc}\n")
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hython -m hsl.inspector",
        description="Describe the renderable state of a Houdini .hip file.",
    )
    parser.add_argument("hip", nargs="?", default="",
                        help="Path to the .hip file (omitted for --filter-aovs)")
    parser.add_argument("--json", dest="json_out", default="",
                        help="Write the manifest here (default: stdout)")
    parser.add_argument("--export-usd", action="store_true",
                        help="Also write each ROP's stage to disk for husk")
    parser.add_argument("--usd-dir", default="",
                        help="Where exported USD files go")
    parser.add_argument("--flatten", action="store_true",
                        help="Flatten the stage on export (portable, larger)")
    parser.add_argument("--rop", default="",
                        help="Only inspect this ROP path")
    parser.add_argument("--frame", type=float, default=None,
                        help="Describe the scene as it is at this frame "
                             "(default: the frame the .hip opens on). The "
                             "manifest always records which frame it used.")
    parser.add_argument("--export-frames", nargs=3, type=int, default=None,
                        metavar=("START", "END", "INC"),
                        help="Export only this range instead of the ROP's full "
                             "authored range")
    parser.add_argument("--allow-volume-bake", action="store_true",
                        help="Export even when live volumes would bake ~GB/frame "
                             "(default: skip the export for such ROPs)")
    parser.add_argument("--footprint", action="store_true",
                        help="Also count what the scene contains that drives "
                             "render memory (volumes, textures, geometry, "
                             "framebuffer) into manifest.footprint. Off by "
                             "default: it costs real time on a heavy stage")
    parser.add_argument("--footprint-no-vdb-files", action="store_true",
                        help="With --footprint, do not open on-disk .vdb files "
                             "to count their active voxels. That part reads "
                             "each file (~320-350 MB/s) and holds the grid in "
                             "memory; the files it skips are listed in "
                             "footprint.skipped rather than counted as zero")
    parser.add_argument("--render-direct", action="store_true",
                        help="Render the ROP directly inside hython (0 USD disk space)")
    parser.add_argument("--cook", action="store_true",
                        help="Cook a cache/simulation/non-Solaris ROP instead "
                             "of rendering (needs --rop)")
    parser.add_argument("--sequential", action="store_true",
                        help="Cook the whole range in one call, in order. "
                             "Required for simulations: frame N depends on N-1")
    parser.add_argument("--frame-start", type=int, default=None, help="Start frame")
    parser.add_argument("--frame-count", type=int, default=1, help="Frame count")
    parser.add_argument("--frame-inc", type=int, default=1, help="Frame increment")
    parser.add_argument("--renderer", default="", help="Renderer delegate")
    parser.add_argument("--camera", default="", help="Camera override")
    parser.add_argument("--output", default="", help="Output image override")
    parser.add_argument("--res", nargs=2, type=int, default=None, help="Resolution X Y")
    parser.add_argument("--filter-aovs", action="store_true",
                        help="Write an overlay USD keeping only --keep RenderVars")
    parser.add_argument("--usd-in", default="", help="Source USD for --filter-aovs")
    parser.add_argument("--usd-out", default="", help="Overlay USD to write")
    parser.add_argument("--keep", default="",
                        help="Comma-separated RenderVar prim paths to keep")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        dest="settings",
                        help="Override a render-settings attribute, e.g. "
                             "karma:global:samplesperpixel=64 (repeatable)")
    parser.add_argument("--override-settings", action="store_true",
                        help="Write an overlay applying --set to --usd-in")
    parser.add_argument("--override-output", default="",
                        help="Author an overlay redirecting every RenderProduct "
                             "to this file or directory (husk -o only moves the first)")
    parser.add_argument("--relink", action="store_true",
                        help="Repath unresolved assets found under --search dirs")
    parser.add_argument("--search", action="append", default=[],
                        help="Directory to search for missing assets (repeatable)")
    args = parser.parse_args(argv)

    if args.filter_aovs:
        keep = [p for p in args.keep.split(",") if p]
        warnings: list[str] = []
        out = filter_usd_aovs(args.usd_in, args.usd_out, keep, warnings)
        for warning in warnings:
            sys.stderr.write(f"{warning}\n")
        if not out:
            return 1
        sys.stdout.write(out + "\n")
        return 0

    settings_overrides = _parse_settings(args.settings)

    if args.override_settings:
        warnings: list[str] = []
        result = override_render_settings(args.usd_in, args.usd_out,
                                          settings_overrides,
                                          settings_prim=args.rop, warnings=warnings)
        for warning in warnings:
            sys.stderr.write(f"{warning}\n")
        # Sentinel-tagged so the caller finds it past any Houdini banners.
        sys.stdout.write("@@HSL_SETTINGS@@" + json.dumps(result) + "\n")
        sys.stdout.flush()
        return 0 if result.get("usd_out") else 1

    if args.override_output:
        warnings: list[str] = []
        out = override_product_paths(args.usd_in, args.usd_out,
                                     args.override_output, warnings)
        for warning in warnings:
            sys.stderr.write(f"{warning}\n")
        if not out:
            return 1
        sys.stdout.write(out + "\n")
        return 0

    if args.relink:
        result = relink_assets(args.usd_in, args.usd_out, args.search)
        # Sentinel-tagged so the caller finds it past any Houdini/delegate banners.
        sys.stdout.write("@@HSL_RELINK@@" + json.dumps(result) + "\n")
        return 0 if result.get("usd_out") else 1

    if not args.hip:
        parser.error("hip path is required unless --filter-aovs is given")

    if args.cook:
        if not args.rop:
            parser.error("--cook needs --rop to say which node to cook")
        return cook_task(args.hip, args.rop, frame_start=args.frame_start,
                         frame_count=args.frame_count, frame_inc=args.frame_inc,
                         sequential=args.sequential, output=args.output)

    if args.render_direct:
        res = tuple(args.res) if args.res else None
        return render_direct(args.hip, rop_path=args.rop, frame_start=args.frame_start,
                             frame_count=args.frame_count, frame_inc=args.frame_inc,
                             renderer=args.renderer, camera=args.camera,
                             output=args.output, res=res,
                             relink_from=args.search or None,
                             settings_overrides=settings_overrides or None)

    try:
        manifest = inspect(args.hip, export=args.export_usd,
                           usd_dir=args.usd_dir, flatten=args.flatten,
                           rop_filter=args.rop,
                           allow_volume_bake=args.allow_volume_bake,
                           export_frames=(tuple(args.export_frames)
                                          if args.export_frames else None),
                           frame=args.frame,
                           footprint=args.footprint,
                           footprint_vdb_files=not args.footprint_no_vdb_files)
    except Exception:
        # The launcher parses stdout as JSON, so failures must be structured.
        error = {"schema_version": 0, "error": traceback.format_exc()}
        payload = json.dumps(error, indent=2)
        if args.json_out:
            with open(args.json_out, "w") as handle:
                handle.write(payload)
        else:
            sys.stdout.write(payload)
        return 1

    payload = manifest.to_json()
    if args.json_out:
        with open(args.json_out, "w") as handle:
            handle.write(payload)
        sys.stderr.write(f"Wrote {args.json_out}\n")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
