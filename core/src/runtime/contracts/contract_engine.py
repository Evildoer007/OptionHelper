"""OptionReg共享合同引擎。

本模块只承担合同解析、受限公式解释与路径现金流结算。Payoffer、Pricer与
Backtester均调用它，但三者不相互调用。禁止在此处下载行情、修改默认SVG或把
PricingConfig、BacktestConfig写回OptionReg。
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from math import inf
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from runtime.knowledger.registry_loader import (
    RegistryLoadError,
    get_default_registry_path,
    load_registry as _load_registry,
    load_term_catalog as _load_term_catalog,
)
from .contract_types import deep_freeze, deep_thaw, semantic_hash


DEFAULT_REGISTRY_PATH = get_default_registry_path()
_RULE_TERM_KEYS = frozenset({"monitor", "pricing_methods", "constraints", "derived_terms"})
# 这些字段是产品定义的一部分，而不是本次交易可报价、可覆盖的经济参数。
# 它们仍保留在ResolvedContract.terms中，供下游展示和审计读取。
_IMMUTABLE_TERM_KEYS = frozenset({"margin_call", "payoff_figure_basis", "N", "Nvar", "Nvega", "G"})
# 不可覆盖不等于不可进入公式：N/Nvar虽是内部固定100基准，仍须由解释器绑定。
_STATIC_TERM_KEYS = _RULE_TERM_KEYS | frozenset({"margin_call", "payoff_figure_basis"})
_ALLOWED_PRODUCT_KEYS = frozenset({"identity", "terms", "paths"})
_ALLOWED_IDENTITY_KEYS = frozenset({"product_id", "name_zh", "entry_status"})
_ALLOWED_PATH_KEYS = frozenset({"condition", "cases"})
_ALLOWED_CASE_KEYS = frozenset({"domain", "pnl"})
_OBSERVATION_TERM_KEYS = frozenset({"O_KO", "O_KI", "Oc", "Otouch", "Orange", "Ovar", "Ohedge", "Oreset"})
_DEFAULT_STRIKE_ANCHOR_KEYS = {
    "3.1": frozenset({"K1", "K2"}),
    "3.2": frozenset({"K1", "K2"}),
    "3.3": frozenset({"K1", "K2"}),
    "3.4": frozenset({"K1", "K2"}),
    "4.2": frozenset({"Kp", "Kc"}),
    "4.3": frozenset({"K1", "K2", "K3"}),
    "4.4": frozenset({"K1", "K2", "K3", "K4"}),
    "10.2": frozenset({"K1", "K2"}),
    "10.3": frozenset({"K1", "K2"}),
    "10.7": frozenset({"Kd", "Ku"}),
}
_FORMULA_FUNCTIONS = frozenset({
    "min", "max", "first_time", "first_time_after", "first_time_before", "first_time_levels", "first_time_below_levels", "schedule_levels", "step_levels",
    "first_time_below_schedule", "first_observation_on_or_after", "terminal_event_time", "reset_knockout_time", "effective_hedge_time",
    "first_value", "count", "count_until", "exact_observation_count", "observation_ordinal",
    "realized_variance", "accumulated_quantity", "schedule_between", "schedule_matches_ratios",
    "is_monthly_schedule", "require_maturity_observation", "cash",
})
_SETTLEMENT_VARIABLES = frozenset({"S_t", "S_T", "W_t", "W_T", "r_T", "u", "T_contract", "inf"})
_NORMALIZED_PRICE_BASE = 100.0
_PAYOFF_SCALE_TERM_KEYS = frozenset({"N", "Nvar"})
_MATURITY_TIME_TOLERANCE = 7.0 / 365.0
_NUMERIC_COMPARISON_RTOL = 1e-12
_NUMERIC_COMPARISON_ATOL = 1e-12
_MAX_FORMULA_CHARS = 4096
_MAX_FORMULA_NODES = 256
_MAX_FORMULA_DEPTH = 64
_MAX_POWER_EXPONENT = 32.0
_MAX_FORMULA_ITEMS = 100_000
_MAX_ABS_FORMULA_VALUE = 1e100


@lru_cache(maxsize=1)
def _shared_term_catalog() -> Mapping[str, Any]:
    """复用Loader已验证的只读目录，避免每个绘图采样点复制完整Registry。"""
    return _load_term_catalog(DEFAULT_REGISTRY_PATH)


def _quoteable_price_term_keys(terms: Mapping[str, Any], catalog: Mapping[str, Any]) -> frozenset[str]:
    """TermCatalog的price类别是唯一可平移或判定覆盖的依据。"""
    return frozenset(
        key
        for key, value in terms.items()
        if catalog.get(key, {}).get("unit") == "price"
        and catalog.get(key, {}).get("value_type") in {"number", "number_list"}
        and isinstance(value, (int, float, np.number, Sequence))
        and not isinstance(value, (str, bytes, bool))
    )


def _default_strike_anchor(
    product_id: str,
    terms: Mapping[str, Any],
    overrides: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """将显式多执行价产品的默认经济坐标整体平移至最低执行价100。"""
    strike_keys = _DEFAULT_STRIKE_ANCHOR_KEYS.get(product_id)
    if strike_keys is None:
        return dict(terms), False
    if not strike_keys.issubset(terms):
        raise ContractResolutionError(f"{product_id}多执行价锚定缺少已登记执行价条款")
    price_keys = _quoteable_price_term_keys(terms, catalog)
    if set(overrides) & price_keys:
        return dict(terms), False
    strikes = [terms[key] for key in strike_keys]
    if any(isinstance(value, bool) or not isinstance(value, (int, float, np.number)) or not np.isfinite(float(value)) or float(value) <= 0 for value in strikes):
        raise ContractResolutionError(f"{product_id}多执行价锚定要求执行价为正有限数值")
    scale = _NORMALIZED_PRICE_BASE / min(float(value) for value in strikes)
    anchored: dict[str, Any] = {}
    for key, value in terms.items():
        if key not in price_keys:
            anchored[key] = value
        elif isinstance(value, (int, float, np.number)) and not isinstance(value, bool):
            anchored[key] = float(value) * scale
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            anchored[key] = [float(item) * scale for item in value]
        else:
            raise ContractResolutionError(f"{product_id}可报价price条款{key}无法按同一坐标锚定")
    return anchored, True


def _schedule_snapshot(terms: Mapping[str, Any], trading_dates: Sequence[Any] | None = None) -> dict[str, Any]:
    """冻结合同登记的观察选择器。

    含观察条款的正式合同必须在Host已验证的交易日历上解析，不能只冻结selector。
    """
    keys = sorted(_OBSERVATION_TERM_KEYS & set(terms))
    if trading_dates is None:
        if keys:
            raise ContractResolutionError("含观察条款的正式合同必须提供Host验证交易日历")
        return {}
    dates = pd.to_datetime(list(trading_dates), errors="coerce")
    if not len(dates) or dates.isna().any() or not dates.is_monotonic_increasing or dates.has_duplicates:
        raise ContractResolutionError("合同交易日历须为非空、严格递增的有效日期")
    times = np.arange(len(dates), dtype=float)
    snapshot = {
        key: {
            "selector": deep_thaw(terms[key]),
            "status": "resolved",
            "dates": [dates[position].date().isoformat() for position in _schedule_positions(terms[key], times, dates)],
        }
        for key in keys
    }
    if any(not value["dates"] for value in snapshot.values()):
        raise ContractResolutionError("Host验证交易日历未解析出全部合同观察日")
    return snapshot


def _require_observation_calendar_identity(identity: Mapping[str, Any], terms: Mapping[str, Any]) -> None:
    if not (_OBSERVATION_TERM_KEYS & set(terms)):
        return
    for key in ("calendar_id", "calendar_version"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ContractResolutionError(f"含观察条款的正式合同必须提供identity.{key}")


def _validate_resolved_schedule_snapshot(
    terms: Mapping[str, Any], schedules: Mapping[str, Any], identity: Mapping[str, Any],
) -> None:
    keys = frozenset(_OBSERVATION_TERM_KEYS & set(terms))
    if not keys:
        if schedules:
            raise ContractResolutionError("无观察条款的ResolvedContract.resolved_schedules必须为空")
        return
    _require_observation_calendar_identity(identity, terms)
    if set(schedules) != keys:
        raise ContractResolutionError("ResolvedContract.resolved_schedules必须逐一覆盖观察条款")
    for key in sorted(keys):
        value = schedules[key]
        if not isinstance(value, Mapping) or set(value) != {"selector", "status", "dates"}:
            raise ContractResolutionError(f"ResolvedContract.resolved_schedules.{key}外形无效")
        if value["status"] != "resolved" or semantic_hash(value["selector"]) != semantic_hash(terms[key]):
            raise ContractResolutionError(f"ResolvedContract.resolved_schedules.{key}必须为对应selector的resolved日程")
        dates = value["dates"]
        if not isinstance(dates, Sequence) or isinstance(dates, (str, bytes)) or not dates:
            raise ContractResolutionError(f"ResolvedContract.resolved_schedules.{key}必须包含真实观察日")
        try:
            parsed = tuple(pd.Timestamp(item).date().isoformat() for item in dates)
        except (TypeError, ValueError) as error:
            raise ContractResolutionError(f"ResolvedContract.resolved_schedules.{key}包含无效观察日") from error
        if tuple(dates) != parsed or parsed != tuple(sorted(parsed)) or len(set(parsed)) != len(parsed):
            raise ContractResolutionError(f"ResolvedContract.resolved_schedules.{key}观察日必须为严格递增的ISO日期")


def _economic_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """仅保留决定合同现金流语义的身份字段，排除展示与运行追踪字段。"""
    keys = (
        "product_id", "underlyings", "currency", "contract_start_date", "contract_end_date",
        "reference_prices", "price_convention", "calendar_id", "calendar_version",
    )
    return {key: identity.get(key) for key in keys}


class ContractResolutionError(ValueError):
    """OptionReg、合同条款或运行输入不满足约束。"""


class FormulaError(ValueError):
    """受限公式包含不允许的语法、变量或无法解释的经济表达。"""


@dataclass(frozen=True)
class Cashflow:
    """持有方在合同时间轴上的一笔现金流。"""

    time: float
    amount: float

    def to_dict(self) -> dict[str, float]:
        return {"time": float(self.time), "amount": float(self.amount)}


class CashflowBundle:
    """使 ``cash(...) + cash(...)`` 成为受限公式中的唯一现金流组合写法。"""

    def __init__(self, flows: Sequence[Cashflow] = ()) -> None:
        self.flows = tuple(flows)

    def __add__(self, other: object) -> "CashflowBundle":
        if isinstance(other, CashflowBundle):
            return CashflowBundle((*self.flows, *other.flows))
        raise FormulaError("现金流表达式只能以cash(...)相加")

    def __radd__(self, other: object) -> "CashflowBundle":
        if other == 0:
            return self
        return self.__add__(other)


@dataclass(frozen=True)
class ResolvedContract:
    """三个子模块共同读取的本次已解析合同。"""

    identity: Mapping[str, Any]
    terms: Mapping[str, Any]
    term_sources: Mapping[str, str]
    paths: tuple[Mapping[str, Any], ...]
    product_version: str = ""
    resolved_schedules: Mapping[str, Any] | None = None
    contract_fingerprint: str = ""
    registry_snapshot_hash: str = ""
    product_snapshot_hash: str = ""
    product_paths_hash: str = ""

    def __post_init__(self) -> None:
        identity = deep_freeze(self.identity)
        terms = deep_freeze(self.terms)
        sources = deep_freeze(self.term_sources)
        paths = tuple(deep_freeze(path) for path in self.paths)
        product_version = self.product_version or str(identity.get("product_version") or "unversioned")
        if identity.get("product_version") not in {None, product_version}:
            raise ContractResolutionError("ResolvedContract.product_version与identity不一致")
        if set(sources) != set(terms) or any(value not in {"default", "override", "derived"} for value in sources.values()):
            raise ContractResolutionError("term_sources必须逐一覆盖terms且来源值有效")
        if not paths:
            raise ContractResolutionError("ResolvedContract.paths不能为空")
        binding_values = (self.registry_snapshot_hash, self.product_snapshot_hash, self.product_paths_hash)
        if any(binding_values):
            if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in binding_values):
                raise ContractResolutionError("ResolvedContract产品快照绑定必须包含三个64位SHA-256")
            if semantic_hash(paths) != self.product_paths_hash:
                raise ContractResolutionError("ResolvedContract.paths与声明ProductVersion快照不一致")
            if product_version.startswith("unversioned:") and product_version != f"unversioned:{self.product_snapshot_hash[:16]}":
                raise ContractResolutionError("ResolvedContract.product_version与产品快照哈希不一致")
        schedules = deep_freeze(self.resolved_schedules or _schedule_snapshot(terms))
        _validate_resolved_schedule_snapshot(terms, schedules, identity)
        fingerprint = semantic_hash({
            "identity": _economic_identity(identity),
            "terms": terms,
            "paths": paths,
            "resolved_schedules": schedules,
        })
        if self.contract_fingerprint and self.contract_fingerprint != fingerprint:
            raise ContractResolutionError("ResolvedContract.contract_fingerprint校验失败")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "term_sources", sources)
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "product_version", product_version)
        object.__setattr__(self, "resolved_schedules", schedules)
        object.__setattr__(self, "contract_fingerprint", fingerprint)

    @property
    def product_id(self) -> str:
        return str(self.identity["product_id"])

    @property
    def name_zh(self) -> str:
        return str(self.identity["name_zh"])

    @property
    def underlyings(self) -> tuple[str, ...]:
        return tuple(self.identity["underlyings"])

    @property
    def currency(self) -> str:
        return str(self.identity["currency"])

    def to_dict(self) -> dict[str, Any]:
        """返回旧消费者使用的四字段外形。

        新消费者应使用``to_protocol_dict``取得当前正式完整合同。保留此方法可让
        尚未迁移的三个计算模块继续运行，而不复制或修改其消费者代码。
        """
        return {
            "identity": deep_thaw(self.identity),
            "terms": deep_thaw(self.terms),
            "term_sources": deep_thaw(self.term_sources),
            "paths": deep_thaw(self.paths),
        }

    def to_protocol_dict(self) -> dict[str, Any]:
        if not all((self.registry_snapshot_hash, self.product_snapshot_hash, self.product_paths_hash)):
            raise ContractResolutionError("ResolvedContract缺少Registry/ProductVersion快照绑定，不能进入正式协议")
        result = self.to_dict()
        result.update({
            "product_version": self.product_version,
            "resolved_schedules": deep_thaw(self.resolved_schedules),
            "contract_fingerprint": self.contract_fingerprint,
            "registry_snapshot_hash": self.registry_snapshot_hash,
            "product_snapshot_hash": self.product_snapshot_hash,
            "product_paths_hash": self.product_paths_hash,
        })
        return result

    def to_controlled_snapshot(self) -> dict[str, Any]:
        """返回仅供Core与受控ModuleRun输入使用的完整合同快照。

        该快照可由``ResolvedContract(**snapshot)``严格复原并校验原始
        fingerprint与产品快照绑定。它不是公开展示对象；页面和公开工件必须
        使用各模块显式定义的display projection，不能裁剪后仍称作合同。
        """
        return self.to_protocol_dict()

    @classmethod
    def from_controlled_snapshot(cls, snapshot: Mapping[str, Any]) -> "ResolvedContract":
        """严格恢复Core受控快照，拒绝展示投影或不完整对象。"""
        if not isinstance(snapshot, Mapping):
            raise ContractResolutionError("Core受控ResolvedContract快照必须是对象")
        try:
            return cls(**dict(snapshot))
        except (TypeError, ValueError, ContractResolutionError) as error:
            raise ContractResolutionError(f"Core受控ResolvedContract快照无效：{error}") from error


@dataclass(frozen=True)
class PayoffInput:
    """Po唯一输入：已解析合同。"""

    contract: ResolvedContract


@dataclass(frozen=True)
class PayoffEvaluation:
    """某条价格路径上的唯一结算结果。"""

    selected_path: int
    selected_case: int
    monitor_values: dict[str, Any]
    cashflows: tuple[Cashflow, ...]

    @property
    def pnl(self) -> float:
        return float(sum(flow.amount for flow in self.cashflows))

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_path": self.selected_path,
            "selected_case": self.selected_case,
            "monitor_values": _json_scalar_map(self.monitor_values),
            "cashflows": [flow.to_dict() for flow in self.cashflows],
            "pnl": self.pnl,
        }


class EventVector:
    def __init__(self, values: Any, times: Sequence[float]) -> None:
        self.values = np.asarray(values, dtype=bool)
        self.times = np.asarray(times, dtype=float)
        if self.values.shape != self.times.shape:
            raise FormulaError("路径布尔指标与时间网格长度不一致")

    def __and__(self, other: object) -> "EventVector":
        other_vector = _event_vector(other, self.times)
        return EventVector(self.values & other_vector.values, self.times)

    def __or__(self, other: object) -> "EventVector":
        other_vector = _event_vector(other, self.times)
        return EventVector(self.values | other_vector.values, self.times)

    def __invert__(self) -> "EventVector":
        return EventVector(~self.values, self.times)


def _numeric_relation(left: object, right: object, relation: str) -> Any | None:
    """以统一容差解释合同数值边界；字符串、无穷和其他对象保持原生比较。"""
    if isinstance(left, (bool, np.bool_)) or isinstance(right, (bool, np.bool_)):
        return None
    try:
        lhs = np.asarray(left, dtype=float)
        rhs = np.asarray(right, dtype=float)
    except (TypeError, ValueError):
        return None
    if not np.issubdtype(lhs.dtype, np.number) or not np.issubdtype(rhs.dtype, np.number):
        return None
    lhs, rhs = np.broadcast_arrays(lhs, rhs)
    if not np.isfinite(lhs).all() or not np.isfinite(rhs).all():
        return None
    tolerance = _NUMERIC_COMPARISON_ATOL + _NUMERIC_COMPARISON_RTOL * np.maximum(np.abs(lhs), np.abs(rhs))
    if relation == "lt":
        result = lhs < rhs - tolerance
    elif relation == "le":
        result = lhs <= rhs + tolerance
    elif relation == "gt":
        result = lhs > rhs + tolerance
    elif relation == "ge":
        result = lhs >= rhs - tolerance
    elif relation == "eq":
        result = np.abs(lhs - rhs) <= tolerance
    elif relation == "ne":
        result = np.abs(lhs - rhs) > tolerance
    else:
        raise FormulaError("不允许的比较运算")
    return bool(result.item()) if result.ndim == 0 else result


class ObservedValues:
    def __init__(self, values: Sequence[float], times: Sequence[float], ordinals: Sequence[int] | None = None) -> None:
        self.values = np.asarray(values, dtype=float)
        self.times = np.asarray(times, dtype=float)
        if self.values.shape != self.times.shape:
            raise FormulaError("观察价格与时间网格长度不一致")
        self.ordinals = np.arange(1, len(self.values) + 1, dtype=int) if ordinals is None else np.asarray(ordinals, dtype=int)
        if self.ordinals.shape != self.values.shape or (self.ordinals <= 0).any() or len(np.unique(self.ordinals)) != len(self.ordinals):
            raise FormulaError("观察序号必须与观察价格一一对应且为不重复正整数")

    def _compare(self, other: object, relation: str, op) -> EventVector:
        result = _numeric_relation(self.values, other, relation)
        return EventVector(op(self.values, other) if result is None else result, self.times)

    def __lt__(self, other: object) -> EventVector:
        return self._compare(other, "lt", np.less)

    def __le__(self, other: object) -> EventVector:
        return self._compare(other, "le", np.less_equal)

    def __gt__(self, other: object) -> EventVector:
        return self._compare(other, "gt", np.greater)

    def __ge__(self, other: object) -> EventVector:
        return self._compare(other, "ge", np.greater_equal)

    def __eq__(self, other: object) -> EventVector:  # type: ignore[override]
        return self._compare(other, "eq", np.equal)

    def __ne__(self, other: object) -> EventVector:  # type: ignore[override]
        return self._compare(other, "ne", np.not_equal)


class PriceSeries:
    """安全公式可索引的价格序列，支持 ``S_t[O_out]``。"""

    def __init__(self, values: Sequence[float], times: Sequence[float], dates: Sequence[Any] | None = None) -> None:
        self.values = np.asarray(values, dtype=float)
        self.times = np.asarray(times, dtype=float)
        self.dates = pd.to_datetime(list(dates)) if dates is not None else None
        if self.values.ndim != 1 or self.values.shape != self.times.shape:
            raise FormulaError("价格路径必须是一维且与时间网格等长")

    def __getitem__(self, schedule: object) -> ObservedValues:
        positions = _schedule_positions(schedule, self.times, self.dates)
        return ObservedValues(
            self.values[positions],
            self.times[positions],
            _schedule_observation_ordinals(schedule, self.times, self.dates, positions),
        )


@dataclass(frozen=True)
class PricePath:
    """完整单标的或多标的真实市场价格路径，首维为合同时间。

    ``values``保存交易日收盘价，不应由调用方预先归一化；解释器以identity中的
    ``reference_prices``将其映射为内部100基准价格水平。若合同将
    ``observation_price``设为``open``、``high``或``low``，调用方必须在
    ``price_fields``中逐一提供同形状的对应OHLC序列；禁止静默回退至收盘价。
    """

    values: np.ndarray
    times: np.ndarray
    asset_ids: tuple[str, ...]
    dates: tuple[Any, ...] | None = None
    price_fields: Mapping[str, np.ndarray] | None = None
    times_explicit: bool = True

    @classmethod
    def from_values(
        cls,
        values: Sequence[float] | Sequence[Sequence[float]] | np.ndarray,
        *,
        times: Sequence[float] | None = None,
        asset_ids: Sequence[str] = ("UNDERLYING",),
        dates: Sequence[Any] | None = None,
        price_fields: Mapping[str, Sequence[float] | Sequence[Sequence[float]] | np.ndarray] | None = None,
    ) -> "PricePath":
        data = np.asarray(values, dtype=float)
        if data.ndim == 1:
            data = data[:, None]
        if data.ndim != 2 or data.shape[0] < 2 or not np.isfinite(data).all() or (data < 0).any() or (data[0] <= 0).any():
            raise ContractResolutionError("价格路径必须含至少两个非负价格观察点，且首个观察点必须严格为正")
        assets = tuple(str(item) for item in asset_ids)
        if len(assets) != data.shape[1]:
            raise ContractResolutionError("asset_ids数量与价格路径列数不一致")
        grid = np.asarray(times if times is not None else np.linspace(0.0, 1.0, data.shape[0]), dtype=float)
        if grid.shape != (data.shape[0],) or grid[0] < 0.0 or np.any(np.diff(grid) <= 0):
            raise ContractResolutionError("时间网格须非负且严格递增")
        normalized_dates: tuple[Any, ...] | None = None
        if dates is not None:
            if len(dates) != data.shape[0]:
                raise ContractResolutionError("日期网格与价格路径长度不一致")
            date_index = pd.to_datetime(list(dates), errors="coerce")
            if date_index.isna().any() or not date_index.is_monotonic_increasing or date_index.has_duplicates:
                raise ContractResolutionError("日期网格须为严格递增的有效交易日")
            normalized_dates = tuple(date_index)
        fields: dict[str, np.ndarray] = {}
        for source, source_values in dict(price_fields or {}).items():
            if source not in {"open", "high", "low"}:
                raise ContractResolutionError("price_fields仅支持open、high或low；收盘价请使用values")
            source_data = np.asarray(source_values, dtype=float)
            if source_data.ndim == 1:
                source_data = source_data[:, None]
            if source_data.shape != data.shape or not np.isfinite(source_data).all() or (source_data < 0).any() or (source_data[0] <= 0).any():
                raise ContractResolutionError(f"price_fields.{source}必须与收盘价路径同形状且均为非负，首个观察点严格为正")
            fields[source] = source_data
        return cls(data, grid, assets, normalized_dates, fields or None, times is not None)

    def values_for(self, source: str) -> np.ndarray:
        """返回合同指定的原始观察价，缺少OHLC字段时明确拒绝执行。"""
        if source == "close":
            return self.values
        available = (self.price_fields or {}).get(source)
        if available is None:
            raise ContractResolutionError(f"合同观察价格为{source}，但价格路径未提供price_fields.{source}")
        return np.asarray(available, dtype=float)


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    """经唯一Registry Loader读取OptionReg，不提供旧字段兼容层。"""
    try:
        return _load_registry(path)
    except RegistryLoadError as error:
        raise ContractResolutionError(str(error)) from error


def get_product(product_id: str, registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    reg = dict(registry) if registry is not None else load_registry()
    try:
        product = reg["products"][str(product_id)]
    except (KeyError, TypeError) as error:
        raise ContractResolutionError(f"OptionReg不存在产品{product_id}") from error
    if not isinstance(product, Mapping):
        raise ContractResolutionError(f"产品{product_id}不是有效对象")
    return deepcopy(dict(product))


def verify_product_snapshot_binding(
    contract: ResolvedContract,
    registry: Mapping[str, Any],
    *,
    attested_product_version: str | None = None,
) -> ResolvedContract:
    """证明合同事实来自所声明的同一Registry产品快照。"""

    published = not contract.product_version.startswith("unversioned:")
    if published:
        if attested_product_version != contract.product_version:
            raise ContractResolutionError("历史ProductVersion必须同时提供签发版本证明与对应归档Registry快照")
        if re.fullmatch(r"v[1-9]\d*\.\d+\.\d+", contract.product_version) is None:
            raise ContractResolutionError("历史ProductVersion版本格式无效")
    elif attested_product_version is not None:
        raise ContractResolutionError("开发态unversioned合同不得伪装正式ProductVersion证明")
    contract = _verify_snapshot_hashes(contract, registry, enforce_unversioned=not published)
    identity_fields = {
        "contract_id", "underlyings", "currency", "contract_start_date", "contract_end_date",
        "reference_prices", "price_convention", "calendar_id", "calendar_version",
    }
    identity = {key: deep_thaw(value) for key, value in contract.identity.items() if key in identity_fields}
    overrides = {
        key: deep_thaw(contract.terms[key])
        for key, source in contract.term_sources.items()
        if source == "override"
    }
    expected = _resolve_contract(
        contract.product_id,
        identity=identity,
        term_overrides=overrides,
        registry=registry,
        frozen_resolved_schedules=contract.resolved_schedules,
    )
    for label, actual, declared in (
        ("identity", _economic_identity(contract.identity), _economic_identity(expected.identity)),
        ("terms", contract.terms, expected.terms),
        ("term_sources", contract.term_sources, expected.term_sources),
    ):
        if semantic_hash(actual) != semantic_hash(declared):
            raise ContractResolutionError(f"ResolvedContract.{label}不属于声明ProductVersion条款快照")
    return contract


def _verify_snapshot_hashes(
    contract: ResolvedContract,
    registry: Mapping[str, Any],
    *,
    enforce_unversioned: bool = True,
) -> ResolvedContract:
    """校验Registry、产品和paths哈希；解析器内部使用以避免递归。"""

    if not isinstance(contract, ResolvedContract) or not isinstance(registry, Mapping):
        raise ContractResolutionError("产品快照绑定校验需要ResolvedContract与Registry")
    if semantic_hash(registry) != contract.registry_snapshot_hash:
        raise ContractResolutionError("ResolvedContract.Registry快照哈希不一致")
    try:
        product = registry["products"][contract.product_id]
        declared_paths = product["paths"]
    except (KeyError, TypeError) as error:
        raise ContractResolutionError("ResolvedContract产品快照不存在") from error
    product_hash = semantic_hash(product)
    if product_hash != contract.product_snapshot_hash:
        raise ContractResolutionError("ResolvedContract产品快照哈希不一致")
    if semantic_hash(declared_paths) != contract.product_paths_hash or semantic_hash(contract.paths) != contract.product_paths_hash:
        raise ContractResolutionError("ResolvedContract.paths不属于声明ProductVersion快照")
    expected_version = f"unversioned:{product_hash[:16]}"
    if enforce_unversioned and contract.product_version != expected_version:
        raise ContractResolutionError("ResolvedContract.ProductVersion未由当前Registry快照证明")
    return contract


def _resolve_contract(
    product_id: str,
    *,
    identity: Mapping[str, Any] | None = None,
    term_overrides: Mapping[str, Any] | None = None,
    registry: Mapping[str, Any] | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    trading_dates: Sequence[Any] | None = None,
    frozen_resolved_schedules: Mapping[str, Any] | None = None,
) -> ResolvedContract:
    """以默认条款加本次覆盖生成唯一合同；未完整录入的产品拒绝运行。"""
    source_registry = deepcopy(dict(registry)) if registry is not None else load_registry(registry_path)
    product = get_product(product_id, source_registry)
    issues = validate_product_spec(str(product_id), product, source_registry["term_catalog"])
    if issues:
        raise ContractResolutionError(f"产品{product_id}的OptionReg校验失败：{'；'.join(issues)}")
    product_identity = product["identity"]
    if not product_identity["entry_status"]:
        raise ContractResolutionError(f"产品{product_id}尚未完整录入，entry_status=False，Po、P、BT禁止执行")

    supplied_identity = dict(identity or {})
    unknown_identity = set(supplied_identity) - {
        "contract_id", "underlyings", "currency", "contract_start_date", "contract_end_date",
        "reference_prices", "price_convention", "calendar_id",
        "calendar_version",
    }
    if unknown_identity:
        raise ContractResolutionError(f"合同identity含未知字段：{','.join(sorted(unknown_identity))}")
    underlyings = tuple(str(item).strip() for item in supplied_identity.get("underlyings", ("DEFAULT.UNDERLYING",)))
    if not underlyings or any(not item for item in underlyings) or len(set(underlyings)) != len(underlyings):
        raise ContractResolutionError("underlyings必须为无重复的非空标的代码序列")
    source_terms = product["terms"]
    references = supplied_identity.get("reference_prices")
    if references is not None:
        if not isinstance(references, Mapping) or set(references) != set(underlyings):
            raise ContractResolutionError("reference_prices必须逐一覆盖underlyings")
        references = {str(key): _positive_number(value, f"reference_prices.{key}") for key, value in references.items()}

    defaults = {key: deepcopy(value) for key, value in source_terms.items() if key not in _RULE_TERM_KEYS}
    # Payoffer、Pricer和Backtester共用这份合同解释器。历史OptionReg保留的N/Nvar
    # 只是旧资料的规模示例；运行期一律固定为无货币单位的100点标准化结算基准。
    # 不改写资料库或默认示例资产，亦不让客户名义本金进入本次合同。
    for key in _PAYOFF_SCALE_TERM_KEYS & set(defaults):
        defaults[key] = _NORMALIZED_PRICE_BASE
    overrides = dict(term_overrides or {})
    attempted_scale_override = set(overrides) & _PAYOFF_SCALE_TERM_KEYS
    if attempted_scale_override:
        raise ContractResolutionError(
            f"内部标准化基准100不可覆盖：{','.join(sorted(attempted_scale_override))}"
        )
    attempted_immutable = set(overrides) & _IMMUTABLE_TERM_KEYS
    if attempted_immutable:
        raise ContractResolutionError(f"结构性条款不可由用户直接覆盖：{','.join(sorted(attempted_immutable))}")
    derived_keys = set(source_terms.get("derived_terms", {}))
    attempted_derived = set(overrides) & derived_keys
    if attempted_derived:
        raise ContractResolutionError(f"派生条款不可由用户直接覆盖：{','.join(sorted(attempted_derived))}")
    unknown = set(overrides) - set(defaults)
    if unknown:
        raise ContractResolutionError(f"本次条款覆盖含未登记字段：{','.join(sorted(unknown))}")
    final_terms, default_strike_anchored = _default_strike_anchor(
        str(product_id), {**defaults, **overrides}, overrides, source_registry["term_catalog"],
    )
    _validate_terms(final_terms, source_registry["term_catalog"])
    if "S0Vec" in final_terms:
        required_assets = len(final_terms["S0Vec"])
        if required_assets < 2:
            raise ContractResolutionError(f"{product_id}为多标的结构，S0Vec至少须含两个标的基准")
        if len(underlyings) != required_assets:
            raise ContractResolutionError(
                f"{product_id}为多标的结构，underlyings数量必须与S0Vec长度{required_assets}一致"
            )
    price_convention = str(supplied_identity.get("price_convention") or "normalized_100")
    if price_convention == "normalized_100" and references is None:
        # A normalized contract's S0/S0Vec is its declared price coordinate,
        # not an implicit market quote.  Freeze that coordinate explicitly so
        # every formal calculator receives the same complete identity.  An
        # absolute-market contract still requires a Host-supplied quote.
        references = _normalized_reference_prices(final_terms, underlyings)
    if price_convention == "normalized_100":
        _validate_normalized_price_base(final_terms, allow_scaled_coordinate=default_strike_anchored)
    elif price_convention == "absolute_market":
        if references is None:
            raise ContractResolutionError("absolute_market价格口径必须提供reference_prices")
        expected = np.asarray([references[asset] for asset in underlyings], dtype=float)
        if "S0Vec" in final_terms and not np.allclose(np.asarray(final_terms["S0Vec"], dtype=float), expected, rtol=0.0, atol=1e-12):
            raise ContractResolutionError("absolute_market价格口径下S0Vec必须逐一等于reference_prices")
        if "S0" in final_terms and (len(expected) != 1 or not np.isclose(float(final_terms["S0"]), expected[0], rtol=0.0, atol=1e-12)):
            raise ContractResolutionError("absolute_market价格口径下S0必须等于reference_prices")
    else:
        raise ContractResolutionError("price_convention仅支持normalized_100或absolute_market")
    variables = bind_term_symbols(final_terms, source_registry["term_catalog"])
    derived_sources: dict[str, str] = {}
    for key, expression in source_terms.get("derived_terms", {}).items():
        if key in final_terms:
            raise ContractResolutionError(f"派生条款字段与基础条款重名：{key}")
        try:
            value = evaluate_formula(expression, variables)
        except FormulaError as error:
            raise ContractResolutionError(f"派生条款{key}无法计算：{error}") from error
        _validate_terms({key: value}, source_registry["term_catalog"])
        final_terms[key] = value
        variables[str(source_registry["term_catalog"][key]["symbol"])] = value
        derived_sources[key] = "derived"
    for expression in source_terms.get("constraints", []):
        if _coerce_bool(evaluate_formula(expression, variables)) is not True:
            raise ContractResolutionError(f"条款覆盖不满足产品约束：{expression}")
    if _OBSERVATION_TERM_KEYS & set(final_terms):
        _require_observation_calendar_identity(supplied_identity, final_terms)
        if trading_dates is None and frozen_resolved_schedules is None:
            raise ContractResolutionError("含观察条款的正式合同必须提供Host验证交易日历")
    final_terms["monitor"] = deepcopy(source_terms.get("monitor", {}))
    final_terms["pricing_methods"] = list(source_terms["pricing_methods"])
    if "constraints" in source_terms:
        final_terms["constraints"] = list(source_terms["constraints"])
    if "derived_terms" in source_terms:
        final_terms["derived_terms"] = deepcopy(source_terms["derived_terms"])
    term_sources = {key: "override" if key in overrides else "default" for key in defaults}
    term_sources.update(derived_sources)
    for key in final_terms:
        term_sources.setdefault(key, "default")
    registry_snapshot_hash = semantic_hash(source_registry)
    product_snapshot_hash = semantic_hash(product)
    product_paths_hash = semantic_hash(product["paths"])
    product_version = f"unversioned:{product_snapshot_hash[:16]}"
    resolved_identity = {
        "product_id": str(product_id),
        "name_zh": product_identity["name_zh"],
        "entry_status": bool(product_identity["entry_status"]),
        "contract_id": str(supplied_identity.get("contract_id") or f"{product_id}-contract"),
        "underlyings": list(underlyings),
        "currency": str(supplied_identity.get("currency") or "CNY"),
        "contract_start_date": supplied_identity.get("contract_start_date"),
        "contract_end_date": supplied_identity.get("contract_end_date"),
        "reference_prices": references,
        "price_convention": price_convention,
        "calendar_id": supplied_identity.get("calendar_id"),
        "calendar_version": supplied_identity.get("calendar_version"),
        "product_version": product_version,
    }
    contract = ResolvedContract(
        resolved_identity,
        final_terms,
        term_sources,
        tuple(deepcopy(product["paths"])),
        product_version=product_version,
        resolved_schedules=(
            deep_thaw(frozen_resolved_schedules)
            if frozen_resolved_schedules is not None
            else _schedule_snapshot(final_terms, trading_dates)
        ),
        registry_snapshot_hash=registry_snapshot_hash,
        product_snapshot_hash=product_snapshot_hash,
        product_paths_hash=product_paths_hash,
    )
    return _verify_snapshot_hashes(contract, source_registry)


def resolve_contract(
    product_id: str,
    *,
    identity: Mapping[str, Any] | None = None,
    term_overrides: Mapping[str, Any] | None = None,
    registry: Mapping[str, Any] | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    trading_dates: Sequence[Any] | None = None,
) -> ResolvedContract:
    """以默认条款加本次覆盖生成唯一正式合同；观察日必须由Host冻结。"""
    return _resolve_contract(
        product_id,
        identity=identity,
        term_overrides=term_overrides,
        registry=registry,
        registry_path=registry_path,
        trading_dates=trading_dates,
    )


def make_payoff_input(contract: ResolvedContract) -> PayoffInput:
    return PayoffInput(contract=contract)


def bind_term_symbols(terms: Mapping[str, Any], term_catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    catalog = term_catalog or _shared_term_catalog()
    variables: dict[str, Any] = {}
    for key, value in terms.items():
        if key in _STATIC_TERM_KEYS:
            continue
        try:
            symbol = str(catalog[key]["symbol"])
        except (KeyError, TypeError) as error:
            raise ContractResolutionError(f"条款字段{key}未登记在term_catalog") from error
        if symbol in variables:
            raise ContractResolutionError(f"公式变量{symbol}在同一合同中重复绑定")
        variables[symbol] = value
    return variables


def evaluate_contract(contract: ResolvedContract, price_path: PricePath) -> PayoffEvaluation:
    """同一解释器选择唯一经济路径、唯一分段并产出逐笔持有方现金流。"""
    variables = _formula_context(contract, price_path)
    monitor_values = compute_monitor_values(contract, price_path, variables)
    variables.update(monitor_values)
    chosen_paths = [index for index, path in enumerate(contract.paths) if _coerce_bool(evaluate_formula(path["condition"], variables))]
    if len(chosen_paths) != 1:
        raise FormulaError(f"{contract.product_id}在给定价格路径下命中{len(chosen_paths)}条经济路径，必须唯一")
    selected_path = chosen_paths[0]
    _validate_settlement_endpoint(contract, price_path, monitor_values, contract.paths[selected_path]["condition"])
    cases = contract.paths[selected_path]["cases"]
    chosen_cases = [index for index, case in enumerate(cases) if _coerce_bool(evaluate_formula(case["domain"], variables))]
    if len(chosen_cases) != 1:
        raise FormulaError(f"{contract.product_id}路径{selected_path + 1}命中{len(chosen_cases)}个分段，必须唯一")
    selected_case = chosen_cases[0]
    result = evaluate_formula(cases[selected_case]["pnl"], variables)
    if not isinstance(result, CashflowBundle):
        raise FormulaError("pnl必须返回由cash(t, amount)组成的现金流")
    return PayoffEvaluation(selected_path, selected_case, monitor_values, result.flows)


def evaluate_payoff(contract: ResolvedContract, price_path: PricePath) -> PayoffEvaluation:
    """兼容既有调用方的公开估值入口。"""
    return evaluate_contract(contract, price_path)


def _validate_settlement_endpoint(
    contract: ResolvedContract,
    price_path: PricePath,
    monitor_values: Mapping[str, Any],
    selected_condition: str,
) -> None:
    """未到合同期限的输入只能用于已有终止事件的完整提前结算路径。"""
    if "T" not in contract.terms:
        return
    contractual_tenor = float(contract.terms["T"])
    actual_tenor = float(price_path.times[-1])
    if actual_tenor > contractual_tenor + _MATURITY_TIME_TOLERANCE:
        raise ContractResolutionError(
            f"{contract.product_id}价格路径终点{actual_tenor}年晚于合同期限{contractual_tenor}年；"
            "结算路径不得使用到期日后的价格"
        )
    if abs(actual_tenor - contractual_tenor) <= _MATURITY_TIME_TOLERANCE:
        return
    termination_names = ("tau_out", "tau_out_1", "tau_out_2", "tau_hedge", "tau_touch")
    for name in termination_names:
        value = monitor_values.get(name)
        if not isinstance(value, (int, float, np.number)) or not np.isfinite(float(value)):
            continue
        if float(value) > actual_tenor + 1e-12 or not re.search(rf"\b{name}\b", selected_condition):
            continue
        if re.search(rf"\b{name}\s*==\s*inf", selected_condition):
            continue
        return
    raise ContractResolutionError(
        f"{contract.product_id}价格路径终点{actual_tenor}年早于合同期限{contractual_tenor}年；"
        "未终止路径须覆盖至合同到期日或其相邻交易日"
    )


def compute_monitor_values(contract: ResolvedContract, price_path: PricePath, base_variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
    monitor = contract.terms.get("monitor", {})
    if not isinstance(monitor, Mapping):
        raise ContractResolutionError("terms.monitor必须为扁平公式字典")
    variables = dict(base_variables or _formula_context(contract, price_path))
    result: dict[str, Any] = {}
    for name, expression in monitor.items():
        if name in result or not isinstance(name, str) or not isinstance(expression, str):
            raise ContractResolutionError("monitor必须是唯一指标名到字符串公式的映射")
        result[name] = evaluate_formula(expression, variables)
        variables[name] = result[name]
    return result


def evaluate_formula(expression: str, variables: Mapping[str, Any]) -> Any:
    """执行有限AST表达式；绝不使用eval、属性访问、导入或产品专属函数。"""
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("公式必须为非空字符串")
    if len(expression) > _MAX_FORMULA_CHARS:
        raise FormulaError("公式超过最大长度")
    tree = _parse_formula(expression)
    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_FORMULA_NODES:
        raise FormulaError("公式超过最大语法复杂度")
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_FORMULA_DEPTH:
            raise FormulaError("公式超过最大嵌套深度")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    try:
        result = _FormulaEvaluator(variables).visit(tree.body)
    except FormulaError:
        raise
    except (ArithmeticError, TypeError, ValueError, RecursionError, MemoryError) as error:
        raise FormulaError("公式计算超过受限范围") from error
    if isinstance(result, (float, np.floating)) and not np.isfinite(float(result)):
        if float(result) != inf:
            raise FormulaError("公式结果必须为有限数值")
    return result


@lru_cache(maxsize=512)
def _parse_formula(expression: str) -> ast.Expression:
    try:
        return ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError, MemoryError) as error:
        raise FormulaError(f"公式语法错误：{expression}") from error


class _FormulaEvaluator(ast.NodeVisitor):
    def __init__(self, variables: Mapping[str, Any]) -> None:
        self.variables = dict(variables)

    def generic_visit(self, node: ast.AST) -> Any:
        raise FormulaError(f"不允许的公式语法：{node.__class__.__name__}")

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise FormulaError("公式常量仅允许数值、字符串和布尔值")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id == "inf":
            return inf
        try:
            return self.variables[node.id]
        except KeyError as error:
            raise FormulaError(f"公式引用了未绑定变量：{node.id}") from error

    def visit_List(self, node: ast.List) -> list[Any]:
        return [self.visit(item) for item in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.visit(item) for item in node.elts)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        target = self.visit(node.value)
        index = self.visit(node.slice)
        if not isinstance(target, PriceSeries):
            raise FormulaError("下标访问仅允许价格路径，例如S_t[O_out]")
        return target[index]

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.Not):
            return ~value if isinstance(value, EventVector) else not _coerce_bool(value)
        raise FormulaError("不允许的单目运算")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.visit(node.left), self.visit(node.right)
        if any(isinstance(value, (str, bytes, list, tuple, set, frozenset)) for value in (left, right)):
            raise FormulaError("公式不允许对字符串或容器执行算术")
        if isinstance(node.op, ast.Add): return _bounded_formula_value(left + right)
        if isinstance(node.op, ast.Sub): return _bounded_formula_value(left - right)
        if isinstance(node.op, ast.Mult): return _bounded_formula_value(left * right)
        if isinstance(node.op, ast.Div): return _bounded_formula_value(left / right)
        if isinstance(node.op, ast.Pow):
            exponent = _finite_number(right, "幂指数")
            if abs(exponent) > _MAX_POWER_EXPONENT:
                raise FormulaError("幂指数超过受限范围")
            return _bounded_formula_value(left ** exponent)
        if isinstance(node.op, ast.Mod): return _bounded_formula_value(left % right)
        raise FormulaError("不允许的二元运算")

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(value) for value in node.values]
        result = values[0]
        for value in values[1:]:
            if isinstance(result, EventVector) or isinstance(value, EventVector):
                result = _event_vector(result, _event_times(result, value))
                other = _event_vector(value, result.times)
                result = result & other if isinstance(node.op, ast.And) else result | other
            else:
                result = _coerce_bool(result) and _coerce_bool(value) if isinstance(node.op, ast.And) else _coerce_bool(result) or _coerce_bool(value)
        return result

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        results: list[Any] = []
        for operator, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            results.append(_compare(left, right, operator))
            left = right
        result = results[0]
        for item in results[1:]:
            result = result & item if isinstance(result, EventVector) else _coerce_bool(result) and _coerce_bool(item)
        return result

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise FormulaError("公式函数不支持属性、关键字参数或任意调用")
        name = node.func.id
        args = [self.visit(arg) for arg in node.args]
        functions = {
            "min": min,
            "max": max,
            "first_time": _first_time,
            "first_time_after": _first_time_after,
            "first_time_before": _first_time_before,
            "first_time_levels": _first_time_levels,
            "first_time_below_levels": _first_time_below_levels,
            "schedule_levels": _schedule_levels,
            "step_levels": _step_levels,
            "first_time_below_schedule": _first_time_below_schedule,
            "first_observation_on_or_after": _first_observation_on_or_after,
            "terminal_event_time": _terminal_event_time,
            "reset_knockout_time": _reset_knockout_time,
            "effective_hedge_time": _effective_hedge_time,
            "first_value": _first_value,
            "count": _count,
            "count_until": _count_until,
            "exact_observation_count": _exact_observation_count,
            "observation_ordinal": _observation_ordinal,
            "realized_variance": _realized_variance,
            "accumulated_quantity": _accumulated_quantity,
            "schedule_between": _schedule_between,
            "schedule_matches_ratios": _schedule_matches_ratios,
            "is_monthly_schedule": _is_monthly_schedule,
            "require_maturity_observation": _require_maturity_observation,
            "cash": _cash,
        }
        try:
            return functions[name](*args)
        except KeyError as error:
            raise FormulaError(f"公式函数未登记：{name}") from error
        except FormulaError:
            raise
        except (TypeError, ValueError, FloatingPointError) as error:
            raise FormulaError(f"公式函数{name}的参数无效") from error


def validate_product_spec(product_id: str, product: Mapping[str, Any], term_catalog: Mapping[str, Any]) -> list[str]:
    """验证每个OptionReg产品的外形、字段及静态公式语义。

    ``entry_status``只决定模块是否允许执行，不能跳过资料库本身的结构和
    公式检查。否则未完成产品会在恢复执行时才暴露字段或符号错误。
    """
    issues: list[str] = []
    if set(product) != _ALLOWED_PRODUCT_KEYS:
        issues.append("product必须且只能有identity、terms、paths")
        return issues
    identity = product.get("identity")
    terms = product.get("terms")
    paths = product.get("paths")
    if not isinstance(identity, Mapping) or set(identity) != _ALLOWED_IDENTITY_KEYS:
        issues.append("identity字段不符合固定外形")
    elif identity.get("product_id") != product_id:
        issues.append("products外层键与identity.product_id不一致")
    elif not isinstance(identity.get("entry_status"), bool):
        issues.append("identity.entry_status必须为Python布尔值")
    elif not isinstance(identity.get("name_zh"), str) or not identity["name_zh"].strip():
        issues.append("identity.name_zh必须为非空字符串")
    if not isinstance(terms, Mapping) or not terms:
        issues.append("terms必须为非空映射")
        return issues
    if "pricing_methods" not in terms or not isinstance(terms["pricing_methods"], list) or "monte_carlo" not in terms["pricing_methods"]:
        issues.append("terms.pricing_methods必须包含monte_carlo")
    elif len(terms["pricing_methods"]) != len(set(terms["pricing_methods"])):
        issues.append("terms.pricing_methods不得重复")
    if set(terms.get("pricing_methods", [])) - {"black_scholes", "monte_carlo"}:
        issues.append("pricing_methods含未登记方法")
    if "constraints" in terms and (not isinstance(terms["constraints"], list) or not terms["constraints"]):
        issues.append("constraints只能省略或为非空列表")
    if "monitor" in terms and (not isinstance(terms["monitor"], Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in terms["monitor"].items())):
        issues.append("monitor必须为扁平字符串公式字典")
    if "derived_terms" in terms and (not isinstance(terms["derived_terms"], Mapping) or not terms["derived_terms"] or not all(isinstance(key, str) and isinstance(value, str) for key, value in terms["derived_terms"].items())):
        issues.append("derived_terms只能省略或为非空字符串公式字典")
    ordinary_terms = {key: value for key, value in terms.items() if key not in _RULE_TERM_KEYS}
    term_symbols: set[str] = set()
    try:
        _validate_terms(ordinary_terms, term_catalog)
        term_symbols = set(bind_term_symbols(ordinary_terms, term_catalog))
    except ContractResolutionError as error:
        issues.append(str(error))
    # 月度默认每月最后一个交易日，本次合同可覆盖为其他月内交易日。
    if term_symbols:
        derived_symbols: set[str] = set()
        derived_variables: dict[str, Any] = {}
        try:
            derived_variables = bind_term_symbols(ordinary_terms, term_catalog)
            for key, expression in terms.get("derived_terms", {}).items():
                if key in ordinary_terms:
                    issues.append(f"派生条款字段与基础条款重名：{key}")
                    continue
                if key not in term_catalog:
                    issues.append(f"派生条款字段{key}未登记在term_catalog")
                    continue
                issues.extend(_validate_expression(expression, term_symbols | derived_symbols, f"derived_terms.{key}", allow_cash=False))
                value = evaluate_formula(expression, derived_variables)
                _validate_terms({key: value}, term_catalog)
                symbol = str(term_catalog[key]["symbol"])
                if symbol in derived_variables:
                    issues.append(f"派生公式变量{symbol}重复绑定")
                    continue
                derived_variables[symbol] = value
                derived_symbols.add(symbol)
        except (ContractResolutionError, FormulaError) as error:
            issues.append(f"derived_terms无法解释：{error}")
        monitor = terms.get("monitor", {})
        if isinstance(monitor, Mapping):
            prior_monitor_symbols: set[str] = set()
            for monitor_key, expression in monitor.items():
                issues.extend(_validate_expression(
                    expression,
                    term_symbols | derived_symbols | _SETTLEMENT_VARIABLES | prior_monitor_symbols,
                    f"monitor.{monitor_key}",
                    allow_cash=False,
                ))
                prior_monitor_symbols.add(monitor_key)
        if "constraints" in terms:
            for index, expression in enumerate(terms["constraints"]):
                issues.extend(_validate_expression(expression, term_symbols | derived_symbols, f"constraints[{index}]", allow_cash=False))
            try:
                variables = bind_term_symbols(ordinary_terms, term_catalog)
                for key, expression in terms.get("derived_terms", {}).items():
                    value = evaluate_formula(expression, variables)
                    variables[str(term_catalog[key]["symbol"])] = value
                for expression in terms["constraints"]:
                    if not _coerce_bool(evaluate_formula(expression, variables)):
                        issues.append(f"默认条款不满足constraints：{expression}")
            except FormulaError as error:
                issues.append(f"constraints无法解释：{error}")
    if not isinstance(paths, list) or not paths:
        issues.append("paths必须为非空列表")
    else:
        for path in paths:
            if not isinstance(path, Mapping) or set(path) != _ALLOWED_PATH_KEYS or not isinstance(path.get("condition"), str) or not isinstance(path.get("cases"), list) or not path["cases"]:
                issues.append("路径必须且只能有condition与非空cases")
                continue
            for case in path["cases"]:
                if not isinstance(case, Mapping) or set(case) != _ALLOWED_CASE_KEYS or not all(isinstance(case.get(key), str) for key in _ALLOWED_CASE_KEYS):
                    issues.append("分段必须且只能有domain与pnl字符串")
            allowed = term_symbols | derived_symbols | _SETTLEMENT_VARIABLES | set(terms.get("monitor", {}))
            issues.extend(_validate_expression(path["condition"], allowed, "paths.condition", allow_cash=False))
            for case in path["cases"]:
                if isinstance(case, Mapping) and isinstance(case.get("domain"), str) and isinstance(case.get("pnl"), str):
                    issues.extend(_validate_expression(case["domain"], allowed, "paths.cases.domain", allow_cash=False))
                    issues.extend(_validate_expression(case["pnl"], allowed, "paths.cases.pnl", allow_cash=True, require_cash=True))
    return issues


def validate_registry(registry: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
    reg = registry or load_registry()
    errors: dict[str, list[str]] = {}
    for product_id, product in reg["products"].items():
        issues = validate_product_spec(product_id, product, reg["term_catalog"])
        if issues:
            errors[product_id] = issues
    return errors


def _validate_expression(
    expression: str,
    allowed_variables: set[str],
    label: str,
    *,
    allow_cash: bool,
    require_cash: bool = False,
) -> list[str]:
    """只做静态语义核验，不执行产品公式。"""
    try:
        tree = _parse_formula(expression)
    except FormulaError as error:
        return [f"{label}语法无效：{error}"]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    unknown = names - allowed_variables - _FORMULA_FUNCTIONS
    if unknown:
        return [f"{label}引用未登记变量：{','.join(sorted(unknown))}"]
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    if not allow_cash and "cash" in calls:
        return [f"{label}不得生成现金流"]
    if require_cash and "cash" not in calls:
        return [f"{label}必须使用cash(t, amount)表达现金流"]
    return []


def _validate_terms(terms: Mapping[str, Any], catalog: Mapping[str, Any]) -> None:
    for key, value in terms.items():
        if key not in catalog:
            raise ContractResolutionError(f"条款字段{key}未登记在term_catalog")
        definition = catalog[key]
        value_type = definition.get("value_type")
        domain = definition.get("domain", {})
        if value_type == "number":
            _check_number(value, key, domain)
        elif value_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ContractResolutionError(f"条款{key}必须为整数")
            _check_number(value, key, domain)
        elif value_type == "boolean":
            if not isinstance(value, bool):
                raise ContractResolutionError(f"条款{key}必须为布尔值")
        elif value_type == "string":
            if not isinstance(value, str):
                raise ContractResolutionError(f"条款{key}必须为字符串")
            if "enum" in domain and value not in domain["enum"]:
                raise ContractResolutionError(f"条款{key}不在允许枚举内")
            if domain.get("format") == "observation_schedule" and not _is_observation_schedule(value):
                raise ContractResolutionError(f"条款{key}不是有效观察日程：{value}")
        elif value_type == "number_list":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
                raise ContractResolutionError(f"条款{key}必须为非空数值列表")
            for item in value: _check_number(item, key, domain)
        elif value_type == "schedule":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < domain.get("min_items", 1):
                raise ContractResolutionError(f"条款{key}必须为非空日程")
        else:
            raise ContractResolutionError(f"term_catalog.{key}.value_type无效")


def _check_number(value: Any, key: str, domain: Mapping[str, Any]) -> None:
    if not isinstance(value, (int, float, np.number)) or isinstance(value, bool) or not np.isfinite(float(value)):
        raise ContractResolutionError(f"条款{key}必须为有限数值")
    number = float(value)
    if "min" in domain and number < float(domain["min"]): raise ContractResolutionError(f"条款{key}低于最小值")
    if "max" in domain and number > float(domain["max"]): raise ContractResolutionError(f"条款{key}高于最大值")
    if "exclusive_min" in domain and number <= float(domain["exclusive_min"]): raise ContractResolutionError(f"条款{key}必须大于{domain['exclusive_min']}")


def _validate_normalized_price_base(terms: Mapping[str, Any], *, allow_scaled_coordinate: bool = False) -> None:
    """价格水平统一以100为内部基准，真实参考价仅由identity提供。"""
    if allow_scaled_coordinate:
        return
    if "S0" in terms and not np.isclose(float(terms["S0"]), _NORMALIZED_PRICE_BASE, rtol=0.0, atol=1e-12):
        raise ContractResolutionError("S0是固定的内部标准化基准100，不可覆盖为其他数值；请在reference_prices传入真实合同参考价")
    if "S0Vec" in terms:
        levels = np.asarray(terms["S0Vec"], dtype=float)
        if not np.allclose(levels, _NORMALIZED_PRICE_BASE, rtol=0.0, atol=1e-12):
            raise ContractResolutionError("S0Vec各分量必须为内部标准化基准100；请在reference_prices逐一传入真实合同参考价")


def _normalized_reference_prices(terms: Mapping[str, Any], underlyings: Sequence[str]) -> dict[str, float] | None:
    """Derive normalized coordinate references only from frozen S0 facts."""

    if "S0Vec" in terms:
        values = tuple(float(value) for value in terms["S0Vec"])
        if len(values) != len(underlyings):
            raise ContractResolutionError("S0Vec与underlyings数量不一致，无法冻结参考价格")
        return dict(zip(underlyings, values, strict=True))
    if "S0" in terms:
        if len(underlyings) != 1:
            raise ContractResolutionError("多标的normalized_100合同必须使用S0Vec冻结逐标的参考价格")
        return {underlyings[0]: float(terms["S0"])}
    return None


def _formula_context(contract: ResolvedContract, price_path: PricePath) -> dict[str, Any]:
    if price_path.asset_ids != contract.underlyings:
        raise ContractResolutionError("价格路径asset_ids必须与ResolvedContract.underlyings完全一致")
    catalog = _shared_term_catalog()
    variables = bind_term_symbols(contract.terms, catalog)
    # 公式中的T是当前实际结算终点，用于ACT/365票息与期权费的实际天数折算。
    # 合同期限仍保留在ResolvedContract；evaluate_contract随后拒绝半程未终止路径，
    # 只有已发生的提前终止事件才能在合同到期前结算。
    if "T" in contract.terms:
        contractual_tenor = float(contract.terms["T"])
        if not price_path.times_explicit and not np.isclose(contractual_tenor, 1.0, rtol=0.0, atol=1e-12):
            raise ContractResolutionError(
                f"{contract.product_id}合同期限为{contractual_tenor}年，价格路径必须显式提供times；"
                "不得使用默认1年时间网格"
            )
        # T用于现金流的实际结算时点；T_contract保留合同约定期限，供明确
        # 只在到期观察日才可触发的事件判断，不能被相邻交易日的路径终点替代。
        variables["T_contract"] = contractual_tenor
        variables["T"] = float(price_path.times[-1])
    observation_price = str(contract.terms.get("observation_price", "close"))
    raw_values = price_path.values_for(observation_price)
    references = contract.identity.get("reference_prices")
    if references is None:
        if "S0Vec" in contract.terms:
            reference_values = np.asarray(contract.terms["S0Vec"], dtype=float)
        elif "S0" in contract.terms:
            reference_values = np.full(len(price_path.asset_ids), float(contract.terms["S0"]), dtype=float)
        else:
            # 方差互换等不以合同参考价定义经济条款的产品，保留输入价格的原始尺度。
            reference_values = np.asarray(raw_values[0], dtype=float)
    else:
        reference_values = np.asarray([references[asset] for asset in price_path.asset_ids], dtype=float)
    if reference_values.shape != (len(price_path.asset_ids),):
        raise ContractResolutionError("合同参考价数量与标的数量不一致")
    price_convention = str(contract.identity.get("price_convention") or "normalized_100")
    if price_convention == "absolute_market":
        # 兼容旧absolute_market输入，但解释器仍输出100基准点数。价格条款只在
        # 公式上下文转换，ResolvedContract保留原始合同录入值和身份参考价。
        normalized_reference = np.full(len(price_path.asset_ids), _NORMALIZED_PRICE_BASE, dtype=float)
        for key, value in contract.terms.items():
            definition = catalog.get(key, {})
            if definition.get("unit") != "price":
                continue
            symbol = str(definition.get("symbol", ""))
            if not symbol:
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                levels = np.asarray(value, dtype=float)
                if levels.shape == reference_values.shape:
                    variables[symbol] = levels / reference_values * _NORMALIZED_PRICE_BASE
            elif isinstance(value, (int, float, np.number)):
                variables[symbol] = float(value) / float(reference_values[0]) * _NORMALIZED_PRICE_BASE
    elif "S0Vec" in contract.terms:
        normalized_reference = np.asarray(contract.terms["S0Vec"], dtype=float)
    elif "S0" in contract.terms:
        normalized_reference = np.full(len(price_path.asset_ids), float(contract.terms["S0"]), dtype=float)
    else:
        normalized_reference = reference_values
    values = raw_values / reference_values[None, :] * normalized_reference[None, :]
    terminal = values[-1]
    performance = raw_values / reference_values[None, :] * 100.0
    primary_series = PriceSeries(values[:, 0], price_path.times, price_path.dates)
    worst_series = PriceSeries(np.min(performance, axis=1), price_path.times, price_path.dates)
    variables.update({
        "S_t": primary_series,
        "S_T": float(terminal[0]),
        # S_T已按合同的标准化初始价表达；r_T必须与该标准化尺度配对，
        # 从而恒等于原始价格的raw_terminal / contract_reference - 1。
        # 这既保留默认S_0=100的展示尺度，也支持任意合同参考价。
        "r_T": float(terminal[0] / normalized_reference[0] - 1.0),
        # 所有合同收益均以无货币单位的每100标准化单位表达。真实参考价只用于
        # raw路径归一化，绝不把普通期权价差换回货币金额。
        "u": 1.0,
        "W_t": worst_series,
        "W_T": float(np.min(performance[-1])),
    })
    return variables


def _is_observation_schedule(value: str) -> bool:
    """校验已明确的交易日观察日程。"""
    if value in {"daily", "monthly_last"}:
        return True
    match = re.fullmatch(r"monthly_(\d+)(st|nd|rd|th)", value)
    if not match:
        return False
    ordinal, suffix = int(match.group(1)), match.group(2)
    if not 1 <= ordinal <= 31:
        return False
    last_two = ordinal % 100
    expected = "th" if 11 <= last_two <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(ordinal % 10, "th")
    return suffix == expected


def _is_monthly_schedule(value: object) -> bool:
    """识别月度观察日程，供固定月票息结构的合同约束使用。"""
    return isinstance(value, str) and (value == "monthly_last" or bool(re.fullmatch(r"monthly_\d+(?:st|nd|rd|th)", value)))


def _require_maturity_observation(values: object, maturity: object) -> bool:
    """要求观察集合包含实际结算到期点，避免未观察到$T$却套用未触发分段。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("require_maturity_observation第一个参数必须为观察价格序列")
    terminal = _nonnegative_number(maturity, "require_maturity_observation到期时点")
    if not len(values.times) or not np.isclose(values.times[-1], terminal, rtol=0.0, atol=1e-12):
        raise FormulaError("合同约定的观察日程必须包含实际到期日")
    return True


