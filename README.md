# hsl — Solaris Render Launcher

Load a `.hip`, read what the LOP network is actually set up to render, and fire
`husk` at it. GUI, CLI, or importable library.

---

## Why it's split in two

`hou` only exists inside `hython`. `husk` is a separate binary. A GUI that
imports `hou` inherits hython's startup cost, its license checkout, and its
event loop — all for what amounts to a single read of the scene.

So there are two halves that never meet:

```
┌─ hython ──────────────┐         ┌─ plain Python ──────────────┐
│  hsl.inspector        │  JSON   │  hsl.bridge                 │
│  · loads the .hip     │ ──────▶ │  hsl.husk    → argv          │
│  · cooks the LOPs     │manifest │  hsl.runner  → husk procs    │
│  · walks the USD stage│         │  hsl.ui / hsl.cli            │
│  · writes USD to disk │         └─────────────────────────────┘
└───────────────────────┘
```

`hsl/manifest.py` is the contract between them — pure dataclasses, standard
library only. Everything downstream of the manifest is testable without
Houdini installed, which is why `tests/test_core.py` covers it.

Practical payoffs: the UI stays responsive during a two-minute hip load,
manifests can be cached and diffed, and the inspector is reusable as-is for
farm submission.

---

## Install

> Handing hsl to someone who just wants to *run* it? Point them at
> [`INSTALL.md`](INSTALL.md) and a zip from `make release` — this section
> assumes you are working on the source.

```bash
export HFS=/opt/hfs20.5          # or: cd /opt/hfs20.5 && source houdini_setup
pip install PySide6              # GUI only; the CLI has no dependencies
```

`hython` and `husk` are found via `$HFS/bin`, then `PATH`. Override either with
`$HSL_HYTHON` / `$HSL_HUSK`. To see every Houdini install found on the machine
and pick one:

```bash
python -m hsl.cli hython                 # list them (* = the one in use)
python -m hsl.cli hython --set 2         # remember install #2 as the default
```

The GUI has the same list as a dropdown, with a **Rescan** button.

## Two render engines

| | `hython` (**default**) | `husk` |
|---|---|---|
| How | renders the ROP directly in hython | exports USD, then runs husk on it |
| USD on disk | none | one file per ROP |
| Good for | everything single-machine, and **essential for volume-heavy shots** | farm submission, `--aovs`, `--relink-from` |

hython is the default because the USD export is pure overhead for a local
render — and on a scene with live (SOP-imported) volumes it *bakes* them into
the export at tens of GB per frame. The tool refuses that export unless you
pass `--allow-volume-bake`. Switch engines with `--engine husk`.

## Use

```bash
# What is this scene set up to render?
python -m hsl.cli inspect /jobs/shot/shot_v012.hip

# Show the commands without running anything
python -m hsl.cli render shot.hip --frames 1001-1100 --chunk 10 --dry-run

# Render directly in hython (default engine), four processes at a time
python -m hsl.cli render shot.hip \
    --rop /stage/usdrender_rop1 \
    --frames 1001-1100 --chunk 10 --parallel 4

# Export USD and drive husk instead — needed for AOV filtering and relinking
python -m hsl.cli render shot.hip --engine husk \
    --frames 1001-1100 --aovs beauty,depth --relink-from /jobs/shot/tex

# Quarter-res check render of a single frame
python -m hsl.cli render shot.hip --frames 1050 --res 960 540 \
    --renderer BRAY_HdKarmaXPU

# Several ROPs in one run — queued in the order given, and strictly one
# after another at --parallel 1 (the default)
python -m hsl.cli render shot.hip \
    --rop /stage/usdrender_rop1 --rop /stage/usdrender_rop2
python -m hsl.cli render shot.hip --all-rops

# Several scenes back to back. Every scene is read before anything renders,
# so a typo in the third .hip surfaces before the first spends hours.
python -m hsl.cli batch shotA.hip shotB.hip shotC.hip --frames 1001-1100

# Same batch, but render only one chosen ROP per scene: a bare path applies
# everywhere, scene:/path picks a different ROP per scene
python -m hsl.cli batch shotA.hip shotB.hip shotC.hip \
    --rop shotB.hip:/stage/usdrender_fx --rop /stage/usdrender_beauty

# GUI
python -m hsl.cli ui shot.hip
```

