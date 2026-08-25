"""已发生合同事件的正式边界。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
import re
from typing import Any, Mapping

from runtime.contracts.contract_api import bind_term_symbols, resolve_schedule


class ObservedStateError(ValueError):
    """已观测合同状态不完整或与估值日矛盾。"""


@dataclass(frozen=True)
class ObservedContractState:
    """存续合约截至估值日已经发生的事实，不含未来条款或模型假设。"""

    valuation_date: str | None
    lifecycle_status: str = "initial"
    occurred_events: tuple[Mapping[str, Any] | str, ...] = ()
    realized_cashflows: tuple[Mapping[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    state_hash: str | None = None

    @classmethod
    def initial(cls, valuation_date: str | None) -> "ObservedContractState":
        return cls(valuation_date=valuation_date)

    @classmethod
    def from_value(
        cls, value: "ObservedContractState | Mapping[str, Any] | None", *, valuation_date: str | None
    ) -> "ObservedContractState":
        if value is None:
            state = cls.initial(valuation_date)
        elif isinstance(value, cls):
            state = value
        elif isinstance(value, Mapping):
            allowed = {"valuation_date", "lifecycle_status", "occurred_events", "realized_cashflows", "source_refs", "state_hash"}
            unknown = set(value) - allowed
            if unknown:
                raise ObservedStateError("observed_contract_state含未支持字段：" + ",".join(sorted(unknown)))
            state = cls(
                valuation_date=value.get("valuation_date"),
                lifecycle_status=str(value.get("lifecycle_status", "initial")),
                occurred_events=tuple(value.get("occurred_events", ())),
                realized_cashflows=tuple(value.get("realized_cashflows", ())),
                source_refs=tuple(str(item) for item in value.get("source_refs", ())),
                state_hash=value.get("state_hash"),
            )
        else:
            raise ObservedStateError("observed_contract_state必须为ObservedContractState或对象")
        if state.valuation_date not in {None, valuation_date}:
            raise ObservedStateError("observed_contract_state.valuation_date必须与PricingConfig一致")
        state._validate_facts(valuation_date)
        canonical_hash = state._content_hash()
        if state.state_hash is not None and state.state_hash != canonical_hash:
            raise ObservedStateError("observed_contract_state.state_hash与状态内容不一致")
        return replace(state, state_hash=canonical_hash)

    def _validate_facts(self, valuation_date: str | None) -> None:
        allowed_status = {"initial", "active", "terminated", "matured"}
        if self.lifecycle_status not in allowed_status:
            raise ObservedStateError("observed_contract_state.lifecycle_status无效")
        if (self.occurred_events or self.realized_cashflows) and not self.source_refs:
            raise ObservedStateError("已发生事件或现金流必须提供source_refs")
        if any(not isinstance(item, str) or not item.strip() for item in self.source_refs):
            raise ObservedStateError("observed_contract_state.source_refs必须为非空引用")
        cutoff = _date(valuation_date, "valuation_date") if valuation_date else None
        for event in self.occurred_events:
            if not isinstance(event, Mapping):
                raise ObservedStateError("occurred_events必须为可审计事件对象")
            event_type = str(event.get("event_type", "")).strip()
            if not event_type:
                raise ObservedStateError("occurred_events.event_type不能为空")
            event_date = _date(event.get("event_date"), "occurred_events.event_date")
            if cutoff is not None and event_date > cutoff:
                raise ObservedStateError("已发生事件日期不得晚于估值日")
        for cashflow in self.realized_cashflows:
            if not isinstance(cashflow, Mapping):
                raise ObservedStateError("realized_cashflows必须为现金流对象")
            payment_date = _date(cashflow.get("payment_date"), "realized_cashflows.payment_date")
            if cutoff is not None and payment_date > cutoff:
                raise ObservedStateError("已实现现金流日期不得晚于估值日")
            if str(cashflow.get("status", "")).lower() not in {"paid", "settled", "realized"}:
                raise ObservedStateError("realized_cashflows.status必须为paid、settled或realized")

    @property
    def knocked_in(self) -> bool:
        return any(_event_type(event) in {"knock_in", "ki"} for event in self.occurred_events)

    @property
    def knocked_out(self) -> bool:
        return any(_event_type(event) in {"knock_out", "ko"} for event in self.occurred_events)

    @property
    def observation_stage(self) -> int | None:
        stages = [
            int(event["observation_stage"])
            for event in self.occurred_events
            if isinstance(event.get("observation_stage"), int)
            and not isinstance(event.get("observation_stage"), bool)
            and int(event["observation_stage"]) >= 0
        ]
        return max(stages) if stages else None

    @property
    def accumulated_count(self) -> int:
        values = [
            int(event["accumulated_count"])
            for event in self.occurred_events
            if isinstance(event.get("accumulated_count"), int)
            and not isinstance(event.get("accumulated_count"), bool)
            and int(event["accumulated_count"]) >= 0
        ]
        return max(values) if values else 0

    @property
    def has_accumulated_count(self) -> bool:
        return any(
            "accumulated_count" in event
            and isinstance(event.get("accumulated_count"), int)
            and not isinstance(event.get("accumulated_count"), bool)
            for event in self.occurred_events
        )

    @property
    def accumulated_quantity(self) -> float:
        values = [
            float(event["accumulated_quantity"])
            for event in self.occurred_events
            if isinstance(event.get("accumulated_quantity"), (int, float))
            and not isinstance(event.get("accumulated_quantity"), bool)
            and float(event["accumulated_quantity"]) >= 0.0
        ]
        return max(values) if values else 0.0

    @property
    def has_accumulated_quantity(self) -> bool:
        return any(
            "accumulated_quantity" in event
            and isinstance(event.get("accumulated_quantity"), (int, float))
            and not isinstance(event.get("accumulated_quantity"), bool)
            for event in self.occurred_events
        )

    def valuation_state(self, *, contract_start_date: str | None) -> dict[str, Any]:
        """将历史事实一次性编译为数值核状态。"""
        if self.lifecycle_status == "initial" and not self.occurred_events and not self.realized_cashflows:
            return {
                "trading_day": 1,
                "calendar_day": 1,
                "knocked_in": False,
                "knocked_out": False,
                "accumulated_count": 0,
                "accumulated_quantity": 0.0,
                "observation_stage": None,
            }
        valuation = _date(self.valuation_date, "valuation_date")
        start = _date(contract_start_date, "contract_start_date")
        if valuation < start:
            raise ObservedStateError("observed_contract_state估值日不得早于合同起始日")
        if self.knocked_out and self.lifecycle_status not in {"terminated", "matured"}:
            raise ObservedStateError("已敲出合同lifecycle_status必须为terminated或matured")
        stage = self.observation_stage
        return {
            "trading_day": (stage + 1) if stage is not None else 1,
            "calendar_day": (valuation - start).days + 1,
            "knocked_in": self.knocked_in,
            "knocked_out": self.knocked_out,
            "accumulated_count": self.accumulated_count,
            "accumulated_quantity": self.accumulated_quantity,
            "observation_stage": stage,
        }

    def validate_european_vanilla(self) -> None:
        if self.lifecycle_status not in {"initial", "active"}:
            raise ObservedStateError("欧式香草只接受initial或active状态")
        if self.occurred_events or self.realized_cashflows:
            raise ObservedStateError("欧式香草当前不接受已发生事件或已实现现金流")

    def validate_path_events(
        self,
        *,
        contract_start_date: str | None,
        path_dependent: bool,
        requires_accumulated_count: bool,
        requires_accumulated_quantity: bool,
        unsupported_midlife_aggregate: bool,
    ) -> None:
        """Reject historical facts that the remaining-path engine cannot apply."""
        if path_dependent and self.lifecycle_status == "initial" and self.valuation_date is not None and contract_start_date is not None:
            if _date(self.valuation_date, "valuation_date") > _date(contract_start_date, "contract_start_date"):
                raise ObservedStateError("存续期路径合同不得以initial状态绕过已发生观察事实")
        if (
            path_dependent
            and unsupported_midlife_aggregate
            and self.lifecycle_status == "active"
            and self.valuation_date is not None
            and contract_start_date is not None
            and _date(self.valuation_date, "valuation_date") > _date(contract_start_date, "contract_start_date")
        ):
            raise ObservedStateError("存续区间计息的历史n_in与剩余观察分母尚不能完整重建")
        if (
            path_dependent
            and requires_accumulated_count
            and self.lifecycle_status == "active"
            and self.valuation_date is not None
            and contract_start_date is not None
            and _date(self.valuation_date, "valuation_date") > _date(contract_start_date, "contract_start_date")
            and not self.has_accumulated_count
        ):
            raise ObservedStateError("存续票息结构必须在observation_checkpoint提供accumulated_count")
        if (
            path_dependent
            and requires_accumulated_quantity
            and self.lifecycle_status == "active"
            and self.valuation_date is not None
            and contract_start_date is not None
            and _date(self.valuation_date, "valuation_date") > _date(contract_start_date, "contract_start_date")
            and not self.has_accumulated_quantity
        ):
            raise ObservedStateError("存续累购必须在observation_checkpoint提供已累计数量accumulated_quantity")
        if self.lifecycle_status in {"terminated", "matured"}:
            raise ObservedStateError("已终止或到期合同不得重新模拟未来路径；历史结算现金流仅用于审计")
        supported = {"observation_checkpoint", "knock_in", "ki", "knock_out", "ko"}
        unsupported = sorted({_event_type(event) for event in self.occurred_events} - supported)
        if unsupported:
            raise ObservedStateError("存续路径定价尚未实现历史事件：" + ",".join(unsupported))

    def validate_observation_bounds(
        self,
        *,
        contract_start_date: str,
        contract_terms: Mapping[str, Any],
        trading_sessions: tuple[str, ...],
    ) -> None:
        """Cross-check reported path state against the verified calendar.

        Historical counters are facts, not free model parameters.  Their
        upper bounds come from the frozen contract selector evaluated on the
        same verified sessions used by the Host.
        """
        if not self.occurred_events:
            return
        start = _date(contract_start_date, "contract_start_date")
        valuation = _date(self.valuation_date, "valuation_date")
        try:
            sessions = tuple(date.fromisoformat(str(value)) for value in trading_sessions)
        except ValueError as error:
            raise ObservedStateError("已验证交易sessions必须为YYYY-MM-DD") from error
        if not sessions or sessions != tuple(sorted(set(sessions))):
            raise ObservedStateError("已验证交易sessions必须严格递增且不重复")
        completed = tuple(value for value in sessions if start <= value <= valuation)
        if not completed:
            raise ObservedStateError("已验证交易sessions未覆盖合同起始日至估值日")

        stage = self.observation_stage
        if stage is not None and stage >= len(completed):
            raise ObservedStateError(
                f"observation_stage={stage}超过估值日前已完成交易观察上限{len(completed) - 1}"
            )

        if self.has_accumulated_count:
            coupon_observations = _selected_observation_count(
                contract_terms, "n_coupon", sessions, start, valuation,
            )
            if coupon_observations is not None and self.accumulated_count > coupon_observations:
                raise ObservedStateError(
                    f"accumulated_count={self.accumulated_count}超过已完成票息观察数{coupon_observations}"
                )

        if self.has_accumulated_quantity:
            quantity_observations = _selected_observation_count(
                contract_terms, "Q_acc", sessions, start, valuation,
            )
            q = contract_terms.get("q")
            multiplier = contract_terms.get("m")
            if (
                quantity_observations is not None
                and isinstance(q, (int, float)) and not isinstance(q, bool)
                and isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool)
            ):
                maximum = quantity_observations * float(q) * float(multiplier)
                if self.accumulated_quantity > maximum:
                    raise ObservedStateError(
                        f"accumulated_quantity={self.accumulated_quantity}超过合同累计数量上限{maximum}"
                    )

    def completed_observation_count(
        self,
        *,
        contract_start_date: str,
        contract_terms: Mapping[str, Any],
        monitor_key: str,
        trading_sessions: tuple[str, ...],
    ) -> int:
        start = _date(contract_start_date, "contract_start_date")
        valuation = _date(self.valuation_date, "valuation_date")
        sessions = tuple(date.fromisoformat(str(value)) for value in trading_sessions)
        count = _selected_observation_count(
            contract_terms, monitor_key, sessions, start, valuation,
        )
        if count is None:
            raise ObservedStateError(f"冻结合同缺少{monitor_key}观察日程")
        return count

    def to_dict(self) -> dict[str, Any]:
        return {
            "valuation_date": self.valuation_date,
            "lifecycle_status": self.lifecycle_status,
            "occurred_events": list(self.occurred_events),
            "realized_cashflows": list(self.realized_cashflows),
            "source_refs": list(self.source_refs),
            "state_hash": self.state_hash,
        }

    def _content_hash(self) -> str:
        payload = {
            "valuation_date": self.valuation_date,
            "lifecycle_status": self.lifecycle_status,
            "occurred_events": list(self.occurred_events),
            "realized_cashflows": list(self.realized_cashflows),
            "source_refs": list(self.source_refs),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ("ObservedContractState", "ObservedStateError")


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type", "")).strip().lower()


def _date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ObservedStateError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ObservedStateError(f"{label}必须为YYYY-MM-DD") from error


def _selected_observation_count(
    terms: Mapping[str, Any],
    monitor_key: str,
    sessions: tuple[date, ...],
    start: date,
    valuation: date,
) -> int | None:
    monitor = terms.get("monitor")
    if not isinstance(monitor, Mapping) or not isinstance(monitor.get(monitor_key), str):
        return None
    match = re.search(r"\bS_t\[([A-Za-z_][A-Za-z0-9_]*)\]", str(monitor[monitor_key]))
    if match is None:
        raise ObservedStateError(f"{monitor_key}未绑定明确观察日程")
    bindings = bind_term_symbols(terms)
    selector = bindings.get(match.group(1))
    if selector is None:
        raise ObservedStateError(f"{monitor_key}观察日程未在冻结合同中解析")
    eligible = tuple(value for value in sessions if value >= start)
    selected = resolve_schedule(selector, tuple(value.isoformat() for value in eligible))
    return sum(date.fromisoformat(str(value)[:10]) <= valuation for value in selected)
