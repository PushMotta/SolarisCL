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

        # `fieldName` is authored empty on every SOP-imported field observed on
        # Houdini 22.0.368 (8/8 on SandBurst) -- the prim *name* carries it
        # ("density", "vel"). So the fallback is load-bearing, not defensive:
        # without it every warning would name its fields as empty strings.
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
            for name, value in (("trange", 1), ("f1", start), ("f2", end),
                                ("f3", inc), ("fileperframe", 0)):
                if not _set_parm(tmp, name, value):
                    warnings.append(
                        f"{rop_node.path()}: usd_rop has no '{name}' parameter on "
                        f"this build; the exported range may not be {start}-{end}"
                        f"x{inc} as requested."
                    )
        elif not _set_parm(tmp, "trange", 0):
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
            export_frames: Optional[tuple] = None) -> SceneManifest:
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

    overlay_path = os.path.join(tempfile.gettempdir(), "hsl", "relink_direct.usda")
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
            overlay_path = os.path.join(tempfile.gettempdir(), "hsl",
                                        "settings_direct.usda")
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
    parser.add_argument("--export-frames", nargs=3, type=int, default=None,
                        metavar=("START", "END", "INC"),
                        help="Export only this range instead of the ROP's full "
                             "authored range")
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
                                          if args.export_frames else None))
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