def _schedule_positions(schedule: object, times: np.ndarray, dates: pd.DatetimeIndex | None) -> np.ndarray:
    if isinstance(schedule, str):
        if not _is_observation_schedule(schedule):
            raise FormulaError(f"无效观察日程：{schedule}")
        if schedule == "daily":
            return np.arange(len(times))
        if schedule == "monthly_last" or re.fullmatch(r"monthly_\d+(?:st|nd|rd|th)", schedule):
            positions = _monthly_positions(schedule, times, dates)
            return positions[times[positions] > 1e-12]
    if isinstance(schedule, Sequence) and not isinstance(schedule, (str, bytes)):
        positions = np.asarray(schedule, dtype=int)
        if (positions < 0).any() or (positions >= len(times)).any():
            raise FormulaError("观察日程索引超出价格路径")
        return positions
    raise FormulaError("观察日程仅支持daily、monthly_第n个交易日、monthly_last或整数索引序列")


def resolve_schedule(selector: str | Sequence[int], trading_dates: Sequence[Any]) -> tuple[pd.Timestamp, ...]:
    dates = pd.to_datetime(list(trading_dates), errors="coerce")
    if not len(dates) or dates.isna().any() or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise FormulaError("trading_dates必须为非空、严格递增且不重复的有效交易日")
    positions = _schedule_positions(selector, np.arange(len(dates), dtype=float), dates)
    return tuple(pd.Timestamp(dates[index]) for index in positions)


