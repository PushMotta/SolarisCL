# Session handoff — hsl / SolarisCL

Read `AGENTS.md` (canonical brief) and `docs/UNVERIFIED.md` first. This file is
the *session* state: what is done, what is proven, the traps, and what is left.
Rewritten 2026-07-26/27.

## TL;DR

`hsl` is a Solaris/Karma render launcher (GUI + CLI + library). It loads a
Houdini `.hip` under **hython**, reads the render setup from the composed USD
stage, and renders — either **directly in hython** (the default) or by exporting
USD and driving **husk**. The plain-Python half stays testable without Houdini.

- **Repo:** github.com/PushMotta/SolarisCL (private, `main`), baseline commit
  `5e23bad` plus the working-tree overlay collision fix. **177 tests green**;
  boundary / drift / ui-imports green.
- **Project root:** `F:\Nexus Projects\SolarisCL` — the working dir **is** the
  git root (flattened this session; it used to be nested three levels down at
  `extracted\pack\solaris_launcher`, that path is dead). The original delivery
  sits in `_archive\`, gitignored and disposable.

## Environment (this machine — critical)

- Windows 11. `$HFS` is **unset**; hython/husk are **not on PATH**. The tool
  auto-discovers installs; for manual hython calls set e.g.
  `$env:HFS = "C:\Program Files\Side Effects Software\Houdini 22.0.368"`.
- **Two Houdini installs:** `22.0.368` (USD 0.26.5) and `21.0.729` (USD 0.25.5).
  **H21 has an Octane plugin** printing `[Octane]` banners to stdout — probes use
  a `@@SENTINEL@@` prefix so that cannot corrupt parsing.
- **C: is ~94% full (~57 GB free).** Never export a volume-heavy stage to it.
  The volume guard now prevents this by default.
- **Test scene:** `V:\Pushvfx Dropbox\Pedro Motta\Etihad\ETIHAD RAIL_RX_SHARE\3D\HOUDINI\SHOT_SandBurst_ROCHA_v14_Motta.hiplc`
  Textures: `…\3D\HOUDINI\tex`. `V:` is Dropbox — **never write outputs there**.

## The two engines (the big change this session)

`husk.DEFAULT_ENGINE = "hython"`.

| | `hython` (**default**) | `husk` |
|---|---|---|
| How | renders the ROP directly under hython | exports USD, then runs husk |
| USD on disk | none | one file per ROP |
| Needed for | everything local; **essential** for volume-heavy shots | farm submission, `--aovs` |

hython is the default because the export is pure overhead for a local render,
and on a stage with live SOP volumes it **bakes** them at ~tens of GB/frame
(~44 GB observed for one frame of SandBurst; a full range ≈ 1 TB).

**Anything husk cannot do is done as a USD overlay.** That mechanism is now used
four times and proven each time: AOV `orderedVars`, `productName` redirect,
asset relink, and render-setting overrides. For husk the overlay sublayers the
exported USD; for hython a **Sublayer LOP** composes it into the live network
(it sits *stronger* than the incoming stage — verified, and load-bearing: a
weaker one would silently keep the original values).

## What shipped this session

1. **Volume-bake guard.** `inspector.scan_live_volumes()` finds volumes whose
   `OpenVDBAsset.filePath` is empty. `inspect(allow_volume_bake=False)` **skips
   the export**; the CLI aborts with advice. Preflight warns (husk only).
2. **hython is the default engine**, with `--aovs` rejected rather than silently
   ignored on that path, and missing-asset reporting ungated so broken textures
   are never silent.
3. **`hsl hython`** lists Houdini installs, `--set <index|path>` remembers one.
   GUI has the same dropdown plus a **Rescan** button.
4. **Real per-frame progress** for `render_direct` (was 0→100 only).
5. **Preflight on the CLI** (was GUI-only), `--skip-preflight` to override.
6. **T1**: a missing `lopoutput` aborts the export; every override reports if it
   did not land (`_apply_override`).
7. **Export narrowed to `--frames`** instead of the ROP's whole authored range.
8. **Farm submission actually distributes** — Deadline wrote job[0]'s command
   verbatim, so every task would have re-rendered chunk 0.
9. **Output verification** (T6): a render that exits 0 having written nothing is
   marked FAILED.
10. **Multi-product output redirect** + **relink on hython** + **arbitrary Karma
    setting overrides**, all via the overlay mechanism, all on both engines.
11. **Filename preview** — `hsl inspect`, `--dry-run` and the GUI show the files
    a render will actually write.
12. **GUI**: readable status line, Render-settings tab, Output **File…/Folder…**,
    relink wired for hython.

## Traps that cost time (will bite again)

- **Boundary rule is load-bearing.** `hou`/`pxr` only in `inspector.py`, Qt only
  in `ui.py`, stdlib only in `manifest.py`. Never widen it.
- **Never invent a husk flag or Houdini parm.** husk has **no** `--aov`, no
  missing-texture pre-scan, and **no flag to set an arbitrary `karma:*` knob**.
  Verify against `husk --help` / a probe, then record in `docs/UNVERIFIED.md`.
- **git-bash mangles args starting with `/`** (prim paths). Use PowerShell for
  manual hython calls; the tool's own `subprocess` calls are safe.
- **hython crashes on teardown after heavy work.** Always `flush=True` in probes
  or the output is lost.
- **PowerShell read-modify-write corrupts this file set.** A `Get-Content`/
  `Set-Content` round-trip mangled 36 non-ASCII characters in `ui.py` (every `—`
  and `…`). Use the editor tools, not bulk shell rewrites.
- **PowerShell double-quoted here-strings interpolate `$F4`** — it silently
  becomes empty. Use `@'…'@` when a string contains `$`.
- **A blanket find-replace hit the helper it was defining**, creating infinite
  recursion. Check the function's own body after a `replace_all`.
- **Qt eats a single `&`** in a title as an accelerator — use `&&`.
- **`hash()` on a string is randomised per process** — it was the manifest cache
  key, so the cache never hit across runs. Now SHA-1.

## Proven vs not — read this before trusting anything

`docs/UNVERIFIED.md` is the register. Verified on **22.0.368 against the real
shot** this session: the live-volume scan and export guard (0 bytes written,
disk free unchanged), the productName overlay, the Sublayer strength question,
the hython relink (4 unresolved → 0), and Karma setting overrides (113 knobs;
str/bool/int/float each round-tripped with its type intact, a bogus knob
refused).

**Never exercised end to end this session — no frame was ever rendered.** See
`docs/AUDIT_BRIEF.md` for the full list; the short version is that the GUI has
never been clicked in a live session, `render_direct`'s new per-frame loop has
never actually rendered a frame, the husk `PrepareWorker` chain has never run,
and no farm file has been submitted to a real scheduler.

## Open work

- **Fixed after handoff:** hython relink/settings overlays are PID-scoped
  (`relink_direct_<pid>.usda`, `settings_direct_<pid>.usda`). Parallel chunks
  run in separate hython processes, so `--parallel > 1` no longer makes them
  overwrite each other's live overlay.
- **TASKS.md T5** (medium) — `inspect()` cooks at one frame, so a stage whose
  structure changes over time is described from a single moment.
- **Per-frame export timing** was never captured (the disk filled). Safe to
  re-attempt now that the volume guard exists.
- **`resolutionx`/`resolutiony`** on `usdrender_rop` are still unconfirmed —
  `_apply_override` now warns loudly instead of silently ignoring them.
- **`docs/UNVERIFIED.md` A3, E8/E9, F2/F3** — versioned type-name split,
  `--complexity`/`--purpose` values, Karma licence and the Indie cap.
- **Non-ASCII on stdout.** `hsl inspect` prints `✗ ⚠ →` to stdout; on Windows
  that can raise `UnicodeEncodeError` when redirected to a file (stderr is safe,
  Python uses `backslashreplace` there). Pre-existing and scattered — wants one
  deliberate encoding pass.

## How to run / verify

```bash
python -m unittest discover -s tests          # 177 tests, no Houdini needed
python scripts/check_drift.py                 # config pointers still valid
python scripts/check_ui_imports.py            # ui.py imports + Qt names resolve
python scripts/verify_environment.py --report # probe a real Houdini install

python -m hsl.cli hython                      # list Houdini installs
python -m hsl.cli inspect <hip>               # ROPs, res, camera, AOVs, missing
                                              # textures, live volumes, output files
python -m hsl.cli render <hip> --frames 1 --dry-run
python -m hsl.cli render <hip> --frames 1-30 --relink-from <texdir>
python -m hsl.cli render <hip> --set karma:global:samplesperpixel=64
python -m hsl.cli render <hip> --engine husk --aovs beauty,depth --output /r/out/
python -m hsl.cli ui <hip>
```

Manual hython probe (PowerShell — **not** git-bash):

```powershell
$env:HFS = "C:\Program Files\Side Effects Software\Houdini 22.0.368"
$env:PYTHONPATH = "F:\Nexus Projects\SolarisCL"
& "$env:HFS\bin\hython.exe" probe.py <args>
```

Headless GUI check (catches what `check_ui_imports.py` cannot):

```powershell
$env:QT_QPA_PLATFORM = "offscreen"; $env:PYTHONPATH = "F:\Nexus Projects\SolarisCL"
python your_ui_probe.py
```
