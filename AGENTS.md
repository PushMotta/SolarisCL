# hsl — Solaris Render Launcher

> Canonical project brief. `CLAUDE.md` and `GEMINI.md` are thin pointers to this
> file — edit **this** one. See `scripts/check_drift.py`.

Loads a Houdini `.hip`, reads what the LOP network is set up to render, and
launches `husk`. GUI (PySide6), CLI, and importable library.

---

## The one rule that matters

**`hou` and `pxr` may only be imported in `hsl/inspector.py`.**

That file runs under `hython`. Everything else runs in ordinary Python and must
stay importable on a machine with no Houdini installed — that is what makes the
test suite possible. The two halves communicate only through the JSON manifest
defined in `hsl/manifest.py`.

```
┌─ hython ──────────────┐         ┌─ plain Python ──────────────┐
│  hsl/inspector.py     │  JSON   │  hsl/bridge.py   spawns ────┼──┐
│  · loads the .hip     │ ──────▶ │  hsl/husk.py     → argv     │  │
│  · cooks the LOPs     │manifest │  hsl/runner.py   → procs    │  │
│  · walks the USD stage│         │  hsl/ui.py  hsl/cli.py      │  │
│  · writes USD to disk │◀────────┼─────────────────────────────┘  │
└───────────────────────┘  spawn  └────────────────────────────────┘
```

A `PreToolUse` hook enforces this boundary. If it blocks you, do not work
around it by adding a `try: import hou` guard — put the code in `inspector.py`
and pass the result through the manifest.

## Layers

| File | Runs under | Imports Houdini | Tested here |
|---|---|---|---|
| `hsl/manifest.py` | both | no | yes |
| `hsl/inspector.py` | hython only | **yes** | no — needs a real scene |
| `hsl/bridge.py` | plain Python | no | partially |
| `hsl/husk.py` | plain Python | no | yes |
| `hsl/runner.py` | plain Python | no | yes (fake husk) |
| `hsl/ui.py` | plain Python + Qt | no | import-check only |
| `hsl/cli.py` | plain Python | no | yes |

## Non-negotiables

1. **Never widen the boundary.** No `hou` outside `inspector.py`, no Qt outside
   `ui.py`, no `manifest.py` import beyond the standard library.
2. **Never invent a Houdini or husk API.** Parameter names and CLI flags drift
   between versions. Everything currently unproven is listed in
   `docs/UNVERIFIED.md` with a way to check it. Verify before you change it,
   and move the entry when you do.
3. **Parameters are hints, the USD stage is truth.** In Solaris the render
   settings can be authored anywhere in the layer stack and the ROP's
   `rendersettings` parm is often empty. Read the composed stage.
4. **Every read of a node parameter goes through `_parm(node, names, default)`.**
   Never `node.parm("x").eval()` directly — it raises on scenes from a different
   Houdini build.
5. **Tests come with the change.** Anything in the Houdini-free layer needs a
   test in `tests/test_core.py` in the same commit. `python -m unittest
   discover -s tests` must stay green and must not require Houdini.
6. **Qt work happens on the Qt thread.** `RenderQueue` callbacks arrive on
   worker threads; they must cross into the UI through `QueueBridge` signals,
   never by touching a widget directly.

## Conventions

- Python 3.9+ syntax (Houdini 19.5 ships 3.9; do not use `match`, `X | None`
  at runtime, or 3.10+ stdlib).
- `from __future__ import annotations` at the top of every module.
- Dataclasses for data, not dicts. Type hints on public functions.
- No third-party dependencies in the core. PySide6 is for `ui.py` only.
- Errors say what happened and what to do about it. `manifest.warnings` is the
  channel for anything the inspector could not figure out — never fail silently
  and never guess a value to fill a gap.

## Commands

```bash
python -m unittest discover -s tests          # must be green, no Houdini needed
python scripts/check_drift.py                 # config pointers still valid
python scripts/verify_environment.py --report # probe a real Houdini install
./bin/hsl inspect scene.hip                   # read a scene
./bin/hsl render scene.hip --frames 1-10 --dry-run
```

## Where to start reading

`hsl/manifest.py` first — it is the contract, and the rest of the codebase only
makes sense once you know its shape. Then `docs/ARCHITECTURE.md` for why the
split exists, and `docs/UNVERIFIED.md` before touching anything Houdini-facing.
`docs/TASKS.md` holds the backlog with acceptance criteria.
