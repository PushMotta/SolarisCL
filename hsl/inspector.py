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
    Camera, RenderProduct, RenderRop, RenderSettings, RenderVar, SceneManifest,
)

try:
    import hou
except ImportError:  # pragma: no cover - only importable inside hython
    hou = None

try:
    from pxr import Usd, UsdGeom, UsdRender
except ImportError:  # pragma: no cover
    Usd = UsdGeom = UsdRender = None


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


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

def inspect(hip_path: str, export: bool = False,
            usd_dir: str = "", flatten: bool = False,
            rop_filter: str = "") -> SceneManifest:
    """Load a .hip and describe every render ROP in it."""
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

    for node in rop_nodes:
        rop = describe_rop(node, warnings)

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
        else:
            warnings.append(f"{node.path()}: could not obtain a USD stage.")

        if export:
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
    manifest.warnings = warnings
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hython -m hsl.inspector",
        description="Describe the renderable state of a Houdini .hip file.",
    )
    parser.add_argument("hip", help="Path to the .hip file")
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
    args = parser.parse_args(argv)

    try:
        manifest = inspect(args.hip, export=args.export_usd,
                           usd_dir=args.usd_dir, flatten=args.flatten,
                           rop_filter=args.rop)
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
