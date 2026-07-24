# hsl — Solaris Render Launcher

The project brief lives in @AGENTS.md — read it first. It is the canonical
source; this file only holds Claude Code specifics.

## Claude Code setup in this repo

- **Subagents** (`.claude/agents/`) — delegate rather than guessing:
  - `houdini-api` — anything touching `hou` or `pxr`. It knows to probe a live
    hython rather than recall an API.
  - `husk-cli` — anything touching husk flags or `build_command()`.
  - `qt-ui` — `hsl/ui.py`, thread-safety, widget wiring.
  - `test-guardian` — reviews whether a change is actually covered.
- **Commands** (`.claude/commands/`) — `/verify-husk-flags`,
  `/add-parameter-probe`, `/hip-check`, `/ship-check`.
- **Skills** (`.claude/skills/` → symlink of `.agents/skills/`) — shared with
  Antigravity. Edit the `.agents/` copy.
- **Hook** — a `PreToolUse` guard blocks `import hou` outside
  `hsl/inspector.py`. That is intentional; see @AGENTS.md.

## Working agreement

Houdini is probably **not installed on this machine**. Do not claim a Houdini
code path works because it looks right — say plainly that it is unverified and
add it to @docs/UNVERIFIED.md. `python -m unittest discover -s tests` is the
only thing you can actually prove here; run it before saying you are done.

When a change spans both halves, do the manifest first, then the inspector,
then the consumers. The manifest is the contract.
