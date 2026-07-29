"""Progress-bar and ETA text derived from a RenderQueue's tasks.

This used to live in ``hsl/ui.py``, computed straight from ``self.tasks``. It
is plain stdlib logic with no Qt in it -- everything here works on
:mod:`hsl.runner`'s ``Task``/``State`` and plain values -- so keeping it in
``ui.py`` bought it nothing except being untestable: ``ui.py`` is
import-check-only by policy (no PySide6 on the machines running the test
suite), while AGENTS.md's non-negotiable #5 requires a test in
``tests/test_core.py`` for anything in the Houdini-free layer. Moved here so
it has one.

``ui.py`` keeps thin wrapper methods that gather its own state (``self.tasks``,
``self.queue``, and which tasks have actually reported an ``ALF_PROGRESS``
percentage) and call straight through to these functions. No behaviour
changed in the move -- see ``tests/test_core.py`` for the strings this
produces, and the screenshots in the session scratchpad for what they look
like on screen.
"""

from __future__ import annotations

from typing import Container, Optional, Sequence

from .runner import State, Task


def format_eta(seconds: float) -> str:
    """H:MM:SS or M:SS. Plain ASCII digits and colons only -- this lands in a
    progress bar label, never a place that can afford an encoding surprise."""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def eta_seconds(tasks: Sequence[Task], done_frames: int,
                total_frames: int) -> Optional[float]:
    """Seconds remaining, extrapolated from finished chunks' own timings.

    Uses only chunks that have actually finished -- RenderQueue already
    tracks each task's wall-clock duration, so seconds-per-frame is a real
    observed rate, never a guess. None until at least one chunk has finished
    to measure a rate from.
    """
    seconds, frames = 0.0, 0
    for t in tasks:
        if t.state is State.DONE and t.job.chunk.count > 0:
            seconds += t.duration
            frames += t.job.chunk.count
    if frames <= 0 or seconds <= 0:
        return None
    remaining = max(0, total_frames - done_frames)
    return (seconds / frames) * remaining


def task_progress_text(task: Task, progress_seen: Container) -> str:
    """What a queue table's Progress column says for one chunk.

    A chunk of exactly one frame makes ALF_PROGRESS an intra-frame
    percentage -- husk is only ever rendering that one frame, so the number
    *is* how far through it husk is. A chunk of several frames makes the same
    number the chunk's own progress; it is labelled as that rather than
    guessed at as a specific frame within it, since husk does not say which
    frame it is currently on.

    ``progress_seen`` is a container of ``id(task)`` for whichever tasks have
    actually reported a percentage (checked with ``in``, so a set or any
    other container works). The caller owns that bookkeeping -- it is about
    which Qt signal has arrived, not about the task itself -- so hython
    (which never reports one) and a husk task that simply has not printed its
    first line yet both honestly say "running" rather than a percentage that
    never came in.
    """
    if task.state is State.PENDING:
        return "-"
    if task.state is State.RUNNING:
        if id(task) not in progress_seen:
            return "running…"
        if task.job.chunk.count == 1:
            return f"frame {task.job.chunk.start}: {task.progress}%"
        return f"{task.progress}% of {task.job.chunk.count} frames"
    return f"{task.progress}%"


def queue_progress_summary(tasks: Sequence[Task], percent: int,
                           progress_seen: Container) -> str:
    """"Frame N of M" across the whole queue, plus an ETA -- both derived
    only from data the queue already has.

    N counts frames from chunks that have actually finished (exact, since a
    finished chunk's frame numbers are known). It does not interpolate a
    fraction of the currently-running chunk into N -- that would claim to
    know which frame within it is done, which husk does not report for
    chunks longer than one frame. That chunk's own reported percentage is
    shown alongside instead, honestly labelled as the chunk's (see
    :func:`task_progress_text`).
    """
    if not tasks:
        return ""
    total_frames = sum(t.job.chunk.count for t in tasks)
    done_frames = sum(t.job.chunk.count for t in tasks if t.state is State.DONE)
    text = f"{percent}% - Frame {done_frames} of {total_frames}"

    details = []
    for t in tasks:
        if t.state is not State.RUNNING or id(t) not in progress_seen:
            continue
        if t.job.chunk.count == 1:
            details.append(f"frame {t.job.chunk.start}: {t.progress}%")
        else:
            details.append(f"chunk {t.job.chunk}: {t.progress}%")
    if details:
        text += " (" + ", ".join(details) + ")"

    eta = eta_seconds(tasks, done_frames, total_frames)
    if eta is not None:
        text += f" - ETA {format_eta(eta)}"
    return text
