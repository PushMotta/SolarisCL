# Session handoff — hsl / SolarisCL

Read `AGENTS.md` (canonical brief) and `docs/UNVERIFIED.md` first. This file is
the *session* state: what's been done, what's proven, the gotchas, and what's
left. Written 2026-07-25.

## TL;DR

`hsl` is a Solaris/Karma render launcher (GUI + CLI + library). It loads a
Houdini `.hip` under **hython**, reads the render setup from the composed USD
stage, exports USD, and drives **husk** — with a plain-Python side that stays
testable without Houdini. This session verified it against real Houdini, fixed
several silent bugs, added AOV editing + missing-texture scan/relink, and proved
the whole chain on a real production shot.

- **Repo:** github.com/PushMotta/SolarisCL (private, `main`), clean & synced at
  commit `529e71d`. **59 tests green**; boundary/drift/ui-imports green.
- **Project lives at:** `F:\Nexus Projects\SolarisCL` — that directory *is* the
  git repo root (flattened 2026-07-26; it used to be nested at
  `extracted\pack\solaris_launcher`). The original delivery (`files.zip`,
  `solaris_launcher_pack.tar.gz` and the as-unpacked copies) is parked in
  `_archive\`, which is gitignored and safe to delete.
- **Persistent memory** already holds `houdini-env` and `solaris-cl-project`.

## Environment (this machine — critical)

- Windows 11. `$HFS` is **unset**; hython/husk are **not on PATH**. The tool
  auto-discovers installs (`bridge.list_hython_installations`), but for manual
  hython calls set e.g.
  `$env:HFS = "C:\Program Files\Side Effects Software\Houdini 22.0.368"`.
- **Two Houdini installs:** `22.0.368` (USD 0.26.5) and `21.0.729` (USD 0.25.5).
  **H21 has an Octane plugin** that prints `[Octane] …` banners to stdout — the
  probe uses a `@@HSL_PROBE_JSON@@` sentinel so that can't corrupt parsing.
- **C: is ~94% full (~57 GB free of 931 GB).** USD exports of heavy sims fill it
  fast (see cost note). Point `--usd-dir` at a roomier drive for real exports.
- **Test scene:** `V:\Pushvfx Dropbox\Pedro Motta\Etihad\ETIHAD RAIL_RX_SHARE\3D\HOUDINI\SHOT_SandBurst_ROCHA_v14_Motta.hiplc`.
  `V:` is the user's Dropbox — **never write render outputs there**; use scratch.

## Gotchas learned this session (will bite the next session)

- **Boundary rule is load-bearing:** `hou`/`pxr` only in `hsl/inspector.py`, Qt
  only in `hsl/ui.py`, stdlib only in `hsl/manifest.py`. Enforced by
  `.claude/hooks/boundary_guard.py` + tests. Never widen it (no `try: import hou`).
- **Never invent a husk flag or Houdini parm.** husk has **no `--aov`/`--skip-aov`**
  and **no missing-texture pre-scan**; `--mask` is a stage-population mask,
  `--mplay-monitor` is display-only. Verify against `husk --help` / a probe and
  record in `docs/UNVERIFIED.md`.
- **git-bash mangles args starting with `/`** (USD prim paths, `--keep`,
  `--settings`). Run hython/husk calls that take prim-path args from **PowerShell**
  (native, no mangling) — or through the tool's `subprocess` (no shell, safe).
  The tool itself is fine; only manual bash invocations are affected.
- **hython crashes on teardown (exit 5/255) on heavy scenes** *after* the work
  finishes. Python's block-buffered stdout is then lost. Always
  `print(..., flush=True)` (or `sys.stdout.flush()`) in probe scripts, ideally
  incrementally, so results survive.
- **PowerShell piping to `python -c` adds a UTF-8 BOM** → `json.load` fails. Print
  the raw JSON line instead of re-parsing through a pipe.
- A sandbox guard once **blocked `Remove-Item`** because a `C:\Program …` path
  appeared in the same command. Use `rm` (bash) or isolate the command.

## How USD edits work here (AOV filter, relink)

Both are **USD overlays authored under hython** (`hsl/inspector.py`), run via
`hsl/bridge.py`, because husk has no flags for them:
- `filter_usd_aovs` — overlay overriding each `UsdRenderProduct.orderedVars` to a
  chosen subset. Driver: CLI `render --aovs beauty,depth`, UI AOV Manager.
- `relink_assets` — overlay repathing unresolved assets (empty
  `Sdf.AssetPath.resolvedPath`) to same-basename files found under search dirs.
  Driver: CLI `render --relink-from DIR`, UI "Relink textures…".
- Overlays sublayer the export and override just what's needed — **fast, no
  re-cook**. husk/hython render the overlay.

## What was done this session

1. **Assessed the pack**; ran `make check` (proven-vs-asserted).
2. **Verified `inspector.py` against Houdini 21 + 22.** Fixed two *silent*
   version-drift bugs: ROP camera parm is `override_camera` (old candidates were
   all wrong); flatten token is `flattenalllayers` (old `flattenall` is not a
   real menu token, silently ignored). Hardened `verify_environment.py`
   (Octane-banner sentinel), made probe D2 an honest SKIP, reconciled
   `check_drift.py` with the Windows copy-fallback, fixed doc drift.
3. **Reviewed another agent's feature drop and fixed the dangerous parts** —
   invented husk `--aov`/`--skip-aov` (reimplemented AOV editing as USD overlays),
   invented `--disable-depth-of-field` preset flag (removed), a silently-deleted
   scene-load error dialog (restored), a non-functional Frame Preview stub
   (removed), duplicate `find_hython` (deduped). Kept the genuinely good adds:
   hython auto-discovery, preflight, presets, farm export (sketch), and a
   **direct-hython render engine** (`--engine hython` / `render_direct`).
4. **`/hip-check` on the real scene** resolved the two big unknowns: Karma **does**
   author typed `UsdRender.Var` prims (AOVs populate — D2), and `karma:*`/`husk:*`
   settings read off the settings prim (D4).
5. **Added missing-texture scan + relink + output browse.** `scan_missing_assets`
   populates `manifest.missing_assets`; **preflight now raises them as errors**
   (the gap that let a render start with missing maps).
6. **Proved the full chain on the real shot:** scan found **4 real missing gravel
   textures** (`mxn_gravel_crushed`, authored with `D:/Dropbox/WORK/…` absolute
   paths — Dropbox is `V:` here). Relink from `V:\…\3D\HOUDINI\tex` → 0 missing.
   husk rendered the relinked USD, **loaded the 4K gravel textures**, `Render
   complete` in 100 s. First **live** confirmation that `--verbose 3a` emits
   `ALF_PROGRESS`.

## Verified vs. not (see `docs/UNVERIFIED.md`)

- **Verified on 21.0.729 + 22.0.368:** all node types, ROP/USD-ROP parms, husk
  flags, USD schema calls; AOV overlay filter; missing-asset scan + relink
  (scalar + array, recursive search).
- **Known husk facts:** no `--aov`/`--skip-aov`; no missing-texture pre-scan;
  `-o/--output` overrides **only the first product** (multi-product needs a
  productName overlay).
- **Cost of USD export (SandBurst):** hip load ~7 s; `.usd` structure ~57 MB
  (negligible); **VDB volume sidecar ≈ tens of GB per frame** (44 GB observed for
  one frame) — the export **bakes** volumes because they're live SOP-imported
  (`OpenVDBAsset.filePath` is empty; verified 4 volumes / 8 field assets, 0
  filePaths). A full 1–30 export ≈ ~1 TB and won't fit on C:. **For volume-heavy
  shots, the hython-direct engine (no export) is the right choice.** This is now
  **enforced, not just documented**: the export is skipped unless
  `--allow-volume-bake` is passed (follow-up #1, verified 2026-07-26).

## Open follow-ups (prioritized)

1. ~~**Volume-bake preflight warning**~~ — **DONE and VERIFIED on 22.0.368
   (2026-07-26).** `inspector.scan_live_volumes()` groups empty-`filePath`
   `OpenVDBAsset` fields under their owning `UsdVol.Volume` into
   `manifest.live_volumes`. Preflight raises a `volume_bake` **warning** for the
   husk engine; and the real guard — `inspect(allow_volume_bake=False)` **skips
   the export** for a live-volume ROP, so `hsl render --engine husk` aborts with
   advice (use `--engine hython`, cache to `.vdb`, or `--allow-volume-bake`)
   instead of filling the disk. Surfaced in CLI `inspect`/`render` and the UI
   status/preflight tooltip. 62 tests green.
   **Verified on the real shot:** `scan_live_volumes` returned exactly the
   expected 4 volumes / 8 fields (SAND_BURST, SAND_Dev_07, SAND_Dev_06,
   SAND_Front — each `[vel, density]`), and the guard wrote **0 bytes** with C:
   free space unchanged (58.7 GB before and after). A synthetic stage confirmed
   the negative case: a `.vdb`-backed volume is *not* flagged. See
   `docs/UNVERIFIED.md` D6–D8 / D-VOL.
   **Gotcha found:** `fieldName` is authored **empty** on real Solaris volume
   fields (8/8) — the prim name carries it, so the `or prim.GetName()` fallback
   in `scan_live_volumes` is load-bearing. Don't simplify it away.
2. ~~**Complete the render-path override**~~ — **DONE, verified 22.0.368.**
   `inspector.override_product_paths()` authors a `productName` overlay
   redirecting **every** product; the CLI uses it only when the settings prim has
   >1 product (and then does *not* also pass husk `-o`, so it cannot
   double-apply). File mode gives product 0 the exact path and puts the rest
   alongside; directory mode keeps each product's filename. `$F4` survives; an
   empty `productName` falls back to the prim name. See `docs/UNVERIFIED.md` E-OUT.
3. ~~**Relink for the hython-direct engine**~~ — **DONE, verified on the real
   shot.** Not parm-level repath after all: `author_relink_overlay()` writes an
   opinions-only layer and `_insert_relink_layer()` composes it in with a
   **Sublayer LOP** (which sits *stronger* than the incoming stage — checked,
   because a weaker one would silently keep the broken paths). `--relink-from`
   now works on **both** engines. SandBurst went 4 unresolved → 0. See
   `docs/UNVERIFIED.md` G1–G3 / G-RELINK.
4. **Export only the frames being rendered:** export writes the ROP's full
   authored range regardless of `--frames`; narrowing it cuts export cost.
5. ~~**Farm exporter is a sketch**~~ — **DONE.** Deadline now substitutes
   `<STARTFRAME>` per task (`--frame` for husk, `--frame-start` for hython),
   `ChunkSize=1`, and `Frames=` carries the whole covered range as explicit runs.
   Tractor was already distributing correctly. 7 tests assert on the generated
   files, including that no literal start frame survives. See TASKS.md T7.
6. **`render_direct` rough edges** — ⬆ **now the highest-value item: this is the
   default engine.** Progress is faked (0→100, nothing between), so every default
   render looks hung; `resolutionx`/`resolutiony` overrides are unverified
   (likely silent no-op).
7. **TASKS.md T1** (other `_set_parm` returns in `export_usd` unchecked) — harmless
   on 21/22 since the parms exist, but a real gap on an untested build.
8. **Precise per-frame export TIME** was never captured (disk filled). Re-run
   `scratchpad/export_timing.py` (now flushed) with `--usd-dir`/outdir on a drive
   with hundreds of GB free.
9. **Preflight is UI-only.** `preflight.run_preflight_checks` is called from
   `hsl/ui.py` alone; `cmd_render` re-implements a subset inline. So CLI and farm
   users get no disk-space / output-path / resolution checks. Consolidating is
   cheap, removes the duplication, and is **fully testable without Houdini**.

## How to run / verify

```bash
python -m unittest discover -s tests        # 59 tests, no Houdini needed
make check                                   # tests + boundary + drift + ui-imports (green on Windows)
# with $HFS set to a Houdini install:
python scripts/verify_environment.py --report
python -m hsl.cli inspect <hip>              # lists ROPs, res, camera, AOVs, MISSING textures
python -m hsl.cli render <hip> --frames 1 --aovs beauty --relink-from <texdir> --output <path> --dry-run
python -m hsl.cli render <hip> --frames 1 --engine hython   # direct render, no USD export
```

The scratchpad has probe scripts from this session (export_timing.py, scan_real.py,
volume_probe.py, tex_probe.py, and USD fixtures) if useful to re-run.
