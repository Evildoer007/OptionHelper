"""Windows WebView2 shell command contract used by the packaged App."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowsAppWindow:
    executable: Path

    def command(self) -> tuple[str, ...]:
        if self.executable.name != "OptionHelper.exe":
            raise ValueError("Windows application executable must be named OptionHelper.exe")
        return (str(self.executable),)
