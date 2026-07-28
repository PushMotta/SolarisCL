# Audit brief — find what is missing or broken in `hsl`

You are auditing a Solaris/Karma render launcher. Your job is **not** to add
features. It is to find (a) functionality a render launcher should have and this
one does not, and (b) bugs — especially the ones that make a render silently
*wrong* rather than loudly failing.

Read `AGENTS.md` and `HANDOFF.md` first, then `docs/UNVERIFIED.md`.

---

## Ground rules

1. **Never widen the boundary.** `hou`/`pxr` may be imported only in
   `hsl/inspector.py`; Qt only in `hsl/ui.py`; `hsl/manifest.py` is stdlib-only.
   A `PreToolUse` hook enforces this. If it blocks you, the design answer is to
   put the code in `inspector.py` and pass the result through the manifest — not
   to add a `try: import hou` guard.
2. **Never assert a Houdini parm or husk flag from memory.** They drift between
   versions and the failure mode is a silently incorrect render, not an
   exception. Probe a live install or read `husk --help`, then record the result
   in `docs/UNVERIFIED.md`.
3. **Report, don't "fix" quietly.** A finding is worth more with a reproduction
   than a patch without one.
4. **Do not fill C:.** It has ~57 GB free. Never run a USD export of the test
   shot with `--allow-volume-bake` — that is the ~44 GB/frame bake the guard
   exists to prevent. Never write outputs to `V:` (the user's Dropbox).

## What is already known-unverified

Do not spend the audit re-discovering these; they are logged:

- `resolutionx` / `resolutiony` on `usdrender_rop` — probably a no-op, warns now.
- `docs/UNVERIFIED.md` **A3** (versioned `::` type-name split), **E8/E9**
  (`--complexity` / `--purpose` accepted values), **F2/F3** (Karma licence,
  Indie resolution cap).
- `docs/TASKS.md` **T5** — `inspect()` cooks at a single frame.
- Non-ASCII (`✗ ⚠ →`) printed to **stdout** can raise `UnicodeEncodeError` on
  Windows when redirected to a file.

---

## Coverage map — where the bodies are likely buried

| Module | Lines | Automated coverage | Risk |
|---|---|---|---|
| `manifest.py` | 216 | good | low |
| `husk.py` | 415 | good | low |
| `farm.py` | 99 | good (7 tests) | medium — never submitted to a real scheduler |
| `runner.py` | 246 | good (fake husk) | medium — cancel/kill paths barely exercised |
| `preflight.py` | 127 | good | low |
| `presets.py` | 95 | **2 tests** | medium — barely checked |
| `cli.py` | 481 | partial | medium |
| `bridge.py` | 362 | partial — subprocess paths mostly unexercised | **high** |
| `inspector.py` | 1201 | 3 mocked overlay-path tests; no live Houdini coverage | **high** |
| `ui.py` | 1255 | **import-check only** | **high** |

`tests/test_core.py` has 177 tests in 32 classes and must stay green without
Houdini installed.

### The single biggest gap

**No frame has ever actually been rendered by the current code.** Everything
below was verified as *composition* or *argv*, never as a completed render:

- `render_direct`'s per-frame loop (rewritten — renders one frame per
  `render()` call now). Never run.
- The husk `PrepareWorker` chain in the GUI (settings overlay → AOV filter →
  queue). Never run.
- Any farm job file against a real Deadline or Tractor.
- The GUI has **never been clicked in a live session** — only driven headless
  with `QT_QPA_PLATFORM=offscreen`.

An end-to-end render of one frame, on both engines, is probably the highest-value
thing you can do.

---

## Specific things to go after

Each of these is a real question, not a hint that something is wrong.

### Correctness / silent wrongness

1. **Frame-by-frame rendering changed semantics.** `render_direct` used to call
   `render(frame_range=(f1, f2, inc))` once; it now calls `render()` per frame.
   Does that change motion blur, velocity blur, or anything that needs
   neighbouring frames? Does it re-cook the whole stage each frame (and how much
   slower is it)? Test on the real shot.
2. **Chunking + the hython engine.** `jobs_for_rop` chunks frames and the runner
   may run chunks in parallel. Two hython processes each load the whole `.hip`.
   Is that safe with a volume sim? Is it just very slow? Is `--parallel > 1`
   ever a good idea on the hython path, and does anything warn?
3. **Overlay stacking order.** On husk, up to four overlays can apply: relink,
   settings, AOV filter, productName. Check they compose in a sensible order and
   that none silently discards another. `cli.cmd_render` and `ui.PrepareWorker`
   build the chain **separately** — do they agree?
