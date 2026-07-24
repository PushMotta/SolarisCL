---
description: Probe a real husk binary and reconcile every flag in hsl/husk.py against it
argument-hint: "[optional path to husk]"
---

# Verify husk flags

Reconciles section E of @docs/UNVERIFIED.md against the actual binary. Run this
first on any machine that has Houdini — it resolves the highest-risk unknowns
in the project.

## Steps

1. Locate husk: `$1`, else `$HSL_HUSK`, else `$HFS/bin/husk`, else `which husk`.
   If none exists, stop and say so. Do not guess the flags instead.

2. Capture the real interface:
   ```bash
   husk --help
   husk --list-renderers
   ```

3. Compare against every flag emitted by `build_command()` in `hsl/husk.py`,
   listed as E1–E14 in @docs/UNVERIFIED.md. For each: present / absent /
   different spelling / different argument shape.

4. Pay particular attention to:
   - **E13 `--verbose 3a`** — confirm the flag-letter syntax and that `a`
     produces `ALF_PROGRESS n%`. `parse_progress()` depends on the exact format.
     If it differs, fix the regex in `hsl/husk.py` and its test.
   - **E2 `--frame-count`** — confirm it is a count, not an end frame.
   - **E12 `--fast-exit 1`** — confirm it takes an argument rather than being
     a bare flag.
   - **E14 `--list-renderers`** — confirm the output shape matches
     `_RENDERER_LINE`, and that the real delegate names match `DEFAULT_RENDERERS`.

5. Fix `hsl/husk.py` for anything wrong, and update the corresponding test in
   `tests/test_core.py`.

6. Move every confirmed row into the Verified table in @docs/UNVERIFIED.md with
   the Houdini version and today's date. Leave anything still unknown in place.

7. Run `python -m unittest discover -s tests` and report.

## Do not

Mark anything verified that you did not see in the actual `--help` output.
Partial verification is fine and useful; a false Verified row is worse than no
row at all, because it stops the next person from checking.
