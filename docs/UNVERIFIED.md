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

Two further defects were found by a targeted multi-ROP export probe on
2026-07-31 (C7 — colliding export filenames, C8 — `frame_range` defeated by the
ROP's `$FSTART`/`$FEND` expressions) and have since been fixed and re-probed on
both 21.0.729 and 22.0.368; both are written up in section C.

The synthetic probe's one SKIP (D2 — typed AOV prims) was then **resolved
separately** by running `/hip-check` on a real production Karma shot
(`SHOT_SandBurst_ROCHA_v14`, Houdini 22.0.368): the inspector read its ROPs,
resolution, camera, delegate settings and AOVs correctly. See D2/D4 below.

**Workflow:** run `verify_environment.py --report` on any new machine/version.
Move each newly confirmed item into the Verified table with the version you
checked, and fix the code for anything that comes back wrong.

Status key: `?` unverified · `OK` confirmed on a real install · `FIXED` was
wrong, corrected and re-confirmed · `WRONG` proven wrong on a real install and
**not yet fixed**

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

Set via `_set_parm()`, except the frame-range block which goes through
`_force_parm()` (clears the parm's expression, then reads the value back — see
C8). Note that `savestyle` is a **string menu**: `parm.set()` accepts an unknown
token without raising, so a wrong value here is doubly silent — no exception
*and* a return value of `True`. Only using a real menu token works.

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

**Still open (`docs/TASKS.md` T1):** `lopoutput` and the range parms now check
their return values (`lopoutput` aborts the export, the range parms warn), but
`enableoutputprocessor_simplerelativepaths` and `savestyle` still discard theirs
— both are harmless when absent, which is why they are left alone.

### Multi-ROP export in one pass  ·  measured (22.0.368), 2026-07-31

Asked for the CLI's multi-ROP sequential rendering: with `--export-usd` and **no**
`--rop`, does one inspector pass yield a usable `usd_path` for *every* render ROP?
Probed on a throwaway two-ROP `/stage` scene (`sphere → rendersettings →
usdrender_rop`, twice, distinct prim paths and distinct authored ranges 1-2 / 5-7),
plus a deliberately colliding variant. Every parm name used to build the scene was
probed first, not recalled.

| # | Finding | Status | Evidence |
|---|---|---|---|
| C6 | one pass exports **every** ROP found; each `RenderRop.usd_path` is populated, and each file holds that ROP's **own** stage | `OK` (22) | manifest had 2 rops, both `usd_path` non-empty and existing; `stage_usdrender_rop_A.usd` → `/geo_A`, `/Render/rs_A`, `stage_usdrender_rop_B.usd` → `/geo_B`, `/Render/rs_B`. `--rop B` wrote one file only |
| C7 | the export filename is `node.path().strip("/").replace("/", "_") + ".usd"`, so two ROPs whose paths differ only in **where the separator falls** collide silently | `FIXED` (21, 22) | was: `/stage/a_b/rop` and `/stage/a/b_rop` both → `stage_a_b_rop.usd`, **one** file, both manifest entries pointing at it, `/geo_SECOND` only, no warning. Now two files, right stages, warning |
| C8 | `export_usd(frame_range=…)` does **not** narrow the export on a stock `usd_rop` | `FIXED` (21, 22) | was: `f1`/`f2` ship as the expressions `$FSTART` / `$FEND`, `_set_parm` returned `True` while `rawValue()` stayed `'$FSTART'`. Now cleared first and read back — 3-4 asked, 3-4 written |

**C7 was fixed on 2026-07-31.** `inspect()` no longer flattens each node path in
isolation: `_export_usd_names()` takes **every** render ROP discovered in the
scene, groups them by flattened basename, and disambiguates only the groups with
more than one member by appending an 8-char SHA-1 of that node's own path.

- Deterministic on purpose. The suffix is a digest of the path, never a counter
  or a PID — a farm submission holding a `usd_path` must get the same filename
  from the same scene on any machine, in any discovery order.
- Names come from the **unfiltered** ROP list, so `--rop /stage/a_b/rop` writes
  the *same* file a full pass would rather than reverting to the colliding name.
- Ordinary scenes are untouched: a group of one keeps the old basename exactly.
- The disambiguation is announced in `manifest.warnings`, naming both ROPs and
  the file each got, because a filename that silently changed shape is its own
  small trap.

Measured on the same colliding scene, Houdini 22.0.368:

| | before | after |
|---|---|---|
| files on disk | 1 (`stage_a_b_rop.usd`) | 2 (`…_84d15dc9.usd`, `…_06a64410.usd`) |
| `/stage/a_b/rop` → | `stage_a_b_rop.usd`, contents `/geo_SECOND` | `…_84d15dc9.usd`, contents `/geo_FIRST` ✓ |
| `/stage/a/b_rop` → | `stage_a_b_rop.usd`, contents `/geo_SECOND` | `…_06a64410.usd`, contents `/geo_SECOND` ✓ |
| warning | none | `Export filename collision: /stage/a/b_rop and /stage/a_b/rop all flatten to 'stage_a_b_rop.usd'. Only one file would have survived…` |
| `--rop /stage/a_b/rop` alone | — | same `…_84d15dc9.usd` as the full pass |
| non-colliding two-ROP scene | `stage_usdrender_rop_A/B.usd` | **identical**, and no warning |

Re-run on **21.0.729** with the scene rebuilt there: the same two filenames, the
same digests (they are a function of the node path alone, so they are stable
across versions, processes and machines), the same prims in each, same warning.

**C8 was the K4 defect again, in the export path, and was fixed on 2026-07-31.**
`parm.set()` does not beat a default *expression*, and `_set_parm()` reports
`True` because nothing raised — so the miss was invisible on both channels the
code watches. Measured before the fix: asking
`export_usd(rop, out, frame_range=(3, 4, 1))` for two frames wrote **240** time
samples (frames 1–240, 16 705 B).

The fix is `_force_parm()`, used for every parm in `export_usd()`'s range block:
it calls `deleteAllKeyframes()` first (the K4 mechanism, **re-probed here on the
`usd_rop` parms rather than assumed**), sets the value, and then **reads it back**
— so a build where this stops working returns `False` and the existing warning
fires instead of exporting the wrong frames in silence. Probed on 22.0.368, fresh
`usd_rop`, playbar 1–240:

| parm | ships as | `_set_parm` | `_force_parm` |
|---|---|---|---|
| `trange` | `'off'` → 0, no keyframes | `True`, eval 1 ✓ | `True`, eval 1 ✓ |
| `f1` | **`'$FSTART'`**, 1 keyframe | `True`, raw still `'$FSTART'`, eval **1.0** ✗ | `True`, raw `'3'`, eval **3.0** ✓ |
| `f2` | **`'$FEND'`**, 1 keyframe | `True`, raw still `'$FEND'`, eval **240.0** ✗ | `True`, raw `'4'`, eval **4.0** ✓ |
| `f3` | `'1'`, no keyframes | `True`, eval 1.0 ✓ | `True`, eval 1.0 ✓ |
| `fileperframe` | `'off'`, no keyframes | `True`, eval 0 ✓ | `True`, eval 0 ✓ |

`deleteAllKeyframes()` exists on all five and is harmless on the four that carry
no expression. An absent parm still returns `False`, so the warning path is
intact. **21.0.729 produced this table line for line** — same `$FSTART`/`$FEND`
defaults, same failure under `_set_parm`, same success under `_force_parm`.

End to end on the animated two-ROP scene (radius = `$F * 0.1`, so the time
samples are an honest witness of which frames were written):

| run | before | after |
|---|---|---|
| `export_usd(frame_range=(3,4,1))` | 240 samples, 1–240, 16 705 B | **2 samples, [3, 4]**, 1 859 B |
| `--export-frames 3 4 1` (both ROPs) | 240 samples each, 16 705 B | **[3, 4]** each, layer timeCodes 3.0–4.0, 1 859 B |
| no `--export-frames`, ROP A authored 1-2 | 240 samples | **[1, 2]**, 1 857 B |
| no `--export-frames`, ROP B authored 5-7 | 240 samples | **[5, 6, 7]**, 1 915 B |
| `frame_range=None` | — | 1 sample (current frame) |

The last two rows are the wider win: the ROP's **own** authored range was being
ignored too, so *every* husk export covered the whole playbar. The 1 859 B vs
16 705 B on a toy sphere is the shape of the "exporting 1-240 to render frame 12"
cost on a real shot. Repeated on **21.0.729** (scene rebuilt there):
`--export-frames 3 4 1` → samples `[3, 4]`, 1 754 B.

**What is still not proven:** the `_force_parm` read-back returning `False` — the
guard for a build where `deleteAllKeyframes()` stops clearing the expression.
Both installs here clear it, so that branch has never fired against a real
Houdini (same shape as D12). Nor has any of this been run on a **production**
shot; the evidence is a synthetic two-ROP scene on both builds.

**`--export-frames` is global, not per-ROP**: the one tuple replaces *each* ROP's
authored range for every ROP in the pass —
`export_frames or (rop.frame_start, rop.frame_end, rop.frame_inc)`. There is no
per-ROP form, and **no manifest field records what was actually exported**:
`RenderRop`'s `frame_start` / `frame_end` stayed the ROP's own authored 1-2 and
5-7 in both the plain and the `--export-frames 3 4 1` run. A consumer cannot tell
from the manifest which frames a `usd_path` covers. That mattered less when the
answer was always "all of them"; now that the export genuinely narrows (C8), a
consumer that assumes otherwise would ask husk for frames the USD does not carry.
Recording it needs a new `manifest.py` field — see `docs/TASKS.md`.

**For the multi-ROP husk wiring:** C6 is the answer — one pass does give a
per-ROP `usd_path`, so multi-ROP husk is wireable. C7 (filename collisions) and
C8 (the range no-op) are both fixed above, so per-ROP `usd_path` integrity and
the requested frame range can now be relied on.

## D. USD schema access — `hsl/inspector.py` `walk_stage()`

| # | Assumption | Status |
|---|---|---|
| D1 | `prim.IsA(UsdRender.Settings)` matches typed prims authored by Solaris | `OK` (21, 22) |
| D2 | Solaris authors typed `UsdRender.Product` / `UsdRender.Var` prims, not untyped overs | `OK` (22, real scene) — a real Karma shot authored 4 typed `UsdRender.Var` prims; `IsA(UsdRender.Var)` matched them all |
| D3 | Stage metadata key is `renderSettingsPrimPath` | `OK` (21, 22) |
| D4 | Karma knobs are attributes on the settings prim in the `karma:` namespace | `OK` (22, real scene) — ~90 `karma:*` / `husk:*` attrs read off the settings prim |
| D5 | `hou.LopNode.stage()` returns the composed stage at the current time | `OK` (21, 22) — and the "current time" limitation is now **addressed**, see D9–D11 |
| D9 | There is **no** `LopNode.stageAtFrame()`; the frame is a keyword on `stage()` | `OK` (21, 22) |
| D10 | `stage(frame=N)` recomposes per frame and **beats** `hou.setFrame()` | `OK` (21, 22) |
| D11 | A Solaris network authors **no** stage time code range until a Configure Layer LOP sets `starttime` / `endtime` | `OK` (21, 22) |

### Cooking a LOP at a chosen frame  ·  `OK` (21.0.729, 22.0.368)

`docs/TASKS.md` T5 was written as "uses `stageAtFrame()` when given one".
**That method does not exist** on either build — a plausible name, recalled
rather than checked, and exactly the failure this register is for. The complete
list of stage-ish methods on `hou.LopNode` is identical on 21 and 22:

    editableStage · isMostRecentStageLock · stage · stagePrimStats · uneditableStage

The real mechanism is a keyword argument on `stage()` itself:

```
stage(self, output_index = -1, apply_viewport_overrides = False,
      ignore_errors = False, use_last_cook_context_options = True,
      apply_post_layers = True, frame = None, context_options = {}) -> pxr.Usd.Stage
```

> *"A frame number can be provided to return the result of cooking the LOP node
> at a particular frame."*

Probe: `scripts/_probe_frame_cook.py` (self-contained — builds its own network,
needs no `.hip`). It stands up two `rendersettings` LOPs behind a `switch` whose
`input` parm is the expression `$F > 5`, so the **set** of `UsdRender.Settings`
prim paths genuinely differs across the range, and then checks:

| # | Assumption | Status | Evidence |
|---|---|---|---|
| D9 | no `LopNode.stageAtFrame()` | `OK` (21, 22) | absent from `dir(hou.LopNode)` on both |
| D10a | `stage(frame=N)` recomposes | `OK` (21, 22) | frame 1 → `/Render/rendersettings_EARLY`, frame 10 → `…_LATE` |
| D10b | it is stable when frames are revisited | `OK` (21, 22) | 1, 10, 1, 10 returned the same answer each time — a cook cached at another frame is **not** handed back |
| D10c | the flip lands where `$F > 5` says | `OK` (21, 22) | frame 5 EARLY, frame 6 LATE |
| D10d | the explicit frame **beats** `hou.setFrame()` | `OK` (21, 22) | `setFrame(1)` + `stage(frame=10)` → LATE; `setFrame(10)` + `stage(frame=1)` → EARLY |
| D10e | `hou.setFrame()` + `stage()` also works | `OK` (21, 22) | the fallback for a build with no such keyword |

D10d is the load-bearing one. If the keyword were merely a hint that the global
frame overrode, `--frame` would silently describe whichever moment the `.hip`
was saved on — a wrong manifest that looks entirely reasonable. `inspect()`
still calls `hou.setFrame()` **as well**, because node *parameters* evaluate at
the global frame and the manifest reads plenty of those (the task scan, `f1`/
`f2`); the keyword is what makes the stage itself unambiguous. Same
belt-and-braces idea as `render_direct()` setting `f1`/`f2` *and* passing an
explicit `frame_range`.

A build without the keyword raises `TypeError`; `_stage_at()` then falls back to
moving the global frame and **says so in `manifest.warnings`** rather than
quietly describing the wrong moment. That fallback has never been exercised on a
real build, because both installs here have the keyword.

### Stage time codes are usually **not** authored  ·  `OK` (21, 22)

The T5 cross-range check needs a range to compare over, and the obvious source
is the stage's own `GetStartTimeCode()` / `GetEndTimeCode()`. Probed: a
`rendersettings → switch → usdrender_rop` network reports **`0.0 / 0.0`** with
`HasAuthoredTimeCodeRange() == False`, with the playbar sitting on 1–10. The
playbar range does **not** reach the stage.

They appear once a **Configure Layer** LOP sets them — and the parms are
`setstarttime` / `starttime` / `setendtime` / `endtime`. There is no
`starttimecode` / `endtimecode` parm on that node (checked, both builds); that
was the spelling worth guessing wrong.

So `_cross_range_frames()` prefers the stage time code range and **falls back to
the ROP's own authored frame range** when the stage declares none. Without the
fallback the check would silently never fire on an ordinary scene, which is the
under-reporting T5 exists to fix. Demonstrated both ways — see the table below.

**Confirmed on a real production shot**, which is what settles it:
`SHOT_Train_Aerial_DUDA_v05.hiplc` (167 MB, Karma, Houdini 22.0.368) authors
**no** stage time code range either — `0.0 / 0.0`,
`HasAuthoredTimeCodeRange() == False` on all three of its render ROPs, while
`/stage/usdrender_rop1` carries a perfectly ordinary authored range of
**1046–1075**. A stage-time-code-only trigger would have skipped the check
entirely on that shot. The fallback is load-bearing, not a convenience.

That shot also **opens on frame 1074**, not 1 — which is precisely why
`manifest.inspected_frame` has to be recorded rather than assumed. A consumer
that guessed "frame 1" would be wrong by 1073 frames.

### What the cross-range check costs  ·  measured (22.0.368)

Measured on that same shot, timing `stage_for()` per frame:

| ROP | first (cold) cook | extra cook at range start | at range end | check ran? |
|---|---|---|---|---|
| `/stage/usdrender_rop1` (1046–1075) | 94.9 s | 10.1 s (f1046) | 12.4 s (f1075) | yes — same settings both ends, **no warning** |
| `/stage/componentoutput1/thumbnail_render` | 0.2 s | — | — | no: single-frame ROP |
| `/stage/Train_asset/thumbnail_render` | 248.3 s | — | — | no: single-frame ROP |

So on the one ROP that actually renders a range, the check added **~22.5 s on
top of a 94.9 s cook** — a warm re-cook at another frame is roughly an eighth of
the cold one, not a repeat of it. A ROP with `trange = 0` (one frame) has no
range to compare and costs **nothing extra**, which is why the two thumbnail
ROPs — including the 248 s one — were untouched.

The check also produced **no false positive** on a real scene: both endpoints
resolved to `/Render/rendersettings` and no warning was emitted.

Where the inspected frame is already one of the endpoints the walk is reused, so
the usual bill is *one* extra cook, not two.

**D2 is resolved on a real scene.** `/hip-check` on a production Karma shot
(`SHOT_SandBurst_ROCHA_v14`, Houdini 22.0.368) returned 4 typed `UsdRender.Var`
prims — `beauty` (LPE `C.*[LO]`), `CryptoObject`, `CryptoPrimitives`, `depth` —
all matched by `IsA(UsdRender.Var)`. The empty-AOV failure mode did not occur;
the typed-prim assumption holds for Karma. The bare-probe SKIP stays as the
honest answer when a scene authors no vars. `D5`'s single-frame-cook limitation
is addressed by D9–D11 and `inspect(frame=…)`.

**Real-scene note:** in that shot the `UsdRenderProduct.productName` attribute
was empty on the products the settings prim references, so `outputs_for()`
returned no paths — the output location is driven by the ROP override / husk
`--output`, not authored on the product prim. Not an inspector bug (it reads
what is there), but worth knowing: an empty output list in the manifest does not
mean "no outputs". The inspector also correctly *warned* — rather than failing
silently — when the second ROP's input LOP did not cook to a stage at the
current frame.

### Live volumes bake on export  ·  `OK` (22, real scene)

`inspector.scan_live_volumes()` flags volumes that will **bake** into a USD
export. A SOP-imported volume has no `.vdb` on disk: its `UsdVolOpenVDBAsset`
fields carry an **empty** `filePath`, so exporting the stage serialises the
voxels into the layer — ~44 GB observed for a single frame on `SHOT_SandBurst`,
~1 TB for a full range. So the husk (USD-export) path is the wrong engine for
such shots; `render_direct` (hython) renders the live data with no export.

The scan groups empty-`filePath` `OpenVDBAsset` prims under their owning
`UsdVol.Volume` and lands them in `manifest.live_volumes`. Preflight raises a
`volume_bake` **warning** for the husk engine (not the hython engine, which
never exports), and — the real guard — `inspect(..., allow_volume_bake=False)`
**skips the export** for a ROP whose stage has live volumes, so the CLI
`hsl render --engine husk` aborts with advice instead of filling the disk
(`--allow-volume-bake` overrides).

| # | Assumption | Status | Evidence |
|---|---|---|---|
| D6 | `prim.IsA(UsdVol.OpenVDBAsset)` matches Houdini's SOP-imported volume fields | `OK` (22, real scene) | SandBurst `/stage/Render_01` → **8** `OpenVDBAsset` prims, matching the count `volume_probe.py` saw |
| D7 | `GetFilePathAttr().Get().path` is empty for a live field, set for a `.vdb`-referenced one | `OK` (22, real + synthetic) | **0 of 8** fields carried a `filePath` on the real shot; a synthetic stage with one live and one `.vdb`-backed volume flagged **only** the live one |
| D8 | `UsdVol.Volume` owns the field prims (fields are children) | `OK` (22, real scene) | all 8 fields' parents were `Volume`-typed; `_owning_volume()` resolved to that parent every time |

**Verified 2026-07-26 on Houdini 22.0.368** against
`SHOT_SandBurst_ROCHA_v14_Motta.hiplc`. `scan_live_volumes()` returned exactly
**4 volumes × 2 fields = 8** — SAND_BURST, SAND_Dev_07, SAND_Dev_06, SAND_Front,
each `[vel, density]` — matching the independently-observed raw counts.

**The guard was proven end to end on that shot:** `inspect(export=True,
allow_volume_bake=False)` on `/stage/Render_01` skipped the export —
`usd_path` empty, **0 bytes written**, C: free space unchanged (58.7 GB before
and after) — and emitted the explanatory warning. The ~44 GB/frame bake was
genuinely prevented, not merely reported. `allow_volume_bake=True` was
deliberately **not** exercised: that *is* the multi-GB bake, and C: has ~57 GB
free.

**`fieldName` is authored empty on real Solaris volumes** (8/8 on SandBurst) —
the field name lives in the **prim name** (`density`, `vel`). So
`scan_live_volumes()`'s `or prim.GetName()` fallback is load-bearing, not
defensive: reading `GetFieldNameAttr()` alone would label every warning with
empty field names. Do not "simplify" it away.

**Division of labour with the missing-asset scan** (confirmed on the synthetic
stage): an **empty** `filePath` is a *live volume* (bake risk); an **authored
but unresolved** `filePath` is a *missing asset* and is reported by
`scan_missing_assets` instead. The two never double-report the same field.

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

### AOV selection has no husk flag  ·  `OK` (22)

Checked against `husk --help`: husk has **no `--aov` / `--skip-aov` flag** (an
earlier version invented them; they were removed). Which AOV planes a render
writes is defined entirely by each `UsdRenderProduct`'s `orderedVars` in the USD.
The two look-alike flags are *not* it: `--mask` limits stage **population** to a
set of prims, and `--mplay-monitor` only sets which planes the interactive mplay
window shows. So AOV editing is a USD edit: `inspector.filter_usd_aovs()` authors
a thin overlay that sublayers the export and overrides each product's
`orderedVars` to the chosen subset (`bridge.filter_aovs` runs it under hython).
**Verified on 22.0.368** against a fixture — dropping one AOV from a 3-var
product left `orderedVars = [C, depth]` in the flattened composition, the other
product untouched.

### Overriding Karma render settings  ·  `OK` (22, real scene)

**husk cannot do this.** Its `Render Settings Overrides` section is a fixed,
short list — `--camera`, `--output`, `--res`, `--res-scale`, `--pixel-aspect`,
`--make-output-path`, `--disable-disk-check`, `--extra-metadata` — plus a few
Karma-specific ones (`--karma-percent-of-samples`, `--convergence-mode`,
`--disable-motionblur`, `--complexity`, `--purpose`) and `--settings` /
`--list-settings` to choose *which* settings prim. There is **no** flag to set
an arbitrary `karma:*` knob. Checked against `husk --help`; do not add one.

They are ordinary USD attributes on the RenderSettings prim, so they are set
the same way everything else here is — an overlay. Both engines are covered by
one implementation: `override_render_settings()` sublayers the exported USD for
husk, `author_settings_overlay()` + a Sublayer LOP composes into the network for
hython.

**Types come from the stage, never from the text.** `"1"` is a perfectly good
int, float, bool or string, so `_coerce_setting()` reads the value already on
the attribute and converts to *that* type. A knob that is not already present
has no discoverable type and is **refused** — inventing it would author
something the delegate may never read.

**Verified on `SHOT_SandBurst_ROCHA_v14` (22.0.368).** The settings prim carries
**113** `karma:*` / `husk:*` attributes (the earlier note said ~90). One of each
type was overridden through the hython path and read back off the re-cooked
stage:

| knob | type | before → after | type preserved |
|---|---|---|---|
| `husk:default_delegate` | str | `BRAY_HdKarmaXPU` → `BRAY_HdKarmaXPU_hsl` | yes |
| `husk:scene_lights` | bool | `True` → `False` | yes |
| `karma:global:bucketsize` | int | `32` → `39` | yes |
| `karma:global:cacheratio` | float | `0.25` → `0.75` | yes |

A deliberately bogus `karma:global:definitelynotareal_knob` was reported as "not
present on this settings prim" rather than authored. The ROP's input became
`/stage/hsl_settings`, confirming the Sublayer composition.

### Relinking on the hython engine  ·  `OK` (22, real scene)

The husk path relinks by overlaying the *exported* USD. `render_direct` has no
export — it renders the LOP network's composed stage — so the repaths have to
get into the network. `inspector.author_relink_overlay()` writes an
**opinions-only** layer (`over` prims carrying just the fixed asset paths, built
with `Sdf.AttributeSpec`), and `_insert_relink_layer()` composes it in with a
**Sublayer LOP** wired between the ROP and its input.

Two things had to be true, and both were checked rather than assumed:

| # | Assumption | Status | Evidence |
|---|---|---|---|
| G1 | a `sublayer` LOP type exists | `OK` (22) | `createNode("sublayer")` succeeds; `layer` does **not** exist |
| G2 | its file composes **stronger** than the incoming stage | `OK` (22) | two chained Sublayer LOPs: the downstream file's opinion won, and it sat *before* the base in `subLayerPaths` (earlier = stronger in USD) |
| G3 | the file parm is `filepath1` | `OK` (22) | set through `_set_parm`, and the override then took effect |

G2 is the load-bearing one: a sublayer composing *weaker* would leave the broken
paths winning and produce a confidently wrong render.

**Verified end to end on `SHOT_SandBurst_ROCHA_v14` (22.0.368):** the shot's 4
unresolved gravel textures (dead `D:/Dropbox/…` absolute paths) went to **0
missing** after relinking from the real `tex` directory, with the ROP's input
rewired to the inserted `/stage/hsl_relink` node.

### Missing textures & relinking  ·  `OK` (22)

husk has **no pre-scan** for missing textures — it only errors mid-render, which
is why preflight missed them. Detection is a USD job: `inspector.scan_missing_assets`
walks the composed stage and flags every asset attribute whose `Sdf.AssetPath`
has an empty `resolvedPath`. These land in `manifest.missing_assets` and preflight
raises them as errors. Relinking (`inspector.relink_assets`) authors an overlay —
same pattern as the AOV filter — repathing each missing asset to a same-basename
file found under the chosen search dirs; husk/hython then render the relinked USD.
**Verified on 22.0.368:** scan caught scalar + array texture refs, relink repathed
both (recursive search), and a re-scan of the relinked overlay found 0 missing.

### Output path: a single `-o` moves only the first product  ·  `OK` (22)

**Corrected 2026-07-26.** This was written as a flat limitation — "`-o` moves
only the first product" — which overstates it. `husk --help` says:

> *"A comma separated list of filenames can be used to override images when
> there are multiple render products."*

So **one** path redirects only product 0 (the original claim, true as far as it
goes), but a **comma list** redirects several. A multi-product shot given a
single `-o` still silently leaves crypto/depth where the scene pointed them,
which is the failure worth guarding against.

`--help` also documents the tokens `-o` expands, which the filename preview and
the output check depend on: `$F $FF $F4`, `$N` (N'th frame *of the sequence*),
`<F> <FF> <F4>`, and `%d %g %04d`. `expand_frame_token()` resolves the
unambiguous ones and **flags** `$FF` / `%g` / a bare `$N` rather than guessing —
inventing a filename there would have failed good renders (see T6).

So `inspector.override_product_paths()` authors an overlay (same mechanism as
the AOV filter) setting **every** product's `productName`. `--output` naming a
file gives that exact path to the first product and puts the rest alongside it;
naming a directory keeps each product's own filename. The CLI only pays for the
overlay when the settings prim has more than one product — one product still
uses husk's own flag, and then `-o` is *not* also passed, so it cannot
double-apply.

**Verified on 22.0.368** against a synthetic three-product stage:

| product | `--output /renders/v2/hero.exr` | `--output /renders/v3/` |
|---|---|---|
| beauty | `/renders/v2/hero.exr` | `/renders/v3/shot_beauty.$F4.exr` |
| crypto | `/renders/v2/shot_crypto.$F4.exr` | `/renders/v3/shot_crypto.$F4.exr` |
| depth (**empty** productName) | `/renders/v2/depth.exr` | `/renders/v3/depth.exr` |

`$F4` frame tokens survive, a product with no authored `productName` (exactly
what the real SandBurst products had) falls back to its prim name plus the
requested extension, and the source layer is left untouched. Colliding
destinations are reported as a warning rather than silently overwriting.

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
| D2 | typed `UsdRender.Var` prims on a real Karma scene | 22.0.368 | 2026-07-24 | SHOT_SandBurst_ROCHA_v14; 4 AOVs incl. LPE beauty + cryptomatte |
| D3 | stage metadata `renderSettingsPrimPath` | 21.0.729, 22.0.368 | 2026-07-24 | |
| D4 | `karma:*` / `husk:*` attrs on the settings prim | 22.0.368 | 2026-07-24 | ~90 knobs read from the same scene |
| D5 | `LopNode.stage()` returns composed stage | 21.0.729, 22.0.368 | 2026-07-24 | current-frame cook limitation lifted by D9–D11 |
| D9 | **no** `LopNode.stageAtFrame()` — the frame is a `stage()` keyword | 21.0.729, 22.0.368 | 2026-07-29 | TASKS.md T5 named a method that does not exist; `scripts/_probe_frame_cook.py` |
| D10 | `stage(frame=N)` recomposes per frame, is stable across revisits, and **beats** `hou.setFrame()` | 21.0.729, 22.0.368 | 2026-07-29 | switch LOP on `$F > 5`: frames 1/5 → `…_EARLY`, 6/10 → `…_LATE`; `setFrame(1)` + `stage(frame=10)` still gave LATE |
| D11 | stage time codes come from a Configure Layer LOP's `starttime`/`endtime` — a bare network authors none (`0.0/0.0`, `HasAuthoredTimeCodeRange()` False) | 21.0.729, 22.0.368 | 2026-07-29 | there is no `starttimecode` parm; this is why the cross-range check falls back to the ROP's frame range |
| D11-REAL | a **real** Karma shot authors no stage time code range either | 22.0.368 | 2026-07-29 | `SHOT_Train_Aerial_DUDA_v05`: 3 ROPs, all `0.0/0.0`, while `usdrender_rop1` has an authored range of 1046–1075. Scene opens on frame **1074**, not 1 |
| T5-COST | the cross-range check costs one warm re-cook per endpoint | 22.0.368 | 2026-07-29 | same shot: +10.1 s / +12.4 s against a 94.9 s cold cook; single-frame ROPs (incl. a 248 s one) cost nothing extra; no false positive |
| T5-END2END | `--frame` + the cross-range settings-drift warning | 22.0.368 | 2026-07-29 | 4 scenes: drift+timecodes, drift without timecodes, static+timecodes, static without. `--frame 1` → EARLY, `--frame 10` → LATE, each with the right `inspected_frame`, warning on **both** drifting scenes and on **neither** static one |
| D-ASSET | missing-texture scan (`Sdf.AssetPath.resolvedPath == ""`) + overlay relink | 22.0.368 | 2026-07-25 | verified scalar + array assets, recursive search; re-scan of relinked overlay = 0 missing |
| D6 | `IsA(UsdVol.OpenVDBAsset)` matches SOP-imported fields | 22.0.368 | 2026-07-26 | SandBurst: 8 field prims |
| D7 | empty `filePath` ⇒ live volume; set ⇒ cached | 22.0.368 | 2026-07-26 | 0/8 on the real shot; synthetic stage flagged only the live volume |
| D8 | `UsdVol.Volume` owns the field prims | 22.0.368 | 2026-07-26 | 8/8 parents `Volume`-typed |
| D-VOL | `scan_live_volumes` + the export guard | 22.0.368 | 2026-07-26 | 4 volumes / 8 fields; `allow_volume_bake=False` wrote **0 bytes**, disk free unchanged |
| E-OUT | `override_product_paths` redirects every product | 22.0.368 | 2026-07-26 | file + directory mode; `$F4` preserved; empty `productName` falls back to prim name; source untouched |
| G1–G3 | Sublayer LOP exists, composes stronger, parm is `filepath1` | 22.0.368 | 2026-07-26 | `layer` is not a type; downstream file's opinion wins |
| G-RELINK | hython-engine relink on the real shot | 22.0.368 | 2026-07-26 | SandBurst: 4 unresolved → **0** after relink; ROP input rewired to `/stage/hsl_relink` |
| H-SET | arbitrary `karma:*` / `husk:*` overrides via overlay | 22.0.368 | 2026-07-26 | 113 knobs present; str/bool/int/float each overridden and read back with its type intact; unknown knob refused |
| E1–E14 | every flag `build_command()` emits | 21.0.729, 22.0.368 | 2026-07-24 | via `husk --help`; `ALF_PROGRESS` literal wants a live render |
| E-AOV | husk has no AOV flag; selection is USD `orderedVars`; overlay filter works | 22.0.368 | 2026-07-24 | `filter_usd_aovs` verified: dropping a var left the right `orderedVars` |
| F1 | hython/husk under `$HFS/bin` | 21.0.729, 22.0.368 | 2026-07-24 | |
| F4 | hython bridge imports `hsl.inspector`, `describe_rop()` runs | 21.0.729, 22.0.368 | 2026-07-24 | |
| F5 | `hou.hipFile.load` signature | 21.0.729, 22.0.368 | 2026-07-24 | |
| K1 | non-Solaris ROP type names (`geometry`, `ifd`, `opengl`, `comp`, `dop`, `alembic`, `filmboxfbx`, `channel`, `baketexture`, `fetch`, `merge`, `rop_geometry`) all exist and expose `.render()` | 21.0.729, 22.0.368 | 2026-07-28 | both builds agreed exactly; `usdrender_rop`/`usd_rop` are **not** creatable in `/out` (LOP context only), which is why `find_render_rops` scans `/stage` |
| K2 | output parms: `sopoutput` (geometry, rop_geometry), `vm_picture` (ifd), `picture` (opengl), `copoutput` (comp), `dopoutput` (dop), `filename` (alembic), `file` (filecache) | 21.0.729, 22.0.368 | 2026-07-28 | read via `_parm_raw` so `$F4` survives; evaluating instead would name every frame after frame 1 |
| K3 | `filecache::2.0` has **no** `.render()`; it wraps a `render` child of type `rop_geometry` that does | 21.0.729, 22.0.368 | 2026-07-28 | driving the inner ROP with `frame_range=(5,6,1)` wrote exactly frames 5–6 |
| K4 | pressing a File Cache SOP's `execute` ignores an externally set range | 21.0.729, 22.0.368 | 2026-07-28 | `f1`/`f2` default to `$FSTART`/`$FEND` **expressions**; `.set()` does not beat them without `deleteAllKeyframes()`. This is why hsl drives the inner ROP instead |
| K5 | `cachesim` defaults to **1** on `filecache::2.0`; `trange` menu is only `('off','normal')` | 21.0.729, 22.0.368 | 2026-07-28 | so a stock File Cache SOP is treated as sequential and never chunked — conservative on purpose |
| K6 | ROP dependencies readable from `inputs()`; `fetch` points via its `source` parm | 21.0.729, 22.0.368 | 2026-07-28 | `merge`/other non-task nodes are traversed through, not reported |
| K7 | `--output` on a **File Cache SOP** cook repoints the cache | 22.0.368 | 2026-07-29 | setting the SOP's own `file` parm reaches the inner ROP: 2 frames landed at the redirected path, **nothing** at the SOP's authored location |
| K9 | `initsim` discriminates as a sequential signal | 22.0.368 | 2026-07-29 | two Geometry ROPs identical but for this parm: `0` → `sequential=False`, `1` → `sequential=True`. Proves it is read and flips the classification; that "Initialize Simulation OPs" *implies* frame-to-frame state is its documented meaning, not something this probe exercised (see K8) |
| C6 | one `--export-usd` pass exports **every** ROP, each to its own `usd_path`, each holding that ROP's own stage | 22.0.368 | 2026-07-31 | two-ROP throwaway scene: 2 rops, 2 files, `/geo_A` vs `/geo_B`. Multi-ROP husk is wireable |
| C7 | **fixed** — export filenames are made unique per pass (`_export_usd_names`): colliding groups get an 8-char SHA-1 of the node path, and the manifest warns | 21.0.729, 22.0.368 | 2026-07-31 | was one file for `/stage/a_b/rop` + `/stage/a/b_rop`; now 2 files, `/geo_FIRST` and `/geo_SECOND` in the right ones, warning present. `--rop` gives the same name as a full pass; a non-colliding scene's names are unchanged |
| C8 | **fixed** — `export_usd()` sets the range with `_force_parm` (`deleteAllKeyframes()` then set then read back), so `$FSTART`/`$FEND` no longer defeat it | 21.0.729, 22.0.368 | 2026-07-31 | was: asked 3-4, got 240 frames. Now `--export-frames 3 4 1` → samples `[3, 4]` (1 859 B vs 16 705 B); no flag → each ROP's own range, `[1,2]` and `[5,6,7]`. Same defect as K4 |
| K-COOK | **end-to-end headless cook** | 22.0.368 | 2026-07-28 | throwaway scene: `find_output_tasks` found 4 tasks (2 geometry ROPs, 1 dop, 1 filecache in `/obj`), classified cache/cache/sim/cache, read `final_rop → cache_rop` off the input chain, and the queue wrote **6 real `.bgeo.sc` files**; sequential filecache stayed 1 chunk under `chunk_size=2` |

## Still unknown (do not guess)

- **K10** — the **Tractor `.alf` dependency dialect**. `export_tractor_job` now
  encodes a DAG with `-id`, `Instance {}` and `-serialsubtasks 1`, and there are
  tests that the right structure is emitted — but no Tractor exists here to
  accept it. Check: submit a two-task job and confirm the dependant does not
  start until its prerequisite finishes. Deadline export *refuses* dependent
  work rather than guessing, so it needs no equivalent check.
- **K8** — sim correctness itself. A DOP ROP is classified `sim` and cooked in
  one ordered call, which is the *right shape*, but no real solver was run to
  confirm the cache matches an in-session sim. Check: cook a pyro/RBD setup with
  and without hsl and diff the frames. Forcing `sequential=False` on the task
  and confirming the chunked result differs is the quickest way to see the
  guard earning its keep.
- **D12** — the `TypeError` fallback in `_stage_at()`, for a build whose
  `LopNode.stage()` takes no `frame` keyword. Both installs here have it, so
  the fallback path has never actually run against a real Houdini. It moves the
  global frame with `hou.setFrame()` and warns; the *fallback mechanism itself*
  is verified (D10e), only the trigger is not. Check: on a pre-20 build, run
  `hython -m hsl.inspector scene.hip --frame 5` and confirm the warning appears
  and the description is still frame 5's.
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
