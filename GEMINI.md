# hsl — Solaris Render Launcher

The project brief lives in @AGENTS.md — read it first. It is the canonical
source; this file only holds Antigravity specifics.

## Antigravity setup in this repo

- **Rules** (`.agents/rules/`) — `00-architecture.md` is Always On;
  the rest are glob-scoped to the layer they govern.
- **Workflows** (`.agents/workflows/`) — `/verify-husk-flags`,
  `/add-parameter-probe`, `/ship-check`.
- **Skills** (`.agents/skills/`) — shared verbatim with Claude Code.

## Working agreement

Houdini is probably **not installed on this machine**. `hsl/inspector.py`
cannot be executed here; treat every claim about it as unverified and record it
in @docs/UNVERIFIED.md rather than asserting it works.

Antigravity's browser tooling is not useful on this project — there is no web
surface. The verifiable surface is `python -m unittest discover -s tests`.
Prefer an Implementation Plan artifact for anything touching both halves of the
architecture, since the manifest contract has to change first.
