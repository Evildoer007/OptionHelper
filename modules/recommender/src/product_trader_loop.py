"""Mode2产品交易闭环的纯领域控制面。

这里不解析合同、不调用模块，也不采信模型生成的金融数值。模型只能提出
受控条款调整和模块计划；计算事实只能由Host以``EvaluationRecord``带回。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from .candidate_builder import create_term_variant
from .models import CandidateVersion, EvaluationRecord, RecommendationCandidate, RecommendationValidationError


MAX_ROUNDS = 2
ALLOWED_MODULES = frozenset({"payoffer", "pricer", "backtester"})
# 与现有交互层一致：最终仍须由Host按具体产品OptionReg条款再次收窄。
DEFAULT_TERM_KEYS = frozenset({
    "T", "K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "c", "c_max", "alpha",
    "H_KO", "H_KI", "B", "O_KO", "O_KI", "Oc", "settlement", "exercise_style",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")


def _error(message: str) -> RecommendationValidationError:
    return RecommendationValidationError(f"ProductTraderLoop：{message}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


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


def _display_terms(value: Any) -> tuple[Mapping[str, str], ...]:
    rows = _sequence(value, "Host.display_terms")
    result: list[Mapping[str, str]] = []
    for index, item in enumerate(rows):
        row = _mapping(item, f"Host.display_terms[{index}]")
        _exact_fields(row, fields={"label", "value", "source"}, field_name=f"Host.display_terms[{index}]")
        label = str(row["label"]).strip()
        text = str(row["value"]).strip()
        source = str(row["source"]).strip()
        if not label or not text or source not in {"用户输入", "拟采用参数"}:
            raise _error("Host.display_terms必须使用受控客户条款摘要")
        result.append({"label": label, "value": text, "source": source})
    return tuple(result)


@dataclass(frozen=True)
class StructurerProposal:
    """Structurer对一个既有候选的受控计划，不含任何计算结果。"""

    candidate_key: str
    evaluation_plan: tuple[str, ...]
    term_overrides: Mapping[str, str | int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "evaluation_plan": list(self.evaluation_plan),
            "term_overrides": dict(self.term_overrides),
        }


@dataclass(frozen=True)
class TraderDecision:
    """Trader仅可接受候选或要求已有候选重构。"""

    candidate_key: str
    action: str
    term_adjustments: Mapping[str, str | int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "action": self.action,
            "term_adjustments": dict(self.term_adjustments),
        }


@dataclass(frozen=True)
class LoopCandidate:
    """候选的当前版本及其已批准调整；不存在模型生成的估值字段。"""

    candidate: RecommendationCandidate
    term_overrides: Mapping[str, str | int | float]
    status: str = "open"

    def __post_init__(self) -> None:
        if self.status not in {"open", "accepted"}:
            raise _error("LoopCandidate.status无效")
        if self.candidate.candidate_version is None or not self.candidate.candidate_key:
            raise _error("Mode2候选必须携带CandidateVersion与candidate_key")
        if self.candidate.candidate_version.candidate_key != self.candidate.candidate_key:
            raise _error("LoopCandidate候选身份与版本冲突")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "term_overrides": dict(self.term_overrides),
            "status": self.status,
        }


@dataclass(frozen=True)
class EvaluationRequest:
    """Host计算请求的不可歧义绑定，Host须以同版本EvaluationRecord回填。"""

    candidate_id: str
    candidate_key: str
    version_id: str
    module: str
    round_no: int
    input_fingerprint: str
    product_id: str
    underlyings: tuple[str, ...]
    term_overrides: Mapping[str, str | int | float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_key": self.candidate_key,
            "version_id": self.version_id,
            "module": self.module,
            "round_no": self.round_no,
            "input_fingerprint": self.input_fingerprint,
            "product_id": self.product_id,
            "underlyings": list(self.underlyings),
            "term_overrides": dict(self.term_overrides),
        }


@dataclass(frozen=True)
class EvaluationPlan:
    """一个候选版本在某轮需要由Host完成的模块调用。"""

    candidate: LoopCandidate
    modules: tuple[str, ...]
    round_no: int

    def requests(self) -> tuple[EvaluationRequest, ...]:
        version = self.candidate.candidate.candidate_version
        assert version is not None  # 由LoopCandidate.__post_init__保证
        requests = []
        for module in self.modules:
            binding = {
                "candidate_key": version.candidate_key,
                "version_id": version.version_id,
                "module": module,
                "round_no": self.round_no,
                "term_overrides": dict(self.candidate.term_overrides),
            }
            requests.append(EvaluationRequest(
                candidate_id=self.candidate.candidate.candidate_id,
                candidate_key=version.candidate_key,
                version_id=version.version_id,
                module=module,
                round_no=self.round_no,
                input_fingerprint=_fingerprint(binding),
                product_id=version.product_id,
                underlyings=version.underlyings,
                term_overrides=dict(self.candidate.term_overrides),
            ))
        return tuple(requests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "modules": list(self.modules),
            "round_no": self.round_no,
            "requests": [item.to_dict() for item in self.requests()],
        }


@dataclass(frozen=True)
class ProductTraderRound:
    """已经由Structurer计划、可由Host执行的单轮候选评估。"""

    round_no: int
    plans: tuple[EvaluationPlan, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.round_no <= MAX_ROUNDS:
            raise _error("ProductTraderRound.round_no超出上限")
        keys = [plan.candidate.candidate.candidate_key for plan in self.plans]
        if not keys or len(keys) != len(set(keys)):
            raise _error("ProductTraderRound候选必须非空且不重复")

    def to_dict(self) -> dict[str, Any]:
        return {"round_no": self.round_no, "plans": [item.to_dict() for item in self.plans]}


@dataclass(frozen=True)
class LoopTransition:
    """Trader复核后的状态。rework只会产生下一轮的全新CandidateVersion。"""

    next_loop: "ProductTraderLoop | None"
    accepted_candidates: tuple[RecommendationCandidate, ...]
    reworked_candidate_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_loop": self.next_loop.to_dict() if self.next_loop else None,
            "accepted_candidates": [item.to_dict() for item in self.accepted_candidates],
            "reworked_candidate_keys": list(self.reworked_candidate_keys),
        }


@dataclass(frozen=True)
class ProductTraderLoop:
    """至多两轮的产品团队—交易团队闭环状态机。"""

    candidates: tuple[LoopCandidate, ...]
    completed_rounds: int = 0
    allowed_term_keys: frozenset[str] = DEFAULT_TERM_KEYS

    def __post_init__(self) -> None:
        if not 0 <= self.completed_rounds <= MAX_ROUNDS:
            raise _error("completed_rounds超出上限")
        if not self.candidates:
            raise _error("至少需要一个候选")
        keys = [item.candidate.candidate_key for item in self.candidates]
        versions = [item.candidate.candidate_version_id for item in self.candidates]
        if len(keys) != len(set(keys)) or len(versions) != len(set(versions)):
            raise _error("候选身份或当前版本不得重复")
        if not self.allowed_term_keys:
            raise _error("allowed_term_keys不能为空")

    @classmethod
    def start(
        cls,
        candidates: Sequence[RecommendationCandidate],
        *,
        allowed_term_keys: Sequence[str] = tuple(DEFAULT_TERM_KEYS),
    ) -> "ProductTraderLoop":
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise _error("candidates必须为数组")
        keys = frozenset(str(item).strip() for item in allowed_term_keys if str(item).strip())
        if not keys:
            raise _error("allowed_term_keys不能为空")
        return cls(
            candidates=tuple(LoopCandidate(candidate=item, term_overrides={}) for item in candidates),
            allowed_term_keys=keys,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "completed_rounds": self.completed_rounds,
            "allowed_term_keys": sorted(self.allowed_term_keys),
        }

    def parse_structurer_output(self, value: Any) -> tuple[StructurerProposal, ...]:
        """严格接受既有候选的模块计划与全量受控条款覆盖。"""

        payload = _mapping(value, "Structurer输出")
        _exact_fields(payload, fields={"proposals"}, field_name="Structurer输出")
        rows = _sequence(payload["proposals"], "Structurer.proposals")
        if not rows or len(rows) > 10:
            raise _error("Structurer.proposals数量必须位于1至10")
        available = {item.candidate.candidate_key for item in self.candidates if item.status == "open"}
        result: list[StructurerProposal] = []
        seen: set[str] = set()
        for index, raw in enumerate(rows):
            row = _mapping(raw, f"Structurer.proposals[{index}]")
            _exact_fields(
                row,
                fields={"candidate_key", "evaluation_plan", "term_overrides"},
                field_name=f"Structurer.proposals[{index}]",
            )
            candidate_key = _identifier(row["candidate_key"], f"Structurer.proposals[{index}].candidate_key")
            if candidate_key not in available:
                raise _error(f"Structurer引用不存在或已接受候选：{candidate_key}")
            if candidate_key in seen:
                raise _error(f"Structurer重复候选：{candidate_key}")
            seen.add(candidate_key)
            modules = tuple(str(item).strip().lower() for item in _sequence(
                row["evaluation_plan"], f"Structurer.proposals[{index}].evaluation_plan"
            ))
            if not modules or any(item not in ALLOWED_MODULES for item in modules):
                raise _error("Structurer.evaluation_plan只允许payoffer、pricer、backtester")
            if len(set(modules)) != len(modules):
                raise _error("Structurer.evaluation_plan不得重复模块")
            overrides = _controlled_term_overrides(
                row["term_overrides"], allowed_term_keys=self.allowed_term_keys,
                field_name=f"Structurer.proposals[{index}].term_overrides",
            )
            result.append(StructurerProposal(candidate_key, modules, overrides))
        return tuple(result)

    def plan_round(self, structurer_output: Any) -> ProductTraderRound:
        if self.completed_rounds >= MAX_ROUNDS:
            raise _error("已达到两轮上限，禁止继续重构")
        proposals = self.parse_structurer_output(structurer_output)
        states = {item.candidate.candidate_key: item for item in self.candidates}
        plans: list[EvaluationPlan] = []
        round_no = self.completed_rounds + 1
        for proposal in proposals:
            state = states[proposal.candidate_key]
            if dict(proposal.term_overrides) != dict(state.term_overrides):
                state = LoopCandidate(
                    candidate=_new_version(
                        state,
                        overrides=proposal.term_overrides,
                        generation_reason="structurer_term_overrides",
                    ),
                    term_overrides=dict(proposal.term_overrides),
                    status="open",
                )
            plans.append(EvaluationPlan(candidate=state, modules=proposal.evaluation_plan, round_no=round_no))
        return ProductTraderRound(round_no=round_no, plans=tuple(plans))

    def parse_trader_output(self, value: Any, *, round_plan: ProductTraderRound) -> tuple[TraderDecision, ...]:
        payload = _mapping(value, "Trader输出")
        _exact_fields(payload, fields={"decisions"}, field_name="Trader输出")
        rows = _sequence(payload["decisions"], "Trader.decisions")
        expected = {item.candidate.candidate.candidate_key for item in round_plan.plans}
        if len(rows) != len(expected):
            raise _error("Trader必须对本轮每个候选恰好给出一个决定")
        result: list[TraderDecision] = []
        seen: set[str] = set()
        for index, raw in enumerate(rows):
            row = _mapping(raw, f"Trader.decisions[{index}]")
            _exact_fields(
                row,
                fields={"candidate_key", "action", "term_adjustments"},
                field_name=f"Trader.decisions[{index}]",
            )
            key = _identifier(row["candidate_key"], f"Trader.decisions[{index}].candidate_key")
            if key not in expected:
                raise _error(f"Trader不得新增或引用未知候选：{key}")
            if key in seen:
                raise _error(f"Trader重复决定候选：{key}")
            seen.add(key)
            action = str(row["action"]).strip().lower()
            if action not in {"accept", "rework"}:
                raise _error("Trader.action只允许accept或rework")
            adjustments = _controlled_term_overrides(
                row["term_adjustments"], allowed_term_keys=self.allowed_term_keys,
                field_name=f"Trader.decisions[{index}].term_adjustments",
            )
            if action == "accept" and adjustments:
                raise _error("Trader.accept不得附带term_adjustments")
            if action == "rework" and not adjustments:
                raise _error("Trader.rework必须附带term_adjustments")
            result.append(TraderDecision(key, action, adjustments))
        if seen != expected:
            raise _error("Trader决定集合与本轮候选不一致")
        return tuple(result)

    def apply_trader_output(self, round_plan: ProductTraderRound, trader_output: Any) -> LoopTransition:
        if round_plan.round_no != self.completed_rounds + 1:
            raise _error("Trader复核轮次与当前Loop状态不一致")
        if round_plan.round_no > MAX_ROUNDS:
            raise _error("已达到两轮上限")
        decisions = self.parse_trader_output(trader_output, round_plan=round_plan)
        planned = {item.candidate.candidate.candidate_key: item.candidate for item in round_plan.plans}
        states = {item.candidate.candidate_key: item for item in self.candidates}
        reworked: list[str] = []
        for decision in decisions:
            current = planned[decision.candidate_key]
            if decision.action == "accept":
                states[decision.candidate_key] = LoopCandidate(
                    candidate=current.candidate,
                    term_overrides=dict(current.term_overrides),
                    status="accepted",
                )
                continue
            if round_plan.round_no >= MAX_ROUNDS:
                raise _error("第二轮不得再rework")
            merged = {**dict(current.term_overrides), **dict(decision.term_adjustments)}
            if merged == dict(current.term_overrides):
                raise _error("Trader.rework必须实际改变受控条款")
            states[decision.candidate_key] = LoopCandidate(
                candidate=_new_version(
                    current,
                    overrides=merged,
                    generation_reason="trader_term_adjustments",
                ),
                term_overrides=merged,
                status="open",
            )
            reworked.append(decision.candidate_key)
        next_states = tuple(states[item.candidate.candidate_key] for item in self.candidates)
        accepted = tuple(item.candidate for item in next_states if item.status == "accepted")
        if not reworked:
            return LoopTransition(next_loop=None, accepted_candidates=accepted, reworked_candidate_keys=())
        return LoopTransition(
            next_loop=ProductTraderLoop(
                candidates=next_states,
                completed_rounds=round_plan.round_no,
                allowed_term_keys=self.allowed_term_keys,
            ),
            accepted_candidates=accepted,
            reworked_candidate_keys=tuple(reworked),
        )


def attach_host_evaluations(
    round_plan: ProductTraderRound,
    host_results: Any,
) -> ProductTraderRound:
    """把Host已绑定的计算事实附到候选，拒绝模型或错版本回填。"""

    payload = _mapping(host_results, "Host计算结果")
    expected = {item.candidate.candidate.candidate_key for item in round_plan.plans}
    if set(payload) != expected:
        unknown = sorted(set(payload) - expected)
        missing = sorted(expected - set(payload))
        detail = []
        if unknown:
            detail.append(f"未知候选：{','.join(unknown)}")
        if missing:
            detail.append(f"缺少候选：{','.join(missing)}")
        raise _error(f"Host计算结果集合不匹配（{'；'.join(detail)}）")
    attached: list[EvaluationPlan] = []
    for plan in round_plan.plans:
        key = plan.candidate.candidate.candidate_key
        result = _mapping(payload[key], f"Host计算结果.{key}")
        _exact_fields(
            result,
            fields={"evaluation_records", "module_run_refs", "module_statuses", "display_terms", "contract_fingerprint"},
            field_name=f"Host计算结果.{key}",
        )
        raw_records = _sequence(result["evaluation_records"], f"Host计算结果.{key}.evaluation_records")
        records: list[EvaluationRecord] = []
        for raw in raw_records:
            try:
                record = EvaluationRecord.from_mapping(raw) if isinstance(raw, Mapping) else raw
            except (TypeError, ValueError) as error:
                raise _error(f"Host计算结果.{key}.evaluation_records无效") from error
            if not isinstance(record, EvaluationRecord):
                raise _error(f"Host计算结果.{key}.evaluation_records必须使用EvaluationRecord")
            records.append(record)
        if not records or len({item.evaluation_id for item in records}) != len(records):
            raise _error(f"Host计算结果.{key}.evaluation_records必须非空且evaluation_id不重复")
        version = plan.candidate.candidate.candidate_version
        assert version is not None
        if any(item.version_id != version.version_id for item in records):
            raise _error(f"Host计算结果.{key}存在非当前CandidateVersion的EvaluationRecord")
        if any(item.round_no != plan.round_no for item in records):
            raise _error(f"Host计算结果.{key}的EvaluationRecord轮次不一致")
        if any(item.module not in plan.modules for item in records):
            raise _error(f"Host计算结果.{key}包含未计划模块")
        if set(item.module for item in records) != set(plan.modules):
            raise _error(f"Host计算结果.{key}未覆盖evaluation_plan全部模块")
        request_fingerprints = {item.module: item.input_fingerprint for item in plan.requests()}
        if any(item.input_fingerprint != request_fingerprints[item.module] for item in records):
            raise _error(f"Host计算结果.{key}的EvaluationRecord输入指纹不一致")
        raw_refs = _sequence(result["module_run_refs"], f"Host计算结果.{key}.module_run_refs")
        expected_refs = tuple(item.module_run_ref for item in records if item.module_run_ref is not None)
        if tuple(raw_refs) != expected_refs and tuple(
            dict(item) if isinstance(item, Mapping) else getattr(item, "__dict__", item) for item in raw_refs
        ) != tuple(dict(item.__dict__) for item in expected_refs):
            raise _error(f"Host计算结果.{key}.module_run_refs与EvaluationRecord冲突")
        statuses = _mapping(result["module_statuses"], f"Host计算结果.{key}.module_statuses")
        final_statuses = {item.module: item.status for item in records}
        if statuses != final_statuses:
            raise _error(f"Host计算结果.{key}.module_statuses与EvaluationRecord冲突")
        terms = _display_terms(result["display_terms"])
        contract_fingerprint = str(result["contract_fingerprint"]).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", contract_fingerprint):
            raise _error(f"Host计算结果.{key}.contract_fingerprint无效")
        candidate = replace(
            plan.candidate.candidate,
            key_terms=terms,
            module_run_refs=expected_refs,
            module_statuses=final_statuses,
            evaluation_records=tuple(records),
            contract_fingerprint=contract_fingerprint,
            term_overrides=dict(plan.candidate.term_overrides),
        )
        attached.append(EvaluationPlan(
            candidate=LoopCandidate(candidate=candidate, term_overrides=dict(plan.candidate.term_overrides), status="open"),
            modules=plan.modules,
            round_no=plan.round_no,
        ))
    return ProductTraderRound(round_no=round_plan.round_no, plans=tuple(attached))


def _new_version(
    state: LoopCandidate,
    *,
    overrides: Mapping[str, str | int | float],
    generation_reason: str,
) -> RecommendationCandidate:
    normalized = {key: overrides[key] for key in sorted(overrides)}
    variant_constraints_fingerprint = _fingerprint({
        "previous_constraints_fingerprint": state.candidate.constraints_fingerprint,
        "term_overrides": normalized,
    })
    return create_term_variant(
        state.candidate,
        term_overrides=normalized,
        constraints_fingerprint=variant_constraints_fingerprint,
        generation_reason=generation_reason,
    )
