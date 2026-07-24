#!/usr/bin/env python3
"""Validate that the agent configuration is internally consistent.

Two IDEs read this repo through different files. That is a standing invitation
for them to drift apart, and the failure is quiet — one tool silently stops
picking up a rule. This is the cheap check that catches it.

    python scripts/check_drift.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE_CHAR_LIMIT = 12000          # Antigravity's documented per-file limit
problems: list[str] = []
checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(message)


def read(*parts: str) -> str:
    path = os.path.join(ROOT, *parts)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    return dict(re.findall(r"^([\w-]+):\s*(.+)$", match.group(1), re.M))


def _tree_signature(root: str) -> dict:
    """Map of relative-path -> file bytes, for deep directory comparison."""
    signature = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "rb") as handle:
                    signature[rel] = handle.read()
            except OSError:
                signature[rel] = None
    return signature


def _is_faithful_copy(copy_dir: str, source_dir: str) -> bool:
    """True if copy_dir is an exact content copy of source_dir (no drift)."""
    return _tree_signature(copy_dir) == _tree_signature(source_dir)


# 1. One canonical brief, two thin pointers.
agents_md = read("AGENTS.md")
check(bool(agents_md), "AGENTS.md is missing — it is the canonical brief")
for pointer in ("CLAUDE.md", "GEMINI.md"):
    text = read(pointer)
    check(bool(text), f"{pointer} is missing")
    check("AGENTS.md" in text, f"{pointer} does not point at AGENTS.md")
    check(len(text) < len(agents_md) if agents_md else True,
          f"{pointer} is longer than AGENTS.md — content has drifted into it")

# 2. Shared directories are still shared — either a symlink to the .agents/
#    source, or (on Windows, where scripts/link_shared.py falls back to copying
#    because symlinks need Developer Mode) a *faithful* copy of it. The failure
#    this guards against is a copy that has DRIFTED from the source, not the
#    copy mode itself.
for link, target in (("commands", "workflows"), ("skills", "skills")):
    path = os.path.join(ROOT, ".claude", link)
    source = os.path.join(ROOT, ".agents", target)
    if os.path.islink(path):
        check(os.path.realpath(path) == source,
              f".claude/{link} points at {os.readlink(path)}, expected ../.agents/{target}")
    elif os.path.isdir(path):
        check(_is_faithful_copy(path, source),
              f".claude/{link} is a copy that has drifted from .agents/{target} — "
              f"re-run scripts/link_shared.py")
    else:
        check(False, f".claude/{link} is missing — run scripts/link_shared.py")

# 3. Subagents are well formed.
agents_dir = os.path.join(ROOT, ".claude", "agents")
check(os.path.isdir(agents_dir), ".claude/agents/ is missing")
for name in sorted(os.listdir(agents_dir) if os.path.isdir(agents_dir) else []):
    if not name.endswith(".md"):
        continue
    meta = frontmatter(read(".claude", "agents", name))
    check("name" in meta, f"agent {name}: no name in frontmatter")
    check("description" in meta, f"agent {name}: no description — it will never be selected")
    check(len(meta.get("description", "")) > 60,
          f"agent {name}: description too short to route on")
    check(meta.get("name", "") == name[:-3],
          f"agent {name}: frontmatter name '{meta.get('name')}' does not match filename")

# 4. Rules fit Antigravity's limit and declare a trigger.
rules_dir = os.path.join(ROOT, ".agents", "rules")
check(os.path.isdir(rules_dir), ".agents/rules/ is missing")
for name in sorted(os.listdir(rules_dir) if os.path.isdir(rules_dir) else []):
    text = read(".agents", "rules", name)
    check(len(text) <= RULE_CHAR_LIMIT,
          f"rule {name}: {len(text)} chars exceeds the {RULE_CHAR_LIMIT} limit")
    meta = frontmatter(text)
    check("trigger" in meta, f"rule {name}: no trigger declared")
    if meta.get("trigger") == "glob":
        check("globs" in meta, f"rule {name}: trigger is glob but no globs given")

# 5. Skills follow the open standard.
skills_dir = os.path.join(ROOT, ".agents", "skills")
for name in sorted(os.listdir(skills_dir) if os.path.isdir(skills_dir) else []):
    skill_md = os.path.join(skills_dir, name, "SKILL.md")
    check(os.path.isfile(skill_md), f"skill {name}: no SKILL.md")
    if os.path.isfile(skill_md):
        meta = frontmatter(read(".agents", "skills", name, "SKILL.md"))
        check("description" in meta, f"skill {name}: description is required")
        check(len(meta.get("description", "")) > 80,
              f"skill {name}: description too short for the agent to route on")

# 6. Settings parse and their hook targets exist.
settings_raw = read(".claude", "settings.json")
check(bool(settings_raw), ".claude/settings.json is missing")
if settings_raw:
    try:
        settings = json.loads(settings_raw)
        for event, entries in settings.get("hooks", {}).items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    command = hook.get("command", "")
                    for token in re.findall(r"\$CLAUDE_PROJECT_DIR/([\w./-]+)", command):
                        check(os.path.exists(os.path.join(ROOT, token)),
                              f"hook {event}: references missing file {token}")
    except ValueError as exc:
        problems.append(f".claude/settings.json is not valid JSON: {exc}")
        checks += 1

# 7. Every @-referenced doc exists.
for source in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
    for ref in re.findall(r"@([\w./-]+\.md)", read(source)):
        check(os.path.exists(os.path.join(ROOT, ref)),
              f"{source} references @{ref}, which does not exist")

# 8. The boundary rule is stated where both tools will see it.
check("inspector.py" in agents_md and "hou" in agents_md,
      "AGENTS.md no longer states the hou/inspector.py boundary")


problems = list(dict.fromkeys(problems))   # same issue can be hit twice

if problems:
    print(f"{len(problems)} problem(s) in {checks} checks:\n")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)

print(f"Agent config consistent: {checks} checks passed.")
sys.exit(0)
