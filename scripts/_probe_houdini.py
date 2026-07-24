#!/usr/bin/env hython
"""Houdini-side half of verify_environment.py. **Run under hython.**

Builds a throwaway Solaris network from scratch and checks every node type,
parameter name and USD schema call that hsl/inspector.py assumes. Self-contained
— it does not need a .hip file, so it can be run on any machine with Houdini
before the tool has ever seen a real scene.

Prints a single JSON array on the last stdout line; Houdini's own banner and
license output surrounds it, which is why the caller scans upward for it.
"""

from __future__ import annotations

import json
import sys
import traceback

OK, MISSING, SKIP = "OK", "MISSING", "SKIP"
results = []

# The caller (verify_environment.py) finds our JSON by this sentinel, so a
# render delegate such as Octane printing "[Octane] ..." banners around our
# output cannot be mistaken for the payload.
PROBE_SENTINEL = "@@HSL_PROBE_JSON@@"


def record(ident, item, status, note=""):
    results.append({"id": ident, "item": item, "status": status, "note": str(note)[:200]})


def emit_and_exit(code=0):
    print(PROBE_SENTINEL + json.dumps(results))
    sys.exit(code)


try:
    import hou
except ImportError:
    record("F1", "hou importable", MISSING, "not running under hython")
    emit_and_exit(1)

try:
    from pxr import Usd, UsdGeom, UsdRender  # noqa: F401
    record("D0", "pxr importable", OK, Usd.GetVersion())
except Exception as exc:  # pragma: no cover
    record("D0", "pxr importable", MISSING, exc)
    emit_and_exit(1)

record("F1", "Houdini version", OK, ".".join(str(v) for v in hou.applicationVersion()))

stage_ctx = hou.node("/stage")

# -- A. node types ---------------------------------------------------------

NODE_TYPES = [
    ("A1", "usdrender_rop"),
    ("A4", "usd_rop"),
    ("A2", "karma"),
    ("--", "rendersettings"),
    ("--", "camera"),
    ("--", "sphere"),
]

created = {}
for ident, type_name in NODE_TYPES:
    try:
        node = stage_ctx.createNode(type_name, f"probe_{type_name}")
        created[type_name] = node
        record(ident, f"node type {type_name}", OK, node.type().name())
    except hou.OperationFailed as exc:
        record(ident, f"node type {type_name}", MISSING, exc)

# -- B. render ROP parameters ---------------------------------------------

ROP_PARMS = [
    ("B1", "trange"), ("B2a", "f1"), ("B2b", "f2"), ("B2c", "f3"),
    ("B3", "renderer"), ("B4", "rendersettings"), ("B5", "override_camera"),
    ("B6", "outputimage"),
]

rop = created.get("usdrender_rop")
if rop is None:
    for ident, name in ROP_PARMS:
        record(ident, f"usdrender_rop.{name}", SKIP, "ROP could not be created")
else:
    available = sorted(p.name() for p in rop.parms())
    record("B0", "usdrender_rop parm count", OK, len(available))
    for ident, name in ROP_PARMS:
        if rop.parm(name) is not None or rop.parmTuple(name) is not None:
            record(ident, f"usdrender_rop.{name}", OK)
        else:
            near = [p for p in available if name[:4] in p][:5]
            record(ident, f"usdrender_rop.{name}", MISSING,
                   f"absent; similar: {near}" if near else "absent, no near match")

# -- C. USD ROP parameters -------------------------------------------------

USD_ROP_PARMS = [
    ("C1", "lopoutput"), ("C2a", "trange"), ("C2b", "f1"),
    ("C3", "fileperframe"), ("C4", "savestyle"),
    ("C5", "enableoutputprocessor_simplerelativepaths"),
]

usd_rop = created.get("usd_rop")
if usd_rop is None:
    for ident, name in USD_ROP_PARMS:
        record(ident, f"usd_rop.{name}", SKIP, "usd_rop could not be created")
