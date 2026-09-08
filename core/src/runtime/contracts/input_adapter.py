"""Compile App-friendly calculator requests into the three formal inputs.

This is the only page/agent request adapter for calculator runs.  Product
semantics remain owned by the Registry Loader and ``resolve_contract``; the
App supplies only authenticated data references and Host scope.
"""

from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from math import isclose, isfinite
import re
from typing import Any, Mapping, Sequence
import weakref


if __name__ != "runtime.contracts.input_adapter":
    raise ImportError(
        "Core input_adapter必须通过正式runtime.contracts.input_adapter路径导入"
    )

from .contract_engine import (
    ContractResolutionError,
    ResolvedContract,
    counted_observation_key,
    derive_contract_end_date,
    load_registry,
    resolve_contract,
    resolve_structural_payoff_contract,
    resolve_schedule,
)
from .contract_types import deep_thaw, semantic_hash
from runtime.protocol.models import DataAssetRef, ObservedContractState, PricingObjective
from runtime.ports.data_store import DataStoreReadPort


_CALCULATORS = frozenset({"payoffer", "pricer", "backtester"})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMON_FIELDS = frozenset({
    "action", "product_id", "identity", "term_overrides",
    # This is a first-run page intent, not a contract identity field.  The
    # Host consumes it while compiling the data-backed contract and never
    # forwards it to a formal calculator input.
    "auto_contract_start_date",
})
_OBSERVATION_TERM_KEYS = frozenset({"O_KO", "O_KI", "Oc", "Otouch", "Orange", "Ovar", "Ohedge", "Oreset"})


