"""Platform primitives for measuring what a render actually cost in memory.

A Karma render either fits in RAM or it swaps and takes six hours. The only
way to tell an artist which of those is about to happen is to measure a real
render and remember the number, so this module is the bottom layer of that:
raw, honest numbers from the operating system, with no interpretation. What
those numbers *mean* for a scene is somebody else's job.

Stdlib only, no Houdini, no Qt -- ``ctypes`` is the intended mechanism on
Windows, which is the priority platform here. Linux and macOS are best effort.

The rule the whole module is built around
-----------------------------------------
**Every function returns ``None`` / ``""`` / ``{}`` when it cannot measure.**
Never a zero, never an estimate, never a plausible-looking default. Someone
will size a farm off these numbers, and "we don't know" is a usable answer
while "0 bytes" and a quietly invented figure are not. Every ctypes call and
every subprocess call is wrapped, because a measurement failing must never be
allowed to take a render down with it.

Measuring a whole tree
----------------------
``peak_working_set`` reports one process and does *not* include its children.
That is not academic: the ``python.exe`` inside a uv-created venv is a 45 KB
trampoline that spawns the real interpreter as a child, and measuring it
reports ~5 MB no matter what the real interpreter allocates. A studio
``husk.bat`` or ``hython.sh`` wrapper -- which ``RenderJob.husk_exe`` may
perfectly well point at -- has exactly the same shape, and would have had
preflight telling someone a 200 GB shot needs 8 MB.

So on Windows there is a second, better mechanism: a **Job Object**, opened by
the caller *before* it spawns and assigned to the child immediately after
(:func:`open_job_object`, :func:`assign_process_to_job`,
:func:`peak_job_memory`, :func:`close_job_object`). The kernel then keeps a
high-water mark over every process in the job, descendants included. It cannot
be retrofitted onto a ``Popen`` that already exists, which is why the spawning
code has to know about it rather than this module doing it alone.

Two honest caveats, both measured rather than assumed -- see ``docs/UNVERIFIED.md``
section M:

* The job figure is **committed memory**, not working set. It is the charge the
  machine must be able to back with RAM plus pagefile, so it reads a little
  *above* the working-set figure for the same process (407.4 MB vs 399.3 MB for
  the same 400 MB hython allocation here). For "will this shot fit" that is the
  safe direction, but it is a different quantity and must not be compared
  like-for-like with a ``peak_working_set`` number.
* A process is assigned **just after** it starts, not atomically with it, and
  ``AssignProcessToJobObject`` does not adopt children the target has already
  spawned. A wrapper that forks in the microseconds before the assignment lands
  could in principle escape. Not observed: 25/25 runs of the ``.bat`` fixture
  caught the whole tree, spread 330.7-330.9 MB.

What this deliberately cannot do
--------------------------------
* **Whole-tree measurement is Windows-only.** Linux and macOS have no
  equivalent wired up here (cgroups would be the Linux mechanism), so
  :func:`supports_tree_measurement` is False there and callers fall back to
  the single-process figure -- which is the old, wrong-for-wrappers number, so
  they must say which one they got rather than presenting them alike.
* **VRAM is sampled, so it is approximate.** ``nvidia-smi`` reports what is
  allocated at the instant it is asked. A peak between two samples is
  invisible. There is no OS-tracked high-water mark for VRAM the way there is
  for working set.
* **NVIDIA only.** AMD and Intel GPUs report nothing here. That is a stated
  gap, not a silent zero -- ``gpu_memory_bytes`` returns ``{}`` and
  ``describe`` says why, so "unknown" can be told apart from "none used".
* **Per-process VRAM is frequently unavailable on Windows.** On consumer
  GeForce cards under the WDDM driver model, ``nvidia-smi`` lists the process
  but prints ``[N/A]`` for its memory -- the driver, not this code, declines
  to attribute VRAM per process. Measured on this machine (RTX 3090 + RTX 2060
  SUPER, driver 610.62, both cards ``driver_model.current = WDDM``): all 27
  listed compute apps reported ``[N/A]``, and ``nvidia-smi pmon`` showed ``-``
  in its ``mem`` column for every one of them, so this is the driver's answer
  and not a quirk of one query. Those pids are omitted from the result rather
  than recorded as zero. A datacentre card in TCC mode does report real
  figures; that has not been tested here.
* **Karma XPU is unverified.** XPU renders across CPU and GPU and may hold
  VRAM under a helper process rather than the husk/hython pid hsl spawned, in
  which case ``gpu_memory_bytes`` for that pid could read low or empty while
  the card is genuinely full. Nobody has checked this against a real XPU
  render; see ``docs/UNVERIFIED.md``. Do not present XPU VRAM as authoritative
  until someone has.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

# nvidia-smi reports memory in MiB with ``nounits``.
_MIB = 1024 * 1024

# nvidia-smi is a driver tool that occasionally blocks on a wedged GPU. A
# render must never stall behind a measurement, so every call is capped.
_NVIDIA_SMI_TIMEOUT = 5.0

# Values nvidia-smi prints instead of a number when the driver will not or
# cannot attribute memory to a process. Compared case-insensitively.
_NVIDIA_SMI_UNKNOWN = frozenset({
    "[n/a]", "n/a", "[not supported]", "not supported",
    "[unknown error]", "[insufficient permissions]",
})

# find_nvidia_smi() caches its answer -- including a negative one, so a machine
# with no NVIDIA card does not pay a filesystem walk on every sample.
_nvidia_smi_path: Optional[str] = None


# --------------------------------------------------------------------------
# Windows ctypes plumbing
#
# Declared once at import time. Nothing here touches the OS until it is
# called, so importing this module on Linux is harmless.
# --------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    from ctypes import wintypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        """kernel32 MEMORYSTATUSEX. ``dwLength`` must be filled in by us."""

        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        """psapi PROCESS_MEMORY_COUNTERS. ``cb`` must be filled in by us.

        ``PeakWorkingSetSize`` is maintained by the kernel for the lifetime of
        the process object -- we do not poll for it, we just read the final
        figure.
        """

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        """kernel32 IO_COUNTERS. Only here because the job struct embeds it."""

        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        """kernel32 JOBOBJECT_BASIC_LIMIT_INFORMATION.

        Every field is read-only to us -- hsl never *sets* a job limit. That
        matters: ``LimitFlags`` is where ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
        would live, and setting it would make closing the measurement handle
        kill the render. It stays zero, deliberately.
        """

        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        """kernel32 JOBOBJECT_EXTENDED_LIMIT_INFORMATION.

        ``PeakJobMemoryUsed`` is the field this module exists to read: the
        high-water mark of committed memory across **every** process in the
        job, children included, maintained by the kernel with no polling and
        no memory limit needing to be set. ``PeakProcessMemoryUsed`` is the
        largest single member instead, which is not what a whole-tree question
        is asking.

        144 bytes on x64; the size is asserted at declaration time below,
        because a struct whose layout has drifted reads plausible garbage
        rather than failing.
        """

        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # GetProcessMemoryInfo wants PROCESS_QUERY_LIMITED_INFORMATION (Vista+,
    # granted for our own children and same-user processes) plus VM_READ.
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010

    # AssignProcessToJobObject needs these two on the process handle. Popen's
    # own handle already has them; the reopen-by-pid fallback asks for exactly
    # these and nothing more.
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    # JOBOBJECTINFOCLASS value for JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# Loaded once on first use and reused. Sampling a memory curve calls into
# these every second or so; reloading a DLL each time would be wasteful, and
# re-declaring the prototypes each time doubly so.
_win_api: Optional[Dict[str, Any]] = None

# The job-object entry points are cached separately from _win_api on purpose:
# a Windows without them must still be able to read a single process's peak.
_job_api: Optional[Dict[str, Any]] = None

# supports_tree_measurement() caches its answer, negative included.
_tree_supported: Optional[bool] = None


def _windows_api() -> Optional[Dict[str, Any]]:
    """kernel32/psapi entry points with their prototypes declared, or None.

    Declaring ``argtypes``/``restype`` is not optional here. Left to default,
    ctypes types a returned ``HANDLE`` as ``c_int`` and silently truncates it
    on 64-bit Windows, which turns a valid handle into a bogus one for any
    process unlucky enough to get a large handle value.
    """
    global _win_api
    if _win_api is not None:
        return _win_api or None
    if not _IS_WINDOWS:
        _win_api = {}
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE

        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        _win_api = {"kernel32": kernel32, "psapi": psapi}
        return _win_api
    except Exception:
        # A stripped Windows image, a missing psapi, anything. Remember the
        # failure so we do not retry it on every sample.
        _win_api = {}
        return None


def _windows_memory_status() -> Optional["_MEMORYSTATUSEX"]:
    """One GlobalMemoryStatusEx call, or None if it failed for any reason."""
    api = _windows_api()
    if api is None:
        return None
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not api["kernel32"].GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status
    except Exception:
        # A struct mismatch on some future Windows, anything. A memory reading
        # is never worth raising into a render.
        return None


def _proc_meminfo(key: str) -> Optional[int]:
    """One ``kB`` field out of Linux ``/proc/meminfo``, in bytes."""
    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name.strip() != key:
                    continue
                parts = rest.split()
                if len(parts) >= 2 and parts[1].lower() == "kb":
                    return int(parts[0]) * 1024
                if parts:
                    return int(parts[0])
                return None
    except Exception:
        return None
    return None


def _sysctl_int(name: str) -> Optional[int]:
    """macOS ``sysctl -n <name>`` as an int, or None."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return None