def resolve_schedules(terms: Mapping[str, Any], trading_dates: Sequence[Any]) -> Mapping[str, tuple[pd.Timestamp, ...]]:
    return deep_freeze({
        key: resolve_schedule(terms[key], trading_dates)
        for key in sorted(_OBSERVATION_TERM_KEYS & set(terms))
    })


def _schedule_observation_ordinals(
    schedule: object,
    times: np.ndarray,
    dates: pd.DatetimeIndex | None,
    positions: np.ndarray,
) -> np.ndarray:
    """返回观察日在完整合同日程中的序号。

    有日期的历史路径以输入的合同观察集合顺序编号；无日期的周期模拟路径则按
    绝对合同时间推定序号，以便分期障碍日程不会从中途路径重新从第1期开始。
    """
    if not isinstance(schedule, str) or dates is not None:
        return np.arange(1, len(positions) + 1, dtype=int)
    observation_times = times[positions]
    if schedule == "monthly_last":
        return _strictly_increasing_ordinals(np.maximum(1, np.rint(observation_times * 12.0).astype(int)))
    if re.fullmatch(r"monthly_\d+(?:st|nd|rd|th)", schedule):
        return _strictly_increasing_ordinals(np.maximum(1, np.ceil(observation_times * 12.0 - 1e-10).astype(int)))
    return np.arange(1, len(positions) + 1, dtype=int)


