"""Task-owned size and deterministic money views of verified public results.

No calculator inputs or stored ModuleRuns are rewritten. Null size returns no
money view. Units are allowlisted; a rate, spot, probability or fair parameter
is never multiplied merely because it is numeric.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import copy
import hashlib
import html
import json
import math
import re
from typing import Any, Mapping

from .errors import ValidationError

CURRENCIES = ("CNY", "USD", "HKD", "EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "SGD")
DEFAULT_POSITION = {"amount": "1000000", "currency": "CNY", "basis": "notional"}
SIZE_BASES = ("notional", "variance_notional")
_GREEK_UNITS = {
    "per_spot": "标的价格单位", "per_spot_squared": "标的价格单位²",
    "per_calendar_day": "日", "per_1pct_volatility": "波动率1个百分点",
    "per_1pct_rate": "利率1个百分点",
    "per_spot_per_1pct_volatility": "每标的价格单位及波动率1个百分点",
    "per_1pct_volatility_squared": "每波动率百分点²",
}


def validate_position(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"amount", "currency", "basis"}:
        raise ValidationError("规模设置必须包含金额、币种和规模口径")
    amount = value["amount"]
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        raise ValidationError("名义规模必须为正数")
    text = str(amount).strip()
    if len(text) > 40 or not re.fullmatch(r"\d+(?:\.\d{1,8})?", text):
        raise ValidationError("名义规模请填写普通数字，最多8位小数")
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise ValidationError("名义规模无效") from error
    if not number.is_finite() or not 0 < number <= Decimal("1000000000000000"):
        raise ValidationError("名义规模必须大于0且不超过1000万亿元")
    if value["currency"] not in CURRENCIES or value["basis"] not in SIZE_BASES:
        raise ValidationError("币种或规模口径不受支持")
    return {"amount": format(number.normalize(), "f"), "currency": value["currency"], "basis": value["basis"]}


def _amount(value: Any, position: Mapping, factor: int = 1) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    # New display values keep decimal digits without rounding the original float.
    with localcontext() as context:
        context.prec = 50
        return format(Decimal(str(value)) * Decimal(position["amount"]) * factor, "f")


def _money_unit(unit: str, currency: str, prefix: str) -> str | None:
    if unit == prefix:
        return currency
    if not unit.startswith(prefix + "_"):
        return None
    suffix = unit[len(prefix) + 1:]
    return f"{currency}/{_GREEK_UNITS[suffix]}" if suffix in _GREEK_UNITS else None


def _metric(label, value, unit, position, factor=1, **metadata):
    return {"label": label, "base_value": value, "amount": _amount(value, position, factor), "unit": unit, **metadata}


def project_pricing(pricing: Mapping, position: Mapping) -> dict:
    basis = pricing.get("value_basis")
    expected = "variance_percent" if position["basis"] == "variance_notional" else "pv_percent"
    if basis != expected:
        return {"status": "not_applicable", "reason": "该估值与所选规模口径不一致；方差互换需使用方差名义金额，其他产品使用名义本金。"}
    # Variance PV public values = variance percentage-squared points / 100.
    factor = 100 if basis == "variance_percent" else 1
    currency = position["currency"]
    rows = [_metric("合同估值", pricing.get("pv_percent"), currency, position, factor),
            _metric("估值标准误", pricing.get("standard_error_percent"), currency, position, factor)]
    for group in ("greeks", "extended_greeks"):
        for name, greek in (pricing.get(group) or {}).items():
            if not isinstance(greek, Mapping):
                continue
            unit = _money_unit(str(greek.get("unit", "")), currency, basis)
            rows.append(_metric(name.capitalize(), greek.get("value") if unit else None,
                                unit or "不适用", position, factor,
                                source_unit=greek.get("unit"), status=greek.get("status"),
                                reason=greek.get("reason") if unit else "未登记金额换算单位"))
    curves, surfaces, scenarios = [], [], []
    for source in pricing.get("risk_curves", []):
        axis = source.get("y_axis", {})
        unit = _money_unit(str(axis.get("unit", "")), currency, basis)
        if unit:
            curve = copy.deepcopy(source)
            curve["y_axis"] = {**axis, "unit": unit}
            curve["points"] = [{**point, "base_y": point.get("y"), "y": _amount(point.get("y"), position, factor)} for point in source.get("points", [])]
            curves.append(curve)
    for source in pricing.get("risk_surfaces", []):
        axis = source.get("z_axis", {})
        unit = _money_unit(str(axis.get("unit", "")), currency, basis)
        if unit:
            surface = copy.deepcopy(source)
            surface["z_axis"] = {**axis, "unit": unit}
            for point in surface.get("data", []):
                values = point.get("value")
                if isinstance(values, list) and len(values) >= 3:
                    point["base_z"] = values[2]
                    values[2] = _amount(values[2], position, factor)
            surfaces.append(surface)
    for source in pricing.get("risk_scenarios", []):
        scenario = {"name": source.get("name") or source.get("scenario_id"),
                    "amount": _amount(source.get("pv_percent"), position, factor), "unit": currency}
        scenarios.append(scenario)
    return {"status": "available", "rows": rows, "risk_curves": curves, "risk_surfaces": surfaces,
            "risk_scenarios": scenarios, "basis": basis,
            "note": "金额Greeks保留原风险因子坐标与变动单位；多标的共同变化不等于逐资产对冲数量。"}


def project_backtest(result: Mapping, position: Mapping) -> dict:
    variance = result.get("product_id") == "9.4"
    if variance != (position["basis"] == "variance_notional"):
        return {"status": "not_applicable", "reason": "回测产品与所选名义规模口径不一致。"}
    factor = 10000 if variance else 1
    currency = position["currency"]
    fields = (("平均合同结算金额", "average_contract_settlement_return"),
              ("中位合同结算金额", "median_contract_settlement_return"),
              ("最低合同结算金额", "minimum_contract_settlement_return"),
              ("最高合同结算金额", "maximum_contract_settlement_return"))
    metrics = result.get("common_metrics", {})
    rows = [_metric(label, metrics.get(key), currency, position, factor) for label, key in fields]
    ledger = []
    for trade in result.get("trade_ledger", []):
        ledger.append({key: copy.deepcopy(trade.get(key)) for key in ("trade_id", "entry_date", "exit_date", "settlement_type")})
        ledger[-1].update(_metric("合同结算金额", trade.get("contract_settlement_return"), currency, position, factor))
    return {"status": "available", "rows": rows, "trade_ledger": ledger,
            "note": "每笔历史样本均使用同一名义规模，样本金额不相加为账户收益；合同起始费用沿用原现金流口径，不额外加回本金。"}


def project_payoff(result: Mapping, position: Mapping) -> dict:
    variance = result.get("product_id") == "9.4"
    if variance != (position["basis"] == "variance_notional"):
        return {"status": "not_applicable", "reason": "收益结构与所选名义规模口径不一致。"}
    facts = result.get("reporter_payoff_facts", {})
    if facts.get("projection_status") != "controlled_percent_machine_facts" or facts.get("payoff_unit") != "percent":
        return {"status": "not_applicable", "reason": "该来源没有可验证的收益结构金额换算事实。"}
    factor = 1 if variance else Decimal("0.01")
    rows = []
    for path in facts.get("paths", []):
        for scenario_index, scenario in enumerate(path.get("scenario_segments", []), 1):
            payoff = scenario.get("payoff", {})
            if payoff.get("unit") != "percent":
                continue
            name = f'{path.get("title") or "收益路径"}，情景{scenario_index}，'
            payoff_basis = path.get("payoff_basis")
            basis_label = {"net_after_premium": "含期初费用", "gross_before_premium": "未扣期初费用"}.get(payoff_basis, "沿用原费用口径")
            for suffix, key in (("采样最低结算", "minimum_percent"), ("采样最高结算", "maximum_percent")):
                rows.append(_metric(name + suffix + "，" + basis_label, payoff.get(key), position["currency"], position, factor,
                                    domain=copy.deepcopy(scenario.get("domain")), payoff_basis=payoff_basis))
    return {"status": "available", "rows": rows,
            "note": "金额范围对应原收益图的采样情景和定义域，不代表全域最大收益或最大损失；开闭端点、路径规则和期初费用沿用原图。"}


def project_run(module: str, result: Mapping, contract: Mapping, position: Mapping) -> dict:
    if module == "pricer":
        pricing = result.get("pricing", result)
        # Fair terms themselves are rates/strikes and must never become money.
        fair = result.get("fair_parameter")
        if isinstance(fair, Mapping):
            return {"status": "not_applicable", "reason": "反解条款保留原单位，不能作为金额乘以本金；金额估值请使用普通估值结果。"}
        return project_pricing(pricing, position)
    if module == "backtester":
        payload = result.get("backtest", result)
        return project_backtest(payload, position)
    if module == "payoffer":
        return project_payoff(result, position)
    return {"status": "not_applicable", "reason": "该模块没有金额换算。"}


def task_amounts(tasks, results, identity, task_id: str, *, references=None, position=None, allow_selected_tasks=False) -> dict:
    task = tasks.get(identity, task_id)
    setting = validate_position(task.get("position_size")) if position is None else validate_position(position)
    output = {"task_id": task_id, "position_size": setting, "runs": []}
    if setting is None:
        return output
    refs = task.get("run_refs", []) if references is None else references
    for ref in refs:
        if not isinstance(ref, Mapping) or ref.get("module") not in {"pricer", "backtester", "payoffer"}:
            continue
        if ref.get("task_id") != task_id and not allow_selected_tasks:
            raise ValidationError("金额换算来源必须属于当前任务")
        record = results.resolve_module_run(identity, dict(ref))
        if record.get("status") not in {"succeeded", "partial"}:
            continue
        data = record["result"]
        contract = data.get("resolved_contract", {})
        entry = {"module": ref["module"], "run_id": ref["run_id"], "source_ref": dict(ref),
                 "product_id": data.get("product_id") or data.get("pricing", {}).get("product_id") or contract.get("identity", {}).get("product_id"),
                 "created_at": record.get("created_at"),
                 "projection": project_run(ref["module"], data, contract, setting)}
        output["runs"].append(entry)
    output["projection_hash"] = hashlib.sha256(json.dumps(output, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return output


def amount_report_html(view: Mapping) -> bytes:
    """A portable, immutable amount schedule accompanying a source report."""
    escape = lambda value: html.escape(str(value if value is not None else "不适用"))
    setting = view["position_size"]
    title = "名义规模金额附表"
    chapters, links = [], []
    for index, run in enumerate(view.get("runs", []), 1):
        projection = run["projection"]
        name = f'{index}.{run.get("product_id") or ""} {run["module"]}'
        links.append(f'<a href="#run-{index}">{escape(name)}</a>')
        body = f'<h2 id="run-{index}">{escape(name)}</h2><p>来源：{escape(run["run_id"])}</p>'
        if projection.get("status") != "available":
            body += f'<p>{escape(projection.get("reason"))}</p>'
        else:
            body += '<table><tr><th>项目</th><th>金额</th><th>单位</th></tr>'
            for row in projection["rows"]:
                body += f'<tr><td>{escape(row["label"])}</td><td>{escape(row["amount"])}</td><td>{escape(row["unit"])}</td></tr>'
            body += '</table><p>' + escape(projection.get("note")) + '</p>'
            if projection.get("trade_ledger"):
                body += '<h3>逐笔结算</h3><table><tr><th>入场日</th><th>结算日</th><th>合同结算金额</th></tr>'
                for trade in projection["trade_ledger"]:
                    body += f'<tr><td>{escape(trade["entry_date"])}</td><td>{escape(trade["exit_date"])}</td><td>{escape(trade["amount"])}</td></tr>'
                body += '</table>'
        chapters.append(body)
    result = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>body{{margin:0;color:#222;background:white;font:14px/1.8 system-ui}}nav{{position:fixed;width:190px;padding:28px;height:100vh;overflow:auto;border-right:1px solid #ddd}}nav a{{display:block;color:#333;margin-bottom:12px}}main{{margin-left:250px;padding:30px;max-width:900px}}h1{{font-size:26px}}h2{{margin-top:38px;font-size:20px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;text-align:left;padding:8px;overflow-wrap:anywhere}}p{{overflow-wrap:anywhere}}@media(max-width:700px){{nav{{position:static;height:auto;width:auto}}main{{margin:0;padding:20px}}}}@media print{{nav{{display:none}}main{{margin:0}}}}</style><nav aria-label="目录">{''.join(links)}</nav><main><h1>{title}</h1><p>规模：{escape(setting['amount'])}{escape(setting['currency'])}；口径：{escape('名义本金' if setting['basis']=='notional' else '每方差百分点平方的名义金额')}。</p><p>本附表冻结生成时的规模与来源。百分比、合同、模型和已完成计算保持原口径；不进行外汇换算，不表示实际保证金或本金保本。</p>{''.join(chapters)}<p>金额附表指纹：{escape(view.get('projection_hash'))}</p></main></html>'''
    return result.encode()


def selected_run_references(request: Mapping) -> list[dict]:
    """Extract only concrete RunRefs already resolved by Reporter selection."""
    references = []
    required = {"module", "tenant_id", "task_id", "run_id", "expected_result_file_hash", "expected_artifact_manifest_hash"}
    def visit(value):
        if isinstance(value, Mapping):
            if required.issubset(value):
                ref = {key: value[key] for key in required}
                if ref not in references:
                    references.append(ref)
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    # evidence_refs can contain alternatives; only selected module_run_refs and
    # explicit quote_items define the report's calculation scope.
    visit(request.get("source_refs", {}).get("module_run_refs", {}))
    visit(request.get("quote_items", []))
    return references


def attach_amount_schedule(delivery: dict, view: Mapping | None) -> None:
    if not view or not view.get("position_size"):
        return
    name = "position-amounts.html"
    delivery["artifacts"].append({"name": name, "content_type": "text/html; charset=utf-8", "content": amount_report_html(view)})
    delivery["audit"]["position_amounts"] = copy.deepcopy(view)
    item = {"role": "supplement", "display_name": "名义规模金额附表", "artifact_name": name}
    delivery["audit"]["public_delivery"]["deliveries"].append(item)
    delivery["amount_schedule"] = name


def amount_fact_rows(view: Mapping, run: Mapping) -> list[dict]:
    """Bind OptChat numerical citations to size plus immutable source evidence."""
    setting = view["position_size"]
    facts = []
    for row in run["projection"].get("rows", []):
        if row.get("amount") is None:
            continue
        number = float(row["amount"])
        if not math.isfinite(number):
            continue
        binding = {"position": setting, "source_ref": run["source_ref"], "row": row}
        ref = "fact_" + hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        facts.append({"fact_ref": ref, "label": f'{row["label"]}，规模{setting["amount"]}{setting["currency"]}',
                      "value": number, "decimal_value": row["amount"], "unit": row["unit"], "module": run["module"]})
    return facts


def position_size_facts(setting: Mapping | None) -> list[dict]:
    if setting is None:
        return []
    reference = "fact_" + hashlib.sha256(json.dumps(setting, sort_keys=True).encode()).hexdigest()
    label = "当前任务名义本金" if setting["basis"] == "notional" else "当前任务每方差百分点平方的名义金额"
    return [{"fact_ref": reference, "label": label, "value": float(setting["amount"]), "unit": setting["currency"]}]
