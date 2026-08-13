"""只通过Tool Port调度已批准候选，不包含任何金融计算。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from .agent_steps import AgentStepRunner
from .models import CandidateContract, RecommendationCandidate, RecommendationValidationError, module_execution_from_tool_result
from .ports import ToolPort


ALLOWED_MODULES = {"datafetcher", "payoffer", "pricer", "backtester", "reporter"}
EXECUTION_MODULES = {"payoffer", "pricer", "backtester"}


def _module_request(module: str, contract: CandidateContract) -> dict[str, Any]:
    """组装共享ToolSchema输入；合同解析与Schema验证归Core。"""
    request: dict[str, Any] = {"contract": dict(contract.resolved_contract)}
    supplied = dict(contract.module_inputs.get(module, {}))
    schema_fields = {
        "payoffer": set(),
        "pricer": {"pricing_config", "market_data_refs"},
        "backtester": {"backtest_config", "historical_data"},
    }[module]
    unknown = sorted(set(supplied) - schema_fields)
    if unknown:
        raise RecommendationValidationError(f"{module}输入含共享ToolSchema之外字段：{','.join(unknown)}")
    missing = sorted(schema_fields - set(supplied))
    if missing:
        raise RecommendationValidationError(f"{module}缺少共享ToolSchema字段：{','.join(missing)}")
    request.update(supplied)
    return request


def execute(
    candidates: Sequence[RecommendationCandidate],
    *,
    approved_candidate_ids: Sequence[str],
    requested_outputs: Sequence[str],
    tenant_id: str = "local",
    task_id: str,
    run_id: str,
    candidate_contracts: Mapping[str, Mapping[str, Any]],
    step_runner: AgentStepRunner,
    tool_port: ToolPort | None,
) -> tuple[RecommendationCandidate, ...]:
    approved = set(approved_candidate_ids)
    candidate_index = {item.candidate_id: item for item in candidates}
    unknown = sorted(approved - set(candidate_index))
    if unknown:
        raise RecommendationValidationError(f"批准了不存在的候选：{','.join(unknown)}")
    stale_contracts = sorted(set(candidate_contracts) - set(candidate_index))
    if stale_contracts:
        raise RecommendationValidationError(f"CandidateContract不属于当前候选：{','.join(stale_contracts)}")
    unapproved_contracts = sorted(set(candidate_contracts) - approved)
    if unapproved_contracts:
        raise RecommendationValidationError(f"CandidateContract未经当前批准：{','.join(unapproved_contracts)}")
    if not approved:
        return tuple(candidates)
    blocked = {candidate_id for candidate_id in approved if candidate_index[candidate_id].library_status != "ready"}
    runnable = approved - blocked
    if blocked:
        step_runner.audit.append(
            "approval_gate", "blocked", agent_role=None,
            input_value={"approved_candidate_ids": sorted(approved)}, output_value={"blocked": sorted(blocked)},
            detail={"reason": "资料状态非ready，禁止调用计算模块"},
        )
    if not runnable:
        return tuple(
            replace(item, candidate_status="pending_terms") if item.candidate_id in blocked else item
            for item in candidates
        )
    missing_contracts = sorted(runnable - set(candidate_contracts))
    if missing_contracts:
        raise RecommendationValidationError(f"已批准候选缺少CandidateContract：{','.join(missing_contracts)}")
    contracts = {
        candidate_id: CandidateContract.from_mapping(candidate_contracts[candidate_id], candidate=candidate_index[candidate_id])
        for candidate_id in runnable
    }
    requested = {str(item).strip().lower() for item in requested_outputs}
    output_to_module = {
        "payoff": "payoffer", "payoffer": "payoffer",
        "pricing": "pricer", "pricer": "pricer",
        "backtest": "backtester", "backtester": "backtester",
    }
    requested_modules = {output_to_module[item] for item in requested if item in output_to_module}
    if not requested_modules:
        return tuple(
            replace(item, candidate_status="pending_terms") if item.candidate_id in blocked else
            replace(item, candidate_status="approved") if item.candidate_id in runnable else item
            for item in candidates
        )
    if tool_port is None:
        raise RecommendationValidationError("执行ready候选需要Tool Gateway正式端口")
    plan = step_runner.run("Executor", {
        "approved_candidates": [candidate_index[item].to_dict() for item in sorted(runnable)],
        "requested_outputs": sorted(requested),
        "allowed_modules": sorted(EXECUTION_MODULES),
        "rule": "只规划明确请求的模块；不得生成任何金融数值。",
    })
    tool_requests = plan.get("tool_requests", ())
    if isinstance(tool_requests, (str, bytes)) or not isinstance(tool_requests, Sequence):
        raise RecommendationValidationError("Executor.tool_requests必须为数组")
    runs: dict[str, list[Any]] = {item: [] for item in runnable}
    statuses_by_candidate: dict[str, dict[str, str]] = {item: {} for item in runnable}
    seen_calls: set[tuple[str, str]] = set()
    if len(tool_requests) > len(runnable) * len(EXECUTION_MODULES):
        raise RecommendationValidationError("Executor工具调用数量超过候选与模块上限")
    for position, raw in enumerate(tool_requests, start=1):
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Executor.tool_request必须为对象")
        candidate_id = str(raw.get("candidate_id", "")).strip()
        module = str(raw.get("module", "")).strip().lower()
        if candidate_id not in runnable:
            raise RecommendationValidationError(f"Executor试图运行未批准候选：{candidate_id}")
        if module not in EXECUTION_MODULES:
            raise RecommendationValidationError(f"Executor试图调用未授权模块：{module}")
        call_key = (candidate_id, module)
        if call_key in seen_calls:
            raise RecommendationValidationError(f"Executor重复调用{candidate_id}的{module}")
        seen_calls.add(call_key)
        if module not in requested_modules:
            raise RecommendationValidationError(f"Executor试图调用未请求模块：{module}")
        contract = contracts[candidate_id]
        body = _module_request(module, contract)
        try:
            result = tool_port.call(module, body)
            status, run_ref, limitation = module_execution_from_tool_result(
                module,
                result,
                tenant_id=tenant_id,
                task_id=task_id,
                candidate_id=contract.candidate_id,
                catalog_version=contract.catalog_version,
                contract_fingerprint=contract.contract_fingerprint,
            )
            step_runner.audit.append(f"tool.{module}", status, agent_role="Executor", input_value=body,
                                     output_value=result, detail={
                                         "candidate_id": candidate_id, "task_id": task_id,
                                         "recommendation_run_id": run_id, "call_index": position,
                                         **({"limitation": limitation} if limitation else {}),
                                     })
        except RecommendationValidationError:
            raise
        except Exception as error:
            status, run_ref = "failed", None
            step_runner.audit.append(f"tool.{module}", "failed", agent_role="Executor", input_value=body,
                                     output_value=None, detail={
                                         "candidate_id": candidate_id, "task_id": task_id,
                                         "recommendation_run_id": run_id, "call_index": position,
                                         "message": str(error),
                                     })
        statuses_by_candidate[candidate_id][module] = status
        if run_ref is not None:
            runs[candidate_id].append(run_ref)

    expected_calls = {(candidate_id, module) for candidate_id in runnable for module in requested_modules}
    missing_calls = sorted(expected_calls - seen_calls)
    if missing_calls:
        detail = ",".join(f"{candidate_id}:{module}" for candidate_id, module in missing_calls)
        raise RecommendationValidationError(f"Executor漏调已请求模块：{detail}")

    updated: list[RecommendationCandidate] = []
    for item in candidates:
        if item.candidate_id in blocked:
            updated.append(replace(item, candidate_status="pending_terms"))
            continue
        if item.candidate_id not in runnable:
            updated.append(item)
            continue
        item_runs = tuple(runs[item.candidate_id])
        statuses = set(statuses_by_candidate[item.candidate_id].values())
        if statuses == {"succeeded"}:
            candidate_status = "approved"
        elif "unsupported" in statuses and statuses <= {"unsupported", "succeeded"}:
            candidate_status = "unsupported"
        elif statuses & {"partial", "failed", "cancelled", "timed_out"}:
            candidate_status = "pending_data"
        else:
            candidate_status = "running"
        updated.append(replace(
            item,
            candidate_status=candidate_status,
            module_run_refs=item_runs,
            module_statuses=statuses_by_candidate[item.candidate_id],
        ))
    return tuple(updated)
