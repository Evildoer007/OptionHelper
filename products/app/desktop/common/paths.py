"""Resolve platform user-data paths without creating or writing them."""

from pathlib import Path
import sys


class AppPaths:
    def current_user_data_dir(self) -> Path:
        return self.user_data_dir(sys.platform)

    def user_data_dir(self, platform: str) -> Path:
        if platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "OptionHelper"
        if platform == "win32":
            return Path.home() / "AppData" / "Local" / "OptionHelper"
        raise ValueError(f"Unsupported desktop platform: {platform}")
