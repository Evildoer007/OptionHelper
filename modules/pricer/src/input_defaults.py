"""Pricer页面与App正式输入共用的日期、参考价编译边界。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
from math import isclose
from typing import Any, Mapping, Sequence

from runtime.protocol.models import DataAssetRef

from .market_resolver import (
    MarketDataError,
    load_market_history_bytes,
    reference_prices_from_history,
)


class PricerInputDefaultError(ValueError):
    """Friendly input cannot be compiled into a data-backed Pricer contract."""


def local_valuation_date(today: date | None = None) -> str:
    """Return the device-local calendar date in the formal ISO wire format."""
    return (today or date.today()).isoformat()


def compile_pricer_input_defaults(
    request: Mapping[str, Any],
    *,
    data_refs: Sequence[Mapping[str, Any] | DataAssetRef] = (),
    data_store: Any | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Compile a friendly Pricer request without inventing a market reference.

    Every contract gets its valuation date and true contract start price from
    the sole bound DataAssetRef.  This compiler never manufactures a market
    reference from an unbound request.
    """
    if not isinstance(request, Mapping):
        raise PricerInputDefaultError("Pricer输入必须为对象")
    values = deepcopy(dict(request))
    auto_contract_start_date = values.pop("auto_contract_start_date", False)
    if not isinstance(auto_contract_start_date, bool):
        raise PricerInputDefaultError("auto_contract_start_date必须为布尔值")
    config_value = values.get("pricing_config")
    if not isinstance(config_value, Mapping):
        raise PricerInputDefaultError("pricing_config必须为对象")
    config = deepcopy(dict(config_value))
    identity_value = values.get("identity", {})
    if not isinstance(identity_value, Mapping):
        raise PricerInputDefaultError("identity必须为对象")
    identity = deepcopy(dict(identity_value))
    if not data_refs:
        raise PricerInputDefaultError("请先绑定真实DataAssetRef；绑定行情后自动填充S₀Raw。")

    refs = tuple(_data_asset_ref(item) for item in data_refs)
    history_refs = tuple(ref for ref in refs if ref.schema_id == "market-history")
    calendar_refs = tuple(ref for ref in refs if ref.schema_id == "trading-calendar")
    unsupported = tuple(
        ref.schema_id
        for ref in refs
        if ref.schema_id not in {"market-history", "trading-calendar"}
    )
    if unsupported:
        raise PricerInputDefaultError(
            "Pricer正式输入包含不支持的DataAssetRef.schema_id："
            + ",".join(sorted(set(unsupported)))
        )
    if len(history_refs) != 1:
        raise PricerInputDefaultError("Pricer正式输入必须绑定唯一market-history行情DataAssetRef")
    if len(calendar_refs) > 1:
        raise PricerInputDefaultError("Pricer正式输入最多绑定一个trading-calendar交易日历DataAssetRef")
    if data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise PricerInputDefaultError("Pricer正式输入必须由Host注入只读DataAssetRef端口")
    ref = history_refs[0]
    valuation_date = _iso_date(config.get("valuation_date") or local_valuation_date(today), "估值日")
    try:
        payload = data_store.read_bytes(ref, tenant_id=ref.tenant_id)
    except (OSError, PermissionError, TypeError, ValueError) as error:
        raise PricerInputDefaultError(f"无法读取已绑定DataAssetRef：{error}") from error
    if not isinstance(payload, bytes):
        raise PricerInputDefaultError("Host DataAssetRef端口必须返回原始字节")
    if sha256(payload).hexdigest() != ref.content_hash:
        raise PricerInputDefaultError("DataAssetRef.content_hash与Host返回内容不一致")
    try:
        history = load_market_history_bytes(payload)
        identity = with_data_backed_reference(
            identity,
            history,
            valuation_date=valuation_date,
            auto_contract_start_date=auto_contract_start_date,
        )
    except MarketDataError as error:
        raise PricerInputDefaultError(str(error)) from error
    config["valuation_date"] = valuation_date
    values["identity"] = identity
    values["pricing_config"] = config
    return values


def with_data_backed_reference(
    identity_value: Mapping[str, Any],
    history: Any,
    *,
    valuation_date: str,
    auto_contract_start_date: bool = False,
) -> dict[str, Any]:
    """Set or verify the only legal raw contract reference price source."""
    if not isinstance(identity_value, Mapping):
        raise PricerInputDefaultError("identity必须为对象")
    identity = deepcopy(dict(identity_value))
    underlyings = _underlyings(identity)
    effective_valuation = _iso_date(valuation_date, "估值日")
    supplied_start = identity.get("contract_start_date")
    if auto_contract_start_date and supplied_start not in {None, ""}:
        raise PricerInputDefaultError("自动合同起始日不得同时提交具体日期")
    start = _iso_date(supplied_start or effective_valuation, "合同起始日")
    if start > effective_valuation:
        raise PricerInputDefaultError("合同起始日不得晚于估值日")
    if auto_contract_start_date:
        # The page's initial date is a convenience default, not a customer
        # instruction.  Freeze the contract on the latest common close that
        # is genuinely present in the Host-verified market history.  This
        # avoids inventing an intraday close when the requested day has not
        # arrived from the provider yet.
        start = _latest_common_close_date(history, underlyings, start)
    resolved = reference_prices_from_history(
        history, underlyings, contract_start_date=start,
    )
    _set_or_check_references(identity, resolved["reference_prices"])
    identity["contract_start_date"] = start
    return identity