def _strictly_increasing_ordinals(values: np.ndarray) -> np.ndarray:
    """稀疏无日期网格被多个周期目标命中时，保留其先后周期次序。"""
    result = np.asarray(values, dtype=int).copy()
    for index in range(1, len(result)):
        result[index] = max(result[index], result[index - 1] + 1)
    return result


def _monthly_positions(schedule: str, times: np.ndarray, dates: pd.DatetimeIndex | None) -> np.ndarray:
    if dates is not None:
        periods = dates.to_period("M")
        positions: list[int] = []
        last_index = len(dates) - 1
        for period in periods.unique():
            month_positions = np.flatnonzero(periods == period)
            if schedule == "monthly_last":
                candidate = int(month_positions[-1])
                # 最后一个输入点仅在其确为自然月最后一个工作日时才是月度观察点。
                # 因此截断到期日不会被自动视为月末；节假日例外应以显式索引日程给出。
                is_business_month_end = dates[candidate] == dates[candidate] + pd.offsets.BMonthEnd(0)
                if candidate != last_index or is_business_month_end:
                    positions.append(candidate)
                continue
            ordinal = int(re.fullmatch(r"monthly_(\d+)(?:st|nd|rd|th)", schedule).group(1))
            if len(month_positions) >= ordinal:
                positions.append(int(month_positions[ordinal - 1]))
        return np.asarray(positions, dtype=int)
    # 无真实日期的Monte Carlo采用合同起点为锚点的月度网格。第n个交易日按每月
    # 21个交易日折算；不根据当前子路径的长度重新分配月份。
    if schedule == "monthly_last":
        offset = 1.0
    else:
        ordinal = int(re.fullmatch(r"monthly_(\d+)(?:st|nd|rd|th)", schedule).group(1))
        offset = min(ordinal, 21) / 21.0
    targets = _contract_grid_targets(times, 12, offset)
    return _nearest_nonterminal_positions(times, targets)