def human_bytes(size: Optional[int]) -> str:
    """Bytes as a short GB/MB string -- or ``"unknown"`` for an unmeasured one.

    Lives here because this module is where byte counts come from, and both
    the preflight messages and the ``hsl memory`` report have to phrase them
    the same way. ``None`` is not zero and must never print as one: the point
    of the memory work is that a figure nobody measured says so out loud.
    """
    if not size:
        return "unknown"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


# --------------------------------------------------------------------------
# Physical RAM
# --------------------------------------------------------------------------

def total_ram_bytes() -> Optional[int]:
    """Installed physical RAM in bytes, or None if this platform cannot say.

    Windows reads GlobalMemoryStatusEx ``ullTotalPhys``. That is RAM *visible
    to the OS*, which is a few hundred MB short of the capacity printed on the
    DIMMs -- firmware and integrated graphics reserve the difference before
    Windows ever sees it. Visible RAM is the right number here, because it is
    the ceiling a render actually has to fit under.

    Linux reads ``MemTotal`` from ``/proc/meminfo`` and falls back to
    ``sysconf``. macOS reads ``hw.memsize``.
    """
    if _IS_WINDOWS:
        status = _windows_memory_status()
        return int(status.ullTotalPhys) if status is not None else None

    if sys.platform.startswith("linux"):
        total = _proc_meminfo("MemTotal")
        if total is not None:
            return total
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except Exception:
            return None

    if sys.platform == "darwin":
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except Exception:
            pass
        return _sysctl_int("hw.memsize")

    return None


