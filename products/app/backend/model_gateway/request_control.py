"""Cooperative cancellation and deadline control for one model request."""

from __future__ import annotations

from threading import Event, Lock
from time import monotonic
from typing import Callable


class ModelRequestCancelled(RuntimeError):
    pass


class ModelRequestControl:
    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("model request timeout must be positive")
        self._cancelled = Event()
        self._deadline = monotonic() + timeout_seconds
        self._reason = ""
        self._lock = Lock()
        self._cancel_listeners: list[Callable[[str], object]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str) -> None:
        normalized = str(reason or "cancelled")[:80]
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = normalized
            self._cancelled.set()
            listeners = tuple(self._cancel_listeners)
            self._cancel_listeners.clear()
        for listener in listeners:
            try:
                listener(normalized)
            except Exception:
                # Cancellation must not be blocked by an observer.
                continue

    def add_cancel_listener(self, listener: Callable[[str], object]) -> Callable[[], None]:
        """Register a best-effort callback and return its removal function."""

        if not callable(listener):
            raise TypeError("cancel listener must be callable")
        call_now = False
        reason = "cancelled"
        with self._lock:
            if self._cancelled.is_set():
                call_now = True
                reason = self._reason or reason
            else:
                self._cancel_listeners.append(listener)

        if call_now:
            try:
                listener(reason)
            except Exception:
                pass

        def remove() -> None:
            with self._lock:
                try:
                    self._cancel_listeners.remove(listener)
                except ValueError:
                    pass

        return remove

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        """Wait for cancellation without polling the provider implementation."""

        return self._cancelled.wait(None if timeout is None else max(0.0, float(timeout)))

    def remaining_seconds(self, maximum: float) -> float:
        return max(0.05, min(maximum, self._deadline - monotonic()))

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ModelRequestCancelled(self.reason or "model request cancelled")
