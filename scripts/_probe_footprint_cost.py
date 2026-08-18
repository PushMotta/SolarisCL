#!/usr/bin/env hython
"""Probe: what does a footprint scan cost on a real shot? **Run under hython.**

    hython scripts/_probe_footprint_cost.py out.json scene.hip [--no-vdb-files]

Backs the cost claims in `docs/UNVERIFIED.md` section N. Writes JSON to the
**file** named by the first argument -- never parsed from stdout, which carries
Houdini's banner and any third-party delegate's chatter.

It separates the two costs that get confused with each other:

  * cooking each ROP's LOP stage, which `inspect()` pays **anyway** whether or
    not a footprint was asked for, and which dominates on a heavy shot; and
  * each part of the footprint scan itself, timed individually, because the
    parts are not remotely equal -- opening .vdb files and reading point arrays
    are real work, while walking for render settings is not.

Only the second is what `--footprint` actually adds. Reporting the two together
would make the scan look far more expensive than it is.

The scene is opened read-only and nothing is exported, so this is safe to point
at a shot on a shared or backed-up volume.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

out = {"rops": []}


def emit_and_exit(code=0):
    with open(sys.argv[1], "w") as handle:
        handle.write(json.dumps(out, indent=2, default=str))
    sys.exit(code)


try:
    import hou
except ImportError:
    out["error"] = "not running under hython"
    emit_and_exit(1)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hsl import inspector as I          # noqa: E402

hip = sys.argv[2]
count_vdb_files = "--no-vdb-files" not in sys.argv
out["hip"] = hip
out["count_vdb_files"] = count_vdb_files
out["houdini_version"] = ".".join(str(v) for v in hou.applicationVersion())

t0 = time.time()
hou.hipFile.load(hip, suppress_save_prompt=True, ignore_load_warnings=True)
out["hip_load_s"] = round(time.time() - t0, 2)
out["opens_on_frame"] = float(hou.frame())

warnings = []
rops = I.find_render_rops()
out["rop_count"] = len(rops)

# Read at the frame the scene opens on: Houdini authors volume fields
# as time samples with no default value, so the wrong time code makes
# them read as empty.
scan = I._FootprintScan(count_vdb_files=count_vdb_files,
                        time_code=float(hou.frame()))
totals_before = {}

for node in rops:
    entry = {"path": node.path()}
    try:
        t0 = time.time()
        stage, lop = I.stage_for(node, warnings)
        entry["stage_cook_s"] = round(time.time() - t0, 2)
        if stage is None:
            entry["stage"] = None
            out["rops"].append(entry)
            continue

        before = dict(scan.part_seconds)
        t0 = time.time()
        scan.scan(stage)
        entry["footprint_total_s"] = round(time.time() - t0, 3)
        entry["footprint_parts_s"] = {
            k: round(scan.part_seconds[k] - before.get(k, 0.0), 3)
            for k in scan.part_seconds}
        entry["running"] = {
            "volumes": len(scan.volumes), "fields": len(scan.fields),
            "active_voxels": scan.active_voxels, "textures": len(scan.textures),
            "gprims": len(scan.gprims), "points": scan.point_count,
            "instances": scan.instance_count,
        }
    except Exception:
        entry["error"] = traceback.format_exc()[:2000]
    out["rops"].append(entry)

try:
    fp = scan.result()
    out["footprint"] = {
        "volume_count": fp.volume_count, "active_voxels": fp.active_voxels,
        "voxel_bytes": fp.voxel_bytes, "texture_count": fp.texture_count,
        "texture_bytes": fp.texture_bytes, "point_count": fp.point_count,
        "prim_count": fp.prim_count, "instance_count": fp.instance_count,
        "framebuffer_bytes": fp.framebuffer_bytes, "scanned": fp.scanned,
        "seconds": fp.seconds, "skipped": fp.skipped, "heaviest": fp.heaviest,
    }
    out["part_seconds"] = {k: round(v, 3) for k, v in scan.part_seconds.items()}
    out["cook_seconds_total"] = round(
        sum(r.get("stage_cook_s", 0.0) for r in out["rops"]), 2)
except Exception:
    out["result_error"] = traceback.format_exc()[:2000]
finally:
    scan.close()

out["warnings"] = warnings[:40]
emit_and_exit(0)
