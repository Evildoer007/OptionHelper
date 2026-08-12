"""macOS native window source used by the OptionHelper bundle build.

The application window itself is implemented in ``OptionHelperApp.swift`` so
it can use the system WKWebView without a third-party Python bridge.  This
small Python model gives the platform launcher a testable, explicit command
contract instead of retaining the previous unavailable placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MacOSAppWindow:
    """Describe the native shell executable and the loopback URL it loads."""

    executable: Path
    url: str

    def command(self) -> tuple[str, ...]:
        if self.executable.name != "OptionHelper":
            raise ValueError("macOS application executable must be named OptionHelper")
        if not self.url.startswith("http://127.0.0.1:"):
            raise ValueError("macOS App may only load its loopback App Host")
        return (str(self.executable),)
