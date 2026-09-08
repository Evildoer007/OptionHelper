"""校验Hosted当前评估的显式Run引用、模块状态和可选评估记录。"""

from typing import Any, Mapping, Sequence

from runtime.protocol.models import ModuleRunRef

from .models import EvaluationRecord, RecommendationValidationError, RUN_STATUSES


def read_evaluation_records(
    value: Mapping[str, Any],
    *,
    candidate_id: str,
    modules: Sequence[str],
    round_no: int,
    tenant_id: str | None = None,
    task_id: str | None = None,
) -> tuple[EvaluationRecord, ...]:
    """统一当前Host线协议与领域记录；不查找其他Run或补造计算结果。"""

    def reject(message: str) -> None:
        raise RecommendationValidationError(f"Host计算结果.{candidate_id}：{message}")

    if "round_no" in value and (
        type(value["round_no"]) is not int or value["round_no"] != round_no
    ):
        reject("评估不属于当前轮次")
    statuses = value.get("module_statuses")
    if not isinstance(statuses, Mapping) or set(statuses) != set(modules):
        reject("module_statuses未严格覆盖计划模块")
    if any(status not in RUN_STATUSES for status in statuses.values()):
        reject("module_statuses包含无效状态")
    raw_runs = value.get("module_run_refs")
    if isinstance(raw_runs, (str, bytes)) or not isinstance(raw_runs, Sequence):
        reject("缺少显式module_run_refs数组")
    by_module: dict[str, ModuleRunRef] = {}
    for raw in raw_runs:
        try:
            reference = ModuleRunRef(**dict(raw)) if isinstance(raw, Mapping) else raw
        except (TypeError, ValueError) as error:
            raise RecommendationValidationError("Host计算结果包含无效ModuleRunRef") from error
        if not isinstance(reference, ModuleRunRef):
            reject("module_run_refs必须使用Core ModuleRunRef")
        if reference.module not in modules or reference.module in by_module:
            reject("module_run_refs包含非计划模块或重复模块")
        if tenant_id is not None and reference.tenant_id != tenant_id:
            reject("ModuleRunRef不属于当前tenant_id")
        if task_id is not None and reference.task_id != task_id:
            reject("ModuleRunRef不属于当前task_id")
        by_module[reference.module] = reference
    for module, status in statuses.items():
        if (status in {"succeeded", "partial"}) != (module in by_module):
            reject(f"{module}状态与显式ModuleRunRef不一致")

    raw_records = value.get("evaluation_records")
    if "evaluation_records" in value:
        if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
            reject("evaluation_records必须为数组")
        records = tuple(
            EvaluationRecord.from_mapping(item) if isinstance(item, Mapping) else item
            for item in raw_records
        )
        if any(not isinstance(item, EvaluationRecord) for item in records):
            reject("evaluation_records无效")
        if any(item.candidate_id != candidate_id or item.round_no != round_no for item in records):
            reject("存在非当前候选或轮次的EvaluationRecord")
        if len(records) != len(modules) or {item.module for item in records} != set(modules):
            reject("EvaluationRecord未严格覆盖计划模块")
        for record in records:
            if record.status != statuses[record.module] or record.module_run_ref != by_module.get(record.module):
                reject("module_run_refs或module_statuses与EvaluationRecord不一致")
            if record.module_run_ref is not None and (
                record.idempotency_state != "completed" or record.source_mode == "unavailable"
            ):
                reject("EvaluationRecord尚未完成，不能消费Run证据")
        return records

    # App线协议直接返回已执行模块状态和RunRef，无需第二次查询或另造金融事实。
    return tuple(EvaluationRecord(
        evaluation_id=f"evaluation-{candidate_id}-{module}-{round_no}",
        candidate_id=candidate_id,
        module=module,
        status=statuses[module],
        module_run_ref=by_module.get(module),
        limitation=None if module in by_module else str(
            value.get("message") or value.get("reason") or f"{module}未产出可消费的运行结果"
        ),
        idempotency_state="busy" if statuses[module] in {"pending", "running"} else "completed",
        round_no=round_no,
        source_mode="live" if module in by_module else "unavailable",
    ) for module in modules)