# These seals are deliberately process-local.  They are not protocol values,
# are not serialised, and are never accepted from an App/Skill request.  The
# registry keeps a weak reference so an expired binding cannot pin memory or
# leave an id-reuse record that could be mistaken for a newly-created object.
_BINDING_SEALS: dict[int, tuple[weakref.ReferenceType[object], object, str, str]] = {}
_SUPPORTED_TARGET_STATUSES = frozenset({"supported"})
_NON_SUPPORTED_TARGET_STATUSES = frozenset({"unsupported", "solve_semantics_blocked"})
_CASHFLOW_SIGN_CONVENTION_SCHEMA_ID = "optionhelper.pricer.cashflow-sign-convention"
_INITIAL_PRICING_TIME_RULE_SCHEMA_ID = "optionhelper.pricer.initial-pricing-time-rule"
_STRICT_ISO_CALENDAR_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _strict_iso_calendar_date(value: object, *, label: str) -> date:
    """Parse only the canonical ten-character calendar-date representation."""

    if not isinstance(value, str) or _STRICT_ISO_CALENDAR_DATE.fullmatch(value) is None:
        raise ContractResolutionError(
            f"{label}必须严格使用十字符YYYY-MM-DD日期"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ContractResolutionError(
            f"{label}必须为有效YYYY-MM-DD日期"
        ) from error
    if parsed.isoformat() != value:
        raise ContractResolutionError(
            f"{label}必须使用规范YYYY-MM-DD表示"
        )
    return parsed


def _protocol_sequence(value: object, *, label: str) -> tuple[Any, ...]:
    """Accept only the list/tuple sequence forms used by the formal payload."""

    if not isinstance(value, (list, tuple)):
        raise ContractResolutionError(f"{label}必须为list或tuple序列")
    return tuple(value)


def _validate_cashflow_sign_convention(
    value: object,
    *,
    label: str,
) -> None:
    """Validate the executable holder-side accounting rule in a target spec."""

    if not isinstance(value, Mapping):
        raise ContractResolutionError(f"{label}必须为对象")
    payload = deep_thaw(value)
    required = {
        "schema_id",
        "valuation_perspective",
        "holder_cashflow_signs",
        "target_coefficient_sign",
        "target_coefficient",
        "residual",
        "source",
    }
    if set(payload) != required:
        raise ContractResolutionError(
            f"{label}字段必须精确覆盖：" + ",".join(sorted(required))
        )
    if payload["schema_id"] != _CASHFLOW_SIGN_CONVENTION_SCHEMA_ID:
        raise ContractResolutionError(f"{label}.schema_id无效")
    if payload["valuation_perspective"] != "contract_holder":
        raise ContractResolutionError(f"{label}必须使用contract_holder视角")
    if payload["holder_cashflow_signs"] != {"received": 1, "paid": -1}:
        raise ContractResolutionError(
            f"{label}.holder_cashflow_signs必须明确received=1、paid=-1"
        )
    sign = payload["target_coefficient_sign"]
    if sign not in {"positive", "negative"}:
        raise ContractResolutionError(f"{label}.target_coefficient_sign无效")
    coefficient = payload["target_coefficient"]
    if not isinstance(coefficient, Mapping) or set(coefficient) != {"symbol", "sign", "direction"}:
        raise ContractResolutionError(f"{label}.target_coefficient必须包含symbol、sign、direction")
    if not isinstance(coefficient["symbol"], str) or not coefficient["symbol"].strip():
        raise ContractResolutionError(f"{label}.target_coefficient.symbol无效")
    if coefficient["sign"] != sign or coefficient["direction"] != ({"positive": 1, "negative": -1}[sign]):
        raise ContractResolutionError(f"{label}.target_coefficient方向与sign不一致")
    residual = payload["residual"]
    if residual != {
        "operation": "subtract",
        "left": "V",
        "right": "Q",
        "equals": 0.0,
    }:
        raise ContractResolutionError(f"{label}.residual必须严格定义为V-Q=0")
    source = payload["source"]
    if not isinstance(source, Mapping) or set(source) != {"kind", "target_symbol", "coefficient_sign"}:
        raise ContractResolutionError(f"{label}.source必须绑定现金流证据")
    if (
        not isinstance(source["kind"], str)
        or not source["kind"].strip()
        or source["target_symbol"] != coefficient["symbol"]
        or source["coefficient_sign"] != sign
    ):
        raise ContractResolutionError(f"{label}.source与目标系数证据不一致")


def _validate_initial_pricing_time_rule(
    value: object,
    *,
    label: str,
) -> None:
    """Validate the registered new-issuance time rule without executing it."""

    if not isinstance(value, Mapping):
        raise ContractResolutionError(f"{label}必须为对象")
    payload = deep_thaw(value)
    required = {
        "schema_id",
        "kind",
        "required_lifecycle_status",
        "valuation_date",
        "prior_events",
        "prior_cashflows",
        "first_economic_observation",
        "failure_status",
    }
    if set(payload) != required:
        raise ContractResolutionError(
            f"{label}字段必须精确覆盖：" + ",".join(sorted(required))
        )
    if payload["schema_id"] != _INITIAL_PRICING_TIME_RULE_SCHEMA_ID:
        raise ContractResolutionError(f"{label}.schema_id无效")
    if payload["kind"] != "new_issuance_at_contract_start":
        raise ContractResolutionError(f"{label}.kind无效")
    if payload["required_lifecycle_status"] != "initial":
        raise ContractResolutionError(f"{label}必须要求initial状态")
    if payload["valuation_date"] != {
        "field": "valuation_date",
        "relation": "equals",
        "other_field": "contract_start_date",
    }:
        raise ContractResolutionError(f"{label}.valuation_date必须等于contract_start_date")
    if payload["prior_events"] != {
        "field": "occurred_events",
        "count_relation": "equals",
        "count": 0,
    }:
        raise ContractResolutionError(f"{label}.prior_events必须要求数量为0")
    if payload["prior_cashflows"] != {
        "field": "realized_cashflows",
        "count_relation": "equals",
        "count": 0,
    }:
        raise ContractResolutionError(f"{label}.prior_cashflows必须要求数量为0")
    if payload["first_economic_observation"] != {
        "field": "first_economic_observation_date",
        "relation": "on_or_after",
        "other_field": "valuation_date",
        "presence": "if_present",
    }:
        raise ContractResolutionError(f"{label}.first_economic_observation规则无效")
    if payload["failure_status"] != "initial_pricing_time_invalid":
        raise ContractResolutionError(f"{label}.failure_status无效")


def _validate_target_spec_semantics(
    value: object,
    *,
    label: str,
) -> None:
    """Validate directory-owned semantic rules before issuing a Core binding."""

    if not isinstance(value, Mapping):
        raise ContractResolutionError(f"{label}必须为对象")
    payload = deep_thaw(value)
    status = payload.get("support_status")
    if status in _SUPPORTED_TARGET_STATUSES:
        if payload.get("valuation_perspective") != "contract_holder":
            raise ContractResolutionError(f"{label}缺少contract_holder估值视角")
        _validate_cashflow_sign_convention(
            payload.get("cashflow_sign_convention"),
            label=f"{label}.cashflow_sign_convention",
        )
        _validate_initial_pricing_time_rule(
            payload.get("initial_pricing_time_rule"),
            label=f"{label}.initial_pricing_time_rule",
        )
        return
    if status in _NON_SUPPORTED_TARGET_STATUSES:
        for field_name in (
            "valuation_perspective",
            "cashflow_sign_convention",
            "initial_pricing_time_rule",
        ):
            if payload.get(field_name) is not None:
                raise ContractResolutionError(
                    f"{label}非supported目标不得伪造{field_name}"
                )
        return
    # A formal directory must identify the target status.  Older synthetic
    # objects used only by unsealed negative tests are still rejected by the
    # binding seal before candidate recompilation.
    if "support_status" in payload:
        raise ContractResolutionError(f"{label}.support_status无效")


def validate_initial_pricing_time_context(
    rule: Mapping[str, Any],
    *,
    valuation_date: object,
    contract_start_date: object,
    lifecycle_status: object,
    occurred_events: Sequence[Any] = (),
    realized_cashflows: Sequence[Any] = (),
    first_economic_observation_date: object | None = None,
) -> None:
    """Execute the registered initial-pricing boundary against Host facts.

    This helper is intentionally independent of the Pricer implementation so
    a later formal run can apply the exact same rule without trusting an App
    or Skill-provided interpretation.
    """

    _validate_initial_pricing_time_rule(rule, label="initial_pricing_time_rule")
    if lifecycle_status != "initial":
        raise ContractResolutionError("initial_pricing_time_invalid:生命周期必须为initial")
    valuation = _strict_iso_calendar_date(
        valuation_date,
        label="initial_pricing_time_invalid:valuation_date",
    )
    contract_start = _strict_iso_calendar_date(
        contract_start_date,
        label="initial_pricing_time_invalid:contract_start_date",
    )
    if valuation != contract_start:
        raise ContractResolutionError("initial_pricing_time_invalid:valuation_date必须等于contract_start_date")
    events = _protocol_sequence(
        occurred_events,
        label="initial_pricing_time_invalid:occurred_events",
    )
    cashflows = _protocol_sequence(
        realized_cashflows,
        label="initial_pricing_time_invalid:realized_cashflows",
    )
    if events or cashflows:
        raise ContractResolutionError("initial_pricing_time_invalid:不得存在已发生事件或已实现现金流")
    if first_economic_observation_date is not None:
        first_observation = _strict_iso_calendar_date(
            first_economic_observation_date,
            label="initial_pricing_time_invalid:首个经济观察日",
        )
        if first_observation < valuation:
            raise ContractResolutionError("initial_pricing_time_invalid:首个经济观察不得早于valuation_date")


@dataclass(frozen=True)
class ComputeDataRequirements:
    """Core-owned market and calendar demand for one calculator request."""

    history_required: bool
    future_calendar_required: bool
    historical_fields: tuple[str, ...]
    observation_price: str
    tenor_years: float
    observation_count: int | None
    observation_selector: str | tuple[int, ...] | None = None

    @property
    def calendar_lookahead_days(self) -> int:
        """Bound acquisition only; actual observation dates require verified sessions."""
        tenor_days = round(self.tenor_years * 365.0)
        if self.observation_count is None:
            return tenor_days
        if isinstance(self.observation_selector, str) and self.observation_selector.startswith("monthly_"):
            count_days = (self.observation_count + 2) * 31
        elif isinstance(self.observation_selector, tuple):
            count_days = (max(self.observation_selector) + 1) * 3 + 31
        else:
            count_days = self.observation_count * 3 + 31
        return max(tenor_days, count_days)

    def observation_end_date(self, sessions: Sequence[str], start_date: str) -> str | None:
        """Locate the nth selected authenticated session without estimating dates."""
        if self.observation_count is None or self.observation_selector is None:
            return None
        available = tuple(value for value in sessions if value >= start_date)
        if not available:
            return None
        selected = resolve_schedule(self.observation_selector, available)
        if len(selected) < self.observation_count:
            return None
        return selected[self.observation_count - 1].date().isoformat()


def _binding_content_hash(binding: object, kind: str) -> str:
    if kind != "verified_trading_calendar" or type(binding) is not VerifiedTradingCalendarBinding:
        raise ContractResolutionError(f"未知Core binding类型：{kind}")
    calendar = binding
    return semantic_hash({
        "payload": _calendar_binding_payload(
            calendar.calendar_ref,
            calendar.calendar_id,
            calendar.calendar_revision,
            calendar.sessions,
        ),
        "binding_hash": calendar.binding_hash,
    })


def _issue_binding(binding: object, kind: str) -> object:
    seal = object()
    object.__setattr__(binding, "_source_seal", seal)
    binding_id = id(binding)

    def _remove_expired(
        expired_ref: weakref.ReferenceType[object],
        *,
        binding_id: int = binding_id,
    ) -> None:
        entry = _BINDING_SEALS.get(binding_id)
        if entry is not None and entry[0] is expired_ref:
            _BINDING_SEALS.pop(binding_id, None)

    binding_ref = weakref.ref(binding, _remove_expired)
    _BINDING_SEALS[binding_id] = (
        binding_ref,
        seal,
        kind,
        _binding_content_hash(binding, kind),
    )
    return binding


def _require_binding_seal(binding: object, kind: str) -> None:
    if kind != "verified_trading_calendar" or type(binding) is not VerifiedTradingCalendarBinding:
        raise ContractResolutionError(f"Core {kind}必须为factory签发的精确binding")
    entry = _BINDING_SEALS.get(id(binding))
    seal = getattr(binding, "_source_seal", None)
    if entry is None:
        raise ContractResolutionError(f"Core {kind}来源seal校验失败")
    binding_ref, entry_seal, entry_kind, content_hash = entry
    if binding_ref() is not binding or entry_seal is not seal or entry_kind != kind:
        raise ContractResolutionError(f"Core {kind}来源seal校验失败")
    if content_hash != _binding_content_hash(binding, kind):
        raise ContractResolutionError(f"Core {kind}内容在factory签发后发生变化")


def _asset_ref_payload(reference: DataAssetRef) -> dict[str, Any]:
    """Return the full immutable DataAssetRef facts used by a binding hash."""

    return deep_thaw({
        "data_asset_id": reference.data_asset_id,
        "storage_ref": reference.storage_ref,
        "media_type": reference.media_type,
        "schema_id": reference.schema_id,
        "asset_ids": reference.asset_ids,
        "normalized_fields": reference.normalized_fields,
        "coverage": reference.coverage,
        "row_count": reference.row_count,
        "price_convention": reference.price_convention,
        "content_hash": reference.content_hash,
        "lineage": reference.lineage,
        "tenant_id": reference.tenant_id,
        "created_by": reference.created_by,
        "access_scope": reference.access_scope,
        "partition_spec": reference.partition_spec,
    })


def _calendar_binding_payload(
    reference: DataAssetRef,
    calendar_id: str,
    calendar_revision: str,
    sessions: Sequence[str],
) -> dict[str, Any]:
    return {
        "calendar_ref": _asset_ref_payload(reference),
        "calendar_id": calendar_id,
        "calendar_revision": calendar_revision,
        "sessions": tuple(sessions),
    }


@dataclass(frozen=True)
class VerifiedTradingCalendarBinding:
    """Trusted in-process binding of verified calendar bytes and sessions."""

    calendar_ref: DataAssetRef
    calendar_id: str
    calendar_revision: str
    sessions: tuple[str, ...]
    binding_hash: str
    _source_seal: object | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.calendar_ref) is not DataAssetRef:
            raise ContractResolutionError("VerifiedTradingCalendarBinding.calendar_ref必须为DataAssetRef")
        if self.calendar_ref.schema_id != "trading-calendar":
            raise ContractResolutionError("VerifiedTradingCalendarBinding必须绑定trading-calendar DataAssetRef")
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            raise ContractResolutionError("VerifiedTradingCalendarBinding.calendar_id必须为非空字符串")
        if not isinstance(self.calendar_revision, str) or not self.calendar_revision.strip():
            raise ContractResolutionError("VerifiedTradingCalendarBinding.calendar_revision必须为非空字符串")
        if not isinstance(self.sessions, tuple):
            raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions必须为元组")
        _validate_calendar_sessions(self.sessions)
        coverage = self.calendar_ref.coverage
        if coverage.get("calendar_id") not in {None, self.calendar_id}:
            raise ContractResolutionError("VerifiedTradingCalendarBinding.calendar_id与DataAssetRef.coverage不一致")
        if coverage.get("calendar_revision") not in {None, self.calendar_revision}:
            raise ContractResolutionError("VerifiedTradingCalendarBinding.calendar_revision与DataAssetRef.coverage不一致")
        declared_sessions = coverage.get("sessions")
        if declared_sessions is not None and tuple(str(item) for item in declared_sessions) != self.sessions:
            raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions与DataAssetRef.coverage不一致")
        _validate_sha256(self.binding_hash, "VerifiedTradingCalendarBinding.binding_hash")
        expected_hash = semantic_hash(_calendar_binding_payload(
            self.calendar_ref,
            self.calendar_id,
            self.calendar_revision,
            self.sessions,
        ))
        if self.binding_hash != expected_hash:
            raise ContractResolutionError("VerifiedTradingCalendarBinding.binding_hash校验失败")

    @property
    def data_asset_id(self) -> str:
        return self.calendar_ref.data_asset_id

    @property
    def content_hash(self) -> str:
        return self.calendar_ref.content_hash


