"""Starting MediaMTX and FFmpeg without console windows, tied to the app:
on Windows every child goes in a job object that closes with the app, so a
crash never leaves an FFmpeg streaming (or MediaMTX holding port 1935)."""

import subprocess
import sys

from .log import log

NO_WINDOW = 0x08000000
_job = None


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    return lambda proc: kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))


def popen(args, **kwargs):
    """subprocess.Popen, hidden and tied to the app's lifetime."""
    global _job
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", NO_WINDOW)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, **kwargs)
    if sys.platform == "win32":
        try:
            if _job is None:
                _job = _windows_job()
            _job(proc)
        except Exception as e:  # best effort: the app still stops them on a normal exit
            log(f"Couldn't tie {args[0]} to the app ({e})", "warn")
            _job = lambda proc: None
    return proc


def run(args, timeout):
    """Run a short command hidden; returns (exit code, output)."""
    kwargs = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
    r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, **kwargs)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def stop(proc, timeout=3):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout)
