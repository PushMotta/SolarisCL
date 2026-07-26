# Backlog

Ordered by risk. Each has an acceptance criterion — "done" means the criterion
is demonstrated, not that the code looks right.

## T1 — `export_usd()` ignores failed parameter writes  ·  high

`_set_parm()` returns `False` when a parameter does not exist, and
`hsl/inspector.py:304-319` discards every return value. If `lopoutput` is named
differently in the user's Houdini version, the USD is written somewhere else
and husk renders a stale file or fails on a missing path — with no warning.

**Done when:** each `_set_parm()` call site checks the result and appends to
`warnings`; a missing `lopoutput` aborts the export rather than returning a
path that was never written; `docs/UNVERIFIED.md` C1–C5 note the new behaviour.

## ~~T2 — Confirm `--verbose 3a` produces `ALF_PROGRESS`~~  ·  **DONE 2026-07-25**

Verified against `husk --help` (`a` = "Turn on Alfred progress") **and one real
render** of `SHOT_SandBurst_ROCHA_v14` — `ALF_PROGRESS` was emitted live and
`parse_progress()` read it. `docs/UNVERIFIED.md` E13 is in the Verified table.

## ~~T3 — AOV discovery may find nothing~~  ·  **DONE 2026-07-24**

`IsA(UsdRender.Var)` matched all 4 typed `UsdRender.Var` prims on a real Karma
shot (`/hip-check` on `SHOT_SandBurst_ROCHA_v14`, Houdini 22.0.368) — `beauty`
(LPE), `CryptoObject`, `CryptoPrimitives`, `depth`. No untyped-prim fallback is
needed. `docs/UNVERIFIED.md` D2 is in the Verified table.

## ~~T9 — Volume-bake preflight guard~~  ·  **DONE 2026-07-26**

`inspector.scan_live_volumes()` + the `allow_volume_bake` export guard, verified
on 22.0.368 against the real shot (4 volumes / 8 fields; guard wrote 0 bytes).
See `docs/UNVERIFIED.md` D6–D8 / D-VOL.

## T4 — `USD_ROP_TYPES` is declared but never used  ·  low

`hsl/inspector.py:41`. Either wire it into ROP discovery, so a scene that
exports USD without a render ROP is still usable, or delete it.

**Done when:** the constant is used or gone, and the tests reflect the choice.

## T5 — Single-frame introspection under-reports  ·  medium

`.stage()` cooks at the current frame. A stage whose structure changes over
time is described from one moment. See `docs/UNVERIFIED.md` D5.

**Done when:** `inspect()` takes an optional frame, uses `stageAtFrame()` when
given one, and warns if the settings prim set differs between the first and
last frame of the range.

## T6 — No per-chunk output verification  ·  medium

A husk process can exit 0 having written nothing — wrong output path, no write
permission. `RenderQueue` currently trusts the return code.

**Done when:** each finished task checks its expected outputs exist and are
non-empty, and marks itself failed if not. Testable with the fake husk.

## T7 — Farm submission  ·  medium

`husk.jobs_for_rop()` already produces one job per chunk, which is the whole
of what a submitter needs.

**Done when:** `hsl/farm.py` emits a Deadline or Tractor job from a list of
`RenderJob`s, with tests that assert on the generated job file rather than on
a live scheduler.

## T8 — Manifest caching is unused by the CLI  ·  low

`bridge.load_cached()` / `save_cached()` exist and only the GUI writes them.
A repeated `hsl render` on an unchanged hip reloads Houdini every time.

**Done when:** `cmd_render` uses the cache when the hip is older than it, with
`--no-cache` to force a re-read, and a test for the staleness comparison.
