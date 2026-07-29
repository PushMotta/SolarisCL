#!/usr/bin/env hython
"""Probe: how does a LOP stage get cooked at a chosen frame? **Run under hython.**

Backs `docs/UNVERIFIED.md` D9-D11 and `docs/TASKS.md` T5. Self-contained -- it
builds its own throwaway Solaris network, so it needs no .hip and can be run on
any machine with Houdini.

    hython scripts/_probe_frame_cook.py out.json [scene.hip]

Writes its JSON report to the **file** named by the first argument, and (if a
second is given) saves the network it built. Nothing useful goes to stdout on
purpose: Houdini's banner and third-party delegates such as Octane print there,
so a caller parsing stdout would be reading whatever they felt like saying.

What it settles, none of which should be taken from memory:

  D9  there is no ``LopNode.stageAtFrame()``; the frame is a keyword on
      ``LopNode.stage()``
  D10 that keyword really recomposes -- and beats ``hou.setFrame()``
  D11 a Solaris network authors no stage time code range until a Configure
      Layer LOP sets ``starttime`` / ``endtime`` (not ``starttimecode``)
"""

from __future__ import annotations

import json
import sys
import time
import traceback

OK, MISSING, SKIP = "OK", "MISSING", "SKIP"
results = []
detail = {}


def record(ident, item, status, note=""):
    results.append({"id": ident, "item": item, "status": status,
                    "note": str(note)[:300]})


def emit_and_exit(code=0):
    out_path = sys.argv[1] if len(sys.argv) > 1 else "frame_cook_probe.json"
    with open(out_path, "w") as handle:
        handle.write(json.dumps({"results": results, "detail": detail}, indent=2))
    sys.exit(code)


try:
    import hou
except ImportError:
    record("F1", "hou importable", MISSING, "not running under hython")
    emit_and_exit(1)

try:
    from pxr import UsdRender
except Exception as exc:
    record("D0", "pxr importable", MISSING, exc)
    emit_and_exit(1)

detail["houdini_version"] = ".".join(str(v) for v in hou.applicationVersion())
record("F1", "Houdini version", OK, detail["houdini_version"])


def settings_set(stage):
    return sorted(str(p.GetPath()) for p in stage.Traverse()
                  if p.IsA(UsdRender.Settings))


# -- D9. the shape of the API ---------------------------------------------

stage_methods = sorted(n for n in dir(hou.LopNode) if "tage" in n)
detail["lopnode_stage_methods"] = stage_methods

if hasattr(hou.LopNode, "stageAtFrame"):
    # If a future build adds it, this stops being a silent assumption.
    record("D9", "LopNode.stageAtFrame() absent", MISSING,
           "this build HAS stageAtFrame(); hsl uses stage(frame=...) instead")
else:
    record("D9", "LopNode.stageAtFrame() absent", OK, stage_methods)

doc = hou.LopNode.stage.__doc__ or ""
detail["stage_doc"] = doc
record("D9b", "LopNode.stage() takes a `frame` argument",
       OK if "frame" in doc.split(")")[0] else MISSING,
       doc.strip().splitlines()[0] if doc.strip() else "no docstring")


# -- build a network whose RenderSettings set changes over time -----------

