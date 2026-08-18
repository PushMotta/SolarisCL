"""Running husk processes.

Deliberately built on ``subprocess`` + threads rather than ``QProcess`` so the
same queue drives the CLI and the GUI. The UI layer subscribes with a callback
and re-emits Qt signals from it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Sequence

from .husk import (
    RenderJob, build_command, expand_frame_token, has_unexpanded_tokens,
    looks_like_error, parse_progress,
)


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"        # something it depended on did not finish


# States a task can no longer leave.
TERMINAL_STATES = (State.DONE, State.FAILED, State.CANCELLED, State.SKIPPED)


# How often running processes are looked at for memory. RAM has a real
# operating-system peak counter, but GPU memory does not -- it can only be
# polled, so a spike between two samples is missed. Two seconds trades catching
# real peaks against spawning nvidia-smi too often; anything obtained this way
# is flagged ``vram_sampled`` so nobody reads it as exact.
SAMPLE_INTERVAL = 2.0


@dataclass
class Task:
    job: RenderJob
    state: State = State.PENDING
    progress: int = 0
    returncode: Optional[int] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    log: list[str] = field(default_factory=list)
    # What this process actually used, in bytes. ``None`` means *not measured*
    # -- never zero, and never an estimate. See hsl/sysinfo.py for what each
    # platform can and cannot answer.
    peak_rss: Optional[int] = None
    peak_vram: Optional[int] = None
    vram_sampled: bool = False     # GPU figures are polled, so a spike may be missed
    # True when peak_rss covers the whole process tree (a Job Object), False
    # when it is only the process hsl spawned. Not a detail: a studio
    # ``husk.bat`` wrapper measured single-process reads ~8 MB for a render
    # that really used 300 MB, so a consumer must be able to tell the two
    # apart rather than trusting whichever it was handed.
    peak_rss_is_tree: bool = False
    _proc: Optional[subprocess.Popen] = None
    # The Job Object this task's process was spawned into, if any. Opened
    # before the Popen and closed in the same finally that clears _proc.
    _job: Optional[object] = None

    @property
    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def command(self) -> list[str]:
        return build_command(self.job)


# Event names passed to the callback:
#   ("task_started", task)
#   ("task_output",  task, line)
#   ("task_progress", task, percent)
#   ("task_finished", task)
#   ("queue_finished", queue)
EventCallback = Callable[..., None]


class RenderQueue:
    """Runs a list of :class:`RenderJob` with a concurrency limit."""

    def __init__(self, jobs: Sequence[RenderJob], max_parallel: int = 1,
                 on_event: Optional[EventCallback] = None,
                 keep_log_lines: int = 2000,
                 env: Optional[dict] = None,
                 measure_memory: bool = True,
                 record_memory: bool = True):
        self.tasks = [Task(job=job) for job in jobs]
        self.max_parallel = max(1, int(max_parallel))
        self.on_event = on_event or (lambda *a, **k: None)
        self.keep_log_lines = keep_log_lines
        self.env = env
        # Watching costs one cheap poll per couple of seconds; filing the
        # result is what makes preflight able to say "this scene needed 48 GB
        # last time" instead of predicting. Both are off in tests that do not
        # want a user-profile file written.
        self.measure_memory = measure_memory
        self.record_memory = record_memory
        self._sampler_stop = threading.Event()
        self.warnings: list[str] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._slots = threading.Semaphore(self.max_parallel)
        self._known_ids = {t.job.task_id for t in self.tasks if t.job.task_id}
        self._validate_dependencies()

    def _validate_dependencies(self) -> None:
        """Reject cycles up front; note references this queue cannot resolve.

        A cycle can never be scheduled, so it is a construction error rather
        than something to discover halfway through a render.

        An *unknown* dependency is a different thing and must not raise:
        rendering one ROP out of a scene legitimately leaves its upstream out
        of the queue. Those are treated as already satisfied and recorded in
        ``warnings``, so the decision is visible rather than silent.
        """
        edges: dict[str, set] = {}
        for task in self.tasks:
            tid = task.job.task_id
            if tid:
                edges.setdefault(tid, set()).update(task.job.depends_on or ())

        known = set(edges)
        for tid in sorted(edges):
            for dep in sorted(edges[tid] - known):
                self.warnings.append(
                    f"{tid} depends on {dep}, which is not in this queue -- "
                    f"treating it as already finished."
                )

        # Kahn's algorithm: peel off everything with no outstanding
        # dependency. Whatever will not peel is in a cycle.
        remaining = {tid: set(deps) & known for tid, deps in edges.items()}
        peeled = True
        while peeled:
            peeled = False
            for tid in [t for t, deps in remaining.items() if not deps]:
                del remaining[tid]
                for deps in remaining.values():
                    deps.discard(tid)
                peeled = True
        if remaining:
            raise ValueError(
                "dependency cycle between: " + ", ".join(sorted(remaining))
                + " -- these tasks each wait on another in the group, so none "
                  "of them can ever start."
            )

    # -- lifecycle --------------------------------------------------------

    def start(self, block: bool = False) -> None:
        driver = threading.Thread(target=self._drive, name="hsl-queue", daemon=True)
        driver.start()
        if block:
            driver.join()

    def cancel(self) -> None:
        """Ask every running husk to stop and mark the rest cancelled."""
        self._cancel.set()
        with self._lock:
            for task in self.tasks:
                if task.state is State.PENDING:
                    task.state = State.CANCELLED
                elif task.state is State.RUNNING and task._proc:
                    _terminate(task._proc)

    @property
    def finished(self) -> bool:
        return all(t.state in TERMINAL_STATES for t in self.tasks)

    @property
    def progress(self) -> int:
        """Overall percentage across all tasks."""
        if not self.tasks:
            return 100
        total = 0
        for task in self.tasks:
            # Every terminal state counts as finished work, however it ended:
            # the bar tracks "how much of the queue is left", not success.
            total += 100 if task.state in TERMINAL_STATES else task.progress
        return total // len(self.tasks)

    # -- internals --------------------------------------------------------

    def _group_status(self):
        """(finished ok, will never finish) over task ids. Call under the lock.

        A task id covers every chunk of one OutputTask, so it counts as done
        only when *all* of its chunks are -- half a cache is not an input its
        dependants can use.
        """
        groups: dict[str, list] = {}
        for task in self.tasks:
            tid = task.job.task_id
            if tid:
                groups.setdefault(tid, []).append(task)

        done, blocked = set(), set()
        for tid, members in groups.items():
            states = {m.state for m in members}
            if states & {State.FAILED, State.CANCELLED, State.SKIPPED}:
                blocked.add(tid)
            elif states == {State.DONE}:
                done.add(tid)
        return done, blocked

    def _classify(self):
        """Sort pending tasks into (start now, skip, keep waiting).

        Anything returned in the first list is already marked RUNNING, so a
        second pass around the driver loop cannot start it twice.
        """
        launch, skipped, waiting = [], [], []
        with self._lock:
            done, blocked = self._group_status()
            for task in self.tasks:
                if task.state is not State.PENDING:
                    continue
                deps = task.job.depends_on or ()
                if any(dep in blocked for dep in deps):
                    task.state = State.SKIPPED
                    task.finished_at = time.time()
                    self._record(task, "Skipped: a task it depends on did not "
                                       "finish successfully.")
                    skipped.append(task)
                elif all(dep in done or dep not in self._known_ids
                         for dep in deps):
                    task.state = State.RUNNING          # claim it
                    launch.append(task)
                else:
                    waiting.append(task)
        return launch, skipped, waiting

    def _running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self.tasks if t.state is State.RUNNING)

    def _abandon(self, waiting) -> None:
        """Nothing is running and nothing can start -- give up on the rest."""
        with self._lock:
            for task in waiting:
                if task.state is State.PENDING:
                    task.state = State.SKIPPED
                    task.finished_at = time.time()
                    self._record(task, "Skipped: its dependencies can never "
                                       "be satisfied.")
        for task in waiting:
            self.on_event("task_finished", task)

    def _measure_loop(self) -> None:
        """Poll running processes for memory until the queue is done.

        The import is deliberately function-local: ``sysinfo`` touches ctypes
        and platform binaries, and nothing there is allowed to stop the queue
        from even loading. Every failure degrades to "not measured".
        """
        try:
            from . import sysinfo
        except ImportError:
            # Instrumentation is optional. Its absence is a fact about the
            # install, reported by `hsl memory --machine`, not a fault in this
            # render -- so it must not put a warning on every queue. A failure
            # *during* sampling is a surprise, and does warn (below).
            return
        try:
            watch_gpu = bool(sysinfo.find_nvidia_smi())
            while not self._sampler_stop.is_set():
                with self._lock:
                    live = {t._proc.pid: t for t in self.tasks
                            if t.state is State.RUNNING and t._proc is not None}
                if live:
                    # Sampled RSS is the fallback for platforms with no peak
                    # counter; on Windows the exact figure replaces it at exit.
                    for pid, task in live.items():
                        rss = sysinfo.current_rss(pid)
                        if rss and rss > (task.peak_rss or 0):
                            task.peak_rss = rss
                    if watch_gpu:
                        # Matched by pid. A delegate that hands its GPU work to
                        # a child process is therefore missed -- reported as
                        # unknown rather than guessed at.
                        for pid, used in sysinfo.gpu_memory_bytes(list(live)).items():
                            task = live.get(pid)
                            if task is not None and used > (task.peak_vram or 0):
                                task.peak_vram = used
                                task.vram_sampled = True
                self._sampler_stop.wait(SAMPLE_INTERVAL)
        except Exception as exc:        # never let measurement kill a render
            self.warnings.append(f"memory sampling stopped: {exc}")

    def _open_job(self):
        """A Job Object to spawn the next render into, or None.

        Function-local import for the same reason ``_measure_loop`` has one:
        ``sysinfo`` touches ctypes, and nothing there is allowed to stop a
        render from starting.
        """
        try:
            from . import sysinfo
            return sysinfo.open_job_object()
        except Exception:
            return None

    def _finish_measurement(self, task: Task) -> None:
        """Take the exact peak if the OS has one, then file the result.

        Three sources, best first:

        1. **The Job Object.** A kernel high-water mark over the whole process
           tree, so a studio ``husk.bat`` wrapper is measured *through* rather
           than instead of. Windows only.
        2. **PeakWorkingSetSize** for the spawned process alone. The old
           behaviour, and still right for a direct spawn -- but a wrapper
           reads as the wrapper, which is why the choice is recorded in
           ``peak_rss_is_tree`` rather than left for a consumer to guess.
        3. Whatever ``_measure_loop`` sampled, which can miss a spike between
           samples and is therefore only the last resort.

        Both exact figures *replace* the sampled one rather than being
        reconciled with it. Every one of them is allowed to be unavailable;
        the task simply keeps the best it got, and ``None`` if that is none.
        """
        try:
            from . import sysinfo
            tree = sysinfo.peak_job_memory(task._job)
            if tree:
                task.peak_rss = tree
                task.peak_rss_is_tree = True
            else:
                exact = sysinfo.peak_working_set(task._proc)
                if exact:
                    task.peak_rss = exact
                    task.peak_rss_is_tree = False
        except Exception:
            pass            # keep whatever sampling managed to see

        if not self.record_memory:
            return
        if task.peak_rss is None and task.peak_vram is None:
            return          # an entry that says nothing only dilutes the history
        try:
            from . import memlog
        except ImportError:
            return          # optional, as above
        try:
            memlog.record(memlog.MemorySample(
                hip_path=task.job.hip_file or task.job.usd_file,
                rop_path=task.job.rop_path,
                engine=task.job.engine,
                frames=task.job.chunk.count,
                peak_rss=task.peak_rss,
                peak_rss_is_tree=task.peak_rss_is_tree,
                peak_vram=task.peak_vram,
                vram_sampled=task.vram_sampled,
                when=task.finished_at or time.time(),
            ))
        except Exception as exc:
            # A history we could not write is not a failed render -- but say so
            # rather than losing the measurement silently.
            self.warnings.append(f"could not record memory use: {exc}")

    def _drive(self) -> None:
        sampler = None
        if self.measure_memory:
            sampler = threading.Thread(target=self._measure_loop,
                                       name="hsl-memory", daemon=True)
            sampler.start()
        try:
            while not self._cancel.is_set():
                launch, skipped, waiting = self._classify()

                for task in skipped:
                    self.on_event("task_finished", task)

                for task in launch:
                    if self._cancel.is_set():
                        break
                    self._slots.acquire()
                    if self._cancel.is_set():
                        self._slots.release()
                        break
                    thread = threading.Thread(
                        target=self._run_task, args=(task,),
                        name=f"hsl-{task.job.chunk}", daemon=True)
                    thread.start()
                    self._threads.append(thread)

                if launch or skipped:
                    continue                    # re-check: that may have freed more
                if not waiting:
                    break                       # nothing left to start
                if self._running_count() == 0:
                    # Nothing running and nothing runnable. Cycles are rejected
                    # in the constructor, so this is a safety net rather than an
                    # expected path -- but hanging forever would be worse.
                    self._abandon(waiting)
                    break
                time.sleep(0.02)                # wait for a dependency to land
        finally:
            for thread in list(self._threads):
                thread.join()
            # Stopped only once no process is left to watch, so a task that
            # outlives the driver loop is still measured to its end.
            self._sampler_stop.set()
            if sampler is not None:
                sampler.join(timeout=SAMPLE_INTERVAL * 2)
            self.on_event("queue_finished", self)

    def _run_task(self, task: Task) -> None:
        job = None
        try:
            if self._cancel.is_set():
                task.state = State.CANCELLED
                return

            cmd = task.command
            task.state = State.RUNNING
            task.started_at = time.time()
            self.on_event("task_started", task)

            # Before the spawn, not after: a Job Object cannot adopt a process
            # tree retroactively, so this is the one thing that has to happen
            # in the right order. It carries no limits, so it can neither slow
            # the render down nor kill it.
            if self.measure_memory:
                job = self._open_job()
                task._job = job

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self.env,
                    cwd=os.path.dirname(task.job.usd_file) or None,
                    **_process_group_kwargs(),
                )
            except OSError as exc:
                task.state = State.FAILED
                task.returncode = -1
                self._record(task, f"Could not start husk: {exc}")
                self.on_event("task_finished", task)
                return

            task._proc = proc

            # Immediately, while the child is still opening its own files and
            # before it can fork anything the job would miss. A False here is
            # not an error -- it means this platform or this Windows will not
            # do whole-tree measurement, and _finish_measurement falls back to
            # the single-process peak.
            if job is not None:
                try:
                    from . import sysinfo
                    sysinfo.assign_process_to_job(job, proc)
                except Exception:
                    pass

            try:
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    self._record(task, line)
                    self.on_event("task_output", task, line)

                    percent = parse_progress(line)
                    if percent is not None and percent != task.progress:
                        task.progress = percent
                        self.on_event("task_progress", task, percent)
            finally:
                proc.stdout.close()

            proc.wait()
            task.returncode = proc.returncode
            task.finished_at = time.time()

            if self._cancel.is_set():
                outcome = State.CANCELLED
            elif proc.returncode == 0 and self._wrote_something(task):
                outcome = State.DONE
            else:
                outcome = State.FAILED

            # Measure a task that ran to its own end, whichever way it ended --
            # a render killed by the machine running out of memory is the most
            # useful sample there is. A cancelled one was cut short by us, so
            # its peak says nothing about what the scene needs.
            #
            # This happens *before* the terminal state is published, and that
            # ordering is load-bearing: the driver starts dependants the moment
            # it sees this task's state, so anything slow between the state
            # change and the event below lets a dependant start before this
            # task has even reported finishing. Measuring first also means
            # ``task_finished`` carries the peaks rather than reaching
            # consumers with them still empty.
            if self.measure_memory and outcome in (State.DONE, State.FAILED):
                self._finish_measurement(task)

            task.state = outcome
            if outcome is State.DONE:
                task.progress = 100
            self.on_event("task_finished", task)
        finally:
            task._proc = None
            # After _finish_measurement, which is the only reader: the peak
            # counter lives on the handle and goes with it. Closing it cannot
            # kill the render -- open_job_object sets no limits, least of all
            # KILL_ON_JOB_CLOSE -- and close_job_object is idempotent, so an
            # unwound cancel that gets here twice is harmless.
            if job is not None:
                task._job = None
                try:
                    from . import sysinfo
                    sysinfo.close_job_object(job)
                except Exception:
                    pass
            self._slots.release()

    def _wrote_something(self, task: Task) -> bool:
        """False only when the render exited 0 and produced none of its outputs.

        A husk process can exit 0 having written nothing at all -- an output
        directory it could not write to, a product pointing somewhere
        unexpected -- and the return code alone cannot tell that from success.

        The check is deliberately timid. It needs ``job.expected_outputs`` to
        know what to look for, and it only fails a task when **every** expected
        file is absent or empty. A partial miss is logged but still passes:
        turning a good render into a false failure over our own path
        arithmetic would be worse than the bug being caught.
        """
        templates = task.job.expected_outputs
        if not templates:
            return True

        chunk = task.job.chunk
        frames = [chunk.start + i * chunk.inc for i in range(chunk.count)]

        expected, missing = [], []
        for template in templates:
            for index, frame in enumerate(frames, 1):
                path = expand_frame_token(template, frame, index)
                if has_unexpanded_tokens(path):
                    # A token we cannot resolve (e.g. $FF, %g) means we do not
                    # know the filename. Checking it would find nothing and fail
                    # a perfectly good render, so leave this one alone.
                    self._record(task, f"Cannot verify output (unresolved token): "
                                       f"{template}")
                    continue
                expected.append(path)
                try:
                    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                        missing.append(path)
                except OSError:
                    missing.append(path)

        if not expected:
            return True

        if len(missing) == len(expected):
            self._record(task, f"Exited 0 but wrote none of its {len(expected)} "
                               f"expected output(s):")
            for path in missing[:5]:
                self._record(task, f"  missing: {path}")
            if len(missing) > 5:
                self._record(task, f"  … and {len(missing) - 5} more")
            return False

        if missing:
            self._record(task, f"warning: {len(missing)} of {len(expected)} "
                               f"expected output(s) missing or empty")
        return True

    def _record(self, task: Task, line: str) -> None:
        task.log.append(line)
        if len(task.log) > self.keep_log_lines:
            del task.log[:len(task.log) - self.keep_log_lines]


# --------------------------------------------------------------------------

def _process_group_kwargs() -> dict:
    """Put husk in its own process group so cancelling kills its children too."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _terminate(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """Terminate politely, then insist."""
    try:
        if os.name == "nt":
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ValueError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            return

    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ValueError, ProcessLookupError):
        pass