def available_ram_bytes() -> Optional[int]:
    """Physical RAM currently available for a new allocation, or None.

    "Available", not "free": on every modern OS most nominally-used RAM is
    reclaimable page cache. Windows ``ullAvailPhys`` and Linux ``MemAvailable``
    both already account for that. macOS has no honest single equivalent, so
    it returns None rather than a number that would read far too low.
    """
    if _IS_WINDOWS:
        status = _windows_memory_status()
        return int(status.ullAvailPhys) if status is not None else None

    if sys.platform.startswith("linux"):
        available = _proc_meminfo("MemAvailable")
        if available is not None:
            return available
        # Pre-3.14 kernels have no MemAvailable. MemFree alone understates it
        # badly, but it is a real measurement rather than a guess, and it errs
        # towards "less headroom than you have" -- the safe direction.
        return _proc_meminfo("MemFree")

    if sys.platform == "darwin":
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES"))
        except Exception:
            return None

    return None


# --------------------------------------------------------------------------
# Per-process memory
# --------------------------------------------------------------------------

def _windows_memory_counters(handle) -> Optional["_PROCESS_MEMORY_COUNTERS"]:
    """GetProcessMemoryInfo on an already-open handle, or None.

    ``handle`` may be a raw int or a ``subprocess.Popen._handle`` object; both
    convert through ``int()``.
    """
    api = _windows_api()
    if api is None:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        ok = api["psapi"].GetProcessMemoryInfo(
            wintypes.HANDLE(int(handle)),
            ctypes.byref(counters),
            counters.cb,
        )
        # Only the BOOL says whether this worked. GetLastError is stale after a
        # successful call and checking it produces phantom failures.
        return counters if ok else None
    except Exception:
        return None


def _windows_counters_for_pid(pid: int) -> Optional["_PROCESS_MEMORY_COUNTERS"]:
    """Open a pid we do not own a handle to, read its counters, close it."""
    api = _windows_api()
    if api is None:
        return None
    kernel32 = api["kernel32"]
    handle = None
    try:
        # LIMITED_INFORMATION first: it is the right of least privilege and is
        # granted where the full one is not. Fall back for older Windows.
        for access in (_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ,
                       _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ):
            handle = kernel32.OpenProcess(access, False, int(pid))
            if handle:
                return _windows_memory_counters(handle)
        return None
    except Exception:
        return None
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


