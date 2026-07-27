# Agent pack — Claude Code + Antigravity

Configuration so both IDEs work on this repo under the same constraints, from
one source of truth.

## Setup

```bash
git clone <repo> && cd solaris_launcher
python scripts/link_shared.py     # only needed if the symlinks didn't survive checkout
make check                        # should be all green before you start
```

Then open the folder in either tool. Nothing else to install — the pack is
plain files.

**Claude Code** picks up `CLAUDE.md`, `.claude/settings.json`,
`.claude/agents/`, `.claude/commands/` and `.claude/skills/` automatically.
Approve the hook when prompted on first run.

**Antigravity** picks up `AGENTS.md`, `GEMINI.md`, `.agents/rules/`,
`.agents/skills/` and `.agents/workflows/`. Open Customizations (the "…"
dropdown above the agent panel) and confirm the rules appear under the Rules
tab with the activation modes you expect.

## One source of truth

The two tools use different filenames for the same ideas, so the pack keeps one
copy of everything and points both at it:

| Content | Lives at | Claude Code sees it as | Antigravity sees it as |
|---|---|---|---|
| Project brief | `AGENTS.md` | via `CLAUDE.md` `@`-import | natively |
| Layer rules | `.agents/rules/*.md` | via `AGENTS.md` | natively |
| Skills | `.agents/skills/*/SKILL.md` | `.claude/skills` symlink | natively |
| Procedures | `.agents/workflows/*.md` | `.claude/commands` symlink | natively |

`CLAUDE.md` and `GEMINI.md` are deliberately thin — a pointer to `AGENTS.md`
plus the handful of things that genuinely differ per tool. **Edit `AGENTS.md`,
not the pointers.** `scripts/check_drift.py` fails if content starts
accumulating in them, if a symlink turns into a real directory, or if an agent's
frontmatter stops matching its filename. It runs in `make check`.

Skills work unchanged in both because `SKILL.md` is an open standard that
Antigravity and Claude Code both implement.

## What's in it

**Subagents** (`.claude/agents/`) — four, each with an anti-guessing protocol:

- `houdini-api` (opus) — owns `inspector.py`. Its core instruction is to probe a
  live hython rather than recall an API, because a wrong parameter name here
  fails *silently*: `_parm()` falls through to a default and the render runs with
  the wrong settings, no exception.
- `husk-cli` — owns `husk.py`. Verifies flags against `husk --help`.
- `qt-ui` — owns `ui.py`. Knows the worker-thread → `QueueBridge` → Qt rule.
- `test-guardian` — decides whether a change is actually covered, and is
  explicitly told that saying "no, that's untested" is its job.

**Rules** (`.agents/rules/`) — glob-scoped so each layer's constraints load only
when you're in that layer. `00-architecture.md` is always on.

**Workflows / slash commands** — `/verify-husk-flags`, `/add-parameter-probe`,
`/hip-check`, `/ship-check`. The first three exist because this project's real
risk is Houdini API drift, and each ends by updating `docs/UNVERIFIED.md`.

**Skills** — `solaris-usd-introspection` and `husk-render-submission`. Portable;
they'd work in any Houdini project.

**Boundary hook** — a `PreToolUse` guard that blocks `import hou` outside
`inspector.py`, Qt outside `ui.py`, and third-party imports in `manifest.py`.
It also rejects the `try: import hou / except ImportError` workaround, which is
the obvious way around it. Runs standalone as a linter:

```bash
python .claude/hooks/boundary_guard.py --check-tree .
```

This is the piece that matters most. The invariant is easy to break by accident
and invisible to whoever breaks it — their machine has Houdini, so the tests
still pass. It's covered by 11 tests in `tests/test_boundary_hook.py`.

**Stop hook** — runs the test suite when a turn ends, so a session can't quietly
finish on red.

**Permissions** — tests, scripts and read-only git are pre-approved. `husk` and
`hython` invocations ask first (they're slow and take licenses). `.hip` files
are denied outright: they're large binaries that would flood the context with
nothing readable.

## Docs the agents actually use

`docs/UNVERIFIED.md` is the important one. It enumerates every claim the code
makes about the Houdini and husk APIs that has never been executed — 40-odd
rows across node types, parameter names, USD schema calls, CLI flags and
licensing, each with a way to check it.

The reason to keep it: an agent asked to fix a Houdini bug will produce a
confident, plausible, wrong change, because parameter names drift between
versions and recall is unreliable here. This file tells it which lines are
guesses before it touches them, and every workflow ends by updating it.

`scripts/verify_environment.py` resolves it mechanically. On a machine with
Houdini it probes a real husk binary and builds a throwaway Solaris network to
check every assumed node type, parameter and schema call, then prints a table
of confirmed / wrong / unchecked:

```bash
python scripts/verify_environment.py --report
```

That's the first thing to run when this lands on your workstation. Alongside it,
`docs/ARCHITECTURE.md` explains why the process split exists, and
`docs/TASKS.md` holds a risk-ordered backlog with acceptance criteria — T1–T3
are the known-dangerous ones.

## Verified vs. inferred

Tested here:

- `make check` — 139 tests, boundary lint, 55 drift checks, UI import check.
- The hook, against 12 allow/block cases including the try/except workaround
  and false positives like `import hounddog`.
- `check_drift.py` against deliberately broken configs (severed symlink,
  oversized rule, mismatched agent name, dangling `@` reference) — it catches
  all of them.
- `verify_environment.py` against a fake husk with flags deliberately missing —
  it reports them as FAIL rather than passing them through.

Taken from current docs: Claude Code's `.claude/` layout, hook protocol and
exit-code-2 blocking; Antigravity's `.agents/rules` and `.agents/skills` paths,
the 12,000-character rule limit, `AGENTS.md`/`GEMINI.md` precedence, and
`SKILL.md` frontmatter.

**Inferred, so check these two on first run:**

1. **Rule frontmatter.** Antigravity's docs describe the activation modes
   (Always On, Glob, Model Decision, Manual) but not the YAML that sets them.
   The `trigger:` / `globs:` keys used here follow the convention from its
   lineage. If the Rules tab shows them as Manual, set the mode in the panel —
   the rule content is correct either way.
2. **Workspace workflow path.** `.agents/workflows/` is inferred; the documented
   flow creates workflows through the Customizations panel. If `/ship-check`
   doesn't autocomplete, create the workflows in the panel and paste the file
   contents in.

Neither affects Claude Code, which reads `.claude/commands/` through the symlink.

## First session

Try this, in either tool:

> Read AGENTS.md and docs/UNVERIFIED.md, then run make check and tell me what
> is actually proven about this codebase versus what is asserted.

A good answer distinguishes the 46 passing tests from the entirely unverified
`inspector.py`. If it claims the Houdini layer works, the pack isn't loading —
check that `AGENTS.md` is being read.
