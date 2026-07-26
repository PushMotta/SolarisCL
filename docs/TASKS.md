# Backlog

Ordered by risk. Each has an acceptance criterion — "done" means the criterion
is demonstrated, not that the code looks right.

## ~~T1 — `export_usd()` ignores failed parameter writes~~  ·  **DONE 2026-07-26**

A missing `lopoutput` now **aborts** the export instead of returning a path
nothing was written to, and every frame parm that fails to set appends to
`warnings`. The same class of bug in `render_direct()` is covered by the new
`_apply_override()`, which reports any override that did not land — including
`resolutionx`/`resolutiony`, still unconfirmed on `usdrender_rop` and until now
silently ignored.

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

## ~~T6 — No per-chunk output verification~~  ·  **DONE 2026-07-26**

`RenderJob.expected_outputs` carries the paths a job should produce (the
`--output` override, else the settings prim's products), and
`RenderQueue._wrote_something()` expands their `$F` tokens per frame and checks
each file exists and is non-empty. A task that exits 0 having written **nothing**
is marked FAILED with the missing paths in its log.

Deliberately timid: with no known expectations the check is skipped rather than
guessed at, and a *partial* miss logs a warning but still passes — a false
failure over our own path arithmetic would be worse than the bug being caught.
Covered by five fake-husk tests.

## ~~T7 — Farm submission~~  ·  **DONE 2026-07-26**

The Deadline exporter wrote `first_job`'s command **verbatim**, frame numbers
and all, so every task on the farm would have re-rendered chunk 0 and no other
frame would ever have been produced. `farm.task_command()` now substitutes
Deadline's `<STARTFRAME>` token for the job's frame (`--frame` for husk,
`--frame-start` for hython), pins `--frame-count 1` and drops the per-task
increment, with `ChunkSize=1` so the scheduler owns the splitting.
`frame_expression()` emits the full covered range as explicit runs
(`1001-1100`, `1,6,11`) rather than the `1-19x2` shorthand not every scheduler
parses.

Tractor was already correct — one task per chunk with its real command — and
now also loses a duplicated `-title`. Seven tests assert on the generated files,
including that no literal start frame survives in the Deadline arguments.

## T8 — Manifest caching is unused by the CLI  ·  low

`bridge.load_cached()` / `save_cached()` exist and only the GUI writes them.
A repeated `hsl render` on an unchanged hip reloads Houdini every time.

**Done when:** `cmd_render` uses the cache when the hip is older than it, with
`--no-cache` to force a re-read, and a test for the staleness comparison.
