---
description: Add a new Houdini node parameter to the inspector the safe way
argument-hint: "<what the parameter controls>"
---

# Add a parameter probe

Use when the inspector needs to read something new off a Houdini node. The
point of this workflow is that the naive version — `node.parm("x").eval()` —
fails silently on scenes from a different Houdini build.

## Steps

1. **Find the real name.** If Houdini is available:
   ```bash
   hython -c "import hou; n=hou.node('/stage').createNode('usdrender_rop'); print(sorted(p.name() for p in n.parms()))"
   ```
   If it is not, gather candidate spellings and treat every one as a guess.

2. **Add the field to `hsl/manifest.py` first.** It is the contract; nothing can
   consume what the manifest cannot carry. Give it a type and a default.

3. **Read it in `hsl/inspector.py` through `_parm()`** with candidates ordered
   newest-first:
   ```python
   value = _parm(node, ("newname", "oldname", "legacyname"), default)
   ```
   Add the name to the `raw` dict in `describe_rop()` so it shows up for
   debugging. If the read falls through to the default and that matters, append
   to `warnings` — a silent fallback is the failure mode this whole pattern
   exists to prevent.

4. **Consume it** in `hsl/husk.py`, `hsl/ui.py` or `hsl/cli.py` as needed.

5. **Test the parts that do not need Houdini**: manifest round-trip carries the
   new field, and command construction uses it. Add to `tests/test_core.py`.

6. **Record it.** Add a row to section B or C of @docs/UNVERIFIED.md with status
   `?` unless you probed a live Houdini, in which case add it to Verified with
   the version.

7. Run `python -m unittest discover -s tests` and
   `python .claude/hooks/boundary_guard.py --check-tree .`
