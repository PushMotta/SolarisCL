---
description: Run the inspector against a real .hip and sanity-check what it found
argument-hint: "<path to .hip>"
---

# Check a real scene

The first honest test of `hsl/inspector.py`. Everything in that module is
unverified until this passes on a scene whose correct answers are known.

## Steps

1. Confirm the environment:
   ```bash
   echo "$HFS"; which hython husk
   ```
   If hython is missing, stop — this workflow cannot run.

2. Read the scene:
   ```bash
   ./bin/hsl inspect "$1"
   ```

3. **Check the output against what Houdini actually shows**, item by item.
   Do not just confirm it printed something:
   - Are all render ROPs listed? Compare with `/stage` and `/out` in the GUI.
   - Does the resolution match the Render Settings LOP?
   - Is the camera prim path right?
   - Is the AOV list complete? An empty list is the signature of
     @docs/UNVERIFIED.md item D2 — Solaris authoring untyped prims that
     `IsA(UsdRender.Var)` misses.
   - Is the frame range right, including the increment?

4. Read `manifest.warnings` closely. Every warning is a parameter name that
   fell through to a default — these are the wrong guesses, showing themselves.

5. Check the USD actually written:
   ```bash
   ./bin/hsl inspect "$1" --export-usd
   usdcat --flatten <the exported file> | head -50
   ```
   For an animated shot, confirm time samples are present. Their absence means
   `export_usd()` is silently exporting a single frame — item C2.

6. Dry-run the command and read it critically:
   ```bash
   ./bin/hsl render "$1" --frames 1-10 --chunk 5 --dry-run
   ```

7. Only then render one frame for real.

8. Update @docs/UNVERIFIED.md with everything this proved or disproved, and fix
   what is wrong before moving on.
