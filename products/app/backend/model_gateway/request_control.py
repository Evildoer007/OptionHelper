"""Cooperative cancellation and deadline control for one model request."""

from __future__ import annotations

from threading import Event, Lock
from time import monotonic
from typing import Callable


class ModelRequestCancelled(RuntimeError):
    pass


class ModelRequestControl:
    def __init__(self, timeout_seconds: float, *, max_output_tokens: int | None = None,
                 max_duration_seconds: float | None = None, reasoning_effort: str | None = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("model request timeout must be positive")
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens <= 0):
            raise ValueError("model output budget must be a positive integer")
        if max_duration_seconds is not None and max_duration_seconds <= 0:
            raise ValueError("model total duration must be positive")
        if reasoning_effort not in {None, "none", "low", "high", "max"}:
            raise ValueError("invalid reasoning effort")
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self._absolute_deadline = monotonic() + max_duration_seconds if max_duration_seconds is not None else None
        self._cancelled = Event()
        self._deadline = monotonic() + timeout_seconds
        if self._absolute_deadline is not None:
            self._deadline = min(self._deadline, self._absolute_deadline)
        self._reason = ""
        self._lock = Lock()
        self._cancel_listeners: list[Callable[[str], object]] = []

    def refresh_deadline(self, timeout_seconds: float) -> None:
        """Extend a live stream's idle window without undoing cancellation."""
        with self._lock:
            if not self._cancelled.is_set():
                deadline = monotonic() + timeout_seconds
                self._deadline = min(deadline, self._absolute_deadline) if self._absolute_deadline is not None else deadline

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

    def remaining_seconds(self, maximum: float | None = None) -> float:
        remaining = self._deadline - monotonic()
        return max(0.05, remaining if maximum is None else min(maximum, remaining))

    def raise_if_cancelled(self) -> None:
        if not self.cancelled and monotonic() >= self._deadline:
            self.cancel("model_request_timeout")
        if self.cancelled:
            if self.reason == "model_request_timeout":
                raise TimeoutError("model request deadline exceeded")
            raise ModelRequestCancelled(self.reason or "model request cancelled")
