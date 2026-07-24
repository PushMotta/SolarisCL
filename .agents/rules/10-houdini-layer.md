---
trigger: glob
globs: hsl/inspector.py
---

# The hython layer

## Parameters are hints; the composed USD stage is truth

Solaris authors render settings anywhere in the layer stack, and the ROP's
`rendersettings` parm is frequently empty. Prefer `stage.Traverse()` with the
`UsdRender` schema over reading node parameters.

## Every parameter read goes through the probe helper

```python
value = _parm(node, ("renderer", "husk_renderer", "engine"), "")   # correct
value = node.parm("renderer").eval()                               # forbidden
```

Candidates newest-first. A miss must land in `warnings` so it surfaces in
`manifest.warnings` — silent fallback is the bug this module exists to avoid.

`_set_parm()` returns `False` when a parameter is missing. **Check it.** An
ignored `False` in `export_usd()` writes the USD to the wrong path with no error.

## Do not replace the temporary usd_rop with Usd.Stage.Export()

`Export()` serialises the stage as cooked at one time. Animation, motion blur
samples and value clips vanish silently and the user gets a still frame repeated
across the whole range. This was considered and rejected deliberately.

## Before asserting any API

Probe it, or mark it unverified in @docs/UNVERIFIED.md:

```bash
hython -c "import hou; n=hou.node('/stage').createNode('usdrender_rop'); print(sorted(p.name() for p in n.parms()))"
```