def _validate_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ContractResolutionError(f"{label}必须为64位小写SHA-256")
    if len(set(value)) == 1:
        raise ContractResolutionError(f"{label}不得使用伪造占位哈希")


def _validate_calendar_sessions(values: tuple[object, ...]) -> None:
    if not values:
        raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions必须为非空元组")
    if any(not isinstance(value, str) for value in values):
        raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions必须为ISO日期元组")
    try:
        normalized = tuple(date.fromisoformat(value).isoformat() for value in values)
    except ValueError as error:
        raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions必须为ISO日期元组") from error
    if normalized != values or values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ContractResolutionError("VerifiedTradingCalendarBinding.sessions必须严格递增且不重复")


def preflight_fair_parameter_request(
    request: Mapping[str, Any],
    *,
    target_spec: Mapping[str, Any],
    target_product_id: str,
    resolved_contract: ResolvedContract | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate the no-data boundary for an explicit fair-parameter request.

    This is deliberately an internal Core preflight. The verified tool entry
    obtains the target from the current Pricer directory, never from a request.
    It checks only target identity, fixed dependencies and
    the registered initial-pricing-time rule.  It does not resolve a contract,
    read a DataStore, resolve a calendar, or execute a solver.
    """

    if not isinstance(request, Mapping):
        raise ContractResolutionError("公平参数预检请求必须为对象")
    objective_value = request.get("pricing_objective")
    if objective_value is None:
        return None
    try:
        objective = PricingObjective.from_value(objective_value)
    except (TypeError, ValueError) as error:
        raise ContractResolutionError(f"pricing_objective无效：{error}") from error
    if objective.mode != "fair_parameter":
        return None
    if not isinstance(target_spec, Mapping):
        raise ContractResolutionError("公平参数预检缺少当前目录目标规范")
    if target_spec.get("target_id") != objective.target_id:
        raise ContractResolutionError("pricing_objective.target_id与Core目标绑定不一致")

    target_payload = deep_thaw(target_spec)
    _validate_target_spec_semantics(target_payload, label="Pricer目标规范")
    if target_payload.get("support_status") != "supported":
        raise ContractResolutionError(
            "fair_parameter目标未获得正式反解支持："
            + str(target_payload.get("unsupported_reason") or "unsupported")
        )

    request_identity = request.get("identity", {})
    if not isinstance(request_identity, Mapping):
        raise ContractResolutionError("fair_parameter.identity必须为对象")
    config = request.get("pricing_config")
    if not isinstance(config, Mapping):
        raise ContractResolutionError("fair_parameter.pricing_config必须为对象")
    method = config.get("model_method")
    allowed_methods = target_payload.get("allowed_methods", ())
    allowed_method_values = (
        set(allowed_methods) if isinstance(allowed_methods, (list, tuple)) else set()
    )
    if method is not None and method not in allowed_method_values:
        raise ContractResolutionError("公平参数目标不支持当前定价方法")
    product_id = request.get("product_id")
    contract_identity: Mapping[str, Any] = request_identity
    contract_terms: Mapping[str, Any] | None = None
    first_observation: str | None = None
    if resolved_contract is not None:
        if isinstance(resolved_contract, ResolvedContract):
            contract_identity = resolved_contract.identity
            contract_terms = resolved_contract.terms
            schedules = resolved_contract.resolved_schedules
        elif isinstance(resolved_contract, Mapping):
            candidate_identity = resolved_contract.get("identity", {})
            candidate_terms = resolved_contract.get("terms", {})
            if not isinstance(candidate_identity, Mapping) or not isinstance(candidate_terms, Mapping):
                raise ContractResolutionError("公平参数预检的Host合同缺少identity或terms")
            contract_identity = candidate_identity
            contract_terms = candidate_terms
            schedules = resolved_contract.get("resolved_schedules")
        else:
            raise ContractResolutionError("公平参数预检的Host合同类型无效")
        contract_product = contract_identity.get("product_id")
        if isinstance(product_id, str) and product_id.strip() and contract_product != product_id.strip():
            raise ContractResolutionError("公平参数目标与Host当前合同产品不一致")
        product_id = contract_product
        if isinstance(schedules, Mapping):
            dates = [
                str(day)
                for schedule in schedules.values()
                if isinstance(schedule, Mapping)
                for day in schedule.get("dates", ())
            ]
            if dates:
                try:
                    first_observation = min(date.fromisoformat(day) for day in dates).isoformat()
                except ValueError:
                    first_observation = dates[0]
    if not isinstance(product_id, str) or not product_id.strip():
        raise ContractResolutionError("公平参数预检缺少product_id")
    if target_product_id != product_id.strip():
        raise ContractResolutionError("公平参数目标与product_id不一致")

    fixed_dependencies = target_payload.get("fixed_dependencies", ())
    if not isinstance(fixed_dependencies, (list, tuple)):
        raise ContractResolutionError("公平参数目标fixed_dependencies无效")
    if contract_terms is None:
        registered = load_registry().get("products", {}).get(product_id.strip())
        registered_terms = registered.get("terms") if isinstance(registered, Mapping) else None
        if not isinstance(registered_terms, Mapping):
            raise ContractResolutionError("公平参数预检找不到受控产品条款")
        overrides = request.get("term_overrides", {})
        if not isinstance(overrides, Mapping):
            raise ContractResolutionError("fair_parameter.term_overrides必须为对象")
        contract_terms = {**registered_terms, **dict(overrides)}
    missing = [key for key in fixed_dependencies if key not in contract_terms]
    if missing:
        raise ContractResolutionError(
            "公平参数基础合同缺少目标固定依赖：" + ",".join(str(key) for key in missing)
        )

    valuation_date = config.get("valuation_date")
    if valuation_date in {None, ""}:
        raise ContractResolutionError("公平参数预检必须提供pricing_config.valuation_date")
    auto_start = request.get("auto_contract_start_date", False)
    if not isinstance(auto_start, bool):
        raise ContractResolutionError("auto_contract_start_date必须为布尔值")
    supplied_start = contract_identity.get("contract_start_date")
    if auto_start and supplied_start not in {None, ""}:
        raise ContractResolutionError("自动合同起始日不得同时提交具体日期")
    contract_start = supplied_start or valuation_date
    observed = request.get("observed_contract_state")
    if observed is None:
        lifecycle_status = "initial"
        occurred_events: Sequence[Any] = ()
        realized_cashflows: Sequence[Any] = ()
    else:
        if not isinstance(observed, Mapping):
            raise ContractResolutionError("fair_parameter.observed_contract_state必须为Host对象")
        try:
            frozen_state = ObservedContractState.from_host_payload(observed)
        except (TypeError, ValueError) as error:
            raise ContractResolutionError(f"Host冻结observed_contract_state无效：{error}") from error
        lifecycle_status = frozen_state.lifecycle_status
        occurred_events = frozen_state.occurred_events
        realized_cashflows = frozen_state.realized_cashflows
    validate_initial_pricing_time_context(
        target_payload["initial_pricing_time_rule"],
        valuation_date=valuation_date,
        contract_start_date=contract_start,
        lifecycle_status=lifecycle_status,
        occurred_events=occurred_events,
        realized_cashflows=realized_cashflows,
        first_economic_observation_date=first_observation,
    )
    return {
        "mode": "fair_parameter",
        "target_id": objective.target_id,
        "initial_pricing_time_status": "validated",
    }


def compile_compute_data_requirements(
    module: str,
    request: Mapping[str, Any],
    resolved_contract: Mapping[str, Any] | None = None,
) -> ComputeDataRequirements:
    if module not in _CALCULATORS or not isinstance(request, Mapping):
        raise ContractResolutionError("计算数据需求必须绑定正式计算模块")
    if isinstance(resolved_contract, Mapping):
        terms = resolved_contract.get("terms")
    else:
        product_id = request.get("product_id")
        product = load_registry().get("products", {}).get(product_id) if isinstance(product_id, str) else None
        registered = product.get("terms") if isinstance(product, Mapping) else None
        overrides = request.get("term_overrides", {})
        if not isinstance(registered, Mapping) or not isinstance(overrides, Mapping):
            terms = {}
        else:
            terms = {**registered, **dict(overrides)}
    terms = terms if isinstance(terms, Mapping) else {}
    observation_price = str(terms.get("observation_price", "close"))
    if observation_price not in {"close", "open", "high", "low"}:
        raise ContractResolutionError("合同observation_price必须为close、open、high或low")
    fields = ["close", "adj_close"]
    if observation_price not in fields:
        fields.append(observation_price)
    if "T" not in terms:
        raise ContractResolutionError("合同条款必须显式提供期限T")
    try:
        tenor = float(terms["T"])
    except (TypeError, ValueError) as error:
        raise ContractResolutionError("合同期限T必须为有效数字") from error
    if not isfinite(tenor) or tenor <= 0:
        raise ContractResolutionError("合同期限T必须为正有限数值")
    raw_observation_count = terms.get("n_obs")
    if raw_observation_count is None:
        observation_count = None
    else:
        try:
            numeric_observation_count = float(raw_observation_count)
        except (TypeError, ValueError) as error:
            raise ContractResolutionError("合同n_obs必须为正整数") from error
        if (
            not isfinite(numeric_observation_count)
            or numeric_observation_count <= 0
            or not numeric_observation_count.is_integer()
        ):
            raise ContractResolutionError("合同n_obs必须为正整数")
        observation_count = int(numeric_observation_count)
    has_observations = bool(_OBSERVATION_TERM_KEYS.intersection(terms)) or bool(terms.get("monitor"))
    observation_key = counted_observation_key(terms)
    selector = terms[observation_key] if observation_key is not None else None
    return ComputeDataRequirements(
        history_required=module != "payoffer",
        # Payoffer freezes only a normalized structural schedule. It never
        # turns that internal grid into market or exchange-calendar evidence.
        future_calendar_required=module == "pricer" and has_observations,
        historical_fields=tuple(fields),
        observation_price=observation_price,
        tenor_years=tenor,
        observation_count=observation_count,
        observation_selector=tuple(selector) if isinstance(selector, (list, tuple)) else selector,
    )


def prepare_compute_request(
    module: str,
    request: Mapping[str, Any],
    *,
    data_refs: Sequence[Mapping[str, Any]] = (),
    data_store: DataStoreReadPort | None = None,
) -> dict[str, Any]:
    """Return one formal business request and its immutable contract facts."""
    if module not in _CALCULATORS or not isinstance(request, Mapping):
        raise ContractResolutionError("计算请求必须指定Payoffer、Pricer或Backtester")
    values = dict(request)
    action = str(values.get("action", "run")).strip().lower()
    if action != "run":
        raise ContractResolutionError("正式计算输入编译器只处理run")
    allowed = (
        {"action", "product_id", "term_overrides"}
        if module == "payoffer"
        else set(_COMMON_FIELDS)
    )
    if module == "pricer":
        allowed.update({"pricing_config", "observed_contract_state", "pricing_objective"})
    elif module == "backtester":
        allowed.add("backtest_config")
    if module == "pricer":
        allowed.update({"market_data_refs", "trading_calendar_ref"})
    if module == "backtester":
        allowed.add("historical_data")
    unknown = set(values) - allowed
    if unknown:
        raise ContractResolutionError("计算请求含未知字段：" + ",".join(sorted(unknown)))

    product_id = values.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        raise ContractResolutionError("product_id不能为空")
    overrides = values.get("term_overrides", {})
    if not isinstance(overrides, Mapping):
        raise ContractResolutionError("term_overrides必须为对象")

    if module == "payoffer":
        if data_refs:
            raise ContractResolutionError("Payoffer结构计算不接受行情或交易日历引用")
        contract = resolve_structural_payoff_contract(
            product_id.strip(),
            term_overrides=dict(overrides),
        )
        contract_payload = contract.to_protocol_dict()
        return {
            "request": {"action": "run", "payoff_input": {"contract": contract_payload}},
            "resolved_contract": contract_payload,
            "product_id": contract.product_id,
            "rule_revision": int(contract.identity["rule_revision"]),
        }

    identity = values.get("identity", {})
    if not isinstance(identity, Mapping):
        raise ContractResolutionError("identity必须为对象")

    refs = tuple(_canonical_data_ref(item) for item in data_refs)
    history_refs = tuple(item for item in refs if item.get("schema_id") == "market-history")
    calendar_refs = tuple(item for item in refs if item.get("schema_id") == "trading-calendar")
    unsupported_refs = tuple(
        item for item in refs
        if item.get("schema_id") not in {"market-history", "trading-calendar"}
    )
    if unsupported_refs:
        raise ContractResolutionError("计算请求包含不支持的DataAssetRef.schema_id")
    if module in {"pricer", "backtester"} or history_refs:
        values = _bind_data_backed_contract_identity(module, values, history_refs, data_store)
        identity = values.get("identity", {})
        overrides = values.get("term_overrides", {})
    values = _freeze_first_contract_end_date(module, values)
    identity = values.get("identity", {})
    overrides = values.get("term_overrides", {})
    if len(calendar_refs) > 1:
        # One formal contract has exactly one authenticated observation
        # calendar.  Accepting the first of several supplied assets would make
        # the frozen schedule depend on caller ordering and leave unbound data
        # in the request, including for Payoffer.
        raise ContractResolutionError("计算请求最多绑定一个交易日历DataAssetRef")
    calendar_binding = verified_trading_calendar(calendar_refs[0], data_store) if calendar_refs else None
    resolver_identity = dict(identity)
    if calendar_binding is not None:
        for key in ("calendar_id", "calendar_revision"):
            supplied = resolver_identity.get(key)
            if supplied is not None and supplied != getattr(calendar_binding, key):
                raise ContractResolutionError(f"identity.{key}与Host验证交易日历不一致")
            resolver_identity[key] = getattr(calendar_binding, key)

    contract = resolve_contract(
        product_id.strip(),
        identity=resolver_identity,
        term_overrides=dict(overrides),
        trading_dates=(calendar_binding.sessions if calendar_binding is not None else None),
    )
    contract_payload = contract.to_protocol_dict()
    if module == "pricer":
        config = values.get("pricing_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("pricing_config必须为对象")
        if "demo_mode" in config or "demo_calendar" in config:
            raise ContractResolutionError("正式PricingInput不接受demo_mode或demo_calendar")
        if len(history_refs) != 1 or len(calendar_refs) > 1:
            raise ContractResolutionError("PricingInput必须绑定唯一DataAssetRef历史行情，交易日历最多一项")
        formal = {
            "action": "run",
            "contract": contract_payload,
            "pricing_config": deepcopy(dict(config)),
            "market_data_refs": list(history_refs),
        }
        if "pricing_objective" in values:
            objective = values["pricing_objective"]
            try:
                formal["pricing_objective"] = PricingObjective.from_value(objective).to_protocol_dict()
            except (TypeError, ValueError) as error:
                raise ContractResolutionError(f"pricing_objective无效：{error}") from error
        observed_state = values.get("observed_contract_state")
        if observed_state is not None:
            try:
                frozen_state = ObservedContractState.from_host_payload(observed_state)
            except (TypeError, ValueError) as error:
                raise ContractResolutionError(f"Host冻结observed_contract_state无效：{error}") from error
            formal["observed_contract_state"] = frozen_state.to_protocol_dict()
        if calendar_refs:
            formal["trading_calendar_ref"] = calendar_refs[0]
    else:
        config = values.get("backtest_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("backtest_config必须为对象")
        if len(history_refs) != 1 or len(calendar_refs) > 1:
            raise ContractResolutionError("BacktestInput必须绑定唯一历史DataAssetRef，交易日历最多一项")
        if calendar_refs:
            # The App must persist a calendar-bound history ref *before* this
            # formal compilation.  ``DataAssetRef.storage_ref`` commits every
            # coverage field, so mutating it here would invalidate the opaque
            # reference when Backtester reads the bytes.  We still verify the
            # separately supplied calendar against that persisted coverage in
            # order to freeze observation schedules from authenticated dates.
            _require_history_calendar_matches_verified_calendar(
                history_refs[0], calendar_binding,
            )
            historical_data = history_refs[0]
        else:
            # A non-observation contract can use the calendar provenance already
            # authenticated in its one history asset.  This preserves the
            # complete BacktestInput shape for an offline release probe without
            # allowing any observation schedule to be frozen from those fields.
            _require_declared_history_calendar(history_refs[0])
            historical_data = history_refs[0]
        formal = {
            "action": "run",
            "contract": contract_payload,
            "backtest_config": deepcopy(dict(config)),
            "historical_data": historical_data,
        }
    return {
        "request": formal,
        "resolved_contract": contract_payload,
        "product_id": contract.product_id,
        "rule_revision": int(contract.identity["rule_revision"]),
    }


def _bind_data_backed_contract_identity(
    module: str,
    request: Mapping[str, Any],
    history_refs: Sequence[Mapping[str, Any]],
    data_store: DataStoreReadPort | None,
) -> dict[str, Any]:
    if len(history_refs) != 1 or data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise ContractResolutionError(f"{module}首次合同必须由Host绑定唯一market-history DataAssetRef")
    reference = DataAssetRef(**dict(history_refs[0]))
    try:
        payload = data_store.read_bytes(reference, tenant_id=reference.tenant_id)
    except (OSError, PermissionError, TypeError, ValueError) as error:
        raise ContractResolutionError("Host无法读取首次合同的历史行情") from error
    if not isinstance(payload, bytes) or sha256(payload).hexdigest() != reference.content_hash:
        raise ContractResolutionError("首次合同历史行情字节与DataAssetRef哈希不一致")
    rows = _market_history_rows(payload)
    value = deepcopy(dict(request))
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ContractResolutionError("identity必须为对象")
    identity = deepcopy(dict(raw_identity))
    underlyings = identity.get("underlyings")
    if isinstance(underlyings, str):
        assets = tuple(item.strip() for item in underlyings.split(",") if item.strip())
    elif isinstance(underlyings, (list, tuple)):
        assets = tuple(str(item).strip() for item in underlyings if str(item).strip())
    else:
        assets = ()
    if not assets or len(set(assets)) != len(assets):
        raise ContractResolutionError("identity.underlyings必须为无重复标的列表")
    if module == "payoffer":
        supplied_start = identity.get("contract_start_date")
        if supplied_start not in {None, ""}:
            start = _iso_date(supplied_start, "identity.contract_start_date")
        else:
            coverage_end = reference.coverage.get("end_date")
            start = _latest_common_history_date(
                rows,
                assets,
                _iso_date(coverage_end, "DataAssetRef.coverage.end_date"),
            )
    elif module == "pricer":
        config = value.get("pricing_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("pricing_config必须为对象")
        valuation = _iso_date(config.get("valuation_date"), "pricing_config.valuation_date")
        auto_start = value.pop("auto_contract_start_date", False)
        if not isinstance(auto_start, bool):
            raise ContractResolutionError("auto_contract_start_date必须为布尔值")
        supplied_start = identity.get("contract_start_date")
        if auto_start and supplied_start not in {None, ""}:
            raise ContractResolutionError("自动合同起始日不得同时提交具体日期")
        start = _iso_date(supplied_start or valuation, "identity.contract_start_date")
        if start > valuation:
            raise ContractResolutionError("合同起始日不得晚于请求估值日")
        if auto_start:
            start = _latest_common_history_date(rows, assets, valuation)
    else:
        config = value.get("backtest_config")
        config = config if isinstance(config, Mapping) else {}
        start_value = first_backtest_entry(config, reference.coverage)
        start = _iso_date(start_value, "identity.contract_start_date")
        supplied_start = identity.get("contract_start_date")
        if supplied_start not in {None, ""} and _iso_date(
            supplied_start, "identity.contract_start_date",
        ) != start:
            raise ContractResolutionError(
                "identity.contract_start_date与正式回测入场区间不一致"
            )
    expected = _reference_prices(rows, assets, start)
    supplied = identity.get("reference_prices")
    if supplied is not None:
        if not isinstance(supplied, Mapping) or set(supplied) != set(expected):
            raise ContractResolutionError("identity.reference_prices必须逐一覆盖标的")
        for asset, expected_value in expected.items():
            try:
                actual = float(supplied[asset])
            except (TypeError, ValueError) as error:
                raise ContractResolutionError(f"reference_prices.{asset}必须为正数") from error
            if actual <= 0 or not isclose(actual, expected_value, rel_tol=1e-10, abs_tol=1e-8):
                raise ContractResolutionError(f"reference_prices.{asset}与Host验证的未复权close不一致")
    identity["contract_start_date"] = start
    identity["reference_prices"] = expected
    value["identity"] = identity
    return value


def _freeze_first_contract_end_date(module: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Close a first formal contract with Core's sole civil-tenor rule."""

    value = deepcopy(dict(request))
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ContractResolutionError("identity必须为对象")
    identity = deepcopy(dict(raw_identity))
    start = identity.get("contract_start_date")
    if start in {None, ""} or identity.get("contract_end_date") not in {None, ""}:
        return value
    requirements = compile_compute_data_requirements(module, value)
    identity["contract_end_date"] = derive_contract_end_date(start, requirements.tenor_years)
    value["identity"] = identity
    return value


def _market_history_rows(payload: bytes) -> tuple[dict[str, Any], ...]:
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text))
    except UnicodeDecodeError as error:
        raise ContractResolutionError("历史行情CSV编码无效") from error
    required = {"date", "asset_id", "close", "adj_close"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ContractResolutionError("历史行情必须含date、asset_id、close、adj_close")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in reader:
        try:
            day = date.fromisoformat(str(raw["date"]).strip()).isoformat()
            asset = str(raw["asset_id"]).strip()
            close = float(raw["close"])
            adjusted = float(raw["adj_close"])
        except (TypeError, ValueError) as error:
            raise ContractResolutionError("历史行情含无效date、asset_id、close或adj_close") from error
        if not asset or not isfinite(close) or not isfinite(adjusted) or close <= 0 or adjusted <= 0 or (day, asset) in seen:
            raise ContractResolutionError("历史行情含无效或重复的date、asset_id、close、adj_close")
        seen.add((day, asset))
        rows.append({"date": day, "asset_id": asset, "close": close})
    if not rows:
        raise ContractResolutionError("历史行情为空")
    return tuple(sorted(rows, key=lambda item: (item["date"], item["asset_id"])))


def _latest_common_history_date(rows: Sequence[Mapping[str, Any]], assets: Sequence[str], cutoff: str) -> str:
    dates_by_asset = {
        asset: {str(row["date"]) for row in rows if row["asset_id"] == asset and str(row["date"]) <= cutoff}
        for asset in assets
    }
    common = set.intersection(*(values for values in dates_by_asset.values())) if dates_by_asset else set()
    if not common:
        raise ContractResolutionError("DataAssetRef未覆盖全部标的在请求估值日或此前的共同未复权close")
    return max(common)


def _reference_prices(rows: Sequence[Mapping[str, Any]], assets: Sequence[str], cutoff: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for asset in assets:
        matches = [row for row in rows if row["asset_id"] == asset and str(row["date"]) <= cutoff]
        if not matches:
            raise ContractResolutionError(f"DataAssetRef未覆盖{asset}在合同起始日或此前的未复权close")
        values[asset] = float(matches[-1]["close"])
    return values


def first_backtest_entry(config: Mapping[str, Any], coverage: Mapping[str, Any]) -> object:
    """Return the first declared backtest entry from formal request or coverage fields."""

    entries = config.get("entry_dates")
    candidates = list(entries) if isinstance(entries, list) else []
    candidates.extend((config.get("start_date"), coverage.get("start_date")))
    return next((item for item in candidates if item not in {None, ""}), None)


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContractResolutionError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ContractResolutionError(f"{label}必须为YYYY-MM-DD") from error


def _canonical_data_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractResolutionError("DataAssetRef必须为对象")
    required = {
        "data_asset_id", "storage_ref", "media_type", "schema_id", "asset_ids", "normalized_fields",
        "coverage", "row_count", "price_convention", "content_hash", "lineage", "tenant_id",
        "created_by", "access_scope", "partition_spec",
    }
    if set(value) != required:
        raise ContractResolutionError("DataAssetRef字段必须为" + ",".join(sorted(required)))
    return deepcopy(dict(value))


def verified_trading_calendar(
    value: Mapping[str, Any],
    data_store: DataStoreReadPort | None,
) -> VerifiedTradingCalendarBinding:
    """Read one App-owned calendar asset and freeze only its authenticated facts.

    A browser may name a calendar asset but never supplies calendar identity or
    sessions.  The Host-provided DataStore is the only byte authority here.
    """

    if data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise ContractResolutionError("正式交易日历必须由Host注入经验证的DataStorePort")
    try:
        reference = DataAssetRef(**dict(value))
        encoded = data_store.read_bytes(reference, tenant_id=reference.tenant_id)
    except (TypeError, ValueError, OSError, PermissionError) as error:
        raise ContractResolutionError("Host验证交易日历无法读取") from error
    if not isinstance(encoded, bytes) or sha256(encoded).hexdigest() != reference.content_hash:
        raise ContractResolutionError("Host验证交易日历内容哈希不一致")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractResolutionError("Host验证交易日历不是有效JSON") from error
    if not isinstance(payload, Mapping) or payload.get("schema_id") != "trading-calendar":
        raise ContractResolutionError("Host验证交易日历协议无效")
    coverage = reference.coverage
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    sessions = payload.get("sessions")
    if (
        reference.schema_id != "trading-calendar"
        or reference.media_type != "application/json"
        or not isinstance(calendar_id, str)
        or not calendar_id
        or not isinstance(calendar_revision, str)
        or not calendar_revision
        or not isinstance(sessions, list)
        or isinstance(sessions, (str, bytes))
    ):
        raise ContractResolutionError("Host验证交易日历缺少身份或交易日")
    if set(payload.get("asset_ids", ())) != set(reference.asset_ids):
        raise ContractResolutionError("Host验证交易日历标的与DataAssetRef不一致")
    try:
        normalized = tuple(date.fromisoformat(str(item)).isoformat() for item in sessions)
    except ValueError as error:
        raise ContractResolutionError("Host验证交易日历交易日必须为YYYY-MM-DD") from error
    if not normalized or normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise ContractResolutionError("Host验证交易日历交易日必须严格递增且不重复")
    declared_sessions = coverage.get("sessions")
    if declared_sessions is not None and (
        isinstance(declared_sessions, (str, bytes))
        or not isinstance(declared_sessions, Sequence)
        or tuple(str(item) for item in declared_sessions) != normalized
    ):
        raise ContractResolutionError("Host验证交易日历覆盖声明与内容不一致")
    binding = VerifiedTradingCalendarBinding(
        calendar_ref=reference,
        calendar_id=calendar_id,
        calendar_revision=calendar_revision,
        sessions=normalized,
        binding_hash=semantic_hash(_calendar_binding_payload(
            reference,
            calendar_id,
            calendar_revision,
            normalized,
        )),
    )
    return _issue_binding(binding, "verified_trading_calendar")  # type: ignore[return-value]


def bind_verified_calendar_to_history(
    history_ref: Mapping[str, Any],
    calendar_ref: Mapping[str, Any],
    data_store: DataStoreReadPort | None,
) -> dict[str, Any]:
    """Return a history ref whose calendar facts come only from verified bytes.

    DataFetcher may carry calendar metadata as lineage.  Backtester needs the
    same facts in the signed DataAssetRef coverage, so the Host replaces rather
    than trusts those fields.  The selected sessions are constrained to the
    history coverage; this never invents a date outside the supplied asset.
    """

    history = _canonical_data_ref(history_ref)
    if history["schema_id"] != "market-history":
        raise ContractResolutionError("历史行情必须使用market-history DataAssetRef")
    calendar = verified_trading_calendar(calendar_ref, data_store)
    coverage = deepcopy(dict(history["coverage"]))
    sessions = _history_sessions_in_calendar(coverage, calendar)
    coverage.update({
        "calendar_id": calendar.calendar_id,
        "calendar_revision": calendar.calendar_revision,
        "sessions": list(sessions),
        # Coverage boundaries are canonicalized to actual observed sessions.
        # A request or metadata boundary may be a weekend/holiday and must not
        # be treated as a required trading session.
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "calendar_coverage_end": sessions[-1],
        "calendar_ref": _asset_ref_payload(calendar.calendar_ref),
    })
    return {**history, "coverage": coverage}


def _history_sessions_in_calendar(
    coverage: Mapping[str, Any],
    calendar: VerifiedTradingCalendarBinding,
) -> tuple[str, ...]:
    """Validate a history session set without treating metadata dates as sessions.

    The persisted history asset is authoritative for which sessions were
    actually observed.  The verified calendar must contain that set, and it
    must contain every calendar session between the observed first and last
    session.  This accepts non-trading request/metadata boundaries while still
    rejecting an internal missing trading day.
    """

    raw_history_sessions = coverage.get("sessions")
    if isinstance(raw_history_sessions, (str, bytes)) or not isinstance(raw_history_sessions, (list, tuple)):
        raise ContractResolutionError("历史行情DataAssetRef.coverage必须声明实际交易sessions")
    try:
        history_sessions = tuple(date.fromisoformat(str(value)).isoformat() for value in raw_history_sessions)
    except (TypeError, ValueError) as error:
        raise ContractResolutionError("历史行情DataAssetRef.coverage.sessions必须为YYYY-MM-DD") from error
    if not history_sessions or history_sessions != tuple(sorted(history_sessions)) or len(set(history_sessions)) != len(history_sessions):
        raise ContractResolutionError("历史行情DataAssetRef.coverage.sessions必须严格递增且不重复")
    if type(calendar) is not VerifiedTradingCalendarBinding:
        raise ContractResolutionError("历史行情日历必须使用Core VerifiedTradingCalendarBinding")
    _require_binding_seal(calendar, "verified_trading_calendar")
    calendar_sessions = calendar.sessions
    if any(day not in set(calendar_sessions) for day in history_sessions):
        raise ContractResolutionError("Host验证交易日历未覆盖历史行情实际交易日")
    expected = tuple(day for day in calendar_sessions if history_sessions[0] <= day <= history_sessions[-1])
    if history_sessions != expected:
        raise ContractResolutionError("Host验证交易日历未完整覆盖历史行情实际交易日集合")
    return history_sessions


def _require_history_calendar_matches_verified_calendar(
    history_ref: Mapping[str, Any],
    calendar: VerifiedTradingCalendarBinding | None,
) -> None:
    """Require a persisted history reference to carry the verified calendar.

    This is intentionally a comparison rather than an in-memory mutation of
    the history ref.  A DataAssetRef is metadata-committed by its storage
    reference, therefore adding calendar facts after it was issued makes the
    otherwise correct CSV unreadable to the formal Backtester.
    """

    if calendar is None:
        raise ContractResolutionError("BacktestInput缺少Host验证交易日历")
    if type(calendar) is not VerifiedTradingCalendarBinding:
        raise ContractResolutionError("BacktestInput必须使用Core VerifiedTradingCalendarBinding")
    _require_binding_seal(calendar, "verified_trading_calendar")
    history = _canonical_data_ref(history_ref)
    coverage = history.get("coverage")
    if history.get("schema_id") != "market-history" or not isinstance(coverage, Mapping):
        raise ContractResolutionError("BacktestInput历史行情必须声明交易日历覆盖")
    history_sessions = _history_sessions_in_calendar(coverage, calendar)
    if (
        coverage.get("calendar_id") != calendar.calendar_id
        or coverage.get("calendar_revision") != calendar.calendar_revision
        or tuple(str(value) for value in coverage.get("sessions", ())) != history_sessions
        or coverage.get("calendar_coverage_end") != history_sessions[-1]
    ):
        raise ContractResolutionError(
            "BacktestInput历史行情必须使用已持久化的Host验证交易日历；请重新获取该回测区间行情。"
        )


def _require_declared_history_calendar(history_ref: Mapping[str, Any]) -> None:
    """Require a self-contained historical DataAssetRef calendar declaration."""

    coverage = history_ref.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ContractResolutionError("BacktestInput历史行情必须声明交易日历覆盖")
    sessions = coverage.get("sessions")
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    if (
        not isinstance(sessions, list)
        or not sessions
        or not isinstance(calendar_id, str)
        or not calendar_id
        or not isinstance(calendar_revision, str)
        or not calendar_revision
    ):
        raise ContractResolutionError("BacktestInput历史行情必须显式声明calendar_id、calendar_revision和sessions")


__all__ = (
    "bind_verified_calendar_to_history",
    "compile_compute_data_requirements",
    "ComputeDataRequirements",
    "prepare_compute_request",
    "validate_initial_pricing_time_context",
    "verified_trading_calendar",
)