## Memory: measured, not predicted

Every render records what it actually used — peak RAM per process, and GPU
memory where `nvidia-smi` can report it. Nothing is modelled: a figure that
was not measured reads `unknown`, never a guess.

```bash
python -m hsl.cli memory                 # what past renders used, newest first
python -m hsl.cli memory shot.hip        # just this scene
python -m hsl.cli memory --machine       # what this machine can measure at all
python -m hsl.cli memory --forget        # drop the history
```

Preflight then uses that history: once a scene has been rendered, starting it
again on a machine with less RAM than it needed last time is an **error**, and
coming within 85% of the ceiling is a warning. A scene that has never been
measured says nothing at all — silence beats a check that always chatters.

Why measurement rather than an estimator: memory depends on the delegate
(Karma CPU and XPU differ enormously), the Houdini version, texture cache
budgets that deliberately do not scale with texture count, and BVH constants
nobody publishes. A predicted number would rest on invented constants and be
believed anyway. A recorded one is a fact.

RAM is measured across the **whole process tree** on Windows, via a Job Object
opened at spawn time. That distinction is not academic: Windows' per-process
peak counter ignores children, so a studio `husk.bat` wrapper around a render
that really used 300 MB measured **8 MB** — a plausible, quietly wrong number.
Every record says which kind of figure it is, and `hsl memory --machine` says
what this machine can do.

Known limits, deliberately not papered over:

- **GPU memory is NVIDIA-only, polled, and often unavailable.** Consumer
  GeForce cards under the WDDM driver report no per-process VRAM at all — the
  driver answers `[N/A]`, so hsl records `unknown` rather than `0`. Where it
  does work, figures are sampled and labelled `sampled`, since a spike between
  samples is missed. Matched by process id, so a delegate handing GPU work to
  a child is missed.
- **Whole-tree RAM is Windows-only.** On Linux a wrapper script is still
  measured as the wrapper — now *labelled* as main-process-only rather than
  passed off as the total, but not yet fixed (`docs/UNVERIFIED.md` M18).
- The job figure is **committed** memory, not working set: a genuinely
  different quantity, and not to be compared like-for-like with a
  single-process number.
- A **cancelled** render is not recorded — we cut it short, so its peak says
  nothing about what the scene needs. A render that **failed** is recorded: a
  job killed by the machine running out of memory is the most useful sample
  there is.

## Caches and simulations

`hsl` is not only a render launcher: it cooks the rest of a `.hip` headless
too — File Cache SOPs, Geometry/Alembic/DOP ROPs and the other `/out`
contexts. `hsl inspect` lists them; `hsl cook` runs them.

```bash
python -m hsl.cli inspect shot.hip                 # lists cookable tasks too
python -m hsl.cli cook shot.hip                    # every cache and sim
python -m hsl.cli cook shot.hip --task /obj/geo1/filecache1
python -m hsl.cli cook shot.hip --kind sim --dry-run
```

Dependencies are read from the scene — a ROP wired downstream of another, or a
`fetch` node pointing at one — so tasks run in the right order without being
sequenced by hand. A task whose dependency fails is reported as **skipped**
rather than run against a missing input.

**Simulations are never chunked.** Frame N of a sim depends on N-1, so
splitting one across processes would give each a cold start at its boundary and
write a silently wrong cache. `--chunk` is ignored for anything sequential —
which includes File Cache SOPs, whose `cachesim` parameter defaults to on — and
`hsl cook` says so rather than letting the flag look respected.

This runs entirely under `hython`; husk is not involved and cannot be, since it
only consumes USD.

As a library:

```python
from hsl import bridge, husk

manifest = bridge.inspect_hip("shot.hip", export_usd=True)
rop = manifest.rops[0]
settings = manifest.resolve_settings(rop)

print(settings.resolution, settings.camera)
print([v.label for v in manifest.aovs_for(settings)])

jobs = husk.jobs_for_rop(manifest, rop, rop.usd_path, chunk_size=10)
for job in jobs:
    print(husk.format_command(husk.build_command(job)))
```

---

## What it reads

Node parameters are treated as **hints**. The authoritative source is the
composed USD stage, because in Solaris the render settings can be authored
anywhere in the layer stack — the ROP parameter is just a pointer, and is often
empty.

