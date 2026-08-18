# Session handoff — hsl / SolarisCL

Read `AGENTS.md` (canonical brief) and `docs/UNVERIFIED.md` first. This file is
the *session* state: what is done, what is proven, the traps, and what is left.
Rewritten 2026-07-29.

## TL;DR

`hsl` loads a Houdini `.hip` under **hython** and runs what it finds — Solaris
renders **and**, as of this session, caches and simulations. GUI + CLI +
library. The plain-Python half stays testable with no Houdini installed.

- **Repo:** github.com/PushMotta/SolarisCL (private, `main`).
  Do not trust this file for push state — run
  `git log --oneline origin/main..main`. At rewrite time the UI mode
  restructure and this rewrite itself had not been pushed.
- **Green:** 346 tests, boundary lint, 55 drift checks, ui-imports,
  whitespace. `python -m unittest discover -s tests` needs no Houdini.
- **Project root:** `F:\Nexus Projects\SolarisCL` — the working dir **is** the
  git root. `_archive\` is the original delivery, gitignored and disposable.
- **Note:** commits landed from outside the agent session twice (`6db1da0`,
  `567aa78`, then a push). **Run `git log` before assuming your work is
  uncommitted** — a planned three-way commit split had to be abandoned because
  the work had already been swept into one commit.

## Environment (this machine — critical)

- Windows 11. `$HFS` is **unset**; hython/husk are **not on PATH**. The tool
  auto-discovers installs.
- **Two Houdini installs, both usable for probes:**
  `C:\Program Files\Side Effects Software\Houdini 22.0.368\bin\hython.exe`
  and `…\Houdini 21.0.729\bin\hython.exe`. H21 has an Octane plugin printing
  `[Octane]` banners to stdout — probes should write JSON to a **file**, not
  parse stdout.
- **C: is ~94% full.** Never export a volume-heavy stage to it. The volume
  guard prevents this by default.
- **Real test scene:** `V:\Pushvfx Dropbox\…\SHOT_Train_Aerial_DUDA_v05.hiplc`
  — 3 Solaris ROPs, **opens on frame 1074**, `usdrender_rop1` range 1046–1075,
  cold cook ~95 s (one thumbnail ROP cooks ~250 s). The previous pointer,
  `SHOT_SandBurst_ROCHA_v14_Motta.hiplc`, **no longer exists on V:**, and its
  successor (`…SandBurst_ROCHA_v13`) has no Solaris render ROPs at all. `V:` is
  Dropbox — **never write outputs there.**
- Throwaway probe scenes built this session live in the session scratchpad and
  are gone; `scripts/` has the reusable probes.

## The shape of the thing

`hou`/`pxr` only in `inspector.py`; Qt only in `ui.py`; stdlib only in
`manifest.py`. A `PreToolUse` hook enforces it. **Never widen it** — that
boundary is what makes the test suite possible.

Two engines, `husk.DEFAULT_ENGINE = "hython"`. hython renders the ROP directly
(no USD on disk, and **essential** for volume-heavy shots where an export bakes
~tens of GB/frame). husk exports USD first and is needed for farm submission,
`--aovs` and `--relink-from`. Anything husk cannot do is done as a **USD
overlay** — used five times now and proven each time.

### New this session: it cooks, not just renders

`manifest.OutputTask` is the scheduling unit — any node that can be cooked to
produce files. `hsl cook` runs caches, sims and non-Solaris ROPs; `hsl inspect`
lists them; the GUI has a **Caches & Sims** mode.

- **Dependencies come from the scene** (`inputs()`, seeing through `merge`, and
  following a `fetch` node's `source` parm). `RenderQueue` schedules by
  readiness, rejects cycles up front, and marks dependants **SKIPPED** when a
  prerequisite fails.
- **Simulations are never chunked.** Frame N depends on N−1, so splitting one
  across processes gives each a cold start and writes a silently wrong cache.
  `OutputTask.__post_init__` forces `sequential` for sim kinds — it is
  *unrepresentable*, not merely discouraged. File Cache SOPs count too
  (`cachesim` defaults to 1).
- Farm export encodes the DAG for Tractor (`-id` / `Instance` /
  `-serialsubtasks 1`); **Deadline refuses** dependent work rather than writing
  a job that races.

### Newer still: frame-aware inspection, cook parity, UI progress

- **T5 is done** (`docs/TASKS.md`). `hsl inspect --frame` /
  `inspect_hip(frame=)` describe the stage at a chosen moment;
  `manifest.inspected_frame` always records which moment (real shots open on
  arbitrary frames — 1074, not 1). A warning fires when the RenderSettings
  prim set differs across the range. **`stageAtFrame()` does not exist** — the
  verified mechanism is `LopNode.stage(frame=)` (which beats the global frame)
  plus `hou.setFrame()` for parameter evaluation. See UNVERIFIED D9–D12.
- **`hsl cook` reached parity with `render`:** cached scene reads
  (`--no-cache` to bypass — always safe for cook, which never exports USD) and
  a cook preflight (`run_cook_preflight_checks`: per-task checks,
  `expected_outputs` fallback, missing assets deliberately downgraded to
  warnings; blocks with exit 5 unless `--skip-preflight`).
- **The GUI submits cooks to the farm** through the same "Submit to Farm…"
  button, mode-aware, via `jobs_for_task` (never hand-built). Deadline's
  refusal of dependent work surfaces as a message box pointing at Tractor.
- **Progress displays are honest and testable:** frame counter + observed-rate
  ETA (never invented; omitted until a chunk finishes), intra-frame percent
  for single-frame chunks, `running…` when no data has arrived. The pure logic
  lives in **`hsl/progress.py`** (stdlib-only, unit-tested); `ui.py` keeps
  thin Qt wrappers. The empty-state blank detail box is gone.

### Memory measurement (layers 1 and 3) — done

Deliberately **measurement, never prediction**. A predicted number would rest
on delegate- and version-specific constants nobody publishes, and would be
believed anyway; a recorded one is a fact. New `hsl/sysinfo.py` (platform
primitives, stdlib+ctypes), `hsl/memlog.py` (capped JSON history in the user
profile), a `memory` category in preflight, `Task.peak_rss`/`peak_vram` filled
by a sampler thread in `RenderQueue`, and `hsl memory [--machine|--forget]`.

Design rules worth keeping: an unmeasured value is `None` and prints
`unknown`, never `0`; a sample carrying no measurement at all is not recorded;
a **cancelled** task is not measured (we cut it short) while a **failed** one
is (an out-of-memory kill is the most useful sample there is); and the
measurement imports are function-local in `runner.py` so instrumentation can
never stop the queue from loading — its *absence* is reported by
`hsl memory --machine`, not as a warning on every render.

**Whole-tree measurement (2026-08-18).** `PeakWorkingSetSize` covers only the
process hsl spawned, so a studio `husk.bat` wrapper measured 8 MB for a render
that used 300 MB. Each render is now spawned into a Windows **Job Object**
(opened *before* the `Popen` — it cannot be retrofitted) and `peak_rss` prefers
`PeakJobMemoryUsed`, falling back to the old figure and recording which it got
in `Task.peak_rss_is_tree` / `MemorySample.peak_rss_is_tree`. `hsl memory` and
preflight both label the difference rather than presenting them alike. Windows
only; see `docs/UNVERIFIED.md` M11–M18.

### Newest: the Batch tab, and `batch --rop`

- **The GUI has a third mode, Batch** — add several `.hip` files, each reads
  in the background (one hython at a time, cache-reused), every render ROP
  starts ticked, untick to pick per scene. One primary action, "Render
  Batch", disabled with the reason shown until *every* listed scene is
  readable — the GUI equivalent of `cmd_batch`'s fail-fast. Deliberately
  plain like the CLI: engine/frames/chunk/parallel apply to every scene; no
  per-ROP overrides in this mode. Farm export judges the whole list first —
  the first version would have silently dropped an unreadable scene from a
  farm export, caught and fixed before shipping.
- **`hsl batch --rop SPEC`** (repeatable): bare `/stage/rop` applies to every
  scene, `shotB.hip:/stage/fx` picks per scene (split at last colon,
  extension-insensitive scene match). A SPEC matching nothing anywhere, or a
  scene left with no ROPs, is exit 2 — a typo must never silently change
  what renders. No flags = byte-identical to before.

### Newer: several ROPs in a row, several scenes in a row

- **`hsl render` takes repeatable `--rop` / `--all-rops`.** ROPs queue in the
  order given; the queue starts chunks in submission order, so `--parallel 1`
  (the default) is strictly one ROP after another, and a failed ROP does not
  stop the ones behind it. `--output` with several ROPs is rejected (one path,
  N writers). Multi-ROP jobs carry `task_id=rop path`; single-ROP renders stay
  untagged and byte-identical to before.
- **`hsl batch a.hip b.hip …`** renders whole scenes back to back. Every scene
  is inspected before anything renders (fail fast), task ids are
  `hipname:/rop/path`, and it is deliberately plainer than `render` — no
  per-render editing flags.
- **The GUI render mode grew a ticked ROP table** (hidden for single-ROP
  scenes — zero change to the common case). The override panels edit the
  *current* ROP only; other ticked ROPs render with their own scene settings,
  and the output override is disabled (with the reason shown) when several
  are ticked.
- **Two export defects found and fixed on the way (UNVERIFIED C7/C8):**
  colliding export filenames (`/stage/a_b/rop` vs `/stage/a/b_rop` → same
  file, second wins, silently) now get a stable SHA-1 suffix plus a warning;
  and `f1`/`f2` on the USD ROP are `$FSTART`/`$FEND` *expressions*, so every
  husk export silently covered the **whole playbar** until now —
  `_force_parm()` clears the expression and reads the value back. K4's trap,
  third appearance.

## Proven vs not — read before trusting anything

`docs/UNVERIFIED.md` is the register. Newly **verified on 21.0.729 + 22.0.368**
this session (K1–K10): non-Solaris ROP type names and their output parms, that
`filecache::2.0` has no `.render()` but wraps a `rop_geometry` child that does,
dependency reading through merge and fetch, `--output` repointing a File Cache
SOP, and `initsim` flipping the sequential classification.

**End-to-end cook is proven**: a built scene produced **6 real `.bgeo.sc`
files** through the real queue, with per-frame progress, and cancelling
mid-cook left no orphan hython.

**Still not proven — do not claim otherwise:**

- **K8 — no real solver has ever been run.** A DOP ROP is classified `sim` and
  cooked in one ordered call, which is the right *shape*, but nobody has diffed
  an hsl-cooked pyro/RBD cache against an in-session one. **The user is doing
  this with their own `.hip`.** Quickest way to see the guard earn its keep:
  force `sequential=False` and confirm the chunked result differs.
- **K10 — the Tractor `.alf` dialect.** The structure is tested; no Tractor
  exists here to accept it.
- **Real renders confirmed 2026-07-31.** The user tested the tool and frames
  rendered successfully. Engine, scene and interface were not recorded, so the
  finer-grained unknowns below (ALF_PROGRESS cadence in practice, Karma
  licence, the Indie cap) each stay open on their own terms — "it works" is
  not evidence for any specific one of them. Whether the GUI has been driven
  live rather than offscreen is likewise unrecorded.
- **D12** — the `TypeError` fallback in `_stage_at()` (for a hython whose
  `stage()` lacks the `frame=` keyword) has never triggered; both installs
  here have the keyword.
- **No real Deadline or Tractor has accepted** the exported job files (K10),
  and no real husk run has shown how often `ALF_PROGRESS` actually arrives
  within a frame — the intra-frame display consumes whatever cadence exists.
- A3, E8/E9, F2/F3 — versioned type-name split, `--complexity`/`--purpose`
  values, Karma licence and the Indie cap.

## Traps that cost time (they will bite again)

- **Never invent a husk flag or Houdini parm.** Probe it, then record it.
- **A File Cache SOP's `f1`/`f2` hold `$FSTART`/`$FEND` *expressions*.**
  `parm.set()` does not beat an expression — you need
  `deleteAllKeyframes()` first. A probe asking for 2 frames silently got 10.
  This is why hsl drives the inner ROP with an explicit `frame_range` instead
  of pressing the SOP's `execute` button.
- **Qt eats a single `&`** as a mnemonic — in group titles *and tab labels*.
  Use `&&`. This shipped as a visible bug ("Queue _Logs").
- **cp1252 is still the default Windows console code page.** Printing `→`
  raises `UnicodeEncodeError` *part way through* a report. There is now a test
  (`TestConsoleEncoding`) scanning every console-printing module — keep CLI
  output ASCII.
- **git-bash rewrites args starting with `/`** (node paths become
  `C:\Program Files\Git\out\...`). Use `MSYS_NO_PATHCONV=1`.
- **Offscreen Qt renders text as boxes** unless you set
  `QT_QPA_FONTDIR=C:/Windows/Fonts`. Screenshots are otherwise useless.
- **Do not rebuild a render job by hand.** `jobs_for_task` must delegate to
  `jobs_for_rop` for Solaris ROPs; duplicating it lost the renderer, camera and
  settings prim, and rendered with bare Karma defaults exiting 0.
- **Routing is `RenderJob.cook`, not `task_kind`.** A Mantra/OpenGL ROP *is* a
  render but `--render-direct` cannot drive it (that path resolves nodes via
  `find_render_rops()`, which only knows USD types).
- **PowerShell read-modify-write corrupts this file set** (mangles `—`/`…`).
  Use the editor tools.
- **`hash()` on a string is randomised per process** — was the manifest cache
  key; now SHA-1.
- **Houdini authors volume fields as time samples with no default value.**
  `attr.Get()` at the default time code returns `None` for `filePath`,
  `fieldName` *and* `fieldDataType` on a real Solaris volume — read at
  `inspected_frame` instead. This is not a missing number, it is a **wrong
  answer**: the footprint scan read 481,683,137 active voxels at frame 1074
  and `None` at the default, so it called a volume-dominated shot
  "heaviest: textures on disk". It also invalidates the older D-VOL note
  claiming `fieldName` is authored empty — it is not; it was read at the
  wrong moment. Same trap for time-sampled point positions (a fixture reads
  110 points instead of 160).
- **Windows `PeakWorkingSetSize` covers the process you spawned and *nothing
  it spawns*.** A `.bat` wrapper around a child that allocated 300 MB measured
  **8.1 MB**. Direct spawns are fine (`hython.exe` allocating 400 MB measured
  931.6 MB against a 530.6 MB idle baseline — a 401.0 MB delta). This is why
  memory measurement uses a Job Object, which must be created **at spawn
  time** and cannot be retrofitted onto a live `Popen`. The failure mode is
  the dangerous kind: a plausible small number, not an error.
- **A test that passes against the broken code proves nothing.** The
  dependency-ordering regression test only exposes its bug at
  `max_parallel >= 2`; at 1 the queue's own semaphore serialises the tasks and
  hides it. Always re-run a new regression test against the unfixed code.
- **QMessageBox text is the *inverse* of the `&` trap.** Tab labels and group
  titles eat a single `&` (write `&&`), but message-box body text has no
  mnemonic handling — `&&` there *displays* doubled. One escape per widget
  kind, checked by looking, not by habit.
- **`Task.duration` treats `started_at == 0.0` as "never started"** and
  returns 0 regardless of `finished_at`. A test fixture using 0.0 as a start
  time silently kills every rate/ETA computation built on it.
- **Do not trust a task description's API names** — TASKS.md T5 said
  `stageAtFrame()`; no such method exists on either install. Probe first even
  when the doc sounds specific.
- **Uncommitted work is fragile while agents run.** A subagent ran
  `git checkout -- hsl/cli.py` mid-session and discarded the working diff of
  that file; it was reconstructed, verified hunk-for-hunk, and the suite
  re-proven. Commit landed work before fanning out agents, and diff-check
  after any agent that touches git.

## Open work

- **K8 / K10** as above — K8 (diff a real solver cache) is the user's, with
  their own `.hip`.
- **`ui.py` has no layout tests.** Layout is still verified by rendering
  offscreen and looking. The progress/ETA *logic* is now unit-tested in
  `hsl/progress.py`, so only visual judgement remains uncovered.
- **The hython engine emits no per-frame progress** (`ALF_PROGRESS` is husk's).
  The UI honestly shows `running…`; parsing hython ROP output for progress is
  possible future work, not started.
- **`resolutionx`/`resolutiony`** on `usdrender_rop` still unconfirmed;
  `_apply_override` warns loudly rather than silently ignoring them.
- Concurrent-render check for the PID-scoped overlays was never run with real
  parallel Houdini.
- **`RenderJob.label` has no ROP path**, so two ROPs with identical frame
  ranges get identical Tractor task titles in an exported `.alf`
  (`hsl/farm.py`, pre-existing, surfaced by GUI multi-ROP and now reachable
  from the Batch tab's farm export too).
- **The queue table does not name the scene per row** — with a batch running,
  scene identity lives in the log prefixes, not the Frames column. Weakest
  part of the batch display; fine for one scene, worth a column for many.
- **`_batch_plan()`'s pure part is unit-testable but untested** — scene
  validation + job assembly in `ui.py` (import-check-only by policy). Same
  extraction pattern as `hsl/progress.py` if it starts growing logic.
- The shared status line keeps the batch message after switching tabs until
  something else writes it; deliberate, but look at it in live use.
- **No manifest field records which frames a `usd_path` covers.** Mattered
  little while exports always covered the playbar; post-C8 an export is
  genuinely narrow, so a consumer that assumes otherwise would ask husk for
  frames the USD does not carry. `render`/`batch` forward `--frames` into the
  export, so the CLI is self-consistent — the gap is for *other* consumers.
- The `_force_parm()` read-back-failed branch has never fired (both installs
  clear expressions) — same shape as D12.

## How to run / verify

```bash
python -m unittest discover -s tests           # 346 tests, no Houdini needed
python .claude/hooks/boundary_guard.py --check-tree .
python scripts/check_drift.py
python scripts/check_ui_imports.py
python scripts/verify_environment.py --report  # probe a real Houdini install