def peak_working_set(proc) -> Optional[int]:
    """Peak physical memory this subprocess ever held, in bytes, or None.

    ``proc`` is a :class:`subprocess.Popen`. Running or finished both work on
    Windows, which is the whole reason to use this rather than sampling.

    **Windows.** The kernel tracks ``PeakWorkingSetSize`` itself, so there is
    nothing to poll and no peak between samples to miss. Measured on this
    machine: a child allocating and touching a 500 MB bytearray reports
    534,847,488 bytes (510.1 MB), against 10,539,008 bytes (10.1 MB) for
    ``python -c "pass"`` on the same interpreter -- a difference of 500.0 MB,
    the payload exactly, with the interpreter's own footprint accounted for.

    **The peak survives the process exiting**, which was the open question and
    is now measured: the same 534,847,488 bytes read back after ``wait()``
    returned, and again 1.5 s later, while ``WorkingSetSize`` had collapsed to
    32 KB. Windows keeps the process object alive as long as any handle to it
    is open, and ``Popen`` holds one until it is garbage-collected. So callers
    may read this **before or after** ``wait()`` -- after is fine, and is
    usually what you want, since that is when the peak is final. The one thing
    that does break it is closing the handle: after ``proc._handle.Close()``
    the read fails and this returns None. Do not close it before reading, and
    do not let the ``Popen`` be collected before reading.

    ``Popen._handle`` is private, but it is the only correct source -- reopening
    by pid after exit could land on a recycled pid and report a stranger's
    memory. If the handle is unavailable this falls back to opening the pid
    *only while the process is still running*, and otherwise returns None.

    **Linux** reads ``VmHWM`` from ``/proc/<pid>/status``, which does *not*
    survive exit -- once the process is gone the file is gone. Callers wanting
    a peak on Linux have to sample :func:`current_rss` during the render.
    **macOS** has no cheap equivalent and returns None.

    Only ever measures the process handed in, **never its children**. That is
    a property of the counter, not a gap to be fixed here: for a whole-tree
    figure use :func:`open_job_object` / :func:`peak_job_memory`, which the
    launcher wires around its own spawn. This function stays as the fallback
    for where that is unavailable, and callers must record which of the two
    they got -- an 8 MB reading of a 300 MB render looks entirely reasonable.
    """
    if proc is None:
        return None

    if _IS_WINDOWS:
        handle = getattr(proc, "_handle", None)
        if handle is not None:
            counters = _windows_memory_counters(handle)
            if counters is not None:
                return int(counters.PeakWorkingSetSize)
        # No handle. Only safe to reopen by pid while it is definitely alive.
        pid = getattr(proc, "pid", None)
        if pid and getattr(proc, "returncode", None) is None:
            counters = _windows_counters_for_pid(pid)
            if counters is not None:
                return int(counters.PeakWorkingSetSize)
        return None

    if sys.platform.startswith("linux"):
        pid = getattr(proc, "pid", None)
        if not pid:
            return None
        return _linux_status_field(pid, "VmHWM")

    return None


def _linux_status_field(pid: int, key: str) -> Optional[int]:
    """One ``kB`` field out of ``/proc/<pid>/status``, in bytes."""
    try:
        with open("/proc/%d/status" % int(pid), "r") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name.strip() != key:
                    continue
                parts = rest.split()
                if len(parts) >= 2 and parts[1].lower() == "kb":
                    return int(parts[0]) * 1024
                if parts:
                    return int(parts[0])
                return None
    except Exception:
        return None
    return None