try:
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.playbar.setFrameRange(1, 10)
    try:
        hou.playbar.setPlaybackRange(1, 10)
    except Exception:
        pass
    hou.setFrame(1)

    ctx = hou.node("/stage")

    early = ctx.createNode("rendersettings", "rs_early")
    early.parm("primpath").set("/Render/rendersettings_EARLY")
    late = ctx.createNode("rendersettings", "rs_late")
    late.parm("primpath").set("/Render/rendersettings_LATE")

    switch = ctx.createNode("switch", "hsl_switch")
    switch.setInput(0, early)
    switch.setInput(1, late)
    switch.parm("input").setExpression("$F > 5", hou.exprLanguage.Hscript)
    record("--", "a switch LOP driven by $F is time dependent",
           OK if switch.isTimeDependent() else MISSING)

    # -- D11. where a stage time code range comes from --------------------
    bare = switch.stage()
    detail["timecodes_without_configurelayer"] = {
        "start": bare.GetStartTimeCode(), "end": bare.GetEndTimeCode(),
        "authored": bool(bare.HasAuthoredTimeCodeRange()),
    }
    record("D11a", "a bare Solaris network authors NO stage time code range",
           OK if not bare.HasAuthoredTimeCodeRange() else MISSING,
           detail["timecodes_without_configurelayer"])

    config = ctx.createNode("configurelayer", "hsl_timecodes")
    config.setInput(0, switch)
    detail["configurelayer_parms"] = sorted(p.name() for p in config.parms())
    spelling = {n: (config.parm(n) is not None) for n in
                ("starttime", "endtime", "starttimecode", "endtimecode")}
    detail["configurelayer_timecode_spelling"] = spelling
    record("D11b", "the parms are starttime/endtime, NOT starttimecode",
           OK if (spelling["starttime"] and not spelling["starttimecode"])
           else MISSING, spelling)

    for name, value in (("setstarttime", 1), ("starttime", 1.0),
                        ("setendtime", 1), ("endtime", 10.0)):
        parm = config.parm(name)
        if parm is not None:
            parm.deleteAllKeyframes()    # a set() does not beat an expression
            parm.set(value)

    configured = config.stage()
    detail["timecodes_with_configurelayer"] = {
        "start": configured.GetStartTimeCode(),
        "end": configured.GetEndTimeCode(),
        "authored": bool(configured.HasAuthoredTimeCodeRange()),
    }
    record("D11c", "configurelayer authors the stage time code range",
           OK if (configured.GetStartTimeCode() == 1.0
                  and configured.GetEndTimeCode() == 10.0) else MISSING,
           detail["timecodes_with_configurelayer"])

    rop = ctx.createNode("usdrender_rop", "hsl_probe_rop")
    rop.setInput(0, config)
    for name, value in (("trange", 1), ("f1", 1), ("f2", 10), ("f3", 1)):
        parm = rop.parm(name)
        if parm is not None:
            parm.deleteAllKeyframes()
            parm.set(value)

    # -- D10. does stage(frame=) recompose, and is it stable? -------------
    trials = {}
    for label, frame in (("1", 1.0), ("10", 10.0), ("1 again", 1.0),
                         ("10 again", 10.0), ("5", 5.0), ("6", 6.0)):
        started = time.time()
        trials[label] = {
            "settings": settings_set(config.stage(frame=frame)),
            "seconds": round(time.time() - started, 4),
        }
    detail["stage_frame_kwarg"] = trials

    first = trials["1"]["settings"]
    last = trials["10"]["settings"]
    record("D10a", "stage(frame=N) recomposes per frame",
           OK if first and last and first != last else MISSING,
           {"frame 1": first, "frame 10": last})
    record("D10b", "stage(frame=N) is stable when frames are revisited",
           OK if (trials["1 again"]["settings"] == first
                  and trials["10 again"]["settings"] == last) else MISSING,
           "a cook cached at another frame is not handed back")
    record("D10c", "the switch flips exactly where $F > 5 says it should",
           OK if (trials["5"]["settings"] == first
                  and trials["6"]["settings"] == last) else MISSING)

    # The explicit frame has to win over the global one, or hsl would be
    # describing whichever moment the .hip happened to be saved on.
    hou.setFrame(1)
    at_ten = settings_set(config.stage(frame=10.0))
    hou.setFrame(10)
    at_one = settings_set(config.stage(frame=1.0))
    hou.setFrame(1)
    record("D10d", "stage(frame=N) beats hou.setFrame()",
           OK if (at_ten == last and at_one == first) else MISSING,
           {"setFrame(1) + stage(frame=10)": at_ten,
            "setFrame(10) + stage(frame=1)": at_one})

    # ... and setFrame() alone still works, which is what the fallback for a
    # build without the keyword relies on.
    walked = []
    for frame in (1.0, 10.0, 1.0):
        hou.setFrame(frame)
        walked.append(settings_set(config.stage()))
    record("D10e", "hou.setFrame() + stage() is a usable fallback",
           OK if (walked[0] == first and walked[1] == last
                  and walked[2] == first) else MISSING, walked)

    # -- what one extra cook costs ---------------------------------------
    static = ctx.createNode("rendersettings", "rs_static")
    costs = []
    for frame in (1.0, 10.0, 20.0):
        started = time.time()
        static.stage(frame=frame)
        costs.append(round(time.time() - started, 4))
    detail["static_lop_seconds"] = costs
    detail["time_varying_lop_seconds"] = [v["seconds"] for v in trials.values()]
    record("--", "cost of one extra cook, on this trivial network", OK,
           "time-varying %ss, static %ss -- a real network costs a real cook"
           % (detail["time_varying_lop_seconds"], costs))

    if len(sys.argv) > 2:
        hou.hipFile.save(sys.argv[2])
        detail["saved_hip"] = sys.argv[2]
        record("--", "saved the probe scene", OK, sys.argv[2])

except Exception:
    record("D10", "time-varying stage probe", MISSING,
           traceback.format_exc()[-300:])

emit_and_exit(0)