python -m hsl.cli hython                       # list Houdini installs
python -m hsl.cli inspect <hip>                # ROPs + cookable tasks
python -m hsl.cli render <hip> --frames 1 --dry-run
python -m hsl.cli cook <hip> --dry-run         # caches + sims, in dep order
python -m hsl.cli cook <hip> --task /obj/geo1/filecache1
python -m hsl.cli ui <hip>

python scripts/make_icon.py                    # redraw hsl/assets/hsl.ico
python scripts/make_release.py                 # dist/hsl-<version>.zip (gitignored)
python scripts/make_release.py --bundle-python # + -win64-full.zip: standalone,
                                               # embeddable CPython 3.11.9 +
                                               # PySide6 seeded in, smoke-tested
                                               # with its own interpreter
```

Manual hython probe — write JSON to a **file**, never parse stdout:

```bash
MSYS_NO_PATHCONV=1 "C:/Program Files/Side Effects Software/Houdini 22.0.368/bin/hython.exe" \
    probe.py out.json
```

Headless GUI screenshot (the only way to check layout):

```bash
QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR="C:/Windows/Fonts" python -c "
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from hsl import ui
app = QApplication([]); app.setFont(QFont('Segoe UI', 9))
w = ui.LauncherWindow(); w.resize(1400, 900); w.show(); app.processEvents()
w.grab().save('shot.png')"
```

## Where to start reading

`hsl/manifest.py` (the contract — `OutputTask` is the new half), then
`docs/ARCHITECTURE.md`, then `docs/UNVERIFIED.md` before touching anything
Houdini-facing. `docs/TASKS.md` holds the backlog.
