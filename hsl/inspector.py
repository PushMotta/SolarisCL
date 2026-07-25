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
import json
import os
import sys
import tempfile
import traceback
from typing import Any, Iterable, Optional

from .manifest import (
    Camera, LiveVolume, MissingAsset, RenderProduct, RenderRop, RenderSettings,
    RenderVar, SceneManifest,
)

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
USD_ROP_TYPES = {"usd_rop", "usd", "usdexport"}

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


def scan_missing_assets(stage) -> list[MissingAsset]:
    """Asset-path attributes on the stage whose value does not resolve on disk.

    Every asset-valued attribute (texture ``inputs:file``, volume filenames,
    etc.) carries an ``Sdf.AssetPath`` with a ``resolvedPath``. An authored path
    with an **empty** ``resolvedPath`` is one the resolver could not find — a
    missing texture. This is exactly what husk would fail on mid-render, caught
    here at read time instead. Deduplicated on (attribute, path).
    """
    missing: list[MissingAsset] = []
    seen: set = set()

    def _check(attr, asset):
        if asset is None:
            return
        path = getattr(asset, "path", "") or ""
        resolved = getattr(asset, "resolvedPath", "") or ""
        if not path or resolved:
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
                _check(attr, attr.Get())
            elif type_name == Sdf.ValueTypeNames.AssetArray:
                for asset in (attr.Get() or []):
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


def scan_live_volumes(stage) -> list[LiveVolume]:
    """Volumes whose OpenVDB fields have no on-disk ``.vdb`` -- they bake on export.

    A ``UsdVolOpenVDBAsset`` with an **empty** ``filePath`` carries no reference
    to a file on disk: its voxels are live in the composed stage (SOP-imported).
    Exporting such a stage *bakes* the volume into the exported layer -- tens of
    GB per frame on a real shot, versus a few MB when a ``.vdb`` is referenced.
    The husk (USD-export) path pays that cost every frame; the hython-direct
    engine renders the live data without exporting.

    A field whose ``filePath`` *is* authored (even one that fails to resolve) is
    **not** a bake -- that is a missing VDB, reported by
    :func:`scan_missing_assets` instead. Results are grouped by the owning
    ``UsdVolVolume`` prim so a caller can say "N live volumes".
    """
    if UsdVol is None:
        return []

    order: list[str] = []
    fields_by_volume: dict[str, list[str]] = {}

    for prim in stage.Traverse():
        if not prim.IsA(UsdVol.OpenVDBAsset):
            continue
        asset = UsdVol.OpenVDBAsset(prim)
        file_attr = asset.GetFilePathAttr()
        value = file_attr.Get() if file_attr else None
        path = (getattr(value, "path", "") or "") if value is not None else ""
        if path:
            continue                      # references a real .vdb -- cheap to export

        name_attr = asset.GetFieldNameAttr()
        field_name = str((name_attr.Get() if name_attr else "") or prim.GetName())

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


def stage_for(node, warnings: list[str]):
    """Cook a LOP (or a ROP's input LOP) and return its composed stage."""
    candidates = [node] + [n for n in node.inputs() if n is not None]
    for candidate in candidates:
        if not isinstance(candidate, hou.LopNode):
            continue
        try:
            stage = candidate.stage()
        except hou.Error as exc:
            warnings.append(f"{candidate.path()}: cook failed -- {exc}")
            continue
        if stage is not None:
            return stage, candidate
    return None, None


# --------------------------------------------------------------------------
# USD export
# --------------------------------------------------------------------------