4. **`expected_outputs` vs the overlays.** `jobs_for_rop` fills
   `expected_outputs` from the manifest/`--output`, but the productName overlay
   may rewrite where files land. Can the output verification (T6) now fail a
   good render, or pass a bad one?
5. **`_relinked_usd` and `_relink_dirs` in the GUI** are keyed per ROP but the
   engine can be switched after a relink. Switch engines mid-session and see
   whether stale state leaks.
6. **Preflight resolution check** assumes Indie caps at 1920×1080 (`F3`,
   unverified). Is the warning right, and is `--res-scale` accounted for at all?

### Missing functionality a launcher should probably have

Judge these on merit; some may be deliberate omissions.

- No way to **stop after N failures** or retry a failed chunk.
- No **estimated time remaining** or per-frame timing history.
- `snapshot_interval` is exposed but nothing consumes the snapshots.
- No **render history / log persistence** between sessions — logs vanish.
- Presets exist but cannot be **created or saved** from the GUI.
- No way to render **a subset of ROPs** in one go (one ROP at a time only).
- The AOV manager can only **subset** existing vars — it cannot add or edit one.
- No **`--purpose` / `--complexity`** exposure in the GUI despite husk support.
- Nothing reads husk's **`--extra-metadata`**, `--res-scale`, `--pixel-aspect`
  or `--convergence-mode`, all of which are real flags (see `husk --help`).
- No **dry-run for the hython engine** equivalent of husk's argv preview beyond
  the command line itself.
- `hsl inspect --json` exists; there is **no `--json` on render** for tooling.

### Robustness

- Paths with **spaces and non-ASCII** (the real scene has both). Try a scene in
  a folder with a `#`, `%` or `&` in the name.
- **Cancel** mid-render on both engines: does hython actually die, and do child
  processes go with it? `runner._terminate` uses `CTRL_BREAK_EVENT` on Windows.
- A **read-only or full output directory**.
- Two `hsl` instances rendering at once. The known fixed-path collision is now
  addressed: `inspector._direct_overlay_path` gives relink/settings overlays a
  PID-scoped filename, and parallel chunks run in separate hython processes.
  Still worth exercising concurrently in real Houdini to confirm there is no
  different shared-state failure outside those overlay files.
- A `.hip` that fails to load, a ROP with no input LOP, a scene with zero ROPs.

---

## How to verify against real Houdini

Both installs are present. Use **PowerShell**, never git-bash (it mangles
arguments starting with `/`, which USD prim paths do).

```powershell
$env:HFS = "C:\Program Files\Side Effects Software\Houdini 22.0.368"
$env:PYTHONPATH = "F:\Nexus Projects\SolarisCL"
& "$env:HFS\bin\hython.exe" probe.py <args>
```

Probe rules learned the hard way:

- **`print(..., flush=True)` always.** hython crashes on teardown after heavy
  work and block-buffered output is lost.
- **Prefix output with a sentinel** (`@@MYPROBE@@`) and grep for it. Houdini and
  the Octane plugin on H21 print banners that will otherwise corrupt parsing.
- Do not pipe JSON through `python -c` on PowerShell — it adds a BOM.
- Use `@'…'@` (single-quoted) here-strings; a double-quoted one will interpolate
  `$F4` into nothing.
- Existing probes worth copying live in the session scratchpad and are described
  in `docs/UNVERIFIED.md`.

GUI work must be driven headless — `check_ui_imports.py` only proves imports
resolve, and would **not** have caught the infinite recursion introduced (and
caught) in `_set_status` this session:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"; $env:PYTHONPATH = "F:\Nexus Projects\SolarisCL"
python your_ui_probe.py     # build LauncherWindow, call on_scene_read(manifest), assert
```

A permanent headless GUI smoke test does not exist and would be a good addition.

---

## What a good finding looks like

- **Where**: `file.py:line`.
- **What breaks**: the concrete wrong outcome, not "this looks fragile".
- **Repro**: a command, a test, or a probe script that shows it.
- **Evidence**: actual output, not reasoning about what should happen.
- **Severity**: does it silently corrupt a render, waste hours, or just annoy?

Prefer *silent wrongness* over cosmetics: this codebase's stated enemy is the
render that succeeds and is quietly incorrect.

If you change anything in the Houdini-free layer, a test goes in
`tests/test_core.py` in the same commit and the suite must stay green without
Houdini.
