"""Mode2产品交易闭环的当前候选控制面。

模型只提出受控条款调整和模块计划。候选始终由``candidate_id``标识，
调整会更新同一候选的当前输入，不产生历史拓扑。计算事实只能由Host回填。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from typing import Any, Mapping, Sequence

from .candidate_builder import create_term_variant
from .models import EvaluationRecord, RecommendationCandidate, RecommendationValidationError


MAX_ROUNDS = 2
ALLOWED_MODULES = frozenset({"payoffer", "pricer", "backtester"})
DEFAULT_TERM_KEYS = frozenset({
    "T", "K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "c", "c_max", "alpha",
    "H_KO", "H_KI", "B", "O_KO", "O_KI", "Oc", "settlement", "exercise_style",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")


def _error(message: str) -> RecommendationValidationError:
    return RecommendationValidationError(f"ProductTraderLoop：{message}")


def _identifier(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not _IDENTIFIER.fullmatch(text):
        raise _error(f"{field_name}必须为安全标识符")
    return text


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{field_name}必须为对象")
    return dict(value)


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error(f"{field_name}必须为数组")
    return tuple(value)


def _exact_fields(value: Mapping[str, Any], *, fields: set[str], field_name: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise _error(f"{field_name}含未知字段：{','.join(unknown)}")
    if missing:
        raise _error(f"{field_name}缺少字段：{','.join(missing)}")


def _controlled_term_overrides(
    value: Any,
    *,
    allowed_term_keys: frozenset[str],
    field_name: str,
) -> dict[str, str | int | float]:
    raw = _mapping(value, field_name)
    result: dict[str, str | int | float] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        if key not in allowed_term_keys:
            raise _error(f"{field_name}含未受控条款：{key}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
            raise _error(f"{field_name}.{key}必须为字符串或有限数值")
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise _error(f"{field_name}.{key}必须为有限数值")
        if isinstance(raw_value, str):
            normalized = raw_value.strip()
            if not normalized or len(normalized) > 128:
                raise _error(f"{field_name}.{key}必须为1至128字符的字符串")
            result[key] = normalized
        else:
            result[key] = raw_value
    return {key: result[key] for key in sorted(result)}


def _key_terms(value: Any, field_name: str) -> tuple[Mapping[str, str], ...]:
    rows = _sequence(value, field_name)
    result: list[Mapping[str, str]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"{field_name}[{index}]")
        _exact_fields(row, fields={"label", "value", "source"}, field_name=f"{field_name}[{index}]")
        label = str(row["label"]).strip()
        text = str(row["value"]).strip()
        source = str(row["source"]).strip()
        if not label or not text or source not in {"用户输入", "拟采用参数"}:
            raise _error(f"{field_name}必须使用受控客户条款摘要")
        result.append({"label": label, "value": text, "source": source})
    return tuple(result)


@dataclass(frozen=True)
class StructurerProposal:
    """Structurer对一个当前候选的受控计划。"""

    candidate_id: str
    evaluation_plan: tuple[str, ...]
    term_overrides: Mapping[str, str | int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "evaluation_plan": list(self.evaluation_plan),
            "term_overrides": dict(self.term_overrides),
        }


@dataclass(frozen=True)
class TraderDecision:
    """Trader仅可接受候选或更新同一候选的当前输入。"""

    candidate_id: str
    action: str
    term_adjustments: Mapping[str, str | int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "action": self.action,
            "term_adjustments": dict(self.term_adjustments),
        }


@dataclass(frozen=True)
class LoopCandidate:
    candidate: RecommendationCandidate
    term_overrides: Mapping[str, str | int | float]
    status: str = "open"

    def __post_init__(self) -> None:
        if self.status not in {"open", "accepted"}:
            raise _error("LoopCandidate.status无效")
        _identifier(self.candidate.candidate_id, "LoopCandidate.candidate_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "term_overrides": dict(self.term_overrides),
            "status": self.status,
        }


@dataclass(frozen=True)
class EvaluationRequest:
    """Host按当前产品规则编译并运行一个候选模块所需的输入。"""

    candidate_id: str
    module: str
    round_no: int
    product_id: str
    rule_revision: int
    underlyings: tuple[str, ...]
    current_inputs: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "module": self.module,
            "round_no": self.round_no,
            "product_id": self.product_id,
            "rule_revision": self.rule_revision,
            "underlyings": list(self.underlyings),
            "current_inputs": dict(self.current_inputs),
        }


@dataclass(frozen=True)
class EvaluationPlan:
    candidate: LoopCandidate
    modules: tuple[str, ...]
    round_no: int

    def requests(self) -> tuple[EvaluationRequest, ...]:
        current = self.candidate.candidate
        return tuple(
            EvaluationRequest(
                candidate_id=current.candidate_id,
                module=module,
                round_no=self.round_no,
                product_id=current.product_id,
                rule_revision=current.rule_revision,
                underlyings=current.underlyings,
                current_inputs=dict(current.current_inputs),
            )
            for module in self.modules
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "modules": list(self.modules),
            "round_no": self.round_no,
            "requests": [item.to_dict() for item in self.requests()],
        }


@dataclass(frozen=True)
class ProductTraderRound:
    round_no: int
    plans: tuple[EvaluationPlan, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.round_no <= MAX_ROUNDS:
            raise _error("round_no超出范围")
        identities = [item.candidate.candidate.candidate_id for item in self.plans]
        if len(identities) != len(set(identities)):
            raise _error("同轮计划不得重复候选")

    def to_dict(self) -> dict[str, Any]:
        return {"round_no": self.round_no, "plans": [item.to_dict() for item in self.plans]}


@dataclass(frozen=True)
class LoopTransition:
    next_loop: "ProductTraderLoop | None"
    accepted_candidates: tuple[RecommendationCandidate, ...]
    reworked_candidate_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_loop": self.next_loop.to_dict() if self.next_loop else None,
            "accepted_candidates": [item.to_dict() for item in self.accepted_candidates],
            "reworked_candidate_ids": list(self.reworked_candidate_ids),
        }


@dataclass(frozen=True)
class ProductTraderLoop:
    candidates: tuple[LoopCandidate, ...]
    round_no: int = 1
    allowed_term_keys: frozenset[str] = DEFAULT_TERM_KEYS

    def __post_init__(self) -> None:
        if not self.candidates:
            raise _error("至少需要一个候选")
        if not 1 <= self.round_no <= MAX_ROUNDS:
            raise _error("round_no超出范围")
        identities = [item.candidate.candidate_id for item in self.candidates]
        if len(identities) != len(set(identities)):
            raise _error("candidate_id必须唯一")

    @classmethod
    def start(
        cls,
        candidates: Sequence[RecommendationCandidate],
        *,
        allowed_term_keys: frozenset[str] = DEFAULT_TERM_KEYS,
    ) -> "ProductTraderLoop":
        rows = tuple(
            LoopCandidate(
                candidate=item,
                term_overrides=dict(item.current_inputs.get("term_overrides", {}))
                if isinstance(item.current_inputs.get("term_overrides", {}), Mapping) else {},
            )
            for item in candidates
        )
        return cls(candidates=rows, allowed_term_keys=allowed_term_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_no": self.round_no,
            "candidates": [item.to_dict() for item in self.candidates],
            "allowed_term_keys": sorted(self.allowed_term_keys),
        }

    def parse_structurer_output(self, value: Any) -> tuple[StructurerProposal, ...]:
        payload = _mapping(value, "Structurer输出")
        _exact_fields(payload, fields={"proposals"}, field_name="Structurer输出")
        rows = _sequence(payload["proposals"], "Structurer.proposals")
        available = {item.candidate.candidate_id for item in self.candidates if item.status == "open"}
        seen: set[str] = set()
        result: list[StructurerProposal] = []
        for index, item in enumerate(rows):
            row = _mapping(item, f"Structurer.proposals[{index}]")
            _exact_fields(
                row,
                fields={"candidate_id", "evaluation_plan", "term_overrides"},
                field_name=f"Structurer.proposals[{index}]",
            )
            candidate_id = _identifier(row["candidate_id"], f"Structurer.proposals[{index}].candidate_id")
            if candidate_id not in available:
                raise _error(f"Structurer引用不存在或已接受候选：{candidate_id}")
            if candidate_id in seen:
                raise _error(f"Structurer重复候选：{candidate_id}")
            seen.add(candidate_id)
            modules = tuple(str(module).strip().lower() for module in _sequence(
                row["evaluation_plan"], f"Structurer.proposals[{index}].evaluation_plan"
            ))
            if not modules or len(modules) != len(set(modules)) or any(module not in ALLOWED_MODULES for module in modules):
                raise _error("Structurer.evaluation_plan只允许payoffer、pricer、backtester且不得重复")
            overrides = _controlled_term_overrides(
                row["term_overrides"], allowed_term_keys=self.allowed_term_keys,
                field_name=f"Structurer.proposals[{index}].term_overrides",
            )
            result.append(StructurerProposal(candidate_id, modules, overrides))
        if seen != available:
            raise _error("Structurer必须覆盖全部open候选")
        return tuple(result)

    def plan_round(self, structurer_output: Any) -> ProductTraderRound:
        proposals = self.parse_structurer_output(structurer_output)
        states = {item.candidate.candidate_id: item for item in self.candidates}
        plans: list[EvaluationPlan] = []
        for proposal in proposals:
            state = states[proposal.candidate_id]
            merged = dict(state.term_overrides)
            merged.update(proposal.term_overrides)
            current = create_term_variant(
                state.candidate,
                term_overrides=merged,
                generation_reason=f"mode2_round_{self.round_no}",
            )
            plans.append(EvaluationPlan(
                candidate=LoopCandidate(current, merged, state.status),
                modules=proposal.evaluation_plan,
                round_no=self.round_no,
            ))
        return ProductTraderRound(round_no=self.round_no, plans=tuple(plans))

    def parse_trader_output(
        self,
        value: Any,
        *,
        round_plan: ProductTraderRound,
    ) -> tuple[TraderDecision, ...]:
        payload = _mapping(value, "Trader输出")
        _exact_fields(payload, fields={"decisions"}, field_name="Trader输出")
        rows = _sequence(payload["decisions"], "Trader.decisions")
        expected = {item.candidate.candidate.candidate_id for item in round_plan.plans}
        seen: set[str] = set()
        result: list[TraderDecision] = []
        for index, item in enumerate(rows):
            row = _mapping(item, f"Trader.decisions[{index}]")
            _exact_fields(
                row,
                fields={"candidate_id", "action", "term_adjustments"},
                field_name=f"Trader.decisions[{index}]",
            )
            candidate_id = _identifier(row["candidate_id"], f"Trader.decisions[{index}].candidate_id")
            if candidate_id not in expected:
                raise _error("Trader不得新增或引用未知候选")
            if candidate_id in seen:
                raise _error("Trader不得重复决定候选")
            seen.add(candidate_id)
            action = str(row["action"]).strip().lower()
            if action not in {"accept", "rework"}:
                raise _error("Trader.action必须为accept或rework")
            adjustments = _controlled_term_overrides(
                row["term_adjustments"], allowed_term_keys=self.allowed_term_keys,
                field_name=f"Trader.decisions[{index}].term_adjustments",
            )
            if action == "accept" and adjustments:
                raise _error("accept不得附带term_adjustments")
            if action == "rework" and not adjustments:
                raise _error("rework必须附带term_adjustments")
            result.append(TraderDecision(candidate_id, action, adjustments))
        if seen != expected:
            raise _error("Trader必须逐一决定本轮全部候选")
        return tuple(result)

    def apply_trader_output(self, round_plan: ProductTraderRound, trader_output: Any) -> LoopTransition:
        if round_plan.round_no != self.round_no:
            raise _error("Trader决定不属于当前轮")
        decisions = self.parse_trader_output(trader_output, round_plan=round_plan)
        planned = {item.candidate.candidate.candidate_id: item.candidate for item in round_plan.plans}
        states = {item.candidate.candidate_id: item for item in self.candidates}
        reworked: list[str] = []
        for decision in decisions:
            current = planned[decision.candidate_id]
            if decision.action == "accept":
                states[decision.candidate_id] = LoopCandidate(
                    replace(current.candidate, candidate_status="pending_confirmation"),
                    current.term_overrides,
                    "accepted",
                )
                continue
            if self.round_no >= MAX_ROUNDS:
                raise _error("第二轮不得再rework")
            merged = dict(current.term_overrides)
            merged.update(decision.term_adjustments)
            updated = create_term_variant(
                current.candidate,
                term_overrides=merged,
                generation_reason=f"mode2_rework_round_{self.round_no}",
            )
            states[decision.candidate_id] = LoopCandidate(updated, merged, "open")
            reworked.append(decision.candidate_id)
        next_states = tuple(states[item.candidate.candidate_id] for item in self.candidates)
        accepted = tuple(item.candidate for item in next_states if item.status == "accepted")
        if not reworked:
            return LoopTransition(None, accepted, ())
        return LoopTransition(
            ProductTraderLoop(next_states, self.round_no + 1, self.allowed_term_keys),
            accepted,
            tuple(reworked),
        )


def attach_host_evaluations(
    round_plan: ProductTraderRound,
    host_results: Mapping[str, Mapping[str, Any]],
) -> ProductTraderRound:
    """把Host返回的当前候选运行引用附着到本轮计划。"""

    if not isinstance(host_results, Mapping):
        raise _error("Host计算结果必须为对象")
    expected = {item.candidate.candidate.candidate_id for item in round_plan.plans}
    if set(host_results) != expected:
        raise _error("Host计算结果必须覆盖本轮全部candidate_id")
    attached: list[EvaluationPlan] = []
    for plan in round_plan.plans:
        candidate_id = plan.candidate.candidate.candidate_id
        result = _mapping(host_results[candidate_id], f"Host计算结果.{candidate_id}")
        allowed = {"evaluation_records", "module_run_refs", "module_statuses", "key_terms", "display_terms", "verified_metrics"}
        unknown = sorted(set(result) - allowed)
        if unknown:
            raise _error(f"Host计算结果.{candidate_id}含未知字段：{','.join(unknown)}")
        required = {"evaluation_records", "module_run_refs", "module_statuses"}
        missing = sorted(required - set(result))
        if missing:
            raise _error(f"Host计算结果.{candidate_id}缺少字段：{','.join(missing)}")
        raw_records = _sequence(result["evaluation_records"], f"Host计算结果.{candidate_id}.evaluation_records")
        records = tuple(
            EvaluationRecord.from_mapping(item) if isinstance(item, Mapping) else item
            for item in raw_records
        )
        if any(not isinstance(item, EvaluationRecord) for item in records):
            raise _error(f"Host计算结果.{candidate_id}.evaluation_records无效")
        if any(item.candidate_id != candidate_id or item.round_no != round_plan.round_no for item in records):
            raise _error(f"Host计算结果.{candidate_id}存在非当前候选或轮次的EvaluationRecord")
        record_modules = [item.module for item in records]
        if len(record_modules) != len(set(record_modules)) or set(record_modules) != set(plan.modules):
            raise _error(f"Host计算结果.{candidate_id}未严格覆盖计划模块")
        raw_runs = _sequence(result["module_run_refs"], f"Host计算结果.{candidate_id}.module_run_refs")
        runs = tuple(item.module_run_ref for item in records if item.module_run_ref is not None)
        serialized_runs = tuple(item.__dict__ if hasattr(item, "__dict__") else item for item in runs)
        if tuple(raw_runs) != serialized_runs:
            raise _error(f"Host计算结果.{candidate_id}.module_run_refs与EvaluationRecord不一致")
        statuses = _mapping(result["module_statuses"], f"Host计算结果.{candidate_id}.module_statuses")
        expected_statuses = {item.module: item.status for item in records}
        if {str(key): str(value) for key, value in statuses.items()} != expected_statuses:
            raise _error(f"Host计算结果.{candidate_id}.module_statuses与EvaluationRecord不一致")
        raw_terms = result.get("key_terms", result.get("display_terms", ()))
        terms = _key_terms(raw_terms, f"Host计算结果.{candidate_id}.key_terms")
        states = set(expected_statuses.values())
        candidate_status = (
            "approved" if states <= {"succeeded"} else
            "unsupported" if states <= {"succeeded", "unsupported"} and "unsupported" in states else
            "pending_data"
        )
        updated = replace(
            plan.candidate.candidate,
            key_terms=terms,
            candidate_status=candidate_status,
            module_run_refs=runs,
            module_statuses=expected_statuses,
            evaluation_records=records,
        )
        attached.append(replace(plan, candidate=replace(plan.candidate, candidate=updated)))
    return ProductTraderRound(round_no=round_plan.round_no, plans=tuple(attached))
