"""iFind API字段契约测试；只使用内存fixture，不联网、不解析真实凭据。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "datafetcher" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.datafetcher.config import DataFetcherConfig
from modules.datafetcher.models import SecretRef
from modules.datafetcher.providers.base import ProviderUnauthorized
from modules.datafetcher.providers.ifind_http import IFindHttpProvider, download_history


class IFindHttpContractTest(unittest.TestCase):
    def test_requested_volume_is_downloaded_with_raw_ohlc(self) -> None:
        payload = {"tables": [{
            "thscode": "510300.SH", "time": ["2024-01-02"],
            "table": {"open": [3.0], "high": [3.1], "low": [2.9], "close": [3.05], "volume": [12345]},
        }]}
        requests: list[tuple[str, int]] = []

        def fake_history(_token, *, code, indicators, start_date, end_date, cps):
            self.assertEqual((code, start_date, end_date), ("510300.SH", "2024-01-02", "2024-01-02"))
            requests.append((indicators, cps))
            return payload

        with patch("modules.datafetcher.providers.ifind_http.history_response", new=fake_history):
            frame = download_history(
                access_token="fake", code="510300.SH", start_date="2024-01-02", end_date="2024-01-02",
                asset_type="etf", adjustment="none", include_volume=True,
            )
        self.assertEqual(requests, [("open,high,low,close,volume", 1)])
        self.assertEqual(frame.loc[0, "volume"], 12345)

    def test_host_secret_contains_only_refresh_token(self) -> None:
        reference = SecretRef("host-secret", "ifind")
        config = DataFetcherConfig(
            ifind_secret_ref=reference,
            ifind_secret_port=lambda _ref: '{"refresh_token":"refresh-fixture"}',
        )
        self.assertEqual(IFindHttpProvider._refresh_token(config), "refresh-fixture")

    def test_host_secret_rejects_persisted_access_token(self) -> None:
        reference = SecretRef("host-secret", "ifind")
        config = DataFetcherConfig(
            ifind_secret_ref=reference,
            ifind_secret_port=lambda _ref: '{"access_token":"forbidden","refresh_token":"refresh-fixture"}',
        )
        with self.assertRaisesRegex(ProviderUnauthorized, "仅接受Refresh Token"):
            IFindHttpProvider._refresh_token(config)

    def test_environment_fallback_reads_refresh_token_only(self) -> None:
        with patch.dict("os.environ", {"IFIND_REFRESH_TOKEN": "refresh-from-env", "IFIND_ACCESS_TOKEN": "ignored"}):
            self.assertEqual(IFindHttpProvider._refresh_token(DataFetcherConfig()), "refresh-from-env")


if __name__ == "__main__":
    unittest.main()
