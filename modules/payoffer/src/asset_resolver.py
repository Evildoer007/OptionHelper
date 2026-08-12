"""Payoffer只读默认视觉资产解析。

正常Payoffer运行通过本模块取得与``ResolvedContract``同名的固定JSON/SVG。
JSON声明路径面板、分段绑定与视图类型；现金流、路径条件与分段公式始终来自共享
合同对象，不能被JSON反向覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from runtime.bootstrap import RuntimePaths, bootstrap_runtime
from runtime.contracts.contract_api import (
    ContractResolutionError,
    ResolvedContract,
    get_product,
    load_registry,
    verify_product_snapshot_binding,
)
from runtime.contracts.contract_types import deep_thaw, semantic_hash
from runtime.knowledger.versioning import load_current_product_snapshot, load_packaged_product_snapshot


RUNTIME_PATHS = bootstrap_runtime(__file__)


def _figures_dir(runtime_paths: RuntimePaths) -> Path:
    """由运行时根解析Payoffer自有资产，不依赖源码目录层级。"""
    relative = Path("assets/payoffer/figures") if runtime_paths.mode == "release" else Path("modules/payoffer/figures")
    return runtime_paths.project_root / relative


FIGURES_DIR = _figures_dir(RUNTIME_PATHS)
DEFAULT_JSON_DIR = FIGURES_DIR / "json"
DEFAULT_SVG_DIR = FIGURES_DIR / "svg"
_AXIS_VARIABLES = frozenset({"S_T", "r_T", "W_T", "sigma_realized", "n_in"})
_NON_VISUAL_TERM_FIELDS = frozenset({"constraints", "pricing_methods"})


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
    json_hash: str
    svg_hash: str
    pair_hash: str
    contract_product_version: str
    registry_product_version: str
    asset_version_class: str

    def to_reference(self) -> dict[str, Any]:
        """给PayoffResult保存不暴露本机绝对路径的资产引用。"""
        return {
            "name_zh": self.name_zh,
            "json_asset": f"figures/json/{self.name_zh}.json",
            "svg_golden_master": f"figures/svg/{self.name_zh}.svg",
            "json_hash": self.json_hash,
            "svg_hash": self.svg_hash,
            "asset_pair_hash": self.pair_hash,
            "contract_product_version": self.contract_product_version,
            "registry_product_version": self.registry_product_version,
            "asset_version_class": self.asset_version_class,
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
        "schema_version": 1,
        "layout": "standard_path_cards",
        "path_views": views,
    }


def _validate_visual_template(value: Any, paths: list[Mapping[str, Any]], name_zh: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DefaultAssetError(f"{name_zh}的默认JSON必须含visual_template对象")
    if value.get("schema_version") != 1 or value.get("layout") != "standard_path_cards":
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


def default_assets(name_zh: str) -> dict[str, Path]:
    """历史调用兼容：返回固定默认资产路径。"""
    return figure_asset_paths(name_zh)


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


def default_payload(name_zh: str) -> dict[str, Any]:
    """历史调用兼容：读取默认JSON模板。"""
    return load_default_figure_payload(name_zh)


def _asset_hash(json_bytes: bytes, svg_bytes: bytes) -> tuple[str, str, str]:
    return (
        sha256(json_bytes).hexdigest(),
        sha256(svg_bytes).hexdigest(),
        sha256(json_bytes + b"\0" + svg_bytes).hexdigest(),
    )


def payoff_template_terms_match(default_terms: Mapping[str, Any], registry_terms: Mapping[str, Any]) -> bool:
    """只忽略不参与Payoffer计算或绘图的合同校验和定价方法元数据。"""
    default = {key: value for key, value in default_terms.items() if key not in _NON_VISUAL_TERM_FIELDS}
    registered = {key: value for key, value in registry_terms.items() if key not in _NON_VISUAL_TERM_FIELDS}
    return semantic_hash(default) == semantic_hash(registered)


def _snapshot_for_contract(contract: ResolvedContract) -> tuple[dict[str, Any], str, dict[str, Path], str]:
    """返回合同声明版本唯一允许使用的产品快照。

    开发态合同由当前唯一Registry完整复核；发布态合同只能由受控Catalog中的
    ProductVersion六件套证明，不能因当前OptionReg后来变化而被重新绑定。
    """
    contract_product_version = str(contract.product_version)
    if contract_product_version.startswith("unversioned:"):
        try:
            registry = load_registry()
            verify_product_snapshot_binding(contract, registry)
            product = get_product(contract.product_id, registry)
        except ContractResolutionError as error:
            raise DefaultAssetError(str(error)) from error
        return product, contract_product_version, figure_asset_paths(contract.name_zh), "unversioned_registry_current"

    try:
        loader = load_packaged_product_snapshot if RUNTIME_PATHS.mode == "release" else load_current_product_snapshot
        archived = loader(RUNTIME_PATHS.project_root, str(contract.product_id), contract_product_version)
    except (OSError, ValueError) as error:
        raise DefaultAssetError(f"显式ProductVersion不可用：{error}") from error
    product = archived["product"]
    if not isinstance(product, Mapping):
        raise DefaultAssetError("显式ProductVersion缺少有效产品快照")
    product = deep_thaw(product)
    product_hash = semantic_hash(product)
    paths_hash = semantic_hash(product.get("paths"))
    if contract.product_snapshot_hash != product_hash:
        raise DefaultAssetError("ResolvedContract产品快照哈希与发布ProductVersion不一致")
    if contract.product_paths_hash != paths_hash or semantic_hash(contract.paths) != paths_hash:
        raise DefaultAssetError("ResolvedContract路径快照哈希与发布ProductVersion不一致")
    return product, str(archived["product_version"]), {
        "json": Path(archived["default_json_path"]),
        "svg": Path(archived["default_svg_path"]),
    }, "published_catalog_snapshot"


def verify_contract_snapshot_binding(contract: ResolvedContract) -> None:
    """在不重算合同经济内容的前提下验证其Registry或发布产品快照绑定。"""
    product, _, _, _ = _snapshot_for_contract(contract)
    identity = product.get("identity", {})
    if identity.get("product_id") != contract.product_id or identity.get("name_zh") != contract.name_zh:
        raise DefaultAssetError(f"{contract.product_id}的ResolvedContract身份与所选产品快照不一致")


def resolve_default_visual_asset(contract: ResolvedContract) -> DefaultVisualAsset:
    """为已解析合同绑定一份只读默认视觉资产。

    这里的比较是发布门禁，不是把JSON中的terms/paths塞回合同。若默认资产与
    当前OptionReg不一致，普通运行必须停在资料库维护流程之外。
    """
    name_zh = _asset_name(contract.name_zh)
    contract_product_version = str(contract.product_version)
    product, registry_product_version, paths, asset_version_class = _snapshot_for_contract(contract)
    if product.get("identity", {}).get("product_id") != contract.product_id or product.get("identity", {}).get("name_zh") != name_zh:
        raise DefaultAssetError(f"{contract.product_id}的ResolvedContract名称与所选产品快照不一致")
    try:
        json_bytes = paths["json"].read_bytes()
        svg_bytes = paths["svg"].read_bytes()
    except OSError as error:
        raise DefaultAssetError(f"缺少{name_zh}的固定默认视觉资产") from error
    try:
        template_value = json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DefaultAssetError(f"{name_zh}的ProductVersion默认JSON格式无效") from error
    template = _normalize_figure_payload(template_value, name_zh, paths["json"])

    # 默认JSON记录固定示例及视觉绑定。它必须仍指向当前发布的经济资料，但此处
    # 只做一致性校验，真正计算只会读取contract.paths和contract.terms。
    if not payoff_template_terms_match(template["terms"], product["terms"]):
        raise DefaultAssetError(f"{name_zh}的固定默认JSON条款与OptionReg不一致；请先完成资料库维护后再运行")
    if semantic_hash(template["paths"]) != semantic_hash(product["paths"]):
        raise DefaultAssetError(f"{name_zh}的固定默认JSON路径与OptionReg不一致；请先完成资料库维护后再运行")

    json_hash, svg_hash, pair_hash = _asset_hash(json_bytes, svg_bytes)
    return DefaultVisualAsset(
        product_id=str(contract.product_id),
        name_zh=name_zh,
        json_path=paths["json"],
        svg_path=paths["svg"],
        template=template,
        json_hash=json_hash,
        svg_hash=svg_hash,
        pair_hash=pair_hash,
        contract_product_version=contract_product_version,
        registry_product_version=registry_product_version,
        asset_version_class=asset_version_class,
    )


__all__ = (
    "DEFAULT_JSON_DIR",
    "DEFAULT_SVG_DIR",
    "DefaultAssetError",
    "DefaultVisualAsset",
    "default_assets",
    "default_payload",
    "build_visual_template",
    "figure_asset_paths",
    "load_default_figure_payload",
    "payoff_template_terms_match",
    "verify_contract_snapshot_binding",
    "resolve_default_visual_asset",
)