def export_usd(rop_node, out_path: str, frame_range=None,
               flatten: bool = False, warnings: Optional[list] = None) -> str:
    """Write the ROP's input stage to disk so husk can render it.

    Uses a temporary ``usd_rop`` rather than ``Usd.Stage.Export()``. That
    matters: ``Export()`` serialises the stage as cooked at a single time, so
    animation, motion blur samples and per-frame value clips are silently lost.
    The USD ROP re-cooks per frame and handles all of that.
    """
    warnings = warnings if warnings is not None else []
    parent = rop_node.parent()
    inputs = [n for n in rop_node.inputs() if n is not None]
    source = inputs[0] if inputs else rop_node

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    tmp = parent.createNode("usd_rop", "hsl_tmp_export")
    try:
        tmp.setInput(0, source)
        _set_parm(tmp, "lopoutput", out_path)
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
            _set_parm(tmp, "trange", 1)
            _set_parm(tmp, "f1", start)
            _set_parm(tmp, "f2", end)
            _set_parm(tmp, "f3", inc)
            # One self-contained file keeps the husk command simple.
            _set_parm(tmp, "fileperframe", 0)
        else:
            _set_parm(tmp, "trange", 0)

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
            rop_filter: str = "", allow_volume_bake: bool = True) -> SceneManifest:
    """Load a .hip and describe every render ROP in it.

    When ``export`` is requested and a ROP's stage contains live volumes (see
    :func:`scan_live_volumes`), the export is **skipped** unless
    ``allow_volume_bake`` is true -- baking tens of GB per frame is almost never
    what the caller wants, and the hython-direct engine avoids it entirely. The
    skip is recorded in ``manifest.warnings`` so the caller can explain it.
    """
    if hou is None:
        raise RuntimeError("hsl.inspector must be run under hython, not system Python.")

    warnings: list[str] = []

    hou.hipFile.load(hip_path, suppress_save_prompt=True,
                     ignore_load_warnings=True)

    manifest = SceneManifest(
        hip_path=os.path.abspath(hip_path),
        houdini_version=".".join(str(v) for v in hou.applicationVersion()),
        fps=hou.fps(),
    )

    rop_nodes = find_render_rops()
    if rop_filter:
        rop_nodes = [n for n in rop_nodes if n.path() == rop_filter]
    if not rop_nodes:
        warnings.append("No USD Render ROPs found in /stage or /out.")

    usd_dir = usd_dir or os.path.join(
        tempfile.gettempdir(), "hsl",
        os.path.splitext(os.path.basename(hip_path))[0],
    )

    seen_settings: dict[str, RenderSettings] = {}
    seen_products: dict[str, RenderProduct] = {}
    seen_vars: dict[str, RenderVar] = {}
    seen_cameras: dict[str, Camera] = {}
    seen_assets: dict[tuple, MissingAsset] = {}
    seen_volumes: dict[str, LiveVolume] = {}

    for node in rop_nodes:
        rop = describe_rop(node, warnings)

        stage_volumes: list[LiveVolume] = []
        stage, lop = stage_for(node, warnings)
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
            for asset in scan_missing_assets(stage):
                seen_assets.setdefault((asset.attr_path, asset.asset_path), asset)
            stage_volumes = scan_live_volumes(stage)
            for volume in stage_volumes:
                seen_volumes.setdefault(volume.prim_path, volume)
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
                    f"live volume(s){detail} have no on-disk VDB "
                    f"(OpenVDBAsset.filePath empty) and would bake tens of "
                    f"GB/frame into the export. Render with --engine hython "
                    f"(no export), cache the volumes to .vdb, or pass "
                    f"--allow-volume-bake to export anyway."
                )
            else:
                usd_name = node.path().strip("/").replace("/", "_") + ".usd"
                frame_range = ((rop.frame_start, rop.frame_end, rop.frame_inc)
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
    manifest.warnings = warnings
    return manifest


def render_direct(hip_path: str, rop_path: str = "", frame_start: Optional[int] = None,
                  frame_count: int = 1, frame_inc: int = 1, renderer: str = "",
                  camera: str = "", output: str = "", res: Optional[tuple[int, int]] = None) -> int:
    """Render a LOP ROP directly in hython without exporting USD to disk."""
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

    if renderer:
        _set_parm(target_rop, "renderer", renderer)
        _set_parm(target_rop, "husk_renderer", renderer)
        _set_parm(target_rop, "engine", renderer)
    if camera:
        _set_parm(target_rop, "override_camera", camera)
        _set_parm(target_rop, "camera", camera)
    if output:
        _set_parm(target_rop, "outputimage", output)
        _set_parm(target_rop, "picture", output)
    if res:
        _set_parm(target_rop, "resolutionx", res[0])
        _set_parm(target_rop, "resolutiony", res[1])

    if frame_start is not None:
        f1 = frame_start
        f2 = frame_start + (frame_count - 1) * frame_inc
        _set_parm(target_rop, "trange", 1)
        _set_parm(target_rop, "f1", f1)
        _set_parm(target_rop, "f2", f2)
        _set_parm(target_rop, "f3", frame_inc)
        frame_range = (f1, f2, frame_inc)
    else:
        frame_range = ()

    try:
        sys.stdout.write("ALF_PROGRESS 0%\n")
        sys.stdout.flush()
        target_rop.render(frame_range=frame_range if frame_range else (), verbose=True)
        sys.stdout.write("ALF_PROGRESS 100%\n")
        sys.stdout.flush()
        return 0
    except hou.Error as exc:
        sys.stderr.write(f"Direct render failed on {target_rop.path()}: {exc}\n")
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
    parser.add_argument("--allow-volume-bake", action="store_true",
                        help="Export even when live volumes would bake ~GB/frame "
                             "(default: skip the export for such ROPs)")
    parser.add_argument("--render-direct", action="store_true",
                        help="Render the ROP directly inside hython (0 USD disk space)")
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

    if args.relink:
        result = relink_assets(args.usd_in, args.usd_out, args.search)
        # Sentinel-tagged so the caller finds it past any Houdini/delegate banners.
        sys.stdout.write("@@HSL_RELINK@@" + json.dumps(result) + "\n")
        return 0 if result.get("usd_out") else 1

    if not args.hip:
        parser.error("hip path is required unless --filter-aovs is given")

    if args.render_direct:
        res = tuple(args.res) if args.res else None
        return render_direct(args.hip, rop_path=args.rop, frame_start=args.frame_start,
                             frame_count=args.frame_count, frame_inc=args.frame_inc,
                             renderer=args.renderer, camera=args.camera,
                             output=args.output, res=res)

    try:
        manifest = inspect(args.hip, export=args.export_usd,
                           usd_dir=args.usd_dir, flatten=args.flatten,
                           rop_filter=args.rop,
                           allow_volume_bake=args.allow_volume_bake)
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
