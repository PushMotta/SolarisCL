---
trigger: always_on
---

# Architecture — the boundary

Full brief: @AGENTS.md

`hou` and `pxr` may be imported in **`hsl/inspector.py` only**. That file runs
under `hython`. Every other module must stay importable on a machine with no
Houdini installed — that is what makes the test suite runnable, and it is not
negotiable. The two halves talk only through the JSON manifest in
`hsl/manifest.py`.

A `try: import hou / except ImportError` guard is not a workaround. If code
needs Houdini, it belongs in `inspector.py` and its result travels through the
manifest.

Likewise: Qt only in `hsl/ui.py`; standard library only in `hsl/manifest.py`.

Check before you claim to be done:

```bash
python .claude/hooks/boundary_guard.py --check-tree .
python -m unittest discover -s tests
```

## Never invent a Houdini or husk API

Parameter names and CLI flags drift between versions, and a wrong guess here
fails **silently** — a bad parm name falls through to a default and the user
gets the wrong frame range with no error. Everything currently unproven is in
@docs/UNVERIFIED.md with a way to check it. Verify before changing, and move the
entry when you do.

Houdini is probably not installed on this machine. Say "unverified" rather than
asserting a Houdini code path works.
