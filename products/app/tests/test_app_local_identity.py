"""Local development identities must never be exposed on a network listener."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.errors import ValidationError


class LocalIdentityBoundaryTests(unittest.TestCase):
    def test_local_runtime_refuses_non_loopback_listener(self) -> None:
        with self.assertRaises(ValidationError):
            AppServer(host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