else:
    for ident, name in USD_ROP_PARMS:
        parm = usd_rop.parm(name)
        if parm is None:
            record(ident, f"usd_rop.{name}", MISSING, "absent")
        elif name == "savestyle":
            try:
                tokens = parm.parmTemplate().menuItems()
                status = OK if "flattenalllayers" in tokens else MISSING
                record(ident, "usd_rop.savestyle=flattenalllayers", status, list(tokens))
            except Exception as exc:
                record(ident, "usd_rop.savestyle menu", SKIP, exc)
        else:
            record(ident, f"usd_rop.{name}", OK)

# -- D. USD schema round trip ---------------------------------------------

try:
    settings_lop = created.get("rendersettings")
    camera_lop = created.get("camera")
    if settings_lop and camera_lop:
        settings_lop.setInput(0, camera_lop)
        stage = settings_lop.stage()

        if stage is None:
            record("D5", "LopNode.stage()", MISSING, "returned None")
        else:
            record("D5", "LopNode.stage()", OK)

            typed = {"settings": 0, "product": 0, "var": 0, "camera": 0}
            untyped_render = []
            # Pure organisational containers — not something that should have
            # been a RenderVar/Product. `/Render` itself is legitimately a Scope.
            CONTAINER_TYPES = {"", "Scope", "Xform"}
            for prim in stage.Traverse():
                path = str(prim.GetPath())
                if prim.IsA(UsdRender.Settings):
                    typed["settings"] += 1
                elif prim.IsA(UsdRender.Product):
                    typed["product"] += 1
                elif prim.IsA(UsdRender.Var):
                    typed["var"] += 1
                elif prim.IsA(UsdGeom.Camera):
                    typed["camera"] += 1
                elif ("/Vars" in path or "/Products" in path) and \
                        str(prim.GetTypeName()) not in CONTAINER_TYPES:
                    # A prim sitting exactly where a RenderVar/Product belongs
                    # but not typed as one — the D2 failure mode itself.
                    untyped_render.append(f"{path}<{prim.GetTypeName()}>")

            record("D1", "IsA(UsdRender.Settings)",
                   OK if typed["settings"] else MISSING, typed)
            # D2 silently empties the AOV list when true, but it can only be
            # judged against a scene that actually authors products/vars. A bare
            # rendersettings LOP authors neither, so say SKIP rather than emit a
            # misleading MISSING off the /Render Scope container.
            if untyped_render:
                record("D2", "UsdRender.Var prims are typed", MISSING,
                       f"untyped where vars/products expected: {untyped_render[:5]}")
            elif typed["var"] or typed["product"]:
                record("D2", "UsdRender.Var prims are typed", OK, typed)
            else:
                record("D2", "UsdRender.Var prims are typed", SKIP,
                       "probe scene authors no products/vars; needs a real scene")
            record("--", "IsA(UsdGeom.Camera)",
                   OK if typed["camera"] else MISSING, typed)

            try:
                meta = stage.GetMetadata("renderSettingsPrimPath")
                record("D3", "stage metadata renderSettingsPrimPath", OK, meta)
            except Exception as exc:
                record("D3", "stage metadata renderSettingsPrimPath", MISSING, exc)
    else:
        record("D1", "USD schema probe", SKIP, "probe LOPs unavailable")
except Exception:
    record("D1", "USD schema probe", MISSING, traceback.format_exc()[-200:])

# -- F. bridge mechanics ---------------------------------------------------

try:
    import hsl.inspector as inspector
    record("F4", "hython can import hsl.inspector", OK, inspector.__file__)
    if rop is not None:
        described = inspector.describe_rop(rop, [])
        record("F4b", "describe_rop() runs", OK,
               f"{described.node_path} frames {described.frame_start}-{described.frame_end}")
except Exception:
    record("F4", "hython can import hsl.inspector", MISSING,
           traceback.format_exc()[-200:])

try:
    sig = hou.hipFile.load
    record("F5", "hou.hipFile.load available", OK, str(sig)[:80])
except Exception as exc:
    record("F5", "hou.hipFile.load available", MISSING, exc)

emit_and_exit(0)
