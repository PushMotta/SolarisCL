---
description: Everything that must be true before calling a change finished
---

# Ship check

Run all of it. Report each result rather than summarising as "tests pass".

```bash
python -m unittest discover -s tests                    # must be green
python .claude/hooks/boundary_guard.py --check-tree .   # boundary intact
python scripts/check_drift.py                           # config still consistent
python scripts/check_ui_imports.py                      # ui.py imports resolve
```

Then confirm by reading, not by assuming:

1. **Did the change touch the Houdini-free layer without a test?** If so it is
   incomplete — most of this codebase is testable and that excuse does not apply.
2. **Would the new test fail against the old code?** If not, it proves nothing.
3. **Does @docs/UNVERIFIED.md still match reality?** New guesses added, resolved
   ones moved to Verified with a version number.
4. **Is anything claimed as working that was never executed?** Say so explicitly.
   `inspector.py` and `ui.py` cannot be proven on a machine without Houdini and
   Qt, and pretending otherwise is the most damaging thing you can do here.
5. **Is @AGENTS.md still accurate?** It is the canonical brief — if the
   architecture moved, it moves too.

Finish with a short report: what is proven, what is asserted but unproven, and
what is left.
