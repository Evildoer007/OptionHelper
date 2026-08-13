"""默认资产的受控维护入口。

正常运行路径绝不导入或调用本文件。资料库维护仅能在确认的OptionReg变更后，通过
``publish_default_assets``同时更新默认JSON、默认SVG，并留下可恢复的维护记录。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree

from runtime.contracts.contract_api import load_registry

from modules.payoffer.asset_resolver import build_visual_template, load_default_figure_payload, payoff_template_terms_match
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
_SVG_TEXT_NODE = re.compile(r"<text(?P<attributes>[^>]*)>(?P<content>[^<]*)</text>")
_PUBLIC_UNIT_TEXT_CLASSES = frozenset({"axis-title", "payoff-level-label"})


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


def _refresh_public_unit_text(before: bytes, rendered: bytes) -> bytes:
    """仅将同一渲染链的公开纵轴文本写入冻结SVG，保留全部图形属性。"""
    source = before.decode("utf-8")
    candidate = rendered.decode("utf-8")
    source_nodes = list(_SVG_TEXT_NODE.finditer(source))
    candidate_nodes = list(_SVG_TEXT_NODE.finditer(candidate))
    if len(source_nodes) != len(candidate_nodes):
        raise DefaultAssetMaintenanceError("默认SVG文本节点结构不一致，拒绝发布")

    pieces: list[str] = []
    cursor = 0
    for source_node, candidate_node in zip(source_nodes, candidate_nodes):
        source_attributes = source_node.group("attributes")
        candidate_attributes = candidate_node.group("attributes")
        source_text = source_node.group("content")
        candidate_text = candidate_node.group("content")
        if source_text == candidate_text:
            continue
        classes = {
            token
            for class_value in re.findall(r'class="([^"]+)"', source_attributes)
            for token in class_value.split()
        }
        if not classes.intersection(_PUBLIC_UNIT_TEXT_CLASSES):
            raise DefaultAssetMaintenanceError("每100单位迁移试图修改非公开纵轴文本，拒绝发布")
        candidate_classes = {
            token
            for class_value in re.findall(r'class="([^"]+)"', candidate_attributes)
            for token in class_value.split()
        }
        if classes != candidate_classes:
            raise DefaultAssetMaintenanceError("默认SVG文本角色不一致，拒绝发布")
        content_start, content_end = source_node.span("content")
        pieces.extend((source[cursor:content_start], candidate_text))
        cursor = content_end
    pieces.append(source[cursor:])
    return "".join(pieces).encode("utf-8")


def _normalized_unit_svg(product_id: str, name_zh: str, before: bytes) -> bytes:
    """以固定默认JSON计算新口径文本，同时保持冻结默认SVG布局不变。"""
    template = load_default_figure_payload(name_zh)["visual_template"]
    contract = build_payoff_input(name_zh)
    if contract.product_id != product_id:
        raise DefaultAssetMaintenanceError(f"{name_zh}的默认JSON与OptionReg产品编号不一致")
    paths = [asdict(path) for path in render_paths(contract, template)]
    rendered = render_svg({"name_zh": name_zh, "paths": paths}).encode("utf-8")
    return _refresh_public_unit_text(before, rendered)


def svg_geometry_signature(content: bytes) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """忽略文本内容，锁定SVG元素、属性、坐标、路径、端点与视觉样式。"""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise DefaultAssetMaintenanceError("默认SVG格式无效") from error
    return tuple((element.tag, tuple(sorted(element.attrib.items()))) for element in root.iter())


def plan_payoff_unit_svg_refresh(figures_root: Path = FIGURES_ROOT) -> list[dict[str, Any]]:
    """仅为公开百分比口径更新默认SVG文本，拒绝任何几何或JSON漂移。"""
    registry = load_registry()
    products = registry.get("products", {})
    updates: list[dict[str, Any]] = []
    for product_id, product in products.items():
        name_zh = str(product["identity"]["name_zh"])
        json_path = figures_root / "json" / f"{name_zh}.json"
        svg_path = figures_root / "svg" / f"{name_zh}.svg"
        if not json_path.is_file() or not svg_path.is_file():
            raise DefaultAssetMaintenanceError(f"{product_id}缺少默认JSON或SVG")
        before = svg_path.read_bytes()
        after = _normalized_unit_svg(str(product_id), name_zh, before)
        if before == after:
            continue
        if svg_geometry_signature(before) != svg_geometry_signature(after):
            raise DefaultAssetMaintenanceError(
                f"{product_id}百分比展示迁移改变了默认SVG几何，拒绝发布"
            )
        updates.append({
            "product_id": str(product_id),
            "name_zh": name_zh,
            "svg_path": svg_path,
            "svg_content": after,
            "default_json_hash": _digest(json_path.read_bytes()),
            "reason": "payoffer_percent_public_display",
            "before_svg_hash": _digest(before),
            "after_svg_hash": _digest(after),
        })
    if len(products) != len(list((figures_root / "json").glob("*.json"))):
        raise DefaultAssetMaintenanceError("OptionReg与默认JSON数量不一致")
    return updates


def plan_default_asset_refresh(figures_root: Path = FIGURES_ROOT) -> list[dict[str, Any]]:
    """只读生成维护计划，拒绝任何未经确认的经济资产漂移。"""
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
        if product_id not in _CONFIRMED_REG_REFRESHES:
            if not terms_match or not paths_match:
                raise DefaultAssetMaintenanceError(
                    f"{product_id}的默认资产与OptionReg存在未经确认的经济差异"
                )
            continue

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
                field: _CONFIRMED_REG_REFRESHES.get(product_id, {}).get(field, "visual_template_schema_v1")
                for field in changed
            },
            "before_json_hash": _digest(json_path.read_bytes()),
            "before_svg_hash": _digest(svg_path.read_bytes()),
        })
    if len(products) != len(list((figures_root / "json").glob("*.json"))):
        raise DefaultAssetMaintenanceError("OptionReg与默认JSON数量不一致")
    return updates


def publish_default_assets(approval_id: str, figures_root: Path = FIGURES_ROOT) -> dict[str, Any]:
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
    updates = plan_default_asset_refresh(figures_root)
    if not updates:
        raise DefaultAssetMaintenanceError("没有需要发布的默认资产变更")
    history_root.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
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
            svg_temporary.replace(svg_path)
        staging_root.replace(history_path)
    finally:
        for temporary in staged:
            if temporary.exists():
                temporary.unlink()
        if staging_created and staging_root.exists():
            shutil.rmtree(staging_root)
    return record


def publish_payoff_unit_svg_refresh(approval_id: str, figures_root: Path = FIGURES_ROOT) -> dict[str, Any]:
    """在显式审批号下仅发布已验证的百分比SVG文本变更。"""
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
    updates = plan_payoff_unit_svg_refresh(figures_root)
    if not updates:
        raise DefaultAssetMaintenanceError("没有需要发布的百分比SVG变更")
    history_root.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    replaced_svg_paths: list[Path] = []
    staging_created = False
    try:
        staging_root.mkdir()
        staging_created = True
        before_root = staging_root / "before"
        before_root.mkdir()
        for update in updates:
            svg_path = Path(update["svg_path"])
            (before_root / svg_path.name).write_bytes(svg_path.read_bytes())
            temporary = svg_path.with_name(f".{svg_path.name}.{approval}.tmp")
            temporary.write_bytes(update["svg_content"])
            staged.append(temporary)
        record = {
            "approval_id": approval,
            "module": "payoffer",
            "operation": "default_svg_percent_display_publish",
            "updates": [
                {key: value for key, value in update.items() if key not in {"svg_path", "svg_content"}}
                for update in updates
            ],
        }
        (staging_root / "manifest.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for update in updates:
            svg_path = Path(update["svg_path"])
            temporary = svg_path.with_name(f".{svg_path.name}.{approval}.tmp")
            temporary.replace(svg_path)
            replaced_svg_paths.append(svg_path)
        staging_root.replace(history_path)
    except BaseException:
        before_root = staging_root / "before"
        for svg_path in reversed(replaced_svg_paths):
            before = before_root / svg_path.name
            if not before.is_file():
                raise DefaultAssetMaintenanceError(f"维护回滚缺少发布前SVG：{svg_path.name}")
            rollback = svg_path.with_name(f".{svg_path.name}.{approval}.rollback.tmp")
            rollback.write_bytes(before.read_bytes())
            rollback.replace(svg_path)
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
    "plan_payoff_unit_svg_refresh",
    "publish_payoff_unit_svg_refresh",
    "svg_geometry_signature",
)
