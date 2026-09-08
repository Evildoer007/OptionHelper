"""七模块共用的最小协议对象。

本文件只定义跨模块外形。产品条款、定价参数与回测参数仍分别由
ResolvedContract、Pricer和Backtester负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import isfinite
import re
from typing import TYPE_CHECKING, Any, Mapping

from runtime.contracts.contract_types import deep_freeze, deep_thaw

if TYPE_CHECKING:
    from runtime.contracts.contract_api import ResolvedContract


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field}必须是受控标识符")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field}必须为64位小写SHA-256")
    return value


def _opaque_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field}必须为不含控制字符的不透明引用")
    if value.startswith(("/", "~")) or ".." in value.replace("\\", "/").split("/"):
        raise ValueError(f"{field}必须是不含物理路径的不透明引用")
    return value


@dataclass(frozen=True)
class DataAssetRef:
    data_asset_id: str
    storage_ref: str
    media_type: str
    schema_id: str
    asset_ids: tuple[str, ...]
    normalized_fields: tuple[str, ...]
    coverage: Mapping[str, Any]
    row_count: int
    price_convention: Mapping[str, Any]
    content_hash: str
    lineage: Mapping[str, Any]
    tenant_id: str = "local"
    created_by: str = "local"
    access_scope: tuple[str, ...] = ("read",)
    partition_spec: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.data_asset_id, "DataAssetRef.data_asset_id")
        _opaque_ref(self.storage_ref, "DataAssetRef.storage_ref")
        for name in ("media_type", "schema_id", "tenant_id", "created_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"DataAssetRef.{name}必须为不含控制字符的非空字符串")
        _hash(self.content_hash, "DataAssetRef.content_hash")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or self.row_count < 0:
            raise ValueError("DataAssetRef.row_count必须为非负整数")
        for name in ("asset_ids", "normalized_fields", "access_scope"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"DataAssetRef.{name}必须为非空字符串元组")
            object.__setattr__(self, name, tuple(value))
        for name in ("coverage", "price_convention", "lineage", "partition_spec"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"DataAssetRef.{name}必须为对象")
            object.__setattr__(self, name, deep_freeze(value))


@dataclass(frozen=True)
class CallerContext:
    tenant_id: str
    principal_id: str
    role: str
    capabilities: tuple[str, ...]
    session_id: str
    audience: str
    request_id: str = ""

    def __post_init__(self) -> None:
        for field in ("tenant_id", "principal_id", "role", "session_id", "audience"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"CallerContext.{field}必须为不含控制字符的非空字符串")
        if (
            not isinstance(self.capabilities, tuple)
            or any(not isinstance(item, str) or not item.strip() or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in self.capabilities)
            or len(set(self.capabilities)) != len(self.capabilities)
        ):
            raise ValueError("CallerContext.capabilities必须为不重复的非空字符串元组")
        if not isinstance(self.request_id, str) or any(ord(char) < 32 or ord(char) == 127 for char in self.request_id):
            raise ValueError("CallerContext.request_id不得包含控制字符")


@dataclass(frozen=True)
class SecretRef:
    """跨Host传递的凭据引用，绝不承载凭据值。"""

    provider: str
    key: str
    revision: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.key.strip():
            raise ValueError("SecretRef requires non-empty provider and key")
        if self.revision is not None and not self.revision.strip():
            raise ValueError("SecretRef revision must be non-empty when supplied")

    def redacted(self) -> dict[str, str | None]:
        return {"provider": self.provider, "key": self.key, "revision": self.revision}


@dataclass(frozen=True)
class DataFetchRunRef:
    tenant_id: str
    task_id: str
    data_fetch_run_id: str
    expected_manifest_hash: str
    expected_data_asset_hash: str

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "DataFetchRunRef.tenant_id")
        _identifier(self.task_id, "DataFetchRunRef.task_id")
        _identifier(self.data_fetch_run_id, "DataFetchRunRef.data_fetch_run_id")
        _hash(self.expected_manifest_hash, "DataFetchRunRef.expected_manifest_hash")
        _hash(self.expected_data_asset_hash, "DataFetchRunRef.expected_data_asset_hash")


@dataclass(frozen=True)
class ModuleRunRef:
    module: str
    tenant_id: str
    task_id: str
    run_id: str
    expected_result_file_hash: str
    expected_artifact_manifest_hash: str

    def __post_init__(self) -> None:
        if self.module not in {"payoffer", "pricer", "backtester"}:
            raise ValueError("ModuleRunRef.module必须为三个计算模块之一")
        _identifier(self.tenant_id, "ModuleRunRef.tenant_id")
        _identifier(self.task_id, "ModuleRunRef.task_id")
        _identifier(self.run_id, "ModuleRunRef.run_id")
        _hash(self.expected_result_file_hash, "ModuleRunRef.expected_result_file_hash")
        _hash(self.expected_artifact_manifest_hash, "ModuleRunRef.expected_artifact_manifest_hash")


@dataclass(frozen=True)
class ReportRunRef:
    tenant_id: str
    report_run_id: str
    expected_report_file_hash: str
    expected_artifact_manifest_hash: str

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "ReportRunRef.tenant_id")
        _identifier(self.report_run_id, "ReportRunRef.report_run_id")
        _hash(self.expected_report_file_hash, "ReportRunRef.expected_report_file_hash")
        _hash(self.expected_artifact_manifest_hash, "ReportRunRef.expected_artifact_manifest_hash")


@dataclass(frozen=True)
class ObservedContractEvent:
    """Host已核验的单个合同事件，禁止携带模块私有扩展字段。"""

    event_type: str
    event_date: str
    observation_stage: int | None = None
    accumulated_count: int | None = None
    accumulated_quantity: float | None = None

    def __post_init__(self) -> None:
        _require_nonempty_text(self.event_type, "ObservedContractEvent.event_type")
        _require_iso_date(self.event_date, "ObservedContractEvent.event_date")
        for name, value in (("observation_stage", self.observation_stage), ("accumulated_count", self.accumulated_count)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"ObservedContractEvent.{name}必须为非负整数或null")
        if self.accumulated_quantity is not None:
            if isinstance(self.accumulated_quantity, bool) or not isinstance(self.accumulated_quantity, (int, float)) or not isfinite(float(self.accumulated_quantity)) or self.accumulated_quantity < 0:
                raise ValueError("ObservedContractEvent.accumulated_quantity必须为非负有限数值或null")
            object.__setattr__(self, "accumulated_quantity", float(self.accumulated_quantity))

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "event_date": self.event_date,
            "observation_stage": self.observation_stage,
            "accumulated_count": self.accumulated_count,
            "accumulated_quantity": self.accumulated_quantity,
        }


@dataclass(frozen=True)
class RealizedCashflow:
    """Host已核验的已实现现金流事实。"""

    payment_date: str
    amount: float
    status: str
    cashflow_id: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _require_iso_date(self.payment_date, "RealizedCashflow.payment_date")
        if isinstance(self.amount, bool) or not isinstance(self.amount, (int, float)) or not isfinite(float(self.amount)):
            raise ValueError("RealizedCashflow.amount必须为有限数值")
        object.__setattr__(self, "amount", float(self.amount))
        if self.status not in {"paid", "settled", "realized"}:
            raise ValueError("RealizedCashflow.status必须为paid、settled或realized")
        for name in ("cashflow_id", "currency"):
            value = getattr(self, name)
            if value is not None:
                _require_nonempty_text(value, f"RealizedCashflow.{name}")

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "cashflow_id": self.cashflow_id,
            "payment_date": self.payment_date,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
        }


@dataclass(frozen=True)
class ObservedContractState:
    """Pricer正式请求中由Host冻结的存续合同事实。"""

    valuation_date: str | None
    lifecycle_status: str = "initial"
    occurred_events: tuple[ObservedContractEvent, ...] = ()
    realized_cashflows: tuple[RealizedCashflow, ...] = ()
    source_refs: tuple[str, ...] = ()
    observation_history: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.valuation_date is not None:
            _require_iso_date(self.valuation_date, "ObservedContractState.valuation_date")
        if self.lifecycle_status not in {"initial", "active", "terminated", "matured"}:
            raise ValueError("ObservedContractState.lifecycle_status无效")
        if not isinstance(self.occurred_events, tuple) or not all(isinstance(item, ObservedContractEvent) for item in self.occurred_events):
            raise TypeError("ObservedContractState.occurred_events必须为Host冻结ObservedContractEvent元组")
        if not isinstance(self.realized_cashflows, tuple) or not all(isinstance(item, RealizedCashflow) for item in self.realized_cashflows):
            raise TypeError("ObservedContractState.realized_cashflows必须为Host冻结RealizedCashflow元组")
        if not isinstance(self.source_refs, tuple) or any(not isinstance(item, str) or not item.strip() for item in self.source_refs):
            raise TypeError("ObservedContractState.source_refs必须为非空字符串元组")
        if (self.occurred_events or self.realized_cashflows or self.observation_history) and not self.source_refs:
            raise ValueError("ObservedContractState含已发生事实时必须提供source_refs")
        if self.observation_history is not None:
            object.__setattr__(self, "observation_history", validate_observation_history(
                self.observation_history, valuation_date=self.valuation_date,
            ))
        if self.valuation_date is not None:
            cutoff = date.fromisoformat(self.valuation_date)
            if any(date.fromisoformat(item.event_date) > cutoff for item in self.occurred_events):
                raise ValueError("ObservedContractState事件日期不得晚于估值日")
            if any(date.fromisoformat(item.payment_date) > cutoff for item in self.realized_cashflows):
                raise ValueError("ObservedContractState已实现现金流日期不得晚于估值日")
    @classmethod
    def from_host_payload(cls, value: Mapping[str, Any]) -> "ObservedContractState":
        if not isinstance(value, Mapping):
            raise TypeError("observed_contract_state必须为Host对象")
        allowed = {"valuation_date", "lifecycle_status", "occurred_events", "realized_cashflows", "source_refs", "observation_history"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("observed_contract_state含未知字段：" + ",".join(sorted(str(item) for item in unknown)))
        events = tuple(_observed_event_from_payload(item) for item in value.get("occurred_events", ()))
        cashflows = tuple(_realized_cashflow_from_payload(item) for item in value.get("realized_cashflows", ()))
        source_refs = value.get("source_refs", ())
        if isinstance(source_refs, (str, bytes)):
            raise TypeError("observed_contract_state.source_refs必须为字符串序列")
        return cls(
            valuation_date=value.get("valuation_date"),
            lifecycle_status=value.get("lifecycle_status", "initial"),
            occurred_events=events,
            realized_cashflows=cashflows,
            source_refs=tuple(source_refs),
            observation_history=value.get("observation_history"),
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "valuation_date": self.valuation_date,
            "lifecycle_status": self.lifecycle_status,
            "occurred_events": [item.to_protocol_dict() for item in self.occurred_events],
            "realized_cashflows": [item.to_protocol_dict() for item in self.realized_cashflows],
            "source_refs": list(self.source_refs),
            **({"observation_history": deep_thaw(self.observation_history)} if self.observation_history is not None else {}),
        }

    def to_protocol_dict(self) -> dict[str, Any]:
        return self._content_payload()


def validate_observation_history(value: Mapping[str, Any], *, valuation_date: str | None) -> Mapping[str, Any]:
    """冻结实际交易日价格；该对象不允许未来行情或未标明来源的累计假设。"""
    fields = {"dates", "asset_ids", "close", "price_fields"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("observation_history必须包含dates、asset_ids、close和price_fields")
    dates, assets = value["dates"], value["asset_ids"]
    if not isinstance(dates, (tuple, list)) or not dates:
        raise ValueError("observation_history.dates必须为非空交易日序列")
    for day in dates:
        _require_iso_date(day, "observation_history.dates")
    if tuple(dates) != tuple(sorted(set(dates))) or valuation_date is None or dates[-1] != valuation_date:
        raise ValueError("observation_history日期必须严格递增并截止于实际估值会话")
    if not isinstance(assets, (tuple, list)) or not assets or len(set(assets)) != len(assets):
        raise ValueError("observation_history.asset_ids必须为不重复的标的序列")
    for asset in assets:
        _require_nonempty_text(asset, "observation_history.asset_ids")
    price_fields = value["price_fields"]
    if not isinstance(price_fields, Mapping) or set(price_fields) - {"open", "high", "low"}:
        raise ValueError("observation_history.price_fields仅允许open、high和low")
    for name, matrix in {"close": value["close"], **dict(price_fields)}.items():
        if not isinstance(matrix, (tuple, list)) or len(matrix) != len(dates):
            raise ValueError(f"observation_history.{name}必须逐日覆盖")
        for row in matrix:
            if not isinstance(row, (tuple, list)) or len(row) != len(assets):
                raise ValueError(f"observation_history.{name}必须逐一覆盖标的")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) or x <= 0 for x in row):
                raise ValueError(f"observation_history.{name}价格必须为有限正数")
    return deep_freeze(value)


@dataclass(frozen=True)
class PricingInput:
    contract: ResolvedContract
    pricing_config: Mapping[str, Any]
    market_data_refs: tuple[DataAssetRef, ...] = ()
    trading_calendar_ref: DataAssetRef | None = None
    observed_contract_state: ObservedContractState | None = None
    pricing_objective: "PricingObjective | None" = None

    def __post_init__(self) -> None:
        # Keep this protocol object strict at the Core boundary.  Mapping
        # projections are accepted only by the request adapter, which first
        # reconstructs and verifies a complete ResolvedContract.
        from runtime.contracts.contract_api import ResolvedContract

        if not isinstance(self.contract, ResolvedContract):
            raise TypeError("PricingInput.contract必须为完整ResolvedContract")
        if not isinstance(self.pricing_config, Mapping):
            raise TypeError("PricingInput.pricing_config必须为对象")
        if not isinstance(self.market_data_refs, tuple) or not all(
            isinstance(item, DataAssetRef) for item in self.market_data_refs
        ):
            raise TypeError("PricingInput.market_data_refs必须为DataAssetRef元组")
        if self.trading_calendar_ref is not None and not isinstance(self.trading_calendar_ref, DataAssetRef):
            raise TypeError("PricingInput.trading_calendar_ref必须为DataAssetRef或null")
        if self.observed_contract_state is not None and not isinstance(self.observed_contract_state, ObservedContractState):
            raise TypeError("PricingInput.observed_contract_state必须为Host冻结ObservedContractState")
        object.__setattr__(self, "pricing_config", deep_freeze(self.pricing_config))
        objective = self.pricing_objective
        if objective is not None and not isinstance(objective, PricingObjective):
            objective = PricingObjective.from_value(objective)
            object.__setattr__(self, "pricing_objective", objective)

    def to_protocol_dict(self) -> dict[str, Any]:
        """Serialize the formal input without inventing optional fields.

        In particular, the ordinary valuation default leaves
        ``pricing_objective`` absent.  This keeps the legacy request payload
        and its semantic hash stable while an explicit valuation objective is
        still retained as an audit-visible field.
        """

        def asset_payload(value: DataAssetRef) -> dict[str, Any]:
            return deep_thaw({
                "data_asset_id": value.data_asset_id,
                "storage_ref": value.storage_ref,
                "media_type": value.media_type,
                "schema_id": value.schema_id,
                "asset_ids": value.asset_ids,
                "normalized_fields": value.normalized_fields,
                "coverage": value.coverage,
                "row_count": value.row_count,
                "price_convention": value.price_convention,
                "content_hash": value.content_hash,
                "lineage": value.lineage,
                "tenant_id": value.tenant_id,
                "created_by": value.created_by,
                "access_scope": value.access_scope,
                "partition_spec": value.partition_spec,
            })

        result: dict[str, Any] = {
            "contract": self.contract.to_protocol_dict(),
            "pricing_config": deep_thaw(self.pricing_config),
            "market_data_refs": [asset_payload(item) for item in self.market_data_refs],
        }
        if self.trading_calendar_ref is not None:
            result["trading_calendar_ref"] = asset_payload(self.trading_calendar_ref)
        if self.observed_contract_state is not None:
            result["observed_contract_state"] = self.observed_contract_state.to_protocol_dict()
        if self.pricing_objective is not None:
            result["pricing_objective"] = self.pricing_objective.to_protocol_dict()
        return result


@dataclass(frozen=True)
class PricingObjective:
    """The only business-level switch between valuation and fair solving.

    ``None`` on :class:`PricingInput` deliberately means the legacy ordinary
    valuation flow.  The object is intentionally tiny: all target semantics,
    domains and transformations stay in the Pricer capability directory.
    """

    mode: str
    target_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"valuation", "fair_parameter"}:
            raise ValueError("pricing_objective.mode只能为valuation或fair_parameter")
        if self.mode == "valuation" and self.target_id is not None:
            raise ValueError("pricing_objective.mode=valuation不得提交target_id")
        if self.mode == "fair_parameter":
            if not isinstance(self.target_id, str) or not self.target_id.strip():
                raise ValueError("pricing_objective.mode=fair_parameter必须提交target_id")
            _identifier(self.target_id, "pricing_objective.target_id")

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "PricingObjective") -> "PricingObjective":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("pricing_objective必须为对象")
        unknown = set(value) - {"mode", "target_id"}
        if unknown:
            raise ValueError(
                "pricing_objective含未知字段："
                + ",".join(sorted(str(item) for item in unknown))
            )
        if "mode" not in value:
            raise ValueError("pricing_objective缺少mode")
        return cls(mode=value["mode"], target_id=value.get("target_id"))

    def to_protocol_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode}
        if self.target_id is not None:
            result["target_id"] = self.target_id
        return result


@dataclass(frozen=True)
class BacktestInput:
    contract: ResolvedContract
    backtest_config: Mapping[str, Any]
    historical_data: DataAssetRef


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    storage_ref: str
    media_type: str
    content_hash: str


@dataclass(frozen=True)
class ModuleRun:
    module: str
    analysis_case_id: str
    task_id: str
    run_id: str
    status: str
    input_snapshot: Mapping[str, Any]
    data_refs: tuple[DataAssetRef, ...]
    result: Mapping[str, Any] | None
    resolved_contract_snapshot: Mapping[str, Any] | None = None
    limitations: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error: Mapping[str, Any] | None = None
    candidate_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: str = "local"
    created_by: str = "local"
    access_scope: tuple[str, ...] = ("read",)
    artifact_manifest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_module_name(self.module)
        require_run_status(self.status)
        for label, value in (("analysis_case_id", self.analysis_case_id), ("task_id", self.task_id), ("run_id", self.run_id)):
            if not value:
                raise ValueError(f"ModuleRun.{label}不能为空")
        if self.status in {"succeeded", "partial"}:
            if not self.candidate_id:
                raise ValueError("成功或部分成功的ModuleRun.candidate_id不能为空")
            if not isinstance(self.resolved_contract_snapshot, Mapping):
                raise ValueError("成功或部分成功的ModuleRun必须保存完整ResolvedContract快照")
            identity = self.resolved_contract_snapshot.get("identity")
            revision = identity.get("rule_revision") if isinstance(identity, Mapping) else None
            if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
                raise ValueError("ModuleRun的ResolvedContract快照必须包含正整数rule_revision")


def require_module_name(value: str) -> str:
    allowed = {"payoffer", "pricer", "backtester"}
    if value not in allowed:
        raise ValueError(f"ModuleRun.module必须为{','.join(sorted(allowed))}")
    return value


def require_run_status(value: str) -> str:
    allowed = {
        "created", "validating", "queued", "running", "succeeded", "partial", "failed",
        "unsupported", "cancelled", "timed_out",
    }
    if value not in allowed:
        raise ValueError(f"运行状态无效：{value}")
    return value


def _observed_event_from_payload(value: Any) -> ObservedContractEvent:
    if not isinstance(value, Mapping):
        raise TypeError("observed_contract_state.occurred_events必须为对象列表")
    allowed = {"event_type", "event_date", "observation_stage", "accumulated_count", "accumulated_quantity"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("observed_contract_state.occurred_events含未知字段：" + ",".join(sorted(str(item) for item in unknown)))
    missing = {"event_type", "event_date"} - set(value)
    if missing:
        raise ValueError("observed_contract_state.occurred_events缺少字段：" + ",".join(sorted(missing)))
    return ObservedContractEvent(
        event_type=value["event_type"],
        event_date=value["event_date"],
        observation_stage=value.get("observation_stage"),
        accumulated_count=value.get("accumulated_count"),
        accumulated_quantity=value.get("accumulated_quantity"),
    )


def _realized_cashflow_from_payload(value: Any) -> RealizedCashflow:
    if not isinstance(value, Mapping):
        raise TypeError("observed_contract_state.realized_cashflows必须为对象列表")
    allowed = {"cashflow_id", "payment_date", "amount", "currency", "status"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("observed_contract_state.realized_cashflows含未知字段：" + ",".join(sorted(str(item) for item in unknown)))
    missing = {"payment_date", "amount", "status"} - set(value)
    if missing:
        raise ValueError("observed_contract_state.realized_cashflows缺少字段：" + ",".join(sorted(missing)))
    return RealizedCashflow(
        cashflow_id=value.get("cashflow_id"),
        payment_date=value["payment_date"],
        amount=value["amount"],
        currency=value.get("currency"),
        status=value["status"],
    )


def _require_nonempty_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label}必须为不含控制字符的非空字符串")


def _require_iso_date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须为YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label}必须为YYYY-MM-DD") from error


_RUN_TRANSITIONS = {
    "created": {"validating", "cancelled"},
    "validating": {"queued", "unsupported", "failed", "cancelled"},
    "queued": {"running", "cancelled", "timed_out"},
    "running": {"succeeded", "partial", "failed", "cancelled", "timed_out"},
}


def require_run_transition(current: str, target: str) -> tuple[str, str]:
    require_run_status(current)
    require_run_status(target)
    if target not in _RUN_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法运行状态转换：{current}->{target}")
    return current, target


__all__ = (
    "ArtifactRef", "BacktestInput", "CallerContext", "DataAssetRef", "DataFetchRunRef",
    "ModuleRun", "ModuleRunRef", "ObservedContractEvent", "ObservedContractState",
    "PricingInput", "PricingObjective", "RealizedCashflow", "ReportRunRef", "SecretRef",
    "require_module_name", "require_run_status", "require_run_transition",
)
