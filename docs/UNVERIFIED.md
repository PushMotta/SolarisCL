# Unverified assumptions

Every claim this codebase makes about the Houdini and husk APIs, tracked against
whether it has been **executed on a real installation**.

**Verified on 2026-07-24 against Houdini 21.0.729 (USD 0.25.5) and 22.0.368
(USD 0.26.5)** on Windows, via `python scripts/verify_environment.py --report`.
Both runs came back `47 confirmed, 0 wrong, 1 not checked`. Two assumptions were
found wrong on *both* versions and have been fixed (B5, C4); the remainder is
confirmed. What is still genuinely unknown is listed below with a way to check
it — do not "fix" those from recall, because the failure mode is a silently
incorrect render, not an exception.

**Workflow:** run `verify_environment.py --report` on any new machine/version.
Move each newly confirmed item into the Verified table with the version you
checked, and fix the code for anything that comes back wrong.

Status key: `?` unverified · `OK` confirmed on a real install · `FIXED` was
wrong, corrected and re-confirmed

---

## A. Node type names — `hsl/inspector.py`

| # | Assumption | Status | How to check |
|---|---|---|---|
| A1 | Render ROPs are type `usdrender_rop` (Solaris) or `usdrender` (`/out`) | `OK` (21, 22) — `usdrender_rop` confirmed; `usdrender` on `/out` not exercised | `hou.node('/stage').allSubChildren()` then `n.type().name()` |
| A2 | The Karma LOP wrapper reports type `karma` | `OK` (21, 22) | Drop a Karma LOP, print its type name |
| A3 | Versioned names (`usdrender_rop::3.0`) split cleanly at `::` | `?` — no versioned type names present on either build, so the split path is untested | `_type_name()` in a scene saved from a newer build |
| A4 | `usd_rop` is the USD ROP LOP type used by `export_usd()` | `OK` (21, 22) | `hou.node('/stage').createNode('usd_rop')` must not raise |

`USD_ROP_TYPES` at `inspector.py:41` is still declared but unused — either wire
it into ROP discovery or delete it (see `docs/TASKS.md` T4).

## B. Render ROP parameters — `hsl/inspector.py` `describe_rop()`

All go through `_parm()`, so a wrong name degrades to a default rather than
raising. That is the safe failure — but it means **a wrong guess is invisible**.
Anything that falls back lands in `manifest.warnings`; check those first.

| # | Parameter | Used for | Status |
|---|---|---|---|
| B1 | `trange` — 0 current / 1 range / 2 strict range | frame mode | `OK` (21, 22) |
| B2 | `f1` `f2` `f3` | start / end / increment | `OK` (21, 22) |
| B3 | `renderer` (fallbacks `husk_renderer`, `engine`) | Hydra delegate | `OK` (21, 22) — `renderer` present |
| B4 | `rendersettings` (fallbacks `settingsprim`, `rendersettingsprim`) | settings prim path | `OK` (21, 22) — `rendersettings` present |
| B5 | camera prim override | camera prim | `FIXED` (21, 22) |
| B6 | `outputimage` (fallbacks `picture`, `override_outputimage`) | output override | `OK` (21, 22) — `outputimage` present |

**B5 was wrong on both versions.** The real parm is **`override_camera`** (a
String path, and the only camera-like parm on the ROP). The old candidate list
`("camera", "override_camera_path", "cameraprim")` matched none of them, so the
ROP-level camera override silently read as empty. Fixed: `override_camera` is
now first in the candidate list.

## C. USD ROP parameters — `hsl/inspector.py` `export_usd()`

Set via `_set_parm()`. Note that `savestyle` is a **string menu**: `parm.set()`
accepts an unknown token without raising, so a wrong value here is doubly silent
— no exception *and* a return value of `True`. Only using a real menu token
works.

| # | Parameter | Consequence if wrong | Status |
|---|---|---|---|
| C1 | `lopoutput` | USD written to the default path, husk renders a stale/missing file | `OK` (21, 22) |
| C2 | `trange` / `f1` / `f2` / `f3` | single frame exported for an animated shot | `OK` (21, 22) |
| C3 | `fileperframe` (0 = one file) | per-frame USD files husk is not pointed at | `OK` (21, 22) |
| C4 | flatten via `savestyle` | `--flatten` silently does nothing | `FIXED` (21, 22) |
| C5 | `enableoutputprocessor_simplerelativepaths` exists | harmless if absent | `OK` (21, 22) |

