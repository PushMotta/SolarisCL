---
name: husk-render-submission
description: Builds and runs husk command lines for Houdini Karma/Hydra renders, including frame chunking for farm submission and parsing render progress. Use when constructing husk arguments, splitting frame ranges across processes, wiring up render progress reporting, or debugging why a husk render produced wrong frames or no progress.
---

# Submitting renders with husk

`husk` is Houdini's standalone USD render utility, in `$HFS/bin`. It renders a
USD file with a Hydra delegate — it does not read `.hip` files.

## Shape of a command

```
husk --renderer BRAY_HdKarmaXPU \
     --frame 1001 --frame-count 100 \
     --output /renders/shot.$F4.exr \
     --make-output-path --verbose 3a \
     /path/shot.usd
```

The USD file is positional and goes last. Build **argv lists**, not shell
strings — nothing here needs a shell, and quoting paths inside the list breaks
them.

## The two flags that cause silent wrongness

**`--frame-count`, not an end frame.** husk takes a start plus a count. Passing
an end frame renders the wrong range, and on a chunked farm job that means
either missing frames or every chunk overlapping.

**`--verbose 3a`** — a numeric level plus flag letters, where `a` selects
Alfred-style progress (`ALF_PROGRESS n%`). If a progress bar reads zero
throughout an otherwise successful render, this is why. Any progress parser
depends on that exact format.

Confirm both against `husk --help` for the installed version. Flags drift.

## Chunking for parallel submission

Count **frames rendered**, not frame numbers. With increment 2 across 1–100 and
a chunk size of 10, each chunk renders 10 frames and spans 20 numbers:

```python
total = (end - start) // inc + 1
while rendered < total:
    count = min(chunk_size, total - rendered)
    emit(start=start + rendered * inc, count=count, inc=inc)
    rendered += count
```

Always test that reassembling the chunks reproduces the original frame list
exactly. Off-by-one here is invisible until a client notices a missing frame.

## Running the process

- One husk process per chunk. For a real farm, emit the commands and hand them
  to the scheduler rather than running them locally.
- Parallel husk processes on one machine contend for RAM and cores, and each
  checks out its own Karma license. Two or three is usually the ceiling on a
  workstation.
- Start husk in its own process group (`start_new_session=True`, or
  `CREATE_NEW_PROCESS_GROUP` on Windows). Cancelling must kill the group —
  terminating the parent alone leaves render children running.
- Merge stderr into stdout and read line by line; progress and errors interleave.

## Useful flags

| Flag | Why |
|---|---|
| `--make-output-path` | create missing directories instead of failing at the end of a render |
| `--snapshot 60` | flush a partial image every 60s so a long frame can be checked |
| `--res W H` | quick low-res test renders |
| `--settings /Render/rs_beauty` | pick one of several RenderSettings prims |
| `--fast-exit 1` | skip teardown; meaningful on short frames |
| `--list-renderers` | confirm which delegates are actually registered |

## Licensing

husk takes a Karma render license rather than a full Houdini license, so it
parallelises without consuming interactive seats. On Indie it is capped at
1920×1080.
