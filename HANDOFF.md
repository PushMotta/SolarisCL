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
- **Green:** 227 tests, boundary lint, 55 drift checks, ui-imports,
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
- **No Solaris frame has ever been rendered** by this tool, and the GUI has
  never been clicked in a live session (it is driven offscreen).
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

## How to run / verify

```bash
python -m unittest discover -s tests           # 227 tests, no Houdini needed
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
