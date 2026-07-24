---
trigger: glob
globs: hsl/husk.py,hsl/runner.py,hsl/bridge.py,hsl/cli.py,hsl/manifest.py
---

# The Houdini-free layer

This code must run with no Houdini and no Qt installed. It is fully testable,
so **"I could not verify" is not available here** — write the test.

## husk commands

Build argv **lists**, never shell strings; nothing runs through a shell.
`format_command()` is for display only. Omit empty overrides rather than passing
empty strings. The USD file argument stays last.

Verify flags against `husk --help` before adding them. Two carry hidden risk:

- `--verbose 3a` — the `a` selects Alfred progress, which `parse_progress()`
  reads. Wrong letter means renders succeed with every progress bar at zero.
- `--frame-count` is a **count**, not an end frame.

## Chunking

`frame_chunks()` counts frames *rendered*, not frame numbers. Any change needs a
test proving the chunks reconstruct the original frame list exactly.

## Process handling

`RenderQueue` callbacks fire on worker threads. Keep it that way — it is what
lets the CLI and GUI share one engine. Cancellation must kill the process
*group*, or husk's children survive.

## The manifest is a contract

`hsl/manifest.py` imports the standard library only — hython has its own
site-packages. Changing a field means updating the inspector that produces it
and every consumer that reads it, in that order.
