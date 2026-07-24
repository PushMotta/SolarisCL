---
name: solaris-usd-introspection
description: Reads render-relevant information out of a Houdini Solaris LOP network — RenderSettings, RenderProduct, RenderVar and camera prims, resolution, AOVs, and delegate settings. Use when inspecting a .hip for what it will render, when adding fields to the scene manifest, or when a render's resolution, camera or AOV list comes back wrong or empty.
---

# Introspecting a Solaris stage

## The core principle

**Node parameters are hints. The composed USD stage is truth.**

In Solaris, render settings can be authored anywhere in the layer stack — a
Render Settings LOP, a sublayered USD file, an inline edit, a reference. The
USD Render ROP's `rendersettings` parameter is only a pointer, and it is
frequently empty. Code that reads parameters alone will be wrong on any scene
more complex than a tutorial.

Get the composed stage and traverse it:

```python
stage = lop_node.stage()                 # cooks at the current frame
for prim in stage.Traverse():
    if prim.IsA(UsdRender.Settings):
        ...
```

## What to pull, and from where

| Want | Source |
|---|---|
| resolution, pixel aspect, conform policy | `UsdRender.Settings` attrs |
| which camera | `UsdRender.Settings` `camera` **relationship**, not an attribute |
| output file paths | `UsdRender.Product.GetProductNameAttr()` |
| AOVs | `UsdRender.Var` — `sourceName`, `sourceType`, `dataType` |
| Karma sample counts etc. | namespaced attrs on the settings prim: `karma:*` |
| frame range | ROP parms `trange`/`f1`/`f2`/`f3`, or stage start/end time codes |
| the default settings prim | stage metadata `renderSettingsPrimPath` |

Camera and products are relationships (`GetCameraRel()`, `GetProductsRel()`),
so read them with `.GetTargets()` and handle the empty case.

## Resolution order for the settings prim

1. The ROP's parameter, if set.
2. Stage metadata `renderSettingsPrimPath`.
3. The only settings prim on the stage, if there is exactly one.
4. Otherwise: ambiguous. Ask, or warn — do not pick one silently.

## Failure modes to recognise

**Empty AOV list.** Usually means the prims are not typed as you expect and
`IsA(UsdRender.Var)` returns False. Check `prim.GetTypeName()` directly before
concluding the scene has no AOVs.

**Right resolution, wrong frames.** `.stage()` cooks at the *current* frame. If
the stage's structure varies over time, single-frame introspection under-reports.
`stageAtFrame()` is the escape hatch.

**Everything empty.** The LOP probably failed to cook. Catch `hou.Error` around
`.stage()` and surface the message rather than returning an empty result that
looks like a scene with nothing in it.

## Never read a parameter directly

Names drift between Houdini versions and the failure is silent — a wrong name
returns a default and the render runs with wrong settings, no exception. Probe
a list of candidates and record the miss:

```python
def _parm(node, names, default=None):
    for name in names:
        parm = node.parm(name) or node.parmTuple(name)
        if parm is not None:
            try:
                return parm.eval()
            except Exception:
                continue
    return default
```

## Exporting the stage for husk

husk renders a USD file, not a `.hip`. Use a temporary `usd_rop` wired to the
LOP, not `Usd.Stage.Export()`.

`Export()` serialises the stage as cooked at a single time. Animation, motion
blur samples and per-frame value clips are dropped **silently** — the render
succeeds and every frame is identical, with nothing in the log to explain it.
The ROP re-cooks per frame and handles all of that.