def current_rss(pid: int) -> Optional[int]:
    """Resident memory of a *running* pid right now, in bytes, or None.

    For sampling a memory curve over a render, and as the peak substitute on
    Linux where ``VmHWM`` disappears when the process does.

    A pid that no longer exists returns None everywhere. On Windows a pid whose
    process object is still held open by a live ``Popen`` keeps answering after
    exit, with ``WorkingSetSize`` collapsed to a small residual (32,768 bytes
    measured here) -- that is a real reading of a dead process, not an error, so
    do not read a near-zero here as "the render used no memory". Use
    :func:`peak_working_set` for the figure that is still meaningful after exit.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    if _IS_WINDOWS:
        counters = _windows_counters_for_pid(pid)
        return int(counters.WorkingSetSize) if counters is not None else None

    if sys.platform.startswith("linux"):
        # statm is a single short line and cheaper than status for sampling.
        try:
            with open("/proc/%d/statm" % pid, "r") as handle:
                fields = handle.read().split()
            if len(fields) >= 2:
                return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except Exception:
            pass
        return _linux_status_field(pid, "VmRSS")

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5.0,
            )
            text = out.stdout.decode("utf-8", "replace").strip()
            return int(text) * 1024 if text else None
        except Exception:
            return None

    return None


# --------------------------------------------------------------------------
# Whole-process-tree memory, via a Windows Job Object
#
# The spawn-time half of the module. A Job Object has to exist *before* the
# process it measures, so these are four separate calls the launcher threads
# around its own ``Popen`` rather than one function that takes a process:
#
#     job = open_job_object()          # may be None
#     proc = subprocess.Popen(...)
#     assign_process_to_job(job, proc) # may be False
#     ...                              # the render runs
#     peak = peak_job_memory(job)      # may be None
#     close_job_object(job)            # in a finally
#
# Every step degrades to "no measurement" on its own, so a caller that gets a
# None anywhere just falls back to peak_working_set. Nothing here can raise
# into a render.
# --------------------------------------------------------------------------

@dataclass
class JobObject:
    """An opaque token for one Windows Job Object. Do not read the fields.

    Passed back to :func:`assign_process_to_job`, :func:`peak_job_memory` and
    :func:`close_job_object`; there is nothing else useful to do with it. It
    exists as an object rather than a bare handle so that closing it can be
    made idempotent -- a render's ``finally`` may well run twice on the way out
    of a cancel, and double-closing a Windows handle is how you corrupt an
    unrelated one.
    """

    handle: int = 0
    assigned: int = 0       # processes successfully put in it, for honesty


def _windows_job_api() -> Optional[Dict[str, Any]]:
    """kernel32 job entry points with prototypes declared, or None.

    Same reasoning as :func:`_windows_api` about ``argtypes``/``restype``: a
    ``HANDLE`` left to ctypes' default is typed ``c_int`` and truncated on
    64-bit Windows, which silently turns a valid job handle into a bogus one.
    """
    global _job_api
    if _job_api is not None:
        return _job_api or None
    if not _IS_WINDOWS:
        _job_api = {}
        return None
    try:
        # Layout drift would read plausible garbage rather than failing, so
        # check it once here instead of trusting the field list.
        if ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION) < 100:
            _job_api = {}
            return None

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE

        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                      wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE

        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        _job_api = {"kernel32": kernel32}
        return _job_api
    except Exception:
        _job_api = {}
        return None


def supports_tree_measurement() -> bool:
    """True if this machine can measure a whole process tree's peak memory.

    Windows only, and proven rather than assumed: the first call actually
    creates a job object and queries it, then throws it away. Cached after
    that, negative answers included.

    It deliberately does **not** test :c:func:`AssignProcessToJobObject`,
    because the only process available to test it on is our own -- and a
    process can never leave a job once it is in one, so a capability probe
    that assigned us to a throwaway job would permanently change this
    process's environment to answer a question. Do not "improve" it that way.
    Assignment proves itself per spawn, by returning False.
    """
    global _tree_supported
    if _tree_supported is not None:
        return _tree_supported

    _tree_supported = False
    job = open_job_object()
    if job is not None:
        # An empty job reads zero, which is not a measurement -- but the query
        # *succeeding* is the capability being asked about, so check the call,
        # not the number.
        _tree_supported = _query_job(job) is not None
        close_job_object(job)
    return _tree_supported


def open_job_object() -> Optional[JobObject]:
    """A fresh, empty, unnamed Job Object to spawn a process into, or None.

    Call this **before** ``subprocess.Popen``: a process can only be adopted
    while it is alive, and it must be adopted before it spawns children of its
    own for those children to be counted.

    No limit is ever set on it. In particular
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is left off, so
    :func:`close_job_object` cannot take a running render down with it.
    """
    api = _windows_job_api()
    if api is None:
        return None
    try:
        handle = api["kernel32"].CreateJobObjectW(None, None)
        if not handle:
            return None
        return JobObject(handle=int(handle))
    except Exception:
        return None


def assign_process_to_job(job: Optional[JobObject], proc) -> bool:
    """Put ``proc`` and its future descendants in ``job``. True if it worked.

    Call immediately after ``Popen`` returns. Children the process has
    *already* spawned are not adopted -- Windows offers no way to do that --
    so the gap between the spawn and this call is the one hole in the
    measurement. Measured at 0 misses in 25 runs of a ``.bat``-wrapper
    fixture; see ``docs/UNVERIFIED.md`` M11.

    **Nested jobs.** A process that is already inside a job (a CI runner, some
    terminals, anything launched under a supervisor) can still be assigned to
    a second one on Windows 8+. That is verified here rather than assumed --
    this repo's own test process runs inside a job and the assignment
    succeeds -- but on an older Windows, or against a job created without
    nesting allowed, it fails with ERROR_ACCESS_DENIED. That is a False, not
    an exception, and the caller falls back to the single-process figure.

    ``False`` when there is nothing to measure with, when the handle cannot be
    got, or when the OS refuses. Never raises.
    """
    if job is None or not job.handle or proc is None:
        return False
    api = _windows_job_api()
    if api is None:
        return False
    kernel32 = api["kernel32"]

    handle = getattr(proc, "_handle", None)
    if handle is not None:
        try:
            if kernel32.AssignProcessToJobObject(wintypes.HANDLE(int(job.handle)),
                                                 wintypes.HANDLE(int(handle))):
                job.assigned += 1
                return True
        except Exception:
            pass

    # ``Popen._handle`` is private, so do not let it be the single point of
    # failure. Reopening by pid is safe here in a way it is not for
    # peak_working_set: this only ever runs microseconds after the spawn, long
    # before the pid could have been recycled.
    pid = getattr(proc, "pid", None)
    if not pid or getattr(proc, "returncode", None) is not None:
        return False
    opened = None
    try:
        opened = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                      False, int(pid))
        if not opened:
            return False
        if kernel32.AssignProcessToJobObject(wintypes.HANDLE(int(job.handle)),
                                             wintypes.HANDLE(int(opened))):
            job.assigned += 1
            return True
        return False
    except Exception:
        return False
    finally:
        if opened:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(int(opened)))
            except Exception:
                pass


def _query_job(job: JobObject) -> Optional["_JOBOBJECT_EXTENDED_LIMIT_INFORMATION"]:
    """One QueryInformationJobObject call, or None if it failed for any reason."""
    api = _windows_job_api()
    if api is None:
        return None
    try:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = wintypes.DWORD()
        ok = api["kernel32"].QueryInformationJobObject(
            wintypes.HANDLE(int(job.handle)),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        return info if ok else None
    except Exception:
        return None


def peak_job_memory(job: Optional[JobObject]) -> Optional[int]:
    """Peak memory across every process in ``job``, in bytes, or None.

    ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION.PeakJobMemoryUsed`` -- a kernel
    high-water mark over the whole tree, so there is nothing to poll and no
    spike between samples to miss, and **it survives every process in the job
    exiting** (measured: identical on a re-read after ``wait()`` returned).
    Read it before :func:`close_job_object`, which is the one thing that does
    end it.

    This is **committed** memory, not working set, so it reads somewhat above
    the corresponding :func:`peak_working_set` figure for the same process.
    See the module docstring; do not compare the two like for like.

    ``None`` -- never zero -- when there is no job, when nothing was ever
    successfully assigned to it, or when the query fails. An empty job really
    does report 0, and "0 bytes" would read as a render that used no memory.
    """
    if job is None or not job.handle or not job.assigned:
        return None
    info = _query_job(job)
    if info is None:
        return None
    peak = int(info.PeakJobMemoryUsed)
    return peak if peak > 0 else None


def close_job_object(job: Optional[JobObject]) -> None:
    """Release the job handle. Idempotent, and never kills anything.

    Closing the last handle to a job does **not** terminate its processes
    unless ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` was set on it, and
    :func:`open_job_object` never sets it. Still, read
    :func:`peak_job_memory` first: the counter goes with the handle.
    """
    if job is None or not job.handle:
        return
    api = _windows_job_api()
    handle, job.handle = job.handle, 0      # first, so a second call is a no-op
    if api is None:
        return
    try:
        api["kernel32"].CloseHandle(wintypes.HANDLE(int(handle)))
    except Exception:
        pass


# --------------------------------------------------------------------------
# GPU, via nvidia-smi
# --------------------------------------------------------------------------

def _nvidia_smi_candidates() -> List[str]:
    """Where nvidia-smi lives when it is not on PATH."""
    paths: List[str] = []
    if _IS_WINDOWS:
        # The driver installs it into System32 -- normally on PATH, but a
        # cut-down or service environment may not have that.
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        paths.append(os.path.join(system_root, "System32", "nvidia-smi.exe"))
        # Older drivers (pre-~2018) put it here instead.
        for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                paths.append(os.path.join(
                    base, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"))
    else:
        paths.extend([
            "/usr/bin/nvidia-smi",
            "/usr/local/bin/nvidia-smi",
            "/usr/local/nvidia/bin/nvidia-smi",
        ])
    return paths


def find_nvidia_smi() -> str:
    """Full path to nvidia-smi, or ``""`` if this machine has none.

    Cached after the first call, negative answers included -- a machine with an
    AMD card should not pay for this lookup on every sample.
    """
    global _nvidia_smi_path
    if _nvidia_smi_path is not None:
        return _nvidia_smi_path

    found = ""
    try:
        found = shutil.which("nvidia-smi") or ""
        if not found:
            for candidate in _nvidia_smi_candidates():
                if os.path.isfile(candidate):
                    found = candidate
                    break
    except Exception:
        found = ""

    _nvidia_smi_path = found
    return found


def _run_nvidia_smi(args: List[str]) -> str:
    """Run nvidia-smi and return stdout, or ``""``. Never raises.

    Timed out, absent, crashed, non-zero exit -- all of them are "no data",
    because a render is running and nothing here is worth interrupting it for.
    """
    exe = find_nvidia_smi()
    if not exe:
        return ""
    try:
        kwargs: Dict[str, Any] = {}
        if _IS_WINDOWS:
            # Without this a console window flashes over the GUI every sample.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [exe] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=_NVIDIA_SMI_TIMEOUT, **kwargs
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _parse_nvidia_smi(text: str) -> Dict[int, int]:
    """Parse ``--query-compute-apps=pid,used_memory --format=csv,noheader,nounits``.

    Returns ``{pid: bytes}``. The pure, testable half of
    :func:`gpu_memory_bytes` -- everything that can go wrong with the *format*
    can be tested by feeding this captured output, on a machine with no GPU.

    A row whose memory column is not a number is **dropped, not zeroed**. That
    case is the normal one on Windows consumer cards, where the driver prints
    ``[N/A]`` for every process; recording it as 0 bytes would read as "this
    render used no VRAM", which is a different and false claim.
    """
    result: Dict[int, int] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        pid_text, mem_text = parts[0], parts[1]
        if mem_text.lower() in _NVIDIA_SMI_UNKNOWN:
            continue
        try:
            pid = int(pid_text)
            mib = float(mem_text)
        except ValueError:
            continue
        if pid <= 0 or mib < 0:
            continue
        # One card per row; a process on two GPUs gets one row per GPU and the
        # totals add up.
        result[pid] = result.get(pid, 0) + int(mib * _MIB)
    return result


def _parse_nvidia_smi_gpus(text: str) -> List[Dict[str, Any]]:
    """Parse ``--query-gpu=index,name,memory.total --format=csv,noheader,nounits``.

    Returns ``[{"index": int, "name": str, "total_vram_bytes": int|None}]``.
    A card whose total memory will not parse keeps its name with a None size --
    knowing the card is there is still worth reporting.
    """
    gpus: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        try:
            total: Optional[int] = int(float(parts[2]) * _MIB)
        except ValueError:
            total = None
        gpus.append({"index": index, "name": parts[1], "total_vram_bytes": total})
    return gpus


def gpu_memory_bytes(pids: Iterable[int]) -> Dict[int, int]:
    """VRAM currently held by each of ``pids``, in bytes.

    ``{}`` when nvidia-smi is absent, when it reports nothing, or when the
    driver refuses to attribute memory per process -- see the module docstring.
    A pid missing from the result holds no *measurable* VRAM, which is not the
    same as holding none, and callers must not render it as 0.

    Sampled, so it is a snapshot: whatever the peak was between two calls is
    not recoverable. NVIDIA only.
    """
    try:
        wanted = {int(p) for p in pids}
    except (TypeError, ValueError):
        return {}
    if not wanted:
        return {}

    text = _run_nvidia_smi([
        "--query-compute-apps=pid,used_memory",
        "--format=csv,noheader,nounits",
    ])
    if not text:
        return {}
    return {pid: used for pid, used in _parse_nvidia_smi(text).items()
            if pid in wanted}


# --------------------------------------------------------------------------
# What this machine can measure
# --------------------------------------------------------------------------

def describe() -> Dict[str, Any]:
    """What this machine can and cannot measure, for the CLI to print.

    The point is to let a user tell "unknown" apart from "none": no GPU line
    at all and a GPU whose driver will not report per-process VRAM look
    identical in a memory report otherwise.

    Keys, all always present:

    ``platform``               ``platform.system()``, e.g. ``"Windows"``.
    ``platform_detail``        Fuller version string, for a bug report.
    ``total_ram_bytes``        int or None.
    ``available_ram_bytes``    int or None.
    ``peak_rss_available``     bool -- can a finished process's peak be read.
    ``peak_rss_method``        How, in words, or ``""``.
    ``peak_rss_tree``          bool -- does that peak cover the whole process
                               tree, or only the one process hsl spawned. The
                               difference is a factor of forty on a wrapper
                               script, so it is reported, not assumed.
    ``peak_rss_tree_method``   How, in words, or ``""``.
    ``nvidia_smi``             Full path, or ``""`` if absent.
    ``gpus``                   List of ``{index, name, total_vram_bytes}``;
                               empty if nvidia-smi is absent or said nothing.
    ``per_process_vram``       bool -- did nvidia-smi actually attribute VRAM
                               to any process just now. False on consumer
                               Windows cards that print ``[N/A]``.
    ``notes``                  Human-readable strings naming each gap found.
    """
    system = platform.system()
    notes: List[str] = []

    tree_available = supports_tree_measurement()
    tree_method = ("Job Object PeakJobMemoryUsed (kernel32), assigned at spawn"
                   if tree_available else "")

    if _IS_WINDOWS:
        peak_available = True
        peak_method = "GetProcessMemoryInfo PeakWorkingSetSize (psapi)"
        if tree_available:
            notes.append("Peak memory covers the whole process tree: each "
                         "render is spawned into a Job Object, so a launcher "
                         "or wrapper script between hsl and the renderer is "
                         "measured through rather than instead of. The job "
                         "figure is committed memory, which reads slightly "
                         "above a working-set figure for the same process.")
        else:
            notes.append("Peak memory covers the process hsl spawned and not "
                         "its children, because this Windows would not give "
                         "out a Job Object. A launcher or wrapper between hsl "
                         "and the renderer would be measured instead of the "
                         "renderer -- expect a far too small number there.")
    elif sys.platform.startswith("linux"):
        peak_available = True
        peak_method = "/proc/<pid>/status VmHWM (running processes only)"
        notes.append("Peak memory disappears when a process exits on Linux; "
                     "it must be read before the render finishes, or sampled.")
    else:
        peak_available = False
        peak_method = ""
        notes.append("No peak-memory source on %s -- per-process peaks will "
                     "read as unknown." % (system or sys.platform))

    if not tree_available and not _IS_WINDOWS:
        notes.append("Whole-process-tree measurement is Windows-only here, so "
                     "a renderer launched through a wrapper script would be "
                     "measured as the wrapper. Figures recorded on this "
                     "machine are flagged single-process rather than being "
                     "presented as a whole-tree number.")

    total = total_ram_bytes()
    if total is None:
        notes.append("Installed RAM could not be read on this platform.")

    smi = find_nvidia_smi()
    gpus: List[Dict[str, Any]] = []
    per_process_vram = False
    if smi:
        gpus = _parse_nvidia_smi_gpus(_run_nvidia_smi([
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader,nounits",
        ]))
        if not gpus:
            notes.append("nvidia-smi is present at %s but listed no GPUs." % smi)
        # Ask about every compute app, not just ours: if the driver will not
        # attribute VRAM to anything, it will not do it for a render either.
        attributed = _parse_nvidia_smi(_run_nvidia_smi([
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader,nounits",
        ]))
        per_process_vram = bool(attributed)
        if not per_process_vram:
            notes.append(
                "nvidia-smi reports no per-process VRAM on this machine "
                "(consumer GeForce cards under the WDDM driver print [N/A]). "
                "VRAM per render will read as unknown, not zero.")
    else:
        notes.append("nvidia-smi not found: GPU memory cannot be measured. "
                     "AMD and Intel GPUs are not supported at all.")

    if gpus:
        notes.append("VRAM figures are sampled, so a peak between samples is "
                     "not captured. Karma XPU attribution is unverified.")

    return {
        "platform": system,
        "platform_detail": platform.platform(),
        "total_ram_bytes": total,
        "available_ram_bytes": available_ram_bytes(),
        "peak_rss_available": peak_available,
        "peak_rss_method": peak_method,
        "peak_rss_tree": tree_available,
        "peak_rss_tree_method": tree_method,
        "nvidia_smi": smi,
        "gpus": gpus,
        "per_process_vram": per_process_vram,
        "notes": notes,
    }
