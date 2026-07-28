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

The synthetic probe's one SKIP (D2 — typed AOV prims) was then **resolved
separately** by running `/hip-check` on a real production Karma shot
(`SHOT_SandBurst_ROCHA_v14`, Houdini 22.0.368): the inspector read its ROPs,
resolution, camera, delegate settings and AOVs correctly. See D2/D4 below.

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
| D2 | Solaris authors typed `UsdRender.Product` / `UsdRender.Var` prims, not untyped overs | `OK` (22, real scene) — a real Karma shot authored 4 typed `UsdRender.Var` prims; `IsA(UsdRender.Var)` matched them all |
| D3 | Stage metadata key is `renderSettingsPrimPath` | `OK` (21, 22) |
| D4 | Karma knobs are attributes on the settings prim in the `karma:` namespace | `OK` (22, real scene) — ~90 `karma:*` / `husk:*` attrs read off the settings prim |
| D5 | `hou.LopNode.stage()` returns the composed stage at the current time | `OK` (21, 22) |

**D2 is resolved on a real scene.** `/hip-check` on a production Karma shot
(`SHOT_SandBurst_ROCHA_v14`, Houdini 22.0.368) returned 4 typed `UsdRender.Var`
prims — `beauty` (LPE `C.*[LO]`), `CryptoObject`, `CryptoPrimitives`, `depth` —
all matched by `IsA(UsdRender.Var)`. The empty-AOV failure mode did not occur;
the typed-prim assumption holds for Karma. The bare-probe SKIP stays as the
honest answer when a scene authors no vars. `D5` still carries the known
single-frame-cook limitation — see `docs/TASKS.md` T5.

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
| D5 | `LopNode.stage()` returns composed stage | 21.0.729, 22.0.368 | 2026-07-24 | current-frame cook limitation stands (T5) |
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
| K-COOK | **end-to-end headless cook** | 22.0.368 | 2026-07-28 | throwaway scene: `find_output_tasks` found 4 tasks (2 geometry ROPs, 1 dop, 1 filecache in `/obj`), classified cache/cache/sim/cache, read `final_rop → cache_rop` off the input chain, and the queue wrote **6 real `.bgeo.sc` files**; sequential filecache stayed 1 chunk under `chunk_size=2` |

## Still unknown (do not guess)

- **K7** — whether `--output` on a **File Cache SOP** cook actually repoints the
  cache. `cook_task` sets the SOP's own `file` parm (the one an artist sees) and
  falls back to the inner ROP's `sopoutput`, but whether the two are linked in
  both directions was not probed. Overriding the output of a *ROP* task **is**
  verified (K2). Check: cook a filecache with `--output` and see where the files
  land.
- **K8** — sim correctness itself. A DOP ROP is classified `sim` and cooked in
  one ordered call, which is the *right shape*, but no real solver was run to
  confirm the cache matches an in-session sim. Check: cook a pyro/RBD setup with
  and without hsl and diff the frames.
- **K9** — `initsim` as a sequential signal is read but was only observed at its
  default (0). Whether enabling it on a Geometry ROP genuinely implies
  frame-to-frame state was not tested.
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