**C4 was wrong on both versions.** The menu tokens are
`flattenimplicitlayers / flattenalllayers / separate / flattenstage`; the old
`"flattenall"` was not among them, and because the parm is a string menu the ROP
silently ignored it, making `--flatten` a no-op. Fixed: `export_usd()` now picks
a real token via `_flatten_token()` (prefers `flattenalllayers`, falls back to
`flattenstage`) and warns if neither is present.

**Still open (`docs/TASKS.md` T1):** the other `_set_parm()` calls in
`export_usd()` (`lopoutput`, `trange`, …) still ignore their return values. On
21/22 those parms all exist so nothing is lost today, but on an untested build a
missing `lopoutput` would export to the wrong path with no warning.

## D. USD schema access — `hsl/inspector.py` `walk_stage()`

| # | Assumption | Status |
|---|---|---|
| D1 | `prim.IsA(UsdRender.Settings)` matches typed prims authored by Solaris | `OK` (21, 22) |
| D2 | Solaris authors typed `UsdRender.Product` / `UsdRender.Var` prims, not untyped overs | `?` — **still unverified**; the throwaway probe scene authors no products/vars, so this needs a real shot with known AOVs |
| D3 | Stage metadata key is `renderSettingsPrimPath` | `OK` (21, 22) |
| D4 | Karma knobs are attributes on the settings prim in the `karma:` namespace | `?` — probe authored no karma settings; unexercised |
| D5 | `hou.LopNode.stage()` returns the composed stage at the current time | `OK` (21, 22) |

**D2 is the one that matters most** and is genuinely still unknown: if Solaris
authors AOVs as untyped overs, `IsA(UsdRender.Var)` returns False and the AOV
list comes back empty. The probe now reports this honestly as SKIP rather than a
misleading FAIL. Resolve it by running `/hip-check` on a real scene whose AOVs
you know; if `IsA()` misses them, fall back on `GetTypeName()` or the products'
`orderedVars` targets, with a USD-fixture test. `D5` also carries the known
single-frame-cook limitation — see `docs/TASKS.md` T5.

## E. husk CLI flags — `hsl/husk.py` `build_command()`

Confirmed against `husk --help` on 22.0.368 (and flag presence re-checked on
21.0.729). Every flag `build_command()` emits is present.

| # | Flag | Status | Note |
|---|---|---|---|
| E1 | `--renderer` / `-R` | `OK` (21, 22) | |
| E2 | `--frame` `--frame-count` `--frame-inc` | `OK` (21, 22) | count, not end frame — confirmed |
| E3 | `--settings` | `OK` (21, 22) | |
| E4 | `--camera` | `OK` (21, 22) | |
| E5 | `--output` / `-o` | `OK` (21, 22) | |
| E6 | `--res W H` | `OK` (21, 22) | |
| E7 | `--threads` | `OK` (21, 22) | |
| E8 | `--complexity` | `OK` (21, 22) | accepted values still unconfirmed |
| E9 | `--purpose` | `OK` (21, 22) | comma-separated form still unconfirmed |
| E10 | `--snapshot SECONDS` | `OK` (21, 22) | |
| E11 | `--make-output-path` | `OK` (21, 22) | |
| E12 | `--fast-exit 1` | `OK` (21, 22) | takes an argument — confirmed |
| E13 | `--verbose 3a` — level plus flag letters | `OK` (21, 22) | `husk --help`: `a/A Turn on/off Alfred progress`. The literal `ALF_PROGRESS n%` string still wants one real render to be 100% pinned. |
| E14 | `--list-renderers` output is one delegate per line | `OK` (21, 22) | delegates parse; on 22 the Karma pair matches `DEFAULT_RENDERERS` |

`husk --help` confirms `-V [ --verbose ] arg` is `0-9` plus flag letters where
`a` = "Turn on Alfred progress" — exactly what `parse_progress()` depends on.

## F. Environment and licensing

