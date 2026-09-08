"""按candidate_id调度已批准的当前候选，不编译或保存合同。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from .agent_steps import AgentStepRunner
from .models import EvaluationRecord, RecommendationCandidate, RecommendationValidationError, module_execution_from_tool_result
from .ports import ToolPort


ALLOWED_MODULES = {"datafetcher", "payoffer", "pricer", "backtester", "reporter"}
EXECUTION_MODULES = {"payoffer", "pricer", "backtester"}


def _module_request(module: str, candidate: RecommendationCandidate) -> dict[str, Any]:
    """交给Hosted层的只是当前候选输入；Hosted层按最新OptionReg即时编译。"""

    current_inputs = dict(candidate.current_inputs)
    raw_module_inputs = current_inputs.pop("module_inputs", {})
    if not isinstance(raw_module_inputs, Mapping):
        raise RecommendationValidationError("current_inputs.module_inputs必须为对象")
    supplied = raw_module_inputs.get(module, {})
    if not isinstance(supplied, Mapping):
        raise RecommendationValidationError(f"{module}当前输入必须为对象")
    return {
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "product_id": candidate.product_id,
            "rule_revision": candidate.rule_revision,
            "underlyings": list(candidate.underlyings),
            "current_inputs": current_inputs,
        },
        "module_inputs": dict(supplied),
        "compile_policy": "latest_optionreg_at_confirmation",
    }


def execute(
    candidates: Sequence[RecommendationCandidate],
    *,
    approved_candidate_ids: Sequence[str],
    requested_outputs: Sequence[str],
    tenant_id: str = "local",
    task_id: str,
    run_id: str,
    step_runner: AgentStepRunner,
    tool_port: ToolPort | None,
) -> tuple[RecommendationCandidate, ...]:
    candidate_index = {item.candidate_id: item for item in candidates}
    if len(candidate_index) != len(candidates):
        raise RecommendationValidationError("candidate_id不得重复")
    approved = set(approved_candidate_ids)
    unknown = sorted(approved - set(candidate_index))
    if unknown:
        raise RecommendationValidationError(f"批准了不存在的候选：{','.join(unknown)}")
    if len(approved) != len(tuple(approved_candidate_ids)):
        raise RecommendationValidationError("approved_candidate_ids不得重复")
    if not approved:
        return tuple(candidates)
    blocked = {item for item in approved if candidate_index[item].library_status != "ready"}
    runnable = approved - blocked
    if blocked:
        step_runner.audit.append(
            "approval_gate", "blocked", agent_role=None,
            input_value={"approved_candidate_ids": sorted(approved)}, output_value={"blocked": sorted(blocked)},
            detail={"reason": "资料状态非ready，禁止调用计算模块"},
        )
    requested = {str(item).strip().lower() for item in requested_outputs}
    aliases = {
        "payoff": "payoffer", "payoffer": "payoffer", "pricing": "pricer", "pricer": "pricer",
        "backtest": "backtester", "backtester": "backtester",
    }
    requested_modules = {aliases[item] for item in requested if item in aliases}
    if not runnable or not requested_modules:
        return tuple(
            replace(item, candidate_status="pending_terms") if item.candidate_id in blocked else
            replace(item, candidate_status="approved") if item.candidate_id in runnable else item
            for item in candidates
        )
    if tool_port is None:
        raise RecommendationValidationError("执行ready候选需要Hosted Tool端口")
    plan = step_runner.run("Executor", {
        "approved_candidates": [candidate_index[item].to_dict() for item in sorted(runnable)],
        "requested_outputs": sorted(requested),
        "allowed_modules": sorted(EXECUTION_MODULES),
        "rule": "只规划明确请求的模块；Hosted层按最新OptionReg即时编译当前输入。",
    })
    tool_requests = plan.get("tool_requests", ())
    if isinstance(tool_requests, (str, bytes)) or not isinstance(tool_requests, Sequence):
        raise RecommendationValidationError("Executor.tool_requests必须为数组")
    planned_calls: list[tuple[str, str, dict[str, Any], str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in tool_requests:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Executor.tool_request必须为对象")
        candidate_id = str(raw.get("candidate_id", "")).strip()
        module = str(raw.get("module", "")).strip().lower()
        call_key = candidate_id, module
        if candidate_id not in runnable or module not in EXECUTION_MODULES or module not in requested_modules:
            raise RecommendationValidationError("Executor试图运行未批准候选或未请求模块")
        if call_key in seen:
            raise RecommendationValidationError(f"Executor重复调用{candidate_id}的{module}")
        seen.add(call_key)
        candidate = candidate_index[candidate_id]
        if not candidate.evidence_refs:
            raise RecommendationValidationError("执行候选缺少受控产品证据")
        body = _module_request(module, candidate)
        planned_calls.append((candidate_id, module, body, candidate.evidence_refs[0].catalog_version))
    expected = {(candidate_id, module) for candidate_id in runnable for module in requested_modules}
    if expected - seen:
        raise RecommendationValidationError("Executor漏调已请求模块")

    runs: dict[str, list[Any]] = {}
    statuses: dict[str, dict[str, str]] = {}
    records: dict[str, list[EvaluationRecord]] = {}
    for candidate_id in runnable:
        candidate = candidate_index[candidate_id]
        if any(reference.tenant_id != tenant_id or reference.task_id != task_id for reference in candidate.module_run_refs):
            raise RecommendationValidationError("候选已有ModuleRunRef不属于当前任务")
        runs[candidate_id] = [item for item in candidate.module_run_refs if item.module not in requested_modules]
        statuses[candidate_id] = {module: status for module, status in candidate.module_statuses.items() if module not in requested_modules}
        records[candidate_id] = [item for item in candidate.evaluation_records if item.module not in requested_modules]

    for position, (candidate_id, module, body, catalog_version) in enumerate(planned_calls, start=1):
        limitation = None
        try:
            result = tool_port.call(module, body)
            status, run_ref, limitation = module_execution_from_tool_result(
                module, result, tenant_id=tenant_id, task_id=task_id,
                candidate_id=candidate_id, catalog_version=catalog_version,
            )
            step_runner.audit.append(
                f"tool.{module}", status, agent_role="Executor", input_value=body, output_value=result,
                detail={"candidate_id": candidate_id, "recommendation_run_id": run_id, "call_index": position,
                        **({"limitation": limitation} if limitation else {})},
            )
        except RecommendationValidationError:
            raise
        except Exception as error:
            status, run_ref = "failed", None
            limitation = str(error)
            step_runner.audit.append(
                f"tool.{module}", "failed", agent_role="Executor", input_value=body, output_value=None,
                detail={"candidate_id": candidate_id, "recommendation_run_id": run_id,
                        "call_index": position, "message": str(error)},
            )
        statuses[candidate_id][module] = status
        if run_ref is not None:
            runs[candidate_id].append(run_ref)
        records[candidate_id].append(EvaluationRecord(
            evaluation_id=f"evaluation-{candidate_id}-{module}", candidate_id=candidate_id,
            module=module, status=status, module_run_ref=run_ref,
            limitation=None if run_ref is not None else limitation or f"{module}未产出可消费的运行结果",
            idempotency_state="busy" if status in {"pending", "running"} else "completed",
            source_mode="live" if run_ref is not None else "unavailable",
        ))
    updated: list[RecommendationCandidate] = []
    for item in candidates:
        if item.candidate_id in blocked:
            updated.append(replace(item, candidate_status="pending_terms"))
            continue
        if item.candidate_id not in runnable:
            updated.append(item)
            continue
        state = set(statuses[item.candidate_id].values())
        candidate_status = (
            "approved" if state == {"succeeded"} else
            "unsupported" if "unsupported" in state and state <= {"unsupported", "succeeded"} else
            "pending_data" if state & {"partial", "failed", "cancelled", "timed_out"} else "running"
        )
        updated.append(replace(
            item, candidate_status=candidate_status, module_run_refs=tuple(runs[item.candidate_id]),
            module_statuses=statuses[item.candidate_id], evaluation_records=tuple(records[item.candidate_id]),
        ))
    return tuple(updated)
