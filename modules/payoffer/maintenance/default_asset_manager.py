"""默认资产的受控维护入口。

正常运行路径绝不导入或调用本文件。资料库维护仅能在确认的OptionReg变更后，通过
``publish_default_assets``同时更新默认JSON、默认SVG，并留下可恢复的维护记录。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from runtime.contracts.contract_api import load_registry

from modules.payoffer.asset_resolver import build_visual_template, payoff_template_terms_match
from modules.payoffer.impl.engine import build_payoff_input, render_paths
from modules.payoffer.impl.svg_renderer import render_svg
from runtime.contracts.contract_types import semantic_hash


MODULE_ROOT = Path(__file__).resolve().parents[1]
FIGURES_ROOT = MODULE_ROOT / "figures"
HISTORY_ROOT = MODULE_ROOT / "maintenance" / "history"
_CONFIRMED_REG_REFRESHES = {
    "8.1": {
        "terms": "confirmed_accumulator_contract_time_and_percent_basis",
        "paths": "confirmed_accumulator_contract_time_and_percent_basis",
    },
    "9.14": {"terms": "confirmed_c_reset_correction"},
    "9.25": {"paths": "confirmed_path_correction"},
}
_OPTIONLIB_EVIDENCE = {
    "8.1": ("Q_{\\mathrm{acc}}", "$uQ_{\\mathrm{acc}}(S_T-K)$"),
    "9.14": ("c_{reset}=10\\%", "重置日当天1天按$c_{reset}$计息"),
    "9.25": ("max(S_T/S_0-1,F-1)", "承担最多$1-F$的标的跌幅"),
}


class DefaultAssetMaintenanceError(ValueError):
    """维护请求不满足受控发布规则。"""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def inventory(figures_root: Path = FIGURES_ROOT) -> dict[str, str]:
    """返回默认JSON与SVG共有中文名的内容哈希，不修改任何文件。"""
    result: dict[str, str] = {}
    for payload in sorted((figures_root / "json").glob("*.json")):
        svg = figures_root / "svg" / f"{payload.stem}.svg"
        if svg.is_file():
            result[payload.stem] = _digest(payload.read_bytes() + b"\0" + svg.read_bytes())
    return result


def _verify_optionlib_evidence(product_id: str) -> None:
    """确认OptionReg的已批准修正仍有OptionLib资料依据。"""
    evidence = _OPTIONLIB_EVIDENCE.get(product_id)
    if evidence is None:
        raise DefaultAssetMaintenanceError(f"{product_id}不是允许发布默认资产的确认修正")
    optionlib = MODULE_ROOT.parent.parent / "references" / "optionlib.md"
    try:
        source = optionlib.read_text(encoding="utf-8")
    except OSError as error:
        raise DefaultAssetMaintenanceError("无法读取OptionLib，拒绝维护默认资产") from error
    missing = [fragment for fragment in evidence if fragment not in source]
    if missing:
        raise DefaultAssetMaintenanceError(
            f"{product_id}缺少OptionLib依据：{'、'.join(missing)}"
        )


def _default_svg(
    product_id: str,
    product: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> bytes:
    """以当前OptionReg维护合同渲染正式SVG，绝不引用旧默认JSON的经济规则。"""
    name_zh = str(product["identity"]["name_zh"])
    contract = build_payoff_input(name_zh, maintenance_registry=registry)
    if contract.product_id != product_id:
        raise DefaultAssetMaintenanceError("默认资产维护的产品编号与合同不一致")
    template = build_visual_template(product["paths"])
    paths = [asdict(path) for path in render_paths(contract, template)]
    return render_svg({"name_zh": contract.name_zh, "paths": paths}).encode("utf-8")


def plan_default_asset_refresh(
    figures_root: Path = FIGURES_ROOT,
    *,
    approved_product_id: str | None = None,
    approval_reason: str | None = None,
) -> list[dict[str, Any]]:
    """只读生成维护计划，拒绝任何未经确认的经济资产漂移。"""
    approved_product = str(approved_product_id or "").strip()
    reason = str(approval_reason or "").strip()
    if approved_product and len(reason) < 8:
        raise DefaultAssetMaintenanceError("Desk高级维护必须填写不少于8个字的确认原因")
    registry = load_registry()
    products = {
        str(product["identity"]["name_zh"]): (str(product_id), product)
        for product_id, product in registry["products"].items()
    }
    updates: list[dict[str, Any]] = []
    for json_path in sorted((figures_root / "json").glob("*.json")):
        name_zh = json_path.stem
        try:
            product_id, product = products[name_zh]
        except KeyError as error:
            raise DefaultAssetMaintenanceError(f"默认JSON{name_zh}未绑定OptionReg产品") from error
        try:
            current = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DefaultAssetMaintenanceError(f"默认JSON{name_zh}无法读取") from error
        if current.get("name_zh") != name_zh:
            raise DefaultAssetMaintenanceError(f"默认JSON{name_zh}名称不一致")
        terms_match = payoff_template_terms_match(current["terms"], product["terms"])
        if product_id == "8.1":
            # 8.1的本次批准修正涉及Q_acc监测表达式；该字段通常不影响默认
            # 图形模板比对，却必须与OptionReg完整同源，避免资料资产保留旧公式。
            terms_match = semantic_hash(current["terms"]) == semantic_hash(product["terms"])
        paths_match = semantic_hash(current["paths"]) == semantic_hash(product["paths"])
        explicitly_approved = product_id == approved_product
        if product_id not in _CONFIRMED_REG_REFRESHES and not explicitly_approved:
            if not terms_match or not paths_match:
                raise DefaultAssetMaintenanceError(
                    f"{product_id}的默认资产与OptionReg存在未经确认的经济差异"
                )
            continue

        if not explicitly_approved:
            _verify_optionlib_evidence(product_id)
        updated = dict(current)
        changed: list[str] = []
        if not terms_match:
            updated["terms"] = product["terms"]
            changed.append("terms")
        if not paths_match:
            updated["paths"] = product["paths"]
            changed.append("paths")
        template = build_visual_template(product["paths"])
        if current.get("visual_template") != template:
            updated["visual_template"] = template
            changed.append("visual_template")
        if not changed:
            continue
        svg_path = figures_root / "svg" / f"{name_zh}.svg"
        if not svg_path.is_file():
            raise DefaultAssetMaintenanceError(f"默认SVG{name_zh}缺失")
        payload = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        svg_content = _default_svg(product_id, product, registry)
        updates.append({
            "product_id": product_id,
            "name_zh": name_zh,
            "json_path": json_path,
            "content": payload.encode("utf-8"),
            "svg_content": svg_content,
            "changed_fields": changed,
            "reasons": {
                field: reason if explicitly_approved else _CONFIRMED_REG_REFRESHES.get(product_id, {}).get(field, "visual_template_schema")
                for field in changed
            },
            "before_json_hash": _digest(json_path.read_bytes()),
            "before_svg_hash": _digest(svg_path.read_bytes()),
        })
    if len(products) != len(list((figures_root / "json").glob("*.json"))):
        raise DefaultAssetMaintenanceError("OptionReg与默认JSON数量不一致")
    return updates


def publish_default_assets(
    approval_id: str,
    figures_root: Path = FIGURES_ROOT,
    *,
    approved_product_id: str | None = None,
    approval_reason: str | None = None,
) -> dict[str, Any]:
    """在显式审批号下发布计划，并将JSON/SVG证据写入维护记录。"""
    approval = str(approval_id).strip()
    if not approval or any(character in approval for character in ("/", "\\", "\0")):
        raise DefaultAssetMaintenanceError("维护发布必须提供安全的approval_id")
    history_root = figures_root.parent / "maintenance" / "history"
    history_path = history_root / approval
    staging_root = history_root / f".{approval}.tmp"
    if history_path.exists():
        raise DefaultAssetMaintenanceError(f"维护记录已存在：{approval}")
    if staging_root.exists():
        raise DefaultAssetMaintenanceError(f"维护暂存证据已存在，拒绝覆盖：{staging_root.name}")
    updates = plan_default_asset_refresh(
        figures_root,
        approved_product_id=approved_product_id,
        approval_reason=approval_reason,
    )
    if not updates:
        raise DefaultAssetMaintenanceError("没有需要发布的默认资产变更")
    if approved_product_id and {update["product_id"] for update in updates} != {str(approved_product_id)}:
        raise DefaultAssetMaintenanceError("Desk高级维护一次只能发布当前选中的一个产品")
    history_root.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    replaced: list[tuple[Path, Path]] = []
    staging_created = False
    try:
        staging_root.mkdir()
        staging_created = True
        before_root = staging_root / "before"
        before_root.mkdir()
        for update in updates:
            json_path = Path(update["json_path"])
            svg_path = figures_root / "svg" / f"{update['name_zh']}.svg"
            (before_root / json_path.name).write_bytes(json_path.read_bytes())
            (before_root / svg_path.name).write_bytes(svg_path.read_bytes())
            json_temporary = json_path.with_name(f".{json_path.name}.{approval}.tmp")
            svg_temporary = svg_path.with_name(f".{svg_path.name}.{approval}.tmp")
            json_temporary.write_bytes(update["content"])
            svg_temporary.write_bytes(update["svg_content"])
            staged.extend((json_temporary, svg_temporary))
        record = {
            "approval_id": approval,
            "module": "payoffer",
            "operation": "default_asset_template_publish",
            "approval_reason": str(approval_reason or "").strip() or None,
            "updates": [
                {
                    key: value
                    for key, value in update.items()
                    if key not in {"json_path", "content", "svg_content"}
                }
                | {
                    "after_json_hash": _digest(update["content"]),
                    "after_svg_hash": _digest(update["svg_content"]),
                }
                for update in updates
            ],
        }
        (staging_root / "manifest.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for update in updates:
            json_temporary = Path(update["json_path"]).with_name(
                f".{Path(update['json_path']).name}.{approval}.tmp"
            )
            svg_path = figures_root / "svg" / f"{update['name_zh']}.svg"
            svg_temporary = svg_path.with_name(f".{svg_path.name}.{approval}.tmp")
            json_temporary.replace(Path(update["json_path"]))
            replaced.append((Path(update["json_path"]), before_root / Path(update["json_path"]).name))
            svg_temporary.replace(svg_path)
            replaced.append((svg_path, before_root / svg_path.name))
        staging_root.replace(history_path)
    except BaseException:
        for target, before in reversed(replaced):
            if not before.is_file():
                raise DefaultAssetMaintenanceError(f"维护回滚缺少发布前资产：{target.name}")
            rollback = target.with_name(f".{target.name}.{approval}.rollback.tmp")
            rollback.write_bytes(before.read_bytes())
            rollback.replace(target)
        raise
    finally:
        for temporary in staged:
            if temporary.exists():
                temporary.unlink()
        if staging_created and staging_root.exists():
            shutil.rmtree(staging_root)
    return record


__all__ = (
    "DefaultAssetMaintenanceError",
    "inventory",
    "plan_default_asset_refresh",
    "publish_default_assets",
)
