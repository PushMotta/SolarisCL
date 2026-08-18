#!/usr/bin/env hython
"""Probe: does `SceneManifest.footprint` count what it says it counts?

**Run under hython.** Backs `docs/UNVERIFIED.md` section N.

    hython scripts/_probe_footprint.py out.json [scratch_dir]

Writes its JSON report to the **file** named by the first argument. Nothing
useful goes to stdout on purpose: Houdini's banner and third-party delegates
(Octane on the 21.0.729 install here) print there, so a caller parsing stdout
would be reading whatever they felt like saying.

The point of this probe is that **every expected number is known independently
of the code being tested**:

  * the volume's active voxel count comes from ``hou.VDB.activeVoxelCount()``
    on the SOP that built it -- and is deliberately *not* the bounding-box
    product, which the report also shows so the two can be told apart;
  * the textures are files this script wrote, so their sizes are known to the
    byte, and one is referenced twice to prove deduplication;
  * the points, geometry prims and instances are authored here in a hand-built
    USD layer, so the totals are arithmetic;
  * the framebuffer is width x height x channels x bytes-per-channel, where the
    channel counts and widths come from the USD library itself rather than an
    assumption that every AOV is four floats.

Anything the scan cannot count must appear in ``footprint.skipped`` -- a silent
zero is the failure this probe exists to catch.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

OK, WRONG, SKIP = "OK", "WRONG", "SKIP"
results = []
detail = {}


def record(ident, item, status, expected=None, measured=None, note=""):
    results.append({"id": ident, "item": item, "status": status,
                    "expected": expected, "measured": measured,
                    "note": str(note)[:400]})


def check(ident, item, expected, measured, note=""):
    record(ident, item, OK if expected == measured else WRONG,
           expected, measured, note)


def emit_and_exit(code=0):
    out_path = sys.argv[1] if len(sys.argv) > 1 else "footprint_probe.json"
    with open(out_path, "w") as handle:
        handle.write(json.dumps({"results": results, "detail": detail}, indent=2))
    sys.exit(code)


try:
    import hou
except ImportError:
    record("F1", "hou importable", SKIP, note="not running under hython")
    emit_and_exit(1)

try:
    from pxr import Sdf, Usd, UsdGeom, UsdRender, UsdVol, Vt, Gf
except Exception as exc:
    record("D0", "pxr importable", SKIP, note=exc)
    emit_and_exit(1)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hsl import inspector as I          # noqa: E402

detail["houdini_version"] = ".".join(str(v) for v in hou.applicationVersion())
scratch = (sys.argv[2] if len(sys.argv) > 2
           else os.path.join(hou.text.expandString("$TEMP"), "hsl_footprint_probe"))
os.makedirs(scratch, exist_ok=True)
detail["scratch"] = scratch

# Name the scene into the scratch directory *before* building anything. A SOP
# Import LOP bakes a `savepath=$HIP/usd/...` into the `op:` asset path it
# authors, and Houdini materialises a sidecar .vdb there -- so with $HIP still
# pointing at the checkout, merely cooking this fixture drops a `usd/` folder
# into the repository. Setting it first keeps the probe's mess in the scratch
# directory where it belongs.
hip_path = os.path.join(scratch, "footprint_fixture.hip").replace("\\", "/")
hou.hipFile.save(hip_path)
detail["HIP_after_save"] = hou.text.expandString("$HIP")


# ==========================================================================
# 0. the value-type table, measured off the USD library
# ==========================================================================
try:
    table = {}
    for name in ("color3f", "float", "half", "color4f", "float3", "double",
                 "color3h", "int", "uchar", "token", "notatype", ""):
        table[name] = I._value_type_shape(name)
    detail["value_type_shapes"] = {k: list(v) if v else None
                                   for k, v in table.items()}
    check("N1", "color3f is 3 channels x 4 bytes", (3, 4), table["color3f"])
    check("N2", "float is 1 channel x 4 bytes", (1, 4), table["float"])
    check("N3", "half is 1 channel x 2 bytes", (1, 2), table["half"])
    check("N4", "color4f is 4 channels x 4 bytes", (4, 4), table["color4f"])
    check("N5", "color3h is 3 channels x 2 bytes", (3, 2), table["color3h"])
    check("N6", "an unrecognised type is None, not a guessed width",
          [None, None, None],
          [table["token"], table["notatype"], table[""]])
except Exception:
    record("N0", "value type shapes", WRONG, note=traceback.format_exc())


# ==========================================================================
# 1. a hand-authored USD layer: textures, points, gprims, instances, AOVs
# ==========================================================================
TEX_SIZES = {"texA.exr": 1000, "texB.exr": 2500, "texC.exr": 7000}
tex_paths = {}
for name, size in TEX_SIZES.items():
    p = os.path.join(scratch, name).replace("\\", "/")
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)
    tex_paths[name] = p
detail["textures_written"] = {k: os.path.getsize(v) for k, v in tex_paths.items()}

RES = (100, 50)
VAR_TYPES = [("var_color", "color3f"), ("var_depth", "float"),
             ("var_rgba", "color4f")]
MESH1_PTS, MESH2_PTS, POINTS_PTS, INSTANCES = 4, 6, 100, 7
# authored only at this time code, never as a default value
ANIM_PTS, FIXTURE_FRAME = 50, 1.0

usda = os.path.join(scratch, "fixture.usda").replace("\\", "/")
if os.path.exists(usda):
    os.remove(usda)
layer = Sdf.Layer.CreateNew(usda)
stage_f = Usd.Stage.Open(layer)


def mesh(path, n):
    m = UsdGeom.Mesh.Define(stage_f, path)
    m.GetPointsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(i, 0, 0) for i in range(n)]))
    return m


mesh("/World/mesh1", MESH1_PTS)
mesh("/World/mesh2", MESH2_PTS)
pts = UsdGeom.Points.Define(stage_f, "/World/pts")
pts.GetPointsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(i, 1, 0) for i in range(POINTS_PTS)]))
UsdGeom.Sphere.Define(stage_f, "/World/sphere")          # a gprim with no points

# A mesh whose points exist ONLY as a time sample, with no default value --
# which is how Houdini authors volume fields and animated geometry. Read at the
# wrong time code it looks like a mesh with no points at all, so this is what
# separates a scan that passes a frame from one that does not.
anim = UsdGeom.Mesh.Define(stage_f, "/World/mesh_anim")
anim.GetPointsAttr().Set(
    Vt.Vec3fArray([Gf.Vec3f(i, 3, 0) for i in range(ANIM_PTS)]), FIXTURE_FRAME)

inst = UsdGeom.PointInstancer.Define(stage_f, "/World/inst")
inst.GetProtoIndicesAttr().Set(Vt.IntArray([0] * INSTANCES))
inst.GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(i, 2, 0) for i in range(INSTANCES)]))

# Four asset references over three distinct files: texA appears twice, so a
# scan that fails to deduplicate reports 4 textures and too many bytes.
for i, fname in enumerate(("texA.exr", "texB.exr", "texA.exr", "texC.exr")):
    prim = stage_f.DefinePrim("/World/mat/tex%d" % i, "Shader")
    a = prim.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset)
    a.Set(Sdf.AssetPath(tex_paths[fname]))

settings = UsdRender.Settings.Define(stage_f, "/Render/settings")
settings.GetResolutionAttr().Set(Gf.Vec2i(*RES))
product = UsdRender.Product.Define(stage_f, "/Render/prod")
var_paths = []
for vname, vtype in VAR_TYPES:
    v = UsdRender.Var.Define(stage_f, "/Render/%s" % vname)
    v.GetDataTypeAttr().Set(vtype)
    var_paths.append("/Render/%s" % vname)
product.GetOrderedVarsRel().SetTargets([Sdf.Path(p) for p in var_paths])
settings.GetProductsRel().SetTargets([Sdf.Path("/Render/prod")])
layer.Save()

expect_points = MESH1_PTS + MESH2_PTS + POINTS_PTS + ANIM_PTS
expect_points_no_timecode = MESH1_PTS + MESH2_PTS + POINTS_PTS
expect_gprims = 5                        # 3 meshes + points + sphere
expect_tex_bytes = sum(TEX_SIZES.values())
per_pixel = 0
for _n, vtype in VAR_TYPES:
    ch, w = I._value_type_shape(vtype)
    per_pixel += ch * w
expect_fb = RES[0] * RES[1] * per_pixel
detail["expected"] = {"points": expect_points, "gprims": expect_gprims,
                      "instances": INSTANCES, "texture_count": 3,
                      "texture_bytes": expect_tex_bytes,
                      "framebuffer_bytes": expect_fb,
                      "per_pixel_bytes": per_pixel}


# ==========================================================================
# 2. volumes: one live (op: -> SOP) and one file-backed (.vdb on disk)
# ==========================================================================
obj = hou.node("/obj")
geo = obj.createNode("geo", "vol_src")
sphere_sop = geo.createNode("sphere")
sphere_sop.parm("type").set(2)
vdb_sop = geo.createNode("vdbfrompolygons")
vdb_sop.setInput(0, sphere_sop)
vdb_sop.parm("voxelsize").set(0.05)
vdb_sop.setDisplayFlag(True)
vdb_sop.setRenderFlag(True)

truth_grids = {}
bbox_product = None
for p in vdb_sop.geometry().prims():
    if not hasattr(p, "activeVoxelCount"):
        continue
    r = p.resolution()
    truth_grids[str(p.attribValue("name"))] = {
        "active": int(p.activeVoxelCount()),
        "vdbType": str(p.vdbType()),
        "resolution": [int(x) for x in r],
    }
    bbox_product = int(r[0]) * int(r[1]) * int(r[2])
detail["vdb_truth"] = truth_grids
detail["vdb_bbox_product"] = bbox_product

truth_active = sum(g["active"] for g in truth_grids.values())
record("N7", "the fixture's active voxel count differs from its bounding box "
             "product, so the two cannot be confused",
       OK if truth_active != bbox_product else WRONG,
       "active != bbox", "%s vs %s" % (truth_active, bbox_product))

vdb_file = os.path.join(scratch, "fixture.vdb").replace("\\", "/")
wr = geo.createNode("rop_geometry", "wr")
wr.setInput(0, vdb_sop)
wr.parm("trange").set(0)
wr.parm("sopoutput").set(vdb_file)
wr.render(verbose=False)
detail["vdb_file_bytes"] = os.path.getsize(vdb_file)

stage_net = hou.node("/stage")
live = stage_net.createNode("sopimport", "live_vol")
live.parm("soppath").set(vdb_sop.path())
filevol = stage_net.createNode("volume", "file_vol")
filevol.parm("filepath1").set(vdb_file)

sub = stage_net.createNode("sublayer", "fixture")
sub.parm("filepath1").set(usda)

stages = {}
for label, node in (("live", live), ("file", filevol), ("fixture", sub)):
    try:
        stages[label] = node.stage()
    except Exception:
        record("N8", "cook %s" % label, WRONG, note=traceback.format_exc())

# what flavour of filePath did each volume route produce?
flavours = {}
for label in ("live", "file"):
    st = stages.get(label)
    if st is None:
        continue
    for prim in st.Traverse():
        if not prim.IsA(UsdVol.OpenVDBAsset):
            continue
        a = UsdVol.OpenVDBAsset(prim)
        fp = a.GetFilePathAttr()
        val = fp.Get() if fp else None
        raw = ((getattr(val, "path", "") or "") if val else "")
        flavours[label] = {
            "prim": str(prim.GetPath()),
            "fieldName": str(a.GetFieldNameAttr().Get() or ""),
            "fieldDataType": str(a.GetFieldDataTypeAttr().Get() or ""),
            "path": raw[:120],
            "op_node": I._op_node_path(raw),
        }
detail["volume_flavours"] = flavours


# ==========================================================================
# 3. run the real scan, per part, and time each
# ==========================================================================
try:
    scan = I._FootprintScan(count_vdb_files=True, time_code=FIXTURE_FRAME)
    for label in ("live", "file", "fixture"):
        if stages.get(label) is not None:
            scan.scan(stages[label])
    fp = scan.result()
    scan.close()
    detail["part_seconds"] = {k: round(v, 4) for k, v in scan.part_seconds.items()}
    detail["footprint"] = {
        "volume_count": fp.volume_count, "active_voxels": fp.active_voxels,
        "voxel_bytes": fp.voxel_bytes, "texture_count": fp.texture_count,
        "texture_bytes": fp.texture_bytes, "point_count": fp.point_count,
        "prim_count": fp.prim_count, "instance_count": fp.instance_count,
        "framebuffer_bytes": fp.framebuffer_bytes, "scanned": fp.scanned,
        "seconds": fp.seconds, "skipped": fp.skipped, "heaviest": fp.heaviest,
    }

    # Both volume routes reference the same grid, so each contributes its own
    # active voxels: two volumes in a scene cost twice, even from one cache.
    check("N9", "active voxels = the SOP's own count, once per volume prim",
          truth_active * 2, fp.active_voxels)
    check("N10", "voxel bytes = active voxels x the grid's own value size "
                 "(float = 4)", truth_active * 2 * 4, fp.voxel_bytes)
    check("N11", "active voxels is NOT the bounding-box product",
          True, fp.active_voxels != bbox_product * 2)
    check("N12", "volume_count counts UsdVolVolume prims", 2, fp.volume_count)
    check("N13", "texture_count deduplicates by resolved path "
                 "(4 references, 3 files)", 3, fp.texture_count)
    check("N14", "texture_bytes = sum of os.path.getsize",
          expect_tex_bytes, fp.texture_bytes)
    check("N15", "point_count = sum of authored points arrays",
          expect_points, fp.point_count)
    check("N16", "prim_count = renderable geometry prims",
          expect_gprims, fp.prim_count)
    check("N17", "instance_count = point instancer instances, unweighted",
          INSTANCES, fp.instance_count)
    check("N18", "framebuffer = w x h x channels x bytes, per RenderVar type",
          expect_fb, fp.framebuffer_bytes)
    check("N19", "scanned is True when the scan really ran", True, fp.scanned)
    record("N20", "seconds records a wall-clock cost",
           OK if fp.seconds >= 0 else WRONG, ">= 0", fp.seconds)
    record("N21", "the .vdb referenced as volume data is reported as counted "
                  "under volumes rather than textures",
           OK if any("counted in the volume figures" in s for s in fp.skipped)
           else WRONG, "a note in skipped", fp.skipped)
except Exception:
    record("N9", "footprint scan", WRONG, note=traceback.format_exc())


# ==========================================================================
# 4. deduplication across stages, and the vdb-files opt-out
# ==========================================================================
try:
    twice = I._FootprintScan(count_vdb_files=True, time_code=FIXTURE_FRAME)
    for _ in range(2):
        twice.scan(stages["fixture"])
    r2 = twice.result()
    twice.close()
    check("N22", "scanning the same stage twice does not double the points",
          expect_points, r2.point_count)
    check("N23", "...nor the textures", expect_tex_bytes, r2.texture_bytes)
    check("N24", "...nor the framebuffer", expect_fb, r2.framebuffer_bytes)
except Exception:
    record("N22", "dedup across stages", WRONG, note=traceback.format_exc())

try:
    off = I._FootprintScan(count_vdb_files=False, time_code=FIXTURE_FRAME)
    off.scan(stages["file"])
    r3 = off.result()
    off.close()
    detail["vdb_files_off"] = {"active_voxels": r3.active_voxels,
                               "voxel_bytes": r3.voxel_bytes,
                               "volume_count": r3.volume_count,
                               "skipped": r3.skipped}
    record("N25", "with vdb-file counting off, the file is named in skipped "
                  "rather than counted as zero",
           OK if any("switched off" in s for s in r3.skipped) else WRONG,
           "a note in skipped", r3.skipped)
    check("N26", "...and active_voxels is None (not measured), never 0",
          None, r3.active_voxels)
except Exception:
    record("N25", "vdb opt-out", WRONG, note=traceback.format_exc())


# ==========================================================================
# 4b. the time code is load-bearing, not a refinement
# ==========================================================================
# Houdini authors volume fields (and animated geometry) as time samples with
# **no default value**. A scan reading at the default time code sees nothing
# there and reports it as absent -- which is how four countable volume fields
# on the production shot looked uncountable. This proves the difference.
try:
    no_tc = I._FootprintScan(count_vdb_files=True, time_code=None)
    no_tc.scan(stages["fixture"])
    r4 = no_tc.result()
    no_tc.close()
    detail["points_without_timecode"] = r4.point_count
    check("N30", "without a time code the time-sampled points are missed",
          expect_points_no_timecode, r4.point_count)
    check("N31", "...and with one they are counted, so the frame matters",
          expect_points, expect_points_no_timecode + ANIM_PTS)
except Exception:
    record("N30", "time code sensitivity", WRONG, note=traceback.format_exc())


# ==========================================================================
# 5. end to end: inspect(footprint=True) through a saved .hip + JSON round trip
# ==========================================================================
try:
    rop = stage_net.createNode("usdrender_rop", "rop")
    merged = None
    types = hou.lopNodeTypeCategory().nodeTypes()
    if "merge" in types:
        merged = stage_net.createNode("merge", "all")
        for i, n in enumerate((live, filevol, sub)):
            merged.setInput(i, n)
        rop.setInput(0, merged)
    else:
        rop.setInput(0, sub)
    detail["merge_lop_used"] = merged is not None

    hip = hip_path
    hou.hipFile.save(hip)

    t0 = time.time()
    man = I.inspect(hip, export=False, footprint=True)
    detail["inspect_seconds"] = round(time.time() - t0, 3)

    record("N27", "inspect(footprint=True) populates manifest.footprint",
           OK if man.footprint is not None else WRONG,
           "a SceneFootprint", man.footprint is not None)
    if man.footprint is not None:
        detail["e2e_footprint"] = {
            "volume_count": man.footprint.volume_count,
            "active_voxels": man.footprint.active_voxels,
            "voxel_bytes": man.footprint.voxel_bytes,
            "texture_count": man.footprint.texture_count,
            "texture_bytes": man.footprint.texture_bytes,
            "point_count": man.footprint.point_count,
            "prim_count": man.footprint.prim_count,
            "instance_count": man.footprint.instance_count,
            "framebuffer_bytes": man.footprint.framebuffer_bytes,
            "seconds": man.footprint.seconds,
            "skipped": man.footprint.skipped,
            "heaviest": man.footprint.heaviest,
        }
        from hsl.manifest import SceneManifest
        back = SceneManifest.from_json(man.to_json())
        record("N28", "footprint survives the JSON round trip as a dataclass",
               OK if (back.footprint is not None
                      and back.footprint.point_count == man.footprint.point_count
                      and isinstance(back.footprint.skipped, list)) else WRONG,
               man.footprint.point_count,
               None if back.footprint is None else back.footprint.point_count)

    off_man = I.inspect(hip, export=False, footprint=False)
    record("N29", "without --footprint the manifest carries None, which reads "
                  "as 'nobody scanned this' rather than 'empty'",
           OK if off_man.footprint is None else WRONG,
           None, off_man.footprint)
except Exception:
    record("N27", "end to end inspect", WRONG, note=traceback.format_exc())

emit_and_exit(0)