def _contract_grid_targets(times: np.ndarray, periods_per_year: int, offset: float) -> np.ndarray:
    """返回落在当前绝对合同时间窗口内的周期观察目标时点。

    ``offset``以一个周期为单位：周末为1，周内第n个交易日为n/5；月末为1，
    月内第n个交易日为min(n,21)/21。路径起点若本身是一个观察时点可以保留，
    但路径终点不因无日期的近似网格而自动加入观察集合。
    """
    if periods_per_year <= 0 or not 0.0 < offset <= 1.0:
        raise FormulaError("周期观察网格参数无效")
    start, end = float(times[0]), float(times[-1])
    first = int(np.ceil(start * periods_per_year - offset - 1e-12))
    last = int(np.floor(end * periods_per_year - offset + 1e-12))
    indices = np.arange(max(first, 0), last + 1, dtype=float)
    targets = (indices + offset) / periods_per_year
    return targets[(targets >= start - 1e-12) & (targets < end - 1e-12)]


def _nearest_nonterminal_positions(times: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """将无日期周期目标映射到最近模拟网格点，且不把终点视为自动观察日。"""
    positions: list[int] = []
    terminal = len(times) - 1
    for target in targets:
        right = int(np.searchsorted(times, target, side="left"))
        candidates = [index for index in (right - 1, right) if 0 <= index <= terminal]
        if not candidates:
            continue
        position = min(candidates, key=lambda index: abs(float(times[index]) - float(target)))
        if position != terminal:
            positions.append(position)
    return np.asarray(sorted(set(positions)), dtype=int)


def _first_time(value: object) -> float:
    vector = _event_vector(value)
    positions = np.flatnonzero(vector.values)
    return inf if not len(positions) else float(vector.times[positions[0]])


def _first_time_after(value: object, skipped_observations: object) -> float:
    """返回略过前n个观察期后的首次事件时点。"""
    vector = _event_vector(value)
    skipped = int(_nonnegative_number(skipped_observations, "first_time_after锁定观察期"))
    positions = np.flatnonzero(vector.values)
    positions = positions[positions >= skipped]
    return inf if not len(positions) else float(vector.times[positions[0]])


def _first_time_before(value: object, cutoff: object) -> float:
    """返回严格早于合同截止时点的首次事件，适用于明确排除到期日的观察。"""
    vector = _event_vector(value)
    end = _positive_number(cutoff, "first_time_before截止时点")
    positions = np.flatnonzero(vector.values & (vector.times < end))
    return inf if not len(positions) else float(vector.times[positions[0]])


def _first_time_levels(values: object, levels: object) -> float:
    """按每个观察期对应的障碍日程返回首次敲出时点。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("first_time_levels第一个参数必须为观察价格序列")
    thresholds = np.asarray(levels, dtype=float)
    if thresholds.ndim != 1 or thresholds.shape != values.values.shape:
        raise FormulaError("first_time_levels障碍日程必须与观察价格逐期对应")
    hits = np.flatnonzero(_numeric_relation(values.values, thresholds, "ge"))
    return inf if not len(hits) else float(values.times[hits[0]])


def _first_time_below_levels(values: object, levels: object) -> float:
    """按逐观察期障碍日程返回首次向下触发时点。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("first_time_below_levels第一个参数必须为观察价格序列")
    thresholds = np.asarray(levels, dtype=float)
    if thresholds.ndim != 1 or thresholds.shape != values.values.shape:
        raise FormulaError("first_time_below_levels障碍日程必须与观察价格逐期对应")
    hits = np.flatnonzero(_numeric_relation(values.values, thresholds, "le"))
    return inf if not len(hits) else float(values.times[hits[0]])


def _schedule_levels(values: object, schedule: object) -> np.ndarray:
    """将逐观察期障碍日程展开为与观察价格一一对应的数值序列。

    日程使用``[(1, level_1), ..., (n, level_n)]``，序号从1开始且必须完整覆盖
    本次观察集合。这是一种通用的合同日程表达，不包含产品专属分支。
    """
    if not isinstance(values, ObservedValues):
        raise FormulaError("schedule_levels第一个参数必须为观察价格序列")
    if not isinstance(schedule, Sequence) or isinstance(schedule, (str, bytes)):
        raise FormulaError("schedule_levels日程必须为(观察序号,水平)序列")
    levels = np.full(len(values.values), np.nan, dtype=float)
    for item in schedule:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise FormulaError("schedule_levels日程项必须是(观察序号,水平)")
        ordinal, level = item
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise FormulaError("schedule_levels观察序号必须为正整数")
        # 日程可以覆盖完整合同期限，而当前历史或模拟路径只包含其中已发生的
        # 绝对观察期。无日期中途模拟不会把合同第13期错误重编号为本段第1期。
        positions = np.flatnonzero(values.ordinals == ordinal)
        if not len(positions):
            continue
        if len(positions) != 1 or np.isfinite(levels[positions[0]]):
            raise FormulaError("schedule_levels观察序号重复")
        levels[positions[0]] = _finite_number(level, "schedule_levels障碍水平")
    if not np.isfinite(levels).all():
        raise FormulaError("schedule_levels日程未完整覆盖观察集合")
    return levels


def _schedule_between(schedule: object, lower: object, upper: object) -> bool:
    """校验(观察序号,水平)日程严格递增且所有水平位于开区间内。"""
    if not isinstance(schedule, Sequence) or isinstance(schedule, (str, bytes)) or not schedule:
        raise FormulaError("schedule_between日程必须为非空(观察序号,水平)序列")
    lower_value = _finite_number(lower, "schedule_between下界")
    upper_value = _finite_number(upper, "schedule_between上界")
    if lower_value >= upper_value:
        return False
    prior_ordinal = 0
    for item in schedule:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise FormulaError("schedule_between日程项必须是(观察序号,水平)")
        ordinal, level = item
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= prior_ordinal:
            return False
        level_value = _finite_number(level, "schedule_between障碍水平")
        if not lower_value < level_value < upper_value:
            return False
        prior_ordinal = ordinal
    return True


def _schedule_matches_ratios(schedule: object, reference: object, ratios: object) -> bool:
    """校验观察障碍日程按1基连续编号，并等于参考价格乘以逐期比例。"""
    if not isinstance(schedule, Sequence) or isinstance(schedule, (str, bytes)):
        raise FormulaError("schedule_matches_ratios日程必须为(观察序号,水平)序列")
    if not isinstance(ratios, Sequence) or isinstance(ratios, (str, bytes)) or not ratios:
        raise FormulaError("schedule_matches_ratios比例必须为非空序列")
    if len(schedule) != len(ratios):
        return False
    reference_value = _finite_number(reference, "schedule_matches_ratios参考价格")
    for expected_ordinal, (item, ratio) in enumerate(zip(schedule, ratios), start=1):
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise FormulaError("schedule_matches_ratios日程项必须是(观察序号,水平)")
        ordinal, level = item
        if ordinal != expected_ordinal:
            return False
        level_value = _finite_number(level, "schedule_matches_ratios障碍水平")
        ratio_value = _finite_number(ratio, "schedule_matches_ratios比例")
        if not np.isclose(level_value, reference_value * ratio_value, rtol=0.0, atol=1e-10):
            return False
    return True


def _step_levels(values: object, schedule: object, activation: object = "on") -> np.ndarray:
    """按合同时间分段展开障碍水平。

    日程使用``[(起始时点,水平), ...]``，第一个起始时点必须为0。每个观察时点
    activation=on时新水平在起始时点生效；activation=after时在起始时点后的
    首个观察时点生效，适用于周年当日仍沿用上一档的合同条款。
    """
    if not isinstance(values, ObservedValues):
        raise FormulaError("step_levels第一个参数必须为观察价格序列")
    if not isinstance(schedule, Sequence) or isinstance(schedule, (str, bytes)) or not schedule:
        raise FormulaError("step_levels日程必须为非空(起始时点,水平)序列")
    starts: list[float] = []
    levels: list[float] = []
    for item in schedule:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise FormulaError("step_levels日程项必须是(起始时点,水平)")
        starts.append(_nonnegative_number(item[0], "step_levels起始时点"))
        levels.append(_finite_number(item[1], "step_levels障碍水平"))
    if starts[0] != 0.0 or any(later <= earlier for earlier, later in zip(starts, starts[1:])):
        raise FormulaError("step_levels日程起始时点须从0开始且严格递增")
    if activation not in {"on", "after"}:
        raise FormulaError("step_levels生效方式仅支持on或after")
    side = "right" if activation == "on" else "left"
    positions = np.searchsorted(np.asarray(starts), values.times, side=side) - 1
    positions[values.times <= starts[0]] = 0
    return np.asarray(levels, dtype=float)[positions]


def _first_time_below_schedule(values: object, schedule: object) -> float:
    """在稀疏合同观察日程中返回首次严格跌破对应水平的时点。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("first_time_below_schedule第一个参数必须为观察价格序列")
    if not isinstance(schedule, Sequence) or isinstance(schedule, (str, bytes)):
        raise FormulaError("first_time_below_schedule日程必须为(观察序号,水平)序列")
    hits: list[int] = []
    seen: set[int] = set()
    for item in schedule:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise FormulaError("first_time_below_schedule日程项必须是(观察序号,水平)")
        ordinal, level = item
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= len(values.values):
            raise FormulaError("first_time_below_schedule观察序号超出范围")
        if ordinal in seen:
            raise FormulaError("first_time_below_schedule观察序号重复")
        seen.add(ordinal)
        if values.values[ordinal - 1] < _finite_number(level, "first_time_below_schedule障碍水平"):
            hits.append(ordinal - 1)
    return inf if not hits else float(values.times[min(hits)])


def _first_observation_on_or_after(values: object, event_time: object) -> float:
    """返回事件发生当日或其后首个约定观察日；事件未发生时返回无穷。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("first_observation_on_or_after第一个参数必须为观察价格序列")
    if event_time == inf:
        return inf
    start = _nonnegative_number(event_time, "first_observation_on_or_after事件时点")
    positions = np.flatnonzero(values.times >= start)
    return inf if not len(positions) else float(values.times[positions[0]])


def _terminal_event_time(value: object, maturity: object) -> float:
    """仅在实际到期点属于给定观察集合且满足事件时返回到期时点。

    周/月度日程不会因为价格路径结束而自动加入到期点；本函数只用于合同
    明确把最后一档事件限定在到期观察日的情形。
    """
    vector = _event_vector(value)
    terminal = _nonnegative_number(maturity, "terminal_event_time到期时点")
    if not len(vector.times) or not np.isclose(vector.times[-1], terminal, rtol=0.0, atol=1e-12):
        return inf
    return float(terminal) if bool(vector.values[-1]) else inf


def _reset_knockout_time(
    knockout_prices: object,
    reset_time: object,
    initial_knockout_level: object,
    reset_knockout_level: object,
) -> float:
    """重置观察日之前按初始敲出线、当日及之后按重置线判断首次敲出。"""
    if not isinstance(knockout_prices, ObservedValues):
        raise FormulaError("reset_knockout_time第一个参数必须为敲出观察价格序列")
    reset = inf if reset_time == inf else _nonnegative_number(reset_time, "reset_knockout_time重置时点")
    h_initial = _finite_number(initial_knockout_level, "reset_knockout_time初始敲出线")
    h_reset = _finite_number(reset_knockout_level, "reset_knockout_time重置敲出线")
    thresholds = np.where(knockout_prices.times >= reset, h_reset, h_initial)
    hits = np.flatnonzero(knockout_prices.values >= thresholds)
    return inf if not len(hits) else float(knockout_prices.times[hits[0]])


def _effective_hedge_time(
    raw_hedge_time: object,
    knockout_time: object,
    knockin_time: object,
    ki_history_policy: object,
) -> float:
    """按确认书定义避险观察日是否计入历史敲入范围。"""
    if ki_history_policy not in {"include_hedge_date", "exclude_hedge_date"}:
        raise FormulaError("effective_hedge_time敲入历史口径无效")
    raw = inf if raw_hedge_time == inf else _nonnegative_number(raw_hedge_time, "effective_hedge_time避险时点")
    knockout = inf if knockout_time == inf else _nonnegative_number(knockout_time, "effective_hedge_time敲出时点")
    knockin = inf if knockin_time == inf else _nonnegative_number(knockin_time, "effective_hedge_time敲入时点")
    if raw == inf or raw >= knockout:
        return inf
    if ki_history_policy == "include_hedge_date":
        return raw if raw < knockin else inf
    return raw if raw <= knockin else inf


def _first_value(values: object, event: object) -> float:
    """返回事件首次成立时对应的观察价格。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("first_value第一个参数必须为观察价格序列")
    vector = _event_vector(event, values.times)
    if not np.array_equal(vector.times, values.times):
        raise FormulaError("first_value的价格与事件日程不一致")
    positions = np.flatnonzero(vector.values)
    return inf if not len(positions) else float(values.values[positions[0]])


def _count(value: object) -> int:
    if isinstance(value, EventVector): return int(value.values.sum())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)): return len(value)
    raise FormulaError("count仅支持观察事件或序列")


def _count_until(value: object, cutoff: object) -> int:
    """统计不晚于cutoff的观察数；cutoff=inf表示整个观察集合。"""
    end = inf if cutoff == inf else _nonnegative_number(cutoff, "count_until截止时点")
    if isinstance(value, EventVector):
        return int(np.sum(value.values & (value.times <= end)))
    if isinstance(value, ObservedValues):
        return int(np.sum(value.times <= end))
    raise FormulaError("count_until仅支持观察事件或观察价格序列")


def _exact_observation_count(values: object, expected: object) -> int:
    """确认运行路径与合同约定观察日数一致，避免分母与实际观察次数错配。"""
    if not isinstance(values, ObservedValues):
        raise FormulaError("exact_observation_count第一个参数必须为观察价格序列")
    required = int(_positive_number(expected, "exact_observation_count合同观察日数"))
    if required != expected:
        raise FormulaError("exact_observation_count合同观察日数必须为正整数")
    actual = len(values.values)
    if actual != required:
        raise FormulaError(f"观察日数{actual}与合同n_obs={required}不一致；请按实际观察日程覆盖n_obs")
    return actual


def _observation_ordinal(value: object, event_time: object) -> float:
    """返回事件在观察集合中的1基序号；事件未发生时返回无穷。"""
    if not isinstance(value, ObservedValues):
        raise FormulaError("observation_ordinal第一个参数必须为观察价格序列")
    if event_time == inf:
        return inf
    event = _nonnegative_number(event_time, "observation_ordinal事件时点")
    positions = np.flatnonzero(np.isclose(value.times, event, rtol=0.0, atol=1e-12))
    if not len(positions):
        raise FormulaError("observation_ordinal事件时点不属于观察集合")
    return float(positions[0] + 1)


def _realized_variance(value: object, annualization_days: object) -> float:
    series = value.values if isinstance(value, ObservedValues) else np.asarray(value, dtype=float)
    if len(series) < 2: raise FormulaError("方差计算至少需要两个观察值")
    if not np.isfinite(series).all() or (series <= 0).any():
        raise FormulaError("realized_variance要求全部观察价格严格为正")
    annual = _positive_number(annualization_days, "annualization_days")
    log_return = np.diff(np.log(series))
    return float(annual * np.mean(log_return ** 2))


def _accumulated_quantity(
    prices: object,
    knockout_level: object,
    strike: object,
    base_quantity: object,
    multiplier: object,
    lockout_observations: object,
    expected_observations: object,
    maturity: object,
) -> float:
    """标准累购的逐观察日成交量与保股期敲出规则。"""
    if not isinstance(prices, ObservedValues):
        raise FormulaError("accumulated_quantity第一个参数必须为观察价格序列")
    h_out = _finite_number(knockout_level, "accumulated_quantity敲出价")
    k = _finite_number(strike, "accumulated_quantity执行价")
    q = _positive_number(base_quantity, "accumulated_quantity基础数量")
    m = _positive_number(multiplier, "accumulated_quantity累计倍数")
    lock = int(_nonnegative_number(lockout_observations, "accumulated_quantity保股期"))
    expected = int(_positive_number(expected_observations, "accumulated_quantity观察日数"))
    if float(expected_observations) != expected:
        raise FormulaError("accumulated_quantity观察日数必须为正整数")
    end = _positive_number(maturity, "accumulated_quantity到期时点")
    values = prices.values
    if len(values) > expected:
        raise FormulaError("accumulated_quantity观察价格数量超过n_obs")
    if prices.times[-1] >= end - 1e-12 and len(values) != expected:
        raise FormulaError("accumulated_quantity完整到期路径必须正好覆盖n_obs个观察日")
    knockout_events = _numeric_relation(values, h_out, "ge")
    if knockout_events is None:
        raise FormulaError("accumulated_quantity敲出比较必须为数值")
    hits = np.flatnonzero(knockout_events)
    knockout_index = int(hits[0]) if len(hits) else None
    if knockout_index is None:
        variable_days = range(len(values))
        forced_days: range = range(0)
    elif knockout_index + 1 <= lock:
        variable_days = range(knockout_index)
        forced_days = range(knockout_index, min(lock, len(values)))
    else:
        variable_days = range(knockout_index)
        forced_days = range(0)
    quantity = 0.0
    for index in variable_days:
        below_strike = _numeric_relation(values[index], k, "lt")
        if below_strike is None:
            raise FormulaError("accumulated_quantity执行价比较必须为数值")
        quantity += q * (1.0 + (m - 1.0) * float(below_strike))
    return float(quantity + len(forced_days) * q)


def _cash(time: object, amount: object) -> CashflowBundle:
    return CashflowBundle((Cashflow(_nonnegative_number(time, "cash时间"), _finite_number(amount, "cash金额")),))


def _compare(left: object, right: object, operator: ast.cmpop) -> Any:
    relation = (
        "lt" if isinstance(operator, ast.Lt) else "le" if isinstance(operator, ast.LtE)
        else "gt" if isinstance(operator, ast.Gt) else "ge" if isinstance(operator, ast.GtE)
        else "eq" if isinstance(operator, ast.Eq) else "ne" if isinstance(operator, ast.NotEq) else None
    )
    if relation is None:
        raise FormulaError("不允许的比较运算")
    numeric_result = _numeric_relation(left, right, relation)
    if numeric_result is not None:
        return numeric_result
    if isinstance(operator, ast.Lt): return left < right  # type: ignore[operator]
    if isinstance(operator, ast.LtE): return left <= right  # type: ignore[operator]
    if isinstance(operator, ast.Gt): return left > right  # type: ignore[operator]
    if isinstance(operator, ast.GtE): return left >= right  # type: ignore[operator]
    if isinstance(operator, ast.Eq): return left == right
    if isinstance(operator, ast.NotEq): return left != right
    raise FormulaError("不允许的比较运算")


def _event_vector(value: object, times: Sequence[float] | None = None) -> EventVector:
    if isinstance(value, EventVector): return value
    if isinstance(value, (bool, np.bool_)):
        grid = np.asarray(times if times is not None else [0.0], dtype=float)
        return EventVector(np.full(grid.shape, bool(value)), grid)
    raise FormulaError("此公式位置需要路径事件布尔序列")


def _event_times(left: object, right: object) -> np.ndarray:
    if isinstance(left, EventVector): return left.times
    if isinstance(right, EventVector): return right.times
    return np.asarray([0.0])


def _coerce_bool(value: object) -> bool:
    if isinstance(value, (bool, np.bool_)): return bool(value)
    if isinstance(value, np.ndarray) and value.size == 1: return bool(value.item())
    raise FormulaError("路径条件与分段定义域必须返回标量布尔值")


def _positive_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0: raise FormulaError(f"{label}必须为正数")
    return number


def _nonnegative_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0: raise FormulaError(f"{label}不得为负")
    return number


def _finite_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float, np.number)) or isinstance(value, bool) or not np.isfinite(float(value)):
        raise FormulaError(f"{label}必须为有限数值")
    return float(value)


def _bounded_formula_value(value: Any) -> Any:
    if isinstance(value, CashflowBundle):
        return value
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as error:
        raise FormulaError("公式算术仅支持数值或现金流") from error
    if array.size > _MAX_FORMULA_ITEMS or not np.issubdtype(array.dtype, np.number):
        raise FormulaError("公式算术结果超过受限范围")
    try:
        numeric = np.asarray(array, dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise FormulaError("公式算术结果不是受限数值") from error
    if not np.isfinite(numeric).all() or (np.abs(numeric) > _MAX_ABS_FORMULA_VALUE).any():
        raise FormulaError("公式算术结果超过受限范围")
    return value


def _json_scalar_map(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, np.generic): result[key] = value.item()
        elif isinstance(value, np.ndarray): result[key] = value.tolist()
        else: result[key] = value
    return result