| # | Assumption | Status |
|---|---|---|
| F1 | `$HFS/bin/hython` and `$HFS/bin/husk` exist after `source houdini_setup` | `OK` (21, 22) — found via `$HFS/bin` on Windows |
| F2 | husk takes a Karma render license, not a full Houdini license | `?` — not tested |
| F3 | Houdini Indie caps husk at 1920×1080 | `?` — not tested (these are not Indie installs) |
| F4 | `hython -m hsl.inspector` works with `PYTHONPATH` injection (`bridge.py`) | `OK` (21, 22) — hython imports `hsl.inspector` and `describe_rop()` runs against a live ROP |
| F5 | `hou.hipFile.load(..., suppress_save_prompt=True, ignore_load_warnings=True)` signature | `OK` (21, 22) — method present |

---

## Verified

| # | Item | Confirmed on | Date | Note |
|---|---|---|---|---|
| A1 | node type `usdrender_rop` | 21.0.729, 22.0.368 | 2026-07-24 | `usdrender` on `/out` not yet exercised |
| A2 | node type `karma` | 21.0.729, 22.0.368 | 2026-07-24 | |
| A4 | node type `usd_rop` | 21.0.729, 22.0.368 | 2026-07-24 | |
| B1 | `trange` | 21.0.729, 22.0.368 | 2026-07-24 | |
| B2 | `f1` / `f2` / `f3` | 21.0.729, 22.0.368 | 2026-07-24 | |
| B3 | `renderer` | 21.0.729, 22.0.368 | 2026-07-24 | |
| B4 | `rendersettings` | 21.0.729, 22.0.368 | 2026-07-24 | |
| B5 | `override_camera` | 21.0.729, 22.0.368 | 2026-07-24 | **fixed** — was `camera`/`override_camera_path`/`cameraprim` |
| B6 | `outputimage` | 21.0.729, 22.0.368 | 2026-07-24 | |
| C1 | `lopoutput` | 21.0.729, 22.0.368 | 2026-07-24 | return-value check still open (T1) |
| C2 | `trange` / `f1` on `usd_rop` | 21.0.729, 22.0.368 | 2026-07-24 | |
| C3 | `fileperframe` | 21.0.729, 22.0.368 | 2026-07-24 | |
| C4 | `savestyle=flattenalllayers` | 21.0.729, 22.0.368 | 2026-07-24 | **fixed** — was `flattenall` (not a real token) |
| C5 | `enableoutputprocessor_simplerelativepaths` | 21.0.729, 22.0.368 | 2026-07-24 | |
| D1 | `IsA(UsdRender.Settings)` | 21.0.729, 22.0.368 | 2026-07-24 | |
| D3 | stage metadata `renderSettingsPrimPath` | 21.0.729, 22.0.368 | 2026-07-24 | |
| D5 | `LopNode.stage()` returns composed stage | 21.0.729, 22.0.368 | 2026-07-24 | current-frame cook limitation stands (T5) |
| E1–E14 | every flag `build_command()` emits | 21.0.729, 22.0.368 | 2026-07-24 | via `husk --help`; `ALF_PROGRESS` literal wants a live render |
| F1 | hython/husk under `$HFS/bin` | 21.0.729, 22.0.368 | 2026-07-24 | |
| F4 | hython bridge imports `hsl.inspector`, `describe_rop()` runs | 21.0.729, 22.0.368 | 2026-07-24 | |
| F5 | `hou.hipFile.load` signature | 21.0.729, 22.0.368 | 2026-07-24 | |

## Still unknown (do not guess)

- **D2** — whether Solaris authors typed `UsdRender.Var`/`Product` prims. The
  highest-value remaining unknown; needs a real scene with known AOVs
  (`/hip-check`).
- **D4** — `karma:` namespaced delegate knobs on the settings prim.
- **A3** — the `::`-versioned type-name split path.
- **E8/E9** — accepted values for `--complexity` and `--purpose`.
- **F2/F3** — Karma license behaviour and the Indie resolution cap.

---

## What *is* proven without Houdini

The Houdini-free layer is covered by the test suite in `tests/test_core.py`,
including a fake husk executable that exercises the full start / progress /
cancel / failure path of `RenderQueue`. Manifest round-trip, frame chunking
arithmetic, argv construction and progress parsing are all verified there —
independently of the real binary, which is what the section above now covers.
