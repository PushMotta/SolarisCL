---
trigger: glob
globs: tests/**,hsl/**
---

# Testing

`python -m unittest discover -s tests` must stay green **and must not require
Houdini**. `unittest` only; no third-party runner.

A new test must fail without the change it covers. A test that passes against
the old code proves nothing — check this specifically before claiming coverage.

## What is provable here

`manifest.py`, `husk.py`, `runner.py`, `cli.py`, the boundary hook. A change to
any of these without a test is a gap, not a judgement call.

## What is not

`inspector.py` needs `hou`; `ui.py` needs a live event loop. These get
import-checks and honest disclaimers, never claims of correctness.

## Conventions

`RenderQueue` is tested against a fake husk written to a temp dir that emits
real `ALF_PROGRESS` lines and can be told to fail. Extend that rather than
mocking `subprocess` — the point is exercising actual pipe handling.

Name tests for behaviour: `test_ragged_split_covers_every_frame`, not
`test_frame_chunks_2`. Cover error paths, not just the happy one.
