from __future__ import annotations

from dataclasses import dataclass
import unittest

from products.app.backend.authorization.roles import Role
from products.app.backend.errors import UserActionError
from products.app.backend.identity.session_identity import SessionIdentity
from products.app.backend.tool_gateway import ToolGateway
from runtime.contracts.contract_api import resolve_contract


@dataclass
class _RecordedDataAssets:
    calls: list[tuple[object, str | None, bool]]

    def resolve_for_compute(
        self,
        _identity: SessionIdentity,
        requested: object,
        *,
        asset_ids: tuple[str, ...],
        schema_id: str | None = None,
        optional: bool = False,
    ) -> dict[str, object] | None:
        self.calls.append((requested, schema_id, optional))
        return {
            "data_asset_id": "history" if schema_id == "market-history-v1" else "calendar",
            "schema_id": schema_id,
            "asset_ids": list(asset_ids),
        }


class _AutoDataAssets(_RecordedDataAssets):
    def __init__(self) -> None:
        super().__init__([])
        self.calendar_registered = False

    def resolve_for_compute(self, *args, **kwargs):
        schema_id = kwargs.get("schema_id")
        requested = args[1]
        if schema_id == "trading-calendar" and not self.calendar_registered:
            self.calls.append((requested, schema_id, bool(kwargs.get("optional", False))))
            raise UserActionError("market_data_required", "missing")
        return super().resolve_for_compute(*args, **kwargs)

    def register(self, _identity: SessionIdentity, asset: dict[str, object]) -> dict[str, object]:
        self.calendar_registered = True
        return asset


class _FakeDataFetcher:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def dispatch(self, request, _identity, *, request_id=""):
        self.requests.append({**request, "request_id": request_id})
        return {
            "ok": True,
            "data_asset_ref": {
                "data_asset_id": "calendar",
                "schema_id": "trading-calendar",
                "asset_ids": ["000300.SH"],
            },
        }


class TradingCalendarSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = SessionIdentity("admin", "local", Role.ADMIN, "session")

    def _gateway(self) -> tuple[ToolGateway, _RecordedDataAssets]:
        assets = _RecordedDataAssets([])
        gateway = object.__new__(ToolGateway)
        gateway._data_assets = assets
        return gateway, assets

    def test_vanilla_pricer_does_not_bind_an_unrequested_calendar(self) -> None:
        gateway, assets = self._gateway()
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {"identity": {"underlyings": ["000300.SH"]}, "pricing_config": {}},
            self.identity,
            {"resolved_contract": {"identity": {"product_id": "2.1", "underlyings": ["000300.SH"]}, "terms": {}}},
        )
        self.assertEqual([item["schema_id"] for item in refs], ["market-history-v1"])
        self.assertEqual([item[1] for item in assets.calls], ["market-history-v1"])

    def test_path_pricer_binds_history_and_calendar_by_schema(self) -> None:
        gateway, assets = self._gateway()
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {"identity": {"underlyings": ["000300.SH"]}, "pricing_config": {}},
            self.identity,
            {"resolved_contract": {"identity": {"product_id": "5.1", "underlyings": ["000300.SH"]}, "terms": {"pricing_methods": ["monte_carlo"], "monitor": {"frequency": "daily"}}}},
        )
        self.assertEqual(
            [item["schema_id"] for item in refs],
            ["market-history-v1", "trading-calendar"],
        )
        self.assertEqual([item[1] for item in assets.calls], ["market-history-v1", "trading-calendar"])

    def test_terminal_only_mc_does_not_fetch_a_future_calendar(self) -> None:
        gateway, assets = self._gateway()
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {
                "identity": {"underlyings": ["000300.SH"]},
                "pricing_config": {"model_method": "monte_carlo"},
            },
            self.identity,
            {"resolved_contract": {"identity": {"product_id": "6.1", "underlyings": ["000300.SH"]}, "terms": {"pricing_methods": ["monte_carlo"], "monitor": {}}}},
        )
        self.assertEqual([item["schema_id"] for item in refs], ["market-history-v1"])
        self.assertEqual([item[1] for item in assets.calls], ["market-history-v1"])

    def test_terminal_european_portfolio_mc_does_not_bind_a_future_calendar(self) -> None:
        gateway, assets = self._gateway()
        contract = resolve_contract(
            "3.1", identity={"underlyings": ["000300.SH"]},
        ).to_protocol_dict()
        self.assertNotIn("product_id", contract)
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {
                "identity": {"underlyings": ["000300.SH"]},
                "pricing_config": {"model_method": "monte_carlo"},
            },
            self.identity,
            {"resolved_contract": contract},
        )
        self.assertEqual([item["schema_id"] for item in refs], ["market-history-v1"])

    def test_real_protocol_monitor_contract_binds_calendar_without_root_product_id(self) -> None:
        gateway, assets = self._gateway()
        contract = resolve_contract(
            "5.1", identity={"underlyings": ["000300.SH"]},
        ).to_protocol_dict()
        self.assertNotIn("product_id", contract)

        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {"identity": {"underlyings": ["000300.SH"]}, "pricing_config": {}},
            self.identity,
            {"resolved_contract": contract},
        )

        self.assertEqual(
            [item["schema_id"] for item in refs],
            ["market-history-v1", "trading-calendar"],
        )

    def test_explicit_vanilla_calendar_is_preserved(self) -> None:
        gateway, assets = self._gateway()
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {
                "identity": {"product_id": "2.1", "underlyings": ["000300.SH"]},
                "pricing_config": {},
                "trading_calendar_ref": {"data_asset_id": "calendar"},
            },
            self.identity,
            {"resolved_contract": {"identity": {"product_id": "2.1", "underlyings": ["000300.SH"]}, "terms": {}}},
        )
        self.assertEqual(len(refs), 2)
        self.assertEqual(assets.calls[-1], ({"data_asset_id": "calendar"}, "trading-calendar", False))

    def test_path_pricer_automatically_prepares_missing_calendar(self) -> None:
        assets = _AutoDataAssets()
        fetcher = _FakeDataFetcher()
        gateway = object.__new__(ToolGateway)
        gateway._data_assets = assets
        gateway._datafetcher = fetcher
        refs = gateway._resolve_compute_data_refs(
            "pricer",
            {
                "identity": {"underlyings": ["000300.SH"]},
                "pricing_config": {"valuation_date": "2026-08-09"},
            },
            self.identity,
            {
                "resolved_contract": {
                    "identity": {"product_id": "5.1", "underlyings": ["000300.SH"], "contract_end_date": "2026-11-09"},
                    "terms": {"pricing_methods": ["monte_carlo"], "monitor": {"frequency": "daily"}},
                },
            },
            request_id="compute-request",
        )
        self.assertEqual([item["schema_id"] for item in refs], ["market-history-v1", "trading-calendar"])
        self.assertEqual(fetcher.requests[0]["action"], "fetch_calendar")
        self.assertEqual(fetcher.requests[0]["start_date"], "2026-07-26")
        self.assertEqual(fetcher.requests[0]["end_date"], "2026-11-23")
        self.assertNotIn("fields", fetcher.requests[0])
        self.assertNotIn("adjustment", fetcher.requests[0])


if __name__ == "__main__":
    unittest.main()
