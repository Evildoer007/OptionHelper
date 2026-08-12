"""DataFetcher服务可发现性测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "datafetcher" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.datafetcher.service import capability


class DataFetcherServiceTest(unittest.TestCase):
    def test_service_is_explicitly_available_without_exposing_credentials(self) -> None:
        status = capability()
        self.assertTrue(status["ok"])
        self.assertEqual(status["module"], "datafetcher")
        self.assertEqual(status["status"], "available")
        self.assertEqual(status["credentials"], "environment_or_secret_ref_only")
