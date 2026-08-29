"""Cross-process exclusive file lock, shared by api/rate_limit.py and api/session_revocation.py.

A plain threading.Lock() only protects against races between threads WITHIN one process --
under multiple worker processes (e.g. `uvicorn api.server:app --workers 4`, a standard way to
scale for real traffic), each worker has its own separate Python interpreter and its own
separate Lock object, so a threading.Lock provides no protection at all between them. This is
the exact same class of gap guardrail/guardrail.py's mandate-store lock was built to close
(and where a real, reproducible double-spend race was found and fixed earlier this session) --
reused here via the same atomic-lockfile-creation technique rather than duplicating a third
copy of it.
"""
import os
import time
from contextlib import contextmanager

_ACQUIRE_TIMEOUT_SECONDS = 5
_STALE_SECONDS = 10  # a lock file older than this is assumed abandoned by a crashed process


@contextmanager
def file_lock(lock_path: str):
    """Exclusive lock via atomic lockfile creation (os.O_CREAT | O_EXCL is atomic on both
    Windows and POSIX). See guardrail/guardrail.py's _mandate_store_lock for the fuller
    rationale and the Windows-specific PermissionError handling this mirrors."""
    deadline = time.time() + _ACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - os.path.getmtime(lock_path) > _STALE_SECONDS:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
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
