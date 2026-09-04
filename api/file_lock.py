"""Cross-process exclusive file lock, shared by guardrail/guardrail.py, api/rate_limit.py, and
api/session_revocation.py.

A plain threading.Lock() only protects against races between threads WITHIN one process --
under multiple worker processes (e.g. `uvicorn api.server:app --workers 4`, a standard way to
scale for real traffic), each worker has its own separate Python interpreter and its own
separate Lock object, so a threading.Lock provides no protection at all between them. This is
the exact same class of gap guardrail/guardrail.py's mandate-store lock was built to close
(and where a real, reproducible double-spend race was found and fixed earlier this session) --
this is the ONE shared implementation; guardrail.py imports and uses this directly rather than
carrying its own second copy (an earlier version did, and the two copies had already drifted --
this module had a stale-lock PID-liveness check the other one lacked).
"""
import os
import sys
import time
from contextlib import contextmanager

_ACQUIRE_TIMEOUT_SECONDS = 5
_STALE_SECONDS = 10  # a lock file older than this is a CANDIDATE for reclamation -- never
                      # reclaimed on age alone, see _holder_is_alive below.


def _holder_is_alive(lock_path: str) -> bool:
    """Best-effort liveness check for the process that (as of the last successful acquire)
    holds lock_path, so an aged-but-still-genuinely-held lock can't be yanked out from under a
    holder that's merely slow (heavy contention, a loaded filesystem, AV scanning on Windows)
    rather than actually crashed. Age alone used to be the only signal a waiter had -- which
    meant two processes could both believe they held the lock at once whenever a single
    critical section happened to run past _STALE_SECONDS, silently reopening the exact race
    this lock exists to prevent.

    Returns True (treat as still held -- refuse to steal) whenever liveness can't be positively
    ruled out: missing/unreadable/unparseable lock content (an older lock file written before
    this check existed, or a benign read racing the holder's own cleanup) is NOT evidence of a
    crash, so it's treated the same as "alive" rather than as license to steal.
    """
    try:
        with open(lock_path, "r", encoding="ascii") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # no such process -- genuinely gone
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # no such process -- genuinely gone
    except PermissionError:
        return True  # exists, just owned by another user -- still alive
    return True


@contextmanager
def file_lock(lock_path: str):
    """Exclusive lock via atomic lockfile creation (os.O_CREAT | O_EXCL is atomic on both
    Windows and POSIX). The lock file's content is the holding process's PID, so a waiter that
    finds it older than _STALE_SECONDS can check _holder_is_alive before reclaiming it instead
    of trusting age alone."""
    deadline = time.time() + _ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - os.path.getmtime(lock_path) > _STALE_SECONDS and not _holder_is_alive(lock_path):
                    os.remove(lock_path)
                    continue
            except OSError:
                pass  # another process cleaned it up (or is mid-write) between our checks -- fine, just retry
            if time.time() > deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
