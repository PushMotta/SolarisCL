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

from .husk import RenderJob, build_command, looks_like_error, parse_progress


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    job: RenderJob
    state: State = State.PENDING
    progress: int = 0
    returncode: Optional[int] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    log: list[str] = field(default_factory=list)
    _proc: Optional[subprocess.Popen] = None

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
                 env: Optional[dict] = None):
        self.tasks = [Task(job=job) for job in jobs]
        self.max_parallel = max(1, int(max_parallel))
        self.on_event = on_event or (lambda *a, **k: None)
        self.keep_log_lines = keep_log_lines
        self.env = env
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._slots = threading.Semaphore(self.max_parallel)

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
        return all(t.state in (State.DONE, State.FAILED, State.CANCELLED)
                   for t in self.tasks)

    @property
    def progress(self) -> int:
        """Overall percentage across all tasks."""
        if not self.tasks:
            return 100
        total = 0
        for task in self.tasks:
            if task.state in (State.DONE, State.CANCELLED):
                total += 100
            elif task.state is State.FAILED:
                total += 100
            else:
                total += task.progress
        return total // len(self.tasks)

    # -- internals --------------------------------------------------------

    def _drive(self) -> None:
        for task in self.tasks:
            if self._cancel.is_set():
                break
            self._slots.acquire()
            if self._cancel.is_set():
                self._slots.release()
                break
            thread = threading.Thread(target=self._run_task, args=(task,),
                                      name=f"hsl-{task.job.chunk}", daemon=True)
            thread.start()
            self._threads.append(thread)

        for thread in self._threads:
            thread.join()
        self.on_event("queue_finished", self)

    def _run_task(self, task: Task) -> None:
        try:
            if self._cancel.is_set():
                task.state = State.CANCELLED
                return

            cmd = task.command
            task.state = State.RUNNING
            task.started_at = time.time()
            self.on_event("task_started", task)

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
                task.state = State.CANCELLED
            elif proc.returncode == 0:
                task.state = State.DONE
                task.progress = 100
            else:
                task.state = State.FAILED

            self.on_event("task_finished", task)
        finally:
            task._proc = None
            self._slots.release()

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