From the stage: every `UsdRenderSettings` (resolution, camera, pixel aspect,
conform policy, included purposes), every `UsdRenderProduct` (output paths),
every `UsdRenderVar` (your AOVs, with data and source types), every
`UsdGeomCamera`, and any namespaced delegate knobs — `karma:*`, `ri:*`,
`arnold:*` — authored on the settings prim.

From the ROP: renderer, settings prim, camera, frame range.

Settings resolution order: ROP parameter → stage `renderSettingsPrimPath`
metadata → the only settings prim if there's exactly one.

### Version tolerance

`usdrender_rop` parameter names have drifted across 19.0 → 20.5. Every read
goes through `_parm(node, candidate_names, default)`, which tries a list and
falls back quietly rather than raising `AttributeError` on a scene from a
different build. Anything it couldn't find lands in `RenderRop.raw_parms` and
in `manifest.warnings` so you can see what was missed rather than getting a
silently wrong command.

---

## Things worth knowing

**USD export uses a temporary `usd_rop`, not `Usd.Stage.Export()`.** This is
deliberate. `Export()` serialises the stage as cooked at a single time — any
animation, motion blur samples, or per-frame value clips are silently dropped,
and you get a still frame repeated across the range with no error to tell you
why. The temporary ROP re-cooks per frame. It's wired to the render ROP's
input, cooked, and destroyed; the `.hip` is never saved.

**Chunking is measured in frames rendered, not frame numbers.** With an
increment of 2 over 1–100 and a chunk size of 10, each chunk renders 10 frames
and spans 20 frame numbers. `husk` takes `--frame-count`, not an end frame,
which is the usual source of off-by-one errors here.

**`--parallel` is for one machine.** Several husk processes on one box contend
for RAM and cores; each also checks out its own Karma license. Two or three is
usually the sweet spot on a workstation. For real distribution, use `--dry-run`
and feed the commands to your scheduler.

**Verify the husk flags against your build.** `build_command()` emits
`--renderer --frame --frame-count --frame-inc --settings --camera --output
--res --threads --complexity --purpose --snapshot --make-output-path
--fast-exit --verbose`. These are stable across recent Houdini, but run
`husk --help` on your version before trusting the ones you don't already use.
`--verbose` takes a single token: a level plus optional flag letters, where `a`
selects Alfred-style `ALF_PROGRESS n%` output — which is what `parse_progress()`
reads to drive the progress bars. Turning Alfred off leaves the bars at zero.

**Licensing.** `husk` pulls a Karma render license rather than a full Houdini
license, so it parallelises without eating interactive seats. The inspector
does need a real hython license for as long as it takes to load and cook.
On Indie, husk is capped at 1920×1080.

---

## Verified vs. not

The manifest, chunking, command construction, progress parsing and the process
queue are covered by 186 tests, including a fake husk that exercises the full
run/cancel/failure path:

```bash
python -m unittest discover -s tests -v
```

`hsl/inspector.py` needs `hou` and `pxr`. It has now been probed against
**Houdini 21.0.729 and 22.0.368** with `scripts/verify_environment.py` — every
node type, ROP/USD-ROP parameter and USD schema call it relies on is confirmed
on both (see `docs/UNVERIFIED.md`). That probe fixed two silent version-drift
bugs (`override_camera`, `flattenalllayers`). What it does **not** yet cover is a
real scene's AOVs (whether Solaris types `UsdRender.Var` prims — item D2), so
still start with `inspect` on a scene you know well and check the reported
resolution, camera and AOVs against what Houdini shows you. `hsl/ui.py` was
import-checked against stubbed Qt, not exercised live.

---

## Extending

- **Farm submission** — `husk.jobs_for_rop()` already gives you one job per
  chunk. Swap `RenderQueue` for a Deadline/Tractor submitter and emit
  `build_command(job)` per task.
- **In-session variant** — for a shelf tool inside Houdini, skip `bridge`
  entirely and call `inspector.walk_stage(hou.node('/stage/…').stage())`
  directly. The manifest and everything downstream are unchanged.
- **Layout / IFD-style preflight** — `manifest.warnings` is the natural place
  to hang missing-texture and unresolved-reference checks before submitting.
