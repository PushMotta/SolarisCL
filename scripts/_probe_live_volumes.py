#!/usr/bin/env hython
"""Probe: is a **time-sampled** `.vdb` reference read as cached or as live?

**Run under hython.** Backs `docs/UNVERIFIED.md` item N10 (and the correction it
makes to the older D-VOL claim).

    hython scripts/_probe_live_volumes.py out.json [scratch_dir]
    hython scripts/_probe_live_volumes.py out.json --hip SHOT.hiplc [--rop PATH]

Writes its JSON report to the **file** named by the first argument. Nothing
useful goes to stdout on purpose: Houdini's banner and third-party delegates
(Octane on the 21.0.729 install here) print there, so a caller parsing stdout
would be reading whatever they felt like saying.

The defect under test
---------------------
N8 measured that Houdini authors volume field attributes as **time samples with
no default value**, so ``attr.Get()`` -- no time code -- returns ``None`` on a
real Solaris volume. ``scan_live_volumes()`` read ``filePath`` that way and
treated an empty one as "live, SOP-imported, will bake into a USD export". A
scene whose fields point at a time-sampled ``.vdb`` sequence was therefore
reported as a bake risk that does not exist, and ``inspect(
allow_volume_bake=False)`` refused a perfectly safe export.

The fixture carries four volume routes so both directions are shown at once:

  * **cached** -- a Volume LOP whose ``filepath1`` carries ``$F4``, pointing at
    a real ``.vdb`` sequence on disk. Must read **cached**: no bake warning,
    export proceeds.
  * **sampled** -- the same files referenced from a hand-authored layer where
    the attribute exists *only* as a time sample, which is the exact shape N8
    measured on the production shot. Must also read **cached**.
  * **live** -- a SOP-imported volume (``sopimport``), whose ``filePath`` is an
    ``op:`` path back to the SOP. Must **still** read live: this is the ~44 GB
    per frame bake the guard exists to prevent.
  * **gone** -- a time-sampled reference to a ``.vdb`` that is not on disk.
    Must read as a **missing asset**, not as a live volume: the two scans
    divide the work and must never double-report the same field.

Every expectation is checked against the raw USD read (``GetFilePathAttr()`` at
the frame) rather than against the function under test.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

OK, WRONG, SKIP = "OK", "WRONG", "SKIP"
results = []
detail = {}


def record(ident, item, status, expected=None, measured=None, note=""):
    results.append({"id": ident, "item": item, "status": status,
                    "expected": expected, "measured": measured,
                    "note": str(note)[:600]})


def check(ident, item, expected, measured, note=""):
    record(ident, item, OK if expected == measured else WRONG,
           expected, measured, note)


def emit_and_exit(code=0):
    out_path = sys.argv[1] if len(sys.argv) > 1 else "live_volume_probe.json"
    with open(out_path, "w") as handle:
        handle.write(json.dumps({"results": results, "detail": detail},
                                indent=2, default=str))
    sys.exit(code)


try:
    import hou
except ImportError:
    record("F1", "hou importable", SKIP, note="not running under hython")
    emit_and_exit(1)

try:
    from pxr import Sdf, Usd, UsdVol
except Exception as exc:
    record("D0", "pxr importable", SKIP, note=exc)
    emit_and_exit(1)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hsl import inspector as I          # noqa: E402

detail["houdini_version"] = ".".join(str(v) for v in hou.applicationVersion())
args = sys.argv[2:]


def _arg(flag, default=""):
    return args[args.index(flag) + 1] if flag in args else default


def _classify(stage, time_code):
    """scan_live_volumes at a time code, tolerating the pre-fix signature.

    The probe has to run against the code **before** the fix as well as after
    it, so a TypeError here is data, not a crash.
    """
    try:
        return ([v.prim_path for v in
                 I.scan_live_volumes(stage, time_code=time_code)], "")
    except TypeError as exc:
        return None, "scan_live_volumes takes no time_code: %s" % exc


def _missing(stage, time_code):
    try:
        return ([(a.attr_path, a.asset_path) for a in
                 I.scan_missing_assets(stage, time_code=time_code)], "")
    except TypeError as exc:
        return None, "scan_missing_assets takes no time_code: %s" % exc


def _field_facts(stage, time_code):
    """Raw USD truth for every OpenVDBAsset on a stage -- the independent read."""
    facts = {}
    tc = Usd.TimeCode(float(time_code))
    for prim in stage.Traverse():
        if not prim.IsA(UsdVol.OpenVDBAsset):
            continue
        asset = UsdVol.OpenVDBAsset(prim)
        attr = asset.GetFilePathAttr()
        name_attr = asset.GetFieldNameAttr()
        plain = attr.Get() if attr else None
        at_f = attr.Get(tc) if attr else None
        facts[str(prim.GetPath())] = {
            "has_authored_value": bool(attr.HasAuthoredValue()) if attr else None,
            "num_time_samples": int(attr.GetNumTimeSamples()) if attr else None,
            "path_default_tc": (getattr(plain, "path", "") or "") if plain else "",
            "path_at_frame": ((getattr(at_f, "path", "") or "")[:160]
                              if at_f else ""),
            "resolved_at_frame": ((getattr(at_f, "resolvedPath", "") or "")[:160]
                                  if at_f else ""),
            "fieldName_default_tc": str((name_attr.Get() if name_attr else "") or ""),
            "fieldName_at_frame": str((name_attr.Get(tc) if name_attr else "") or ""),
        }
    return facts


# ==========================================================================
# Real-shot mode: no fixture, just read an existing scene and report
# ==========================================================================
if "--hip" in args:
    hip = _arg("--hip")
    only = _arg("--rop")
    detail["mode"] = "real_shot"
    detail["hip"] = hip
    hou.hipFile.load(hip, suppress_save_prompt=True, ignore_load_warnings=True)
    frame = float(hou.frame())
    detail["opened_on_frame"] = frame
    warnings = []
    shot = {}
    for node in I.find_render_rops():
        if only and node.path() != only:
            continue
        stage, _lop = I.stage_for(node, warnings, frame=frame)
        if stage is None:
            shot[node.path()] = {"stage": None}
            continue
        live_plain = [v.prim_path for v in I.scan_live_volumes(stage)]
        live_at, note_l = _classify(stage, frame)
        miss_plain = [a.asset_path for a in I.scan_missing_assets(stage)]
        miss_at, note_m = _missing(stage, frame)
        shot[node.path()] = {
            "fields": _field_facts(stage, frame),
            "live_default_tc": live_plain,
            "live_at_frame": live_at,
            "live_note": note_l,
            "missing_default_tc_count": len(miss_plain),
            "missing_at_frame_count": (None if miss_at is None else len(miss_at)),
            "missing_at_frame_sample": (None if miss_at is None
                                        else [p for _a, p in miss_at][:8]),
            "missing_note": note_m,
        }
    detail["shot"] = shot
    detail["warnings"] = warnings[:40]
    emit_and_exit(0)


# ==========================================================================
# Fixture mode
# ==========================================================================
detail["mode"] = "fixture"
scratch = (args[0] if args and not args[0].startswith("--")
           else os.path.join(hou.text.expandString("$TEMP"),
                             "hsl_live_volume_probe"))
os.makedirs(scratch, exist_ok=True)
detail["scratch"] = scratch

FRAME = 2.0                       # the frame the manifest will describe
detail["frame"] = FRAME

# Name the scene into the scratch directory before building anything: a SOP
# Import LOP bakes savepath=$HIP/usd/... into the op: path it authors and
# Houdini materialises a sidecar there, so leaving $HIP at the checkout would
# drop a usd/ folder into the repository.
hip_path = os.path.join(scratch, "live_volume_fixture.hip").replace("\\", "/")
hou.hipFile.save(hip_path)

# -- a real, tiny .vdb sequence on disk -------------------------------------
obj = hou.node("/obj")
geo = obj.createNode("geo", "vol_src")
sphere = geo.createNode("sphere")
sphere.parm("type").set(2)
vdb = geo.createNode("vdbfrompolygons")
vdb.setInput(0, sphere)
vdb.parm("voxelsize").set(0.15)
vdb.setDisplayFlag(True)
vdb.setRenderFlag(True)

truth_voxels = sum(int(p.activeVoxelCount()) for p in vdb.geometry().prims()
                   if hasattr(p, "activeVoxelCount"))
detail["fixture_active_voxels"] = truth_voxels

seq_pattern = os.path.join(scratch, "cache.$F4.vdb").replace("\\", "/")
writer = geo.createNode("rop_geometry", "wr")
writer.setInput(0, vdb)
writer.parm("trange").set(0)          # current frame only -- no $FSTART/$FEND
writer.parm("sopoutput").set(seq_pattern)
written = []
for f in (1, 2, 3):
    hou.setFrame(f)
    writer.render(verbose=False)
    written.append(os.path.join(scratch, "cache.%04d.vdb" % f).replace("\\", "/"))
hou.setFrame(FRAME)
detail["vdb_files"] = [{"path": p, "bytes": os.path.getsize(p)} for p in written]

# -- route 1: a Volume LOP whose filepath1 carries $F4 ----------------------
stage_net = hou.node("/stage")
cached_lop = stage_net.createNode("volume", "cached_vol")
cached_lop.parm("filepath1").set(seq_pattern)

# -- route 2: a hand-authored layer where filePath exists ONLY as a sample --
# The exact shape N8 measured on the production shot: an authored attribute
# with time samples and no default value. Built with the USD API so the shape
# is not at the mercy of what a LOP happens to do.
hand_usda = os.path.join(scratch, "sampled_volume.usda").replace("\\", "/")
if os.path.exists(hand_usda):
    os.remove(hand_usda)
hand_layer = Sdf.Layer.CreateNew(hand_usda)
hand_stage = Usd.Stage.Open(hand_layer)
vol_prim = UsdVol.Volume.Define(hand_stage, "/World/sampled_vol")
fld = UsdVol.OpenVDBAsset.Define(hand_stage, "/World/sampled_vol/density")
for f, path in zip((1, 2, 3), written):
    fld.GetFilePathAttr().Set(Sdf.AssetPath(path), float(f))
    fld.GetFieldNameAttr().Set("density", float(f))
vol_prim.CreateFieldRelationship("density", fld.GetPath())
hand_layer.Save()
sampled_lop = stage_net.createNode("sublayer", "sampled_vol")
sampled_lop.parm("filepath1").set(hand_usda)

# -- route 3: genuinely live, SOP-imported ---------------------------------
live_lop = stage_net.createNode("sopimport", "live_vol")
live_lop.parm("soppath").set(vdb.path())

# -- route 4: time-sampled references to files that are NOT on disk ---------
# A Volume LOP pointed at a missing file does not cook to a stage at all
# (measured: stage_for returns None), so this route is hand-authored too: one
# OpenVDBAsset and one ordinary texture, each authored only as a time sample.
# The texture is the half of N10 that under-reports -- a missing asset read at
# the default time code is invisible rather than wrong.
gone_usda = os.path.join(scratch, "missing_volume.usda").replace("\\", "/")
if os.path.exists(gone_usda):
    os.remove(gone_usda)
gone_layer = Sdf.Layer.CreateNew(gone_usda)
gone_stage = Usd.Stage.Open(gone_layer)
gone_vol = UsdVol.Volume.Define(gone_stage, "/World/gone_vol")
gone_fld = UsdVol.OpenVDBAsset.Define(gone_stage, "/World/gone_vol/density")
nosuch_vdb = os.path.join(scratch, "nosuch.0002.vdb").replace("\\", "/")
nosuch_tex = os.path.join(scratch, "nosuch_texture.exr").replace("\\", "/")
for f in (1, 2, 3):
    gone_fld.GetFilePathAttr().Set(Sdf.AssetPath(nosuch_vdb), float(f))
    gone_fld.GetFieldNameAttr().Set("density", float(f))
gone_vol.CreateFieldRelationship("density", gone_fld.GetPath())
shader = gone_stage.DefinePrim("/World/mat/tex", "Shader")
tex_attr = shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset)
for f in (1, 2, 3):
    tex_attr.Set(Sdf.AssetPath(nosuch_tex), float(f))
gone_layer.Save()
gone_lop = stage_net.createNode("sublayer", "gone_vol")
gone_lop.parm("filepath1").set(gone_usda)
detail["missing_paths"] = {"vdb": nosuch_vdb, "texture": nosuch_tex}

# A render ROP per route, so the export guard can be exercised per case.
rops = {}
for label, src in (("cached", cached_lop), ("sampled", sampled_lop),
                   ("live", live_lop), ("gone", gone_lop)):
    rs = stage_net.createNode("rendersettings", "rs_%s" % label)
    rs.setInput(0, src)
    rop = stage_net.createNode("usdrender_rop", "rop_%s" % label)
    rop.setInput(0, rs)
    rops[label] = rop
detail["rops"] = dict((k, v.path()) for k, v in rops.items())

hou.hipFile.save(hip_path)
detail["hip"] = hip_path

# ==========================================================================
# 1. what shape did each route actually author?
# ==========================================================================
stages, shapes = {}, {}
cook_warnings = []
for label, rop in rops.items():
    try:
        # The same route inspect() takes: a usdrender_rop is a RopNode, so the
        # stage comes from its input LOP.
        stage, _lop = I.stage_for(rop, cook_warnings, frame=FRAME)
        if stage is None:
            record("S0", "cook %s" % label, WRONG, note=cook_warnings[-3:])
            continue
        stages[label] = stage
        shapes[label] = _field_facts(stage, FRAME)
    except Exception:
        record("S0", "cook %s" % label, WRONG, note=traceback.format_exc())
detail["field_shapes"] = shapes
detail["cook_warnings"] = cook_warnings[:20]


def _one(label):
    facts = shapes.get(label) or {}
    return (list(facts.values()) or [{}])[0]


cached_f, sampled_f = _one("cached"), _one("sampled")
live_f, gone_f = _one("live"), _one("gone")

record("V1", "the hand-authored route reproduces N8's shape: authored, "
             "time-sampled, and None at the default time code",
       OK if (sampled_f.get("has_authored_value")
              and (sampled_f.get("num_time_samples") or 0) > 0
              and not sampled_f.get("path_default_tc")
              and sampled_f.get("path_at_frame")) else WRONG,
       "authored + samples + empty at default tc + a path at the frame",
       dict((k, sampled_f.get(k)) for k in ("has_authored_value",
                                            "num_time_samples",
                                            "path_default_tc",
                                            "path_at_frame")))

record("V2", "a Volume LOP with $F4 in filepath1 -- what does Houdini author?",
       OK if cached_f.get("path_at_frame") else WRONG,
       "a .vdb path at the frame",
       dict((k, cached_f.get(k)) for k in ("num_time_samples",
                                           "path_default_tc",
                                           "path_at_frame")))

record("V3", "the SOP-imported volume carries an op: path back to the SOP",
       OK if I._op_node_path(live_f.get("path_at_frame", "")) else WRONG,
       "an op: path",
       dict((k, live_f.get(k)) for k in ("path_default_tc", "path_at_frame",
                                         "resolved_at_frame")))

record("V4", "the missing-.vdb route authors a path that does not resolve",
       OK if (gone_f.get("path_at_frame")
              and not gone_f.get("resolved_at_frame")) else WRONG,
       "authored, unresolved",
       dict((k, gone_f.get(k)) for k in ("path_at_frame", "resolved_at_frame")))

# ==========================================================================
# 2. classification, at the default time code and at the frame
# ==========================================================================
classified = {}
for label, st in stages.items():
    at_f, note = _classify(st, FRAME)
    miss_at, note_m = _missing(st, FRAME)
    classified[label] = {
        "live_default_tc": [v.prim_path for v in I.scan_live_volumes(st)],
        "live_at_frame": at_f,
        "live_note": note,
        "missing_default_tc": [a.asset_path for a in I.scan_missing_assets(st)],
        "missing_at_frame": (None if miss_at is None
                             else [p for _a, p in miss_at]),
        "missing_note": note_m,
    }
detail["classification"] = classified


def _live_now(label):
    """What the code under test says today: at the frame if it can, else plain."""
    entry = classified.get(label, {})
    at_f = entry.get("live_at_frame")
    return entry.get("live_default_tc") if at_f is None else at_f


def _missing_now(label):
    entry = classified.get(label, {})
    at_f = entry.get("missing_at_frame")
    return entry.get("missing_default_tc") if at_f is None else at_f


check("V5", "a time-sampled reference to a real .vdb is CACHED, not live "
            "(hand-authored route)", [], _live_now("sampled"))
check("V6", "a Volume LOP pointing at a $F4 .vdb sequence is CACHED, not live",
      [], _live_now("cached"))
check("V7", "a SOP-imported volume is STILL flagged live (the ~44 GB/frame "
            "bake guard)", 1, len(_live_now("live") or []))
check("V8", "an authored-but-unresolved .vdb is NOT a live volume", [],
      _live_now("gone"))
check("V9", "an authored-but-unresolved .vdb IS a missing asset, and so is a "
            "time-sampled texture that does not resolve",
      sorted([nosuch_tex, nosuch_vdb]), sorted(_missing_now("gone") or []))
check("V10", "a resolved .vdb sequence is not reported missing", 0,
      len(_missing_now("sampled") or []) + len(_missing_now("cached") or []))
check("V11", "an op: path is never reported as a missing texture", 0,
      len(_missing_now("live") or []))


# ==========================================================================
# 3. the export guard, end to end through inspect()
# ==========================================================================
def _export_run(tag, allow):
    usd_dir = os.path.join(scratch, "usd_%s" % tag)
    man = I.inspect(hip_path, export=True, usd_dir=usd_dir,
                    allow_volume_bake=allow, frame=FRAME)
    out = {}
    for rop in man.rops:
        label = rop.node_path.rsplit("rop_", 1)[-1]
        size = (os.path.getsize(rop.usd_path)
                if rop.usd_path and os.path.exists(rop.usd_path) else 0)
        out[label] = {"usd_path": rop.usd_path, "bytes": size}
    detail["warnings_%s" % tag] = list(man.warnings)
    return out, [w for w in man.warnings if "live volume" in w], man


try:
    guarded, guard_warnings, man_g = _export_run("guard", False)
    detail["export_allow_volume_bake_false"] = guarded
    detail["export_guard_warnings"] = guard_warnings
    detail["manifest_live_volumes"] = [
        {"prim_path": v.prim_path, "field_count": v.field_count,
         "field_names": v.field_names} for v in man_g.live_volumes]
    detail["manifest_missing_assets"] = [
        {"attr": a.attr_path, "path": a.asset_path}
        for a in man_g.missing_assets][:10]

    check("V12", "allow_volume_bake=False still writes 0 bytes for the "
                 "genuinely live volume", 0,
          guarded.get("live", {}).get("bytes"))
    check("V13", "and leaves its usd_path empty", "",
          guarded.get("live", {}).get("usd_path"))
    record("V14", "the cached .vdb sequence EXPORTS under "
                  "allow_volume_bake=False",
           OK if guarded.get("cached", {}).get("bytes", 0) > 0 else WRONG,
           "> 0 bytes", guarded.get("cached"))
    record("V15", "so does the hand-authored time-sampled reference",
           OK if guarded.get("sampled", {}).get("bytes", 0) > 0 else WRONG,
           "> 0 bytes", guarded.get("sampled"))
    record("V16", "exactly one live volume reaches manifest.live_volumes",
           OK if len(man_g.live_volumes) == 1 else WRONG, 1,
           [v.prim_path for v in man_g.live_volumes])
    record("V18", "the live volume's field names are not empty strings "
                  "(the prim-name fallback)",
           OK if all(all(n for n in v.field_names)
                     for v in man_g.live_volumes) else WRONG,
           "non-empty names",
           [v.field_names for v in man_g.live_volumes])
except Exception:
    record("V12", "export guard run", WRONG, note=traceback.format_exc())

try:
    allowed, _w, _m = _export_run("bake", True)
    detail["export_allow_volume_bake_true"] = allowed
    record("V17", "with allow_volume_bake=True the live volume does export",
           OK if allowed.get("live", {}).get("bytes", 0) > 0 else WRONG,
           "> 0 bytes", allowed.get("live"))
except Exception:
    record("V17", "bake-allowed run", WRONG, note=traceback.format_exc())

detail["summary"] = {
    "ok": sum(1 for r in results if r["status"] == OK),
    "wrong": sum(1 for r in results if r["status"] == WRONG),
    "skip": sum(1 for r in results if r["status"] == SKIP),
}
emit_and_exit(0)
