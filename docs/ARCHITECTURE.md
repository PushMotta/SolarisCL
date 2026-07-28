# Architecture

## The constraint everything follows from

`hou` exists only inside `hython`. `husk` is a separate binary. A GUI that
imports `hou` inherits hython's startup cost and license checkout for what
amounts to a single read of the scene, and becomes untestable anywhere Houdini
is not installed.

So the program is split at that seam, and the two halves never import each
other:

```
┌─ hython process ──────┐         ┌─ plain Python process ──────┐
│  hsl/inspector.py     │  JSON   │  hsl/bridge.py    spawns ───┼──┐
│  · loads the .hip     │ ──────▶ │  hsl/husk.py      → argv    │  │
│  · cooks the LOPs     │manifest │  hsl/runner.py    → procs   │  │
│  · walks the USD stage│         │  hsl/ui.py  hsl/cli.py      │  │
│  · writes USD to disk │◀────────┼─────────────────────────────┘  │
└───────────────────────┘  spawn  └────────────────────────────────┘
                                          │
                                          ▼  subprocess per chunk
                                       husk husk husk
```

`hsl/manifest.py` is the contract: pure dataclasses, standard library only,
JSON-serialisable in both directions. It is deliberately the least interesting
file in the project and the one to read first.

## What each side gets

**The inspector gets to be slow.** It loads a hip, cooks a LOP network, walks a
composed stage and writes USD. Tens of seconds to minutes. It runs once, in a
subprocess, and hands back a small JSON document.

**Everything else gets to be tested.** 186 tests run with no Houdini installed,
including the full husk process lifecycle against a fake binary. That is only
possible because no module outside `inspector.py` touches `hou`.

A `PreToolUse` hook enforces the boundary mechanically, because the invariant
is easy to break by accident and the breakage is invisible to whoever breaks it
— their machine has Houdini.

## Two decisions worth knowing about

**Parameters are hints; the composed stage is truth.** Solaris scenes can author
render settings anywhere in the layer stack, and the ROP's `rendersettings`
parameter is often empty. Reading the stage is the only approach that works on
real scenes. Node parameters are read defensively through `_parm()` and used to
pre-fill the UI.

**USD export goes through a temporary `usd_rop`, not `Usd.Stage.Export()`.**
`Export()` serialises the stage as cooked at one time — animation, motion blur
samples and value clips vanish silently, producing a still frame repeated across
the range with nothing in the log to explain it. The temporary ROP re-cooks per
frame, then is destroyed. The `.hip` is never saved.

## Threading

`RenderQueue` uses `subprocess` plus threads rather than `QProcess`, so the CLI
and the GUI share one engine. Callbacks arrive on worker threads; the UI crosses
them into Qt through `QueueBridge` signals. Cancellation kills the process
*group*, since terminating husk alone leaves its render children running.

## Where it is going

`jobs_for_rop()` returns one `RenderJob` per frame chunk, which is already the
shape a farm submitter needs — swapping `RenderQueue` for a Deadline or Tractor
submitter is the intended next step, not a rewrite. See `docs/TASKS.md` T7.