def _latest_common_close_date(history: Any, underlyings: Sequence[str], cutoff: str) -> str:
    """Return the latest real session with a close for every contract asset."""
    import pandas as pd

    try:
        limit = pd.Timestamp(cutoff)
        rows = history.loc[:, ["date", "asset_id", "close"]].copy()
    except (AttributeError, KeyError) as error:
        raise PricerInputDefaultError("历史行情缺少用于冻结合同起始日的close") from error
    rows["date"] = pd.to_datetime(rows["date"], errors="coerce")
    rows["close"] = pd.to_numeric(rows["close"], errors="coerce")
    rows = rows[
        rows["date"].notna()
        & rows["close"].notna()
        & (rows["close"] > 0)
        & (rows["date"] <= limit)
        & rows["asset_id"].isin(underlyings)
    ]
    common = rows.pivot(index="date", columns="asset_id", values="close").reindex(columns=list(underlyings)).dropna()
    if common.empty:
        raise PricerInputDefaultError("DataAssetRef未覆盖全部标的在估值日或此前的共同未复权close")
    return common.index[-1].strftime("%Y-%m-%d")


def validate_frozen_contract_reference(
    contract: Any,
    historical_rows: Sequence[Mapping[str, Any]],
    *,
    valuation_date: str,
) -> dict[str, Any]:
    """Prove that a frozen formal contract uses its real start-price close."""
    identity = getattr(contract, "identity", None)
    underlyings = tuple(getattr(contract, "underlyings", ()))
    if not isinstance(identity, Mapping) or not underlyings:
        raise PricerInputDefaultError("ResolvedContract缺少合同身份或标的")
    start = _iso_date(identity.get("contract_start_date"), "ResolvedContract.contract_start_date")
    if start > _iso_date(valuation_date, "估值日"):
        raise PricerInputDefaultError("ResolvedContract合同起始日不得晚于估值日")
    references = identity.get("reference_prices")
    if not isinstance(references, Mapping) or set(references) != set(underlyings):
        raise PricerInputDefaultError("ResolvedContract必须含逐标的合同起始参考价")
    try:
        resolved = reference_prices_from_history(
            _history_frame(historical_rows), underlyings, contract_start_date=start,
        )
    except MarketDataError as error:
        raise PricerInputDefaultError(str(error)) from error
    _check_references(references, resolved["reference_prices"])
    return resolved


def _history_frame(rows: Sequence[Mapping[str, Any]]):
    # The canonical CSV loader owns all field validation; this tiny adapter
    # keeps the formal frozen-contract validation on that same rule set.
    import pandas as pd

    return pd.DataFrame([dict(row) for row in rows])


def _underlyings(identity: Mapping[str, Any]) -> tuple[str, ...]:
    value = identity.get("underlyings")
    if isinstance(value, str):
        values = tuple(item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple)):
        values = tuple(str(item).strip() for item in value if str(item).strip())
    else:
        values = ()
    if not values or len(set(values)) != len(values):
        raise PricerInputDefaultError("identity.underlyings必须为无重复标的列表")
    return values


def _data_asset_ref(value: Mapping[str, Any] | DataAssetRef) -> DataAssetRef:
    if isinstance(value, DataAssetRef):
        return value
    if not isinstance(value, Mapping):
        raise PricerInputDefaultError("DataAssetRef必须为对象")
    try:
        return DataAssetRef(**dict(value))
    except (TypeError, ValueError) as error:
        raise PricerInputDefaultError(f"DataAssetRef协议字段无效：{error}") from error


def _iso_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PricerInputDefaultError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise PricerInputDefaultError(f"{label}必须为YYYY-MM-DD") from error


def _set_or_check_references(identity: dict[str, Any], expected: Mapping[str, float]) -> None:
    supplied = identity.get("reference_prices")
    if supplied is not None:
        _check_references(supplied, expected)
    identity["reference_prices"] = dict(expected)


def _check_references(supplied: Any, expected: Mapping[str, float]) -> None:
    if not isinstance(supplied, Mapping) or set(supplied) != set(expected):
        raise PricerInputDefaultError("合同起始参考价必须逐一覆盖标的")
    for asset, expected_value in expected.items():
        try:
            actual = float(supplied[asset])
        except (TypeError, ValueError) as error:
            raise PricerInputDefaultError(f"reference_prices.{asset}必须为正数") from error
        if actual <= 0 or not isclose(actual, float(expected_value), rel_tol=1e-10, abs_tol=1e-8):
            raise PricerInputDefaultError(
                f"{asset}合同起始参考价必须来自已绑定DataAssetRef在{asset}合同起始日或此前的未复权close"
            )


__all__ = (
    "PricerInputDefaultError", "compile_pricer_input_defaults", "local_valuation_date",
    "validate_frozen_contract_reference", "with_data_backed_reference",
)
