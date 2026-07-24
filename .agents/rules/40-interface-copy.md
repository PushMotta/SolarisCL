---
trigger: glob
globs: hsl/ui.py,hsl/cli.py
---

# Interface text

Words in the interface are there to make it easier to use, not to decorate it.

- Name things by what the person controls, not how the system is built.
  "Write USD while reading", not "Enable USD ROP export pass".
- Buttons say what happens: "Start render", not "Submit". The name stays
  consistent through the flow — "Start render" leads to "Rendering", not
  "Job dispatched".
- Errors explain what went wrong **and what to do next**. "husk is not on PATH
  and $HFS is not set. Source houdini_setup, or set $HSL_HUSK." Not "husk not
  found."
- Empty states invite an action rather than describing a void. "Read a scene to
  see its render settings", not "No data".
- Sentence case. No filler, no apologising.

## Threading

Qt widgets are touched from the Qt thread only. `RenderQueue` calls back from
worker threads, so every event crosses through `QueueBridge` signals. Adding an
event means: `Signal` on `QueueBridge` → emit in `dispatch()` → connect in
`start_render()` → handle in a `@Slot`. Never write to a widget from `on_event`.
