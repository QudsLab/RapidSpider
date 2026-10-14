"""procguard.py - every process RapidSpider starts dies with it.

Laya, the web interface and the RAM watchdog run inside the Python process, so they already stop with it.
What can outlive it are the child processes: the Playwright driver and the Chromium browsers (headless and headed).

    Windows  the program joins a Job Object marked KILL_ON_JOB_CLOSE. Every child inherits that job, so when this
             process ends for ANY reason (Ctrl+C, window closed, task manager, crash) Windows kills all of them.
    Linux/macOS  children are killed on normal exit, Ctrl+C, SIGTERM and SIGHUP. A hard SIGKILL cannot be caught,
             so on those systems a SIGKILLed run may leave a browser behind.
"""
import atexit
import os
import signal
import sys

_job = None          # keep the handle alive for the whole run: closing it is what kills the children


def _windows_job():
    global _job
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # ExtendedLimitInformation
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    _job = job


def kill_children():
    """terminate every child (and grandchild) of this process; kill what is still alive after 3 seconds."""
    try:
        import psutil
        kids = psutil.Process(os.getpid()).children(recursive=True)
        for p in kids:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(kids, timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
    except Exception:
        pass


def install(log=None):
    """call once, first thing in main(), before any browser is started."""
    say = (log.info if log else print)
    if os.name == "nt":
        try:
            _windows_job()
        except Exception as e:                  # e.g. an outer job that forbids it: fall back to the exit hooks
            say(f"procguard: job object not available ({e}); children are closed on normal exit only")
    atexit.register(kill_children)

    def _bye(signum, _frame):
        kill_children()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):          # SIGINT is left to Python (KeyboardInterrupt) -> atexit
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _bye)
            except (ValueError, OSError):
                pass
