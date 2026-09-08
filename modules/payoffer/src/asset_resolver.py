"""Payoffer只读默认视觉资产解析。

正常Payoffer运行通过本模块取得与``ResolvedContract``同名的固定JSON/SVG。
JSON声明路径面板、分段绑定与视图类型；现金流、路径条件与分段公式始终来自共享
合同对象，不能被JSON反向覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from runtime.bootstrap import RuntimePaths, bootstrap_runtime
from runtime.contracts.contract_api import (
    ContractResolutionError,
    ResolvedContract,
    get_product,
    load_registry,
    verify_current_product_rule,
)
from runtime.contracts.contract_types import deep_thaw


RUNTIME_PATHS = bootstrap_runtime(__file__)


def _figures_dir(runtime_paths: RuntimePaths) -> Path:
    """由运行时根解析Payoffer自有资产，不依赖源码目录层级。"""
    relative = Path("assets/payoffer/figures") if runtime_paths.mode == "release" else Path("modules/payoffer/figures")
    return runtime_paths.project_root / relative


FIGURES_DIR = _figures_dir(RUNTIME_PATHS)
DEFAULT_JSON_DIR = FIGURES_DIR / "json"
DEFAULT_SVG_DIR = FIGURES_DIR / "svg"
_AXIS_VARIABLES = frozenset({"S_T", "r_T", "W_T", "sigma_realized", "n_in"})
_NON_VISUAL_TERM_FIELDS = frozenset({
    # 默认JSON是只读视觉模板；公式解释唯一以OptionReg/ResolvedContract为准。
    # monitor和derived_terms会参与经济路径计算，但不决定图面路径面板的绑定。
    "constraints", "pricing_methods", "monitor", "derived_terms", "N", "Nvar", "Nvega", "G",
})


class DefaultAssetError(ValueError):
    """默认视觉资产缺失、损坏或与冻结产品版本不兼容。"""


@dataclass(frozen=True)
class DefaultVisualAsset:
    """一份只读默认视觉资产及其可审计引用。"""

    product_id: str
    name_zh: str
    json_path: Path
    svg_path: Path
    template: Mapping[str, Any]
    rule_revision: int

    def to_reference(self) -> dict[str, Any]:
        """给PayoffResult保存不暴露本机绝对路径的资产引用。"""
        return {
            "name_zh": self.name_zh,
            "json_asset": f"figures/json/{self.name_zh}.json",
            "svg_golden_master": f"figures/svg/{self.name_zh}.svg",
            "rule_revision": self.rule_revision,
        }


def _asset_name(name_zh: str) -> str:
    name = str(name_zh).strip()
    if not name or any(character in name for character in ("/", "\\", "\x00")):
        raise DefaultAssetError("中文结构名不能为空或包含路径字符")
    return name


def figure_asset_paths(name_zh: str) -> dict[str, Path]:
    """返回固定默认JSON/SVG路径，不写入任何资产。"""
    name = _asset_name(name_zh)
    return {"json": DEFAULT_JSON_DIR / f"{name}.json", "svg": DEFAULT_SVG_DIR / f"{name}.svg"}


def build_visual_template(paths: list[Mapping[str, Any]]) -> dict[str, Any]:
    """由登记路径生成不含经济公式的最小视觉模板。"""
    views: list[dict[str, Any]] = []
    for path_index, path in enumerate(paths, start=1):
        cases = path.get("cases") if isinstance(path, Mapping) else None
        if not isinstance(cases, list) or not cases:
            raise DefaultAssetError(f"路径{path_index}缺少cases，不能生成视觉模板")
        # 横轴变量可能紧邻比较符，例如S_T>=K，因此逐一检查而不是按空白分词。
        axes = {
            token for case in cases if isinstance(case, Mapping)
            for token in _AXIS_VARIABLES if token in str(case.get("domain", ""))
        }
        if len(axes) != 1:
            raise DefaultAssetError(f"路径{path_index}必须且只能声明一个图形横轴")
        axis = next(iter(axes))
        views.append({
            "path_index": path_index,
            "case_indexes": list(range(1, len(cases) + 1)),
            "axis": axis,
            "chart_kind": "conditional_terminal_metric" if axis in {"S_T", "r_T", "W_T"} else "path_statistic_slice",
            "title": f"路径{path_index}",
        })
    return {
        "schema": "optionhelper.payoff-visual-template",
        "layout": "standard_path_cards",
        "path_views": views,
    }


def _validate_visual_template(value: Any, paths: list[Mapping[str, Any]], name_zh: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DefaultAssetError(f"{name_zh}的默认JSON必须含visual_template对象")
    if value.get("schema") != "optionhelper.payoff-visual-template" or value.get("layout") != "standard_path_cards":
        raise DefaultAssetError(f"{name_zh}的visual_template版本或布局类型无效")
    expected = build_visual_template(paths)
    views = value.get("path_views")
    if not isinstance(views, list) or len(views) != len(expected["path_views"]):
        raise DefaultAssetError(f"{name_zh}的visual_template路径面板数量无效")
    for actual, required in zip(views, expected["path_views"]):
        if not isinstance(actual, Mapping):
            raise DefaultAssetError(f"{name_zh}的visual_template路径面板类型无效")
        for key in ("path_index", "case_indexes", "axis", "chart_kind"):
            if actual.get(key) != required[key]:
                raise DefaultAssetError(f"{name_zh}的visual_template.{key}与登记路径不一致")
        if not isinstance(actual.get("title"), str) or not actual["title"].strip():
            raise DefaultAssetError(f"{name_zh}的visual_template.title不能为空")
    return deep_thaw(value)


def _normalize_figure_payload(value: Any, name: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"name_zh", "terms", "paths"}.issubset(value):
        raise DefaultAssetError(f"{name}的固定默认JSON必须含name_zh、terms、paths")
    if value["name_zh"] != name:
        raise DefaultAssetError(f"固定默认JSON的中文结构名与文件名不一致：{path}")
    if not isinstance(value["terms"], dict) or not isinstance(value["paths"], list):
        raise DefaultAssetError(f"{name}的固定默认JSON中terms或paths类型错误")
    result = deep_thaw(value)
    if "visual_template" in value:
        result["visual_template"] = _validate_visual_template(value["visual_template"], value["paths"], name)
    else:
        result["visual_template"] = build_visual_template(value["paths"])
    return result


def load_default_figure_payload(name_zh: str) -> dict[str, Any]:
    """读取固定JSON模板，绝不将其作为合同经济规则写回或替换。"""
    name = _asset_name(name_zh)
    path = figure_asset_paths(name)["json"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DefaultAssetError(f"缺少{name}的固定默认JSON：{path}") from error
    except json.JSONDecodeError as error:
        raise DefaultAssetError(f"{name}的固定默认JSON格式无效：{path}") from error
    # 65份既有冻结JSON早于visual_template字段。缺失时只根据已核验paths生成
    # 内存模板，不写回资产，也不改变terms或paths的严格同源校验。
    return _normalize_figure_payload(value, name, path)


def load_current_visual_template(
    name_zh: str,
    current_paths: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """用当前OptionReg路径生成图形布局，并仅继承资产中的人工标题。

    默认JSON中的terms和paths是历史导出内容，不能约束当前产品规则。产品更新后，
    路径数量、分段和横轴都以当前OptionReg为准；旧资产只保留不影响经济含义的标题。
    """
    name = _asset_name(name_zh)
    path = figure_asset_paths(name)["json"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DefaultAssetError(f"缺少{name}的固定默认JSON：{path}") from error
    except json.JSONDecodeError as error:
        raise DefaultAssetError(f"{name}的固定默认JSON格式无效：{path}") from error
    if not isinstance(value, Mapping) or value.get("name_zh") != name:
        raise DefaultAssetError(f"固定默认JSON的中文结构名与文件名不一致：{path}")

    template = build_visual_template(current_paths)
    saved_views = value.get("visual_template", {}).get("path_views", [])
    if isinstance(saved_views, list) and len(saved_views) == len(template["path_views"]):
        for current, saved in zip(template["path_views"], saved_views):
            if isinstance(saved, Mapping) and isinstance(saved.get("title"), str) and saved["title"].strip():
                current["title"] = saved["title"].strip()
    return template


def payoff_template_terms_match(default_terms: Mapping[str, Any], registry_terms: Mapping[str, Any]) -> bool:
    """只忽略不参与Payoffer计算或绘图的合同校验和定价方法元数据。"""
    default = {key: value for key, value in default_terms.items() if key not in _NON_VISUAL_TERM_FIELDS}
    registered = {key: value for key, value in registry_terms.items() if key not in _NON_VISUAL_TERM_FIELDS}
    return default == registered


def payoff_template_paths_match(default_paths: list[Mapping[str, Any]], registry_paths: list[Mapping[str, Any]]) -> bool:
    """校验默认资产的路径面板绑定，不把JSON中的历史收益公式当作运行时权威。"""
    def signature(paths: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "condition": path["condition"],
                    "cases": [{"domain": case["domain"]} for case in path["cases"]],
                }
                for path in paths
            ]
        except (KeyError, TypeError):
            return []

    return signature(default_paths) == signature(registry_paths)


def _snapshot_for_contract(contract: ResolvedContract) -> tuple[dict[str, Any], int, dict[str, Path]]:
    """Bind the candidate to the current OptionReg rule revision and visual pair."""

    try:
        registry = load_registry()
        verify_current_product_rule(contract, registry)
        product = get_product(contract.product_id, registry)
        rule_revision = int(product["identity"]["rule_revision"])
    except (ContractResolutionError, KeyError, TypeError, ValueError) as error:
        raise DefaultAssetError(str(error)) from error
    return product, rule_revision, figure_asset_paths(contract.name_zh)


def verify_contract_snapshot_binding(contract: ResolvedContract) -> None:
    """验证合同绑定当前OptionReg规则修订。"""
    product, _, _ = _snapshot_for_contract(contract)
    identity = product.get("identity", {})
    if identity.get("product_id") != contract.product_id or identity.get("name_zh") != contract.name_zh:
        raise DefaultAssetError(f"{contract.product_id}的ResolvedContract身份与所选产品快照不一致")


def resolve_default_visual_asset(contract: ResolvedContract) -> DefaultVisualAsset:
    """为已解析合同绑定一份只读默认视觉资产。

    固定JSON只提供路径面板布局。合同条款、条件和现金流始终来自已由当前
    OptionReg编译并校验的ResolvedContract，旧JSON中的经济字段不参与运行。
    """
    name_zh = _asset_name(contract.name_zh)
    product, rule_revision, paths = _snapshot_for_contract(contract)
    if product.get("identity", {}).get("product_id") != contract.product_id or product.get("identity", {}).get("name_zh") != name_zh:
        raise DefaultAssetError(f"{contract.product_id}的ResolvedContract名称与所选产品快照不一致")
    try:
        json_bytes = paths["json"].read_bytes()
        paths["svg"].read_bytes()
    except OSError as error:
        raise DefaultAssetError(f"缺少{name_zh}的固定默认视觉资产") from error
    try:
        json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DefaultAssetError(f"{name_zh}的产品规则默认视觉JSON格式无效") from error
    template = {"visual_template": load_current_visual_template(name_zh, product["paths"])}

    return DefaultVisualAsset(
        product_id=str(contract.product_id),
        name_zh=name_zh,
        json_path=paths["json"],
        svg_path=paths["svg"],
        template=template,
        rule_revision=rule_revision,
    )


__all__ = (
    "DEFAULT_JSON_DIR",
    "DEFAULT_SVG_DIR",
    "DefaultAssetError",
    "DefaultVisualAsset",
    "build_visual_template",
    "figure_asset_paths",
    "load_default_figure_payload",
    "load_current_visual_template",
    "payoff_template_terms_match",
    "payoff_template_paths_match",
    "verify_contract_snapshot_binding",
    "resolve_default_visual_asset",
)
