"""Platform-neutral launcher for the real native desktop WebView shell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class WebViewApp:
    """Launch an already-built native App bundle without a browser fallback."""

    bundle: Path

    def executable(self) -> Path:
        value = self.bundle / "Contents" / "MacOS" / "OptionHelper"
        if not value.is_file() or not value.stat().st_mode & 0o111:
            raise FileNotFoundError("OptionHelper.app缺少可执行WKWebView壳")
        return value

    def launch(self) -> subprocess.Popen[bytes]:
        return subprocess.Popen([str(self.executable())])
