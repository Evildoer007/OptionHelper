"""Process-wide Numba thread control shared by all STANDARD CPU engines."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator


_NUMBA_THREAD_LOCK = RLock()


@contextmanager
def numba_thread_scope(threads: int) -> Iterator[None]:
    """Temporarily set Numba's process-wide thread count without cross-engine races."""
    from numba import get_num_threads, set_num_threads

    with _NUMBA_THREAD_LOCK:
        previous = get_num_threads()
        set_num_threads(threads)
        try:
            yield
        finally:
            set_num_threads(previous)


__all__ = ("numba_thread_scope",)
