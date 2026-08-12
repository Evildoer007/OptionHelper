"""Knowledger技术候选构建、正式版本验证与current读取的唯一入口。"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from pprint import pformat
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from runtime.bootstrap import discover_project_root
from runtime.contracts.contract_api import validate_registry
from runtime.knowledger.registry_loader import get_default_registry_path, load_registry


HASH_SPEC_VERSION = "v1.0"
TECHNICAL_STATUS = "technical_candidate_not_executable"
RULE_TERM_KEYS = frozenset({"monitor", "pricing_methods", "constraints", "derived_terms"})
SNAPSHOT_FILES = {
    "optionlist": "optionlist-fragment.md",
    "optionlib": "optionlib-fragment.md",
    "optionreg": "optionreg-product.py",
    "default_terms": "default-terms.json",
    "default_json": "default-payoff.json",
    "default_svg": "default-payoff.svg",
}


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    return _digest(path.read_bytes())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_version(version: str) -> None:
    if re.fullmatch(r"v[1-9]\d*\.\d+", version) is None:
        raise ValueError("版本必须使用v1.0、v1.1或v2.0格式")


def _validate_timestamp(value: object, field: str) -> None:
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError(f"{field}必须是含时区的ISO时间")


def _project_root(root: str | Path) -> Path:
    result = Path(root).expanduser().resolve()
    if result != discover_project_root(result):
        raise ValueError("root必须是当前OptionHelper开发仓库根目录")
    return result


def _optionlist_products(path: Path) -> list[dict[str, Any]]:
    pattern = re.compile(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(\d+\.\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$")
    products: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = pattern.match(line)
        if match:
            products.append({
                "sequence": int(match[1]), "name": match[2], "product_id": match[3],
                "category": match[4], "status": match[5], "line": line_number,
                "fragment": (line + "\n").encode("utf-8"),
            })
    if len(products) != 65 or [item["sequence"] for item in products] != list(range(1, 66)):
        raise ValueError("OptionList必须精确包含按1至65连续排序的产品")
    return products


def _optionlib_fragments(path: Path) -> dict[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^###\s+(\d+\.\d+)\s+(.+?)\s*$", text, re.MULTILINE))
    result: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(headings):
        product_id = match[1]
        if int(product_id.split(".", 1)[0]) < 2:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        result[product_id] = {"name": match[2], "fragment": text[match.start():end].encode("utf-8")}
    if len(result) != 65:
        raise ValueError("OptionLib必须精确包含65个产品正文片段")
    return result


def _assert_no_duplicate_registry_keys(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: set[object] = set()
        for key_node in node.keys:
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, (str, int, float, bool)):
                continue
            if key_node.value in seen:
                raise ValueError(f"OptionReg第{key_node.lineno}行存在重复字典键{key_node.value!r}")
            seen.add(key_node.value)


def _registry_products(root: Path) -> dict[str, Any]:
    source = root / "references" / "optionreg.py"
    if source.resolve() != get_default_registry_path().resolve():
        raise ValueError("构建器只允许读取Registry Loader注入的OptionReg")
    _assert_no_duplicate_registry_keys(source)
    registry = load_registry()
    errors = validate_registry(registry)
    if errors:
        raise ValueError(f"OptionReg运行校验失败：{errors}")
    products = dict(registry["products"])
    if len(products) != 65 or "weekly_" in json.dumps(registry, ensure_ascii=False):
        raise ValueError("OptionReg必须含65个产品且不得包含weekly观察选择器")
    return products


def _baseline_pairs(root: Path) -> dict[str, str]:
    source = root / "tests" / "baselines" / "payoffer_default_assets.sha256"
    result: dict[str, str] = {}
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            continue
        digest, separator, name = line.partition("  ")
        if separator != "  " or not re.fullmatch(r"[0-9a-f]{64}", digest) or name in result:
            raise ValueError(f"{source}:{line_number}不是唯一有效的Payoff基线记录")
        result[name] = digest
    if len(result) != 65:
        raise ValueError("冻结Payoff资产基线必须精确包含65项")
    return result


def _asset_sources(root: Path, name: str, expected_pair_hash: str) -> tuple[Path, Path, dict[str, Any]]:
    json_path = root / "modules" / "payoffer" / "figures" / "json" / f"{name}.json"
    svg_path = root / "modules" / "payoffer" / "figures" / "svg" / f"{name}.svg"
    if not json_path.is_file() or not svg_path.is_file():
        raise ValueError(f"{name}缺少默认Payoff JSON或SVG")
    pair_hash = _digest(json_path.read_bytes() + b"\0" + svg_path.read_bytes())
    if pair_hash != expected_pair_hash:
        raise ValueError(f"{name}默认Payoff资产与冻结基线不一致")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if "weekly_" in json.dumps(payload, ensure_ascii=False):
        raise ValueError(f"{name}默认Payoff资产仍含weekly运行语义")
    return json_path, svg_path, payload


def _asset_differences(product: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    return [field for field in ("terms", "paths") if _canonical(payload.get(field)) != _canonical(product.get(field))]


def _write_candidate_tree(root: Path, destination: Path, version: str, built_at: str) -> None:
    list_products = _optionlist_products(root / "references" / "optionlist.md")
    lib_products = _optionlib_fragments(root / "references" / "optionlib.md")
    reg_products = _registry_products(root)
    baselines = _baseline_pairs(root)
    ordered_ids = [item["product_id"] for item in list_products]
    if ordered_ids != list(lib_products) or ordered_ids != list(reg_products):
        raise ValueError("OptionList、OptionLib与OptionReg的产品顺序不一致")
    source_hashes = {name: _file_digest(root / "references" / name) for name in ("optionlist.md", "optionlib.md", "optionreg.py")}
    snapshot_id = "knowl-" + _digest(_canonical({"version": version, "sources": source_hashes}))[:20]
    product_refs: dict[str, str] = {}
    product_hashes: dict[str, str] = {}
    asset_drift: dict[str, list[str]] = {}

    for list_product in list_products:
        product_id = list_product["product_id"]
        lib_product = lib_products[product_id]
        reg_product = reg_products[product_id]
        name = list_product["name"]
        identity = reg_product["identity"]
        if name != lib_product["name"] or name != identity["name_zh"] or list_product["status"] != "已录入" or identity["entry_status"] is not True:
            raise ValueError(f"{product_id}三源身份、名称或入库状态不一致")
        if name not in baselines:
            raise ValueError(f"{product_id}未登记冻结Payoff基线")

        product_dir = destination / "products" / product_id
        product_dir.mkdir(parents=True)
        (product_dir / SNAPSHOT_FILES["optionlist"]).write_bytes(list_product["fragment"])
        (product_dir / SNAPSHOT_FILES["optionlib"]).write_bytes(lib_product["fragment"])
        (product_dir / SNAPSHOT_FILES["optionreg"]).write_text(
            "PRODUCT = " + pformat(reg_product, width=120, sort_dicts=False) + "\n", encoding="utf-8",
        )
        default_terms = {key: value for key, value in reg_product["terms"].items() if key not in RULE_TERM_KEYS}
        (product_dir / SNAPSHOT_FILES["default_terms"]).write_bytes(_json_bytes(default_terms))
        json_source, svg_source, payload = _asset_sources(root, name, baselines[name])
        shutil.copyfile(json_source, product_dir / SNAPSHOT_FILES["default_json"])
        shutil.copyfile(svg_source, product_dir / SNAPSHOT_FILES["default_svg"])
        differences = _asset_differences(reg_product, payload)
        if differences:
            asset_drift[product_id] = differences
        snapshot_hashes = {key + "_sha256": _file_digest(product_dir / filename) for key, filename in SNAPSHOT_FILES.items()}
        product_snapshot = {
            "manifest_type": "ProductTechnicalSnapshot",
            "snapshot_id": snapshot_id,
            "candidate_status": TECHNICAL_STATUS,
            "formal_release": False,
            "executable": False,
            "product_id": product_id,
            "name_zh": name,
            "proposed_product_version": version,
            "hash_spec_version": HASH_SPEC_VERSION,
            "built_at": built_at,
            "snapshot_files": SNAPSHOT_FILES,
            "snapshot_sha256": snapshot_hashes,
            "integrity_evidence": {
                "three_source_identity_verified": True,
                "frozen_payoff_pair_verified": True,
                "default_asset_registry_match": not differences,
                "default_asset_registry_differences": differences,
            },
        }
        manifest_path = product_dir / "product-snapshot.json"
        manifest_path.write_bytes(_json_bytes(product_snapshot))
        product_refs[product_id] = manifest_path.relative_to(destination).as_posix()
        product_hashes[product_id] = _file_digest(manifest_path)

    snapshot = {
        "manifest_type": "KnowledgerTechnicalSnapshot",
        "snapshot_id": snapshot_id,
        "candidate_status": TECHNICAL_STATUS,
        "formal_release": False,
        "executable": False,
        "proposed_catalog_version": version,
        "proposed_product_versions": {product_id: version for product_id in ordered_ids},
        "hash_spec_version": HASH_SPEC_VERSION,
        "built_at": built_at,
        "product_count": 65,
        "product_snapshot_refs": product_refs,
        "product_snapshot_sha256": product_hashes,
        "source_file_sha256": source_hashes,
        "frozen_payoff_baseline_sha256": _file_digest(root / "tests" / "baselines" / "payoffer_default_assets.sha256"),
        "integrity_evidence": {
            "registry_runtime_validation": "passed",
            "weekly_runtime_semantics": "absent",
            "formal_execution_blocked": True,
            "default_asset_registry_drift": asset_drift,
        },
    }
    (destination / "snapshot.json").write_bytes(_json_bytes(snapshot))


def build_candidate(root: str | Path, output: str | Path, *, version: str, built_at: str) -> Path:
    """原子生成技术候选；禁止写入正式versions/{version}归档。"""
    root_path = _project_root(root)
    output_path = Path(output).expanduser().resolve()
    _validate_version(version)
    _validate_timestamp(built_at, "built_at")
    formal_root = (root_path / "versions").resolve()
    if formal_root == output_path or formal_root in output_path.parents:
        raise ValueError("技术候选不得写入versions正式版本树")
    if output_path.exists():
        raise FileExistsError(f"候选目录已存在，禁止覆盖：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="knowl-snapshot-", dir=output_path.parent) as directory:
        staging = Path(directory) / "snapshot"
        staging.mkdir()
        _write_candidate_tree(root_path, staging, version, built_at)
        staging.rename(output_path)
    return output_path


def _tree_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): _file_digest(path) for path in sorted(root.rglob("*")) if path.is_file()}


def verify_candidate_integrity(candidate: str | Path) -> dict[str, Any]:
    """仅使用候选内文件验证65项六件套完整性，不依赖当前源。"""
    candidate_path = Path(candidate).expanduser().resolve()
    snapshot = json.loads((candidate_path / "snapshot.json").read_text(encoding="utf-8"))
    if (
        snapshot.get("manifest_type") != "KnowledgerTechnicalSnapshot"
        or snapshot.get("candidate_status") != TECHNICAL_STATUS
        or snapshot.get("formal_release") is not False
        or snapshot.get("executable") is not False
        or "release_id" in snapshot
    ):
        raise ValueError("目标不是不可执行的Knowledger技术候选")
    versions = snapshot.get("proposed_product_versions")
    refs = snapshot.get("product_snapshot_refs")
    hashes = snapshot.get("product_snapshot_sha256")
    if not isinstance(versions, dict) or len(versions) != 65 or not isinstance(refs, dict) or list(refs) != list(versions) or not isinstance(hashes, dict) or list(hashes) != list(versions):
        raise ValueError("技术候选必须含65项有序产品快照引用和哈希")
    for product_id, proposed_version in versions.items():
        expected_ref = f"products/{product_id}/product-snapshot.json"
        if refs[product_id] != expected_ref:
            raise ValueError(f"{product_id}产品快照引用必须固定为{expected_ref}")
        manifest_path = (candidate_path / expected_ref).resolve()
        if candidate_path not in manifest_path.parents:
            raise ValueError(f"{product_id}产品快照引用越出候选根目录")
        if not manifest_path.is_file() or _file_digest(manifest_path) != hashes[product_id]:
            raise ValueError(f"{product_id}产品快照manifest缺失或哈希不一致")
        product = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            product.get("manifest_type") != "ProductTechnicalSnapshot"
            or product.get("snapshot_id") != snapshot.get("snapshot_id")
            or product.get("candidate_status") != TECHNICAL_STATUS
            or product.get("formal_release") is not False
            or product.get("executable") is not False
            or product.get("product_id") != product_id
            or product.get("proposed_product_version") != proposed_version
            or product.get("snapshot_files") != SNAPSHOT_FILES
            or "release_id" in product
        ):
            raise ValueError(f"{product_id}产品快照身份或候选状态无效")
        product_dir = manifest_path.parent
        product_hashes = product.get("snapshot_sha256")
        if not isinstance(product_hashes, dict):
            raise ValueError(f"{product_id}缺少六件套哈希")
        for key, filename in SNAPSHOT_FILES.items():
            path = product_dir / filename
            if not path.is_file() or _file_digest(path) != product_hashes.get(key + "_sha256"):
                raise ValueError(f"{product_id}的{filename}缺失或哈希不一致")
    return snapshot


def verify_candidate_freshness(root: str | Path, candidate: str | Path) -> None:
    """先验候选自身完整性，再与当前权威源重建结果比较。"""
    root_path = _project_root(root)
    candidate_path = Path(candidate).expanduser().resolve()
    snapshot = verify_candidate_integrity(candidate_path)
    with tempfile.TemporaryDirectory(prefix="knowl-verify-") as directory:
        rebuilt = Path(directory) / "snapshot"
        build_candidate(
            root_path, rebuilt,
            version=str(snapshot["proposed_catalog_version"]), built_at=str(snapshot["built_at"]),
        )
        if _tree_hashes(candidate_path) != _tree_hashes(rebuilt):
            raise ValueError("技术候选与当前权威源重建结果不一致")


def verify_candidate(root: str | Path, candidate: str | Path) -> None:
    """兼容入口：执行完整性与freshness双重验证。"""
    verify_candidate_freshness(root, candidate)


def _published_manifest(value: Mapping[str, Any], manifest_type: str, version_field: str, version: str) -> None:
    if "release_id" in value or "candidate_status" in value:
        raise ValueError("正式版本不得包含release_id或候选状态")
    if value.get("manifest_type") != manifest_type or value.get("publication_status") != "published":
        raise ValueError("正式版本清单必须明确标记published")
    if value.get("formal_release") is not True or value.get("executable") is not True or value.get(version_field) != version:
        raise ValueError("正式版本清单版本或可执行状态无效")
    if not isinstance(value.get("published_by"), str) or not value["published_by"].strip():
        raise ValueError("正式版本必须记录published_by")
    _validate_timestamp(value.get("published_at"), "published_at")


def _validate_product_manifests(version_root: Path, catalog: Mapping[str, Any], expected_ids: list[str]) -> None:
    products = catalog["products"]
    manifest_refs = catalog.get("product_manifest_refs")
    manifest_hashes = catalog.get("product_manifest_sha256")
    if not isinstance(manifest_refs, dict) or not isinstance(manifest_hashes, dict) or list(manifest_refs) != expected_ids or list(manifest_hashes) != expected_ids:
        raise ValueError("CatalogVersion缺少有序ProductVersion引用或哈希")
    for product_id in expected_ids:
        product_version = products[product_id]
        _validate_version(product_version)
        manifest_path = version_root / str(manifest_refs[product_id])
        expected_path = version_root / "knowledger" / "products" / product_id / "product-version.json"
        if manifest_path.resolve() != expected_path.resolve() or not manifest_path.is_file():
            raise ValueError(f"{product_id}的ProductVersion引用位置无效")
        if _file_digest(manifest_path) != manifest_hashes[product_id]:
            raise ValueError(f"{product_id}的ProductVersion manifest哈希不一致")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _published_manifest(manifest, "ProductVersion", "product_version", product_version)
        if manifest.get("product_id") != product_id or manifest.get("snapshot_files") != SNAPSHOT_FILES:
            raise ValueError(f"{product_id}的ProductVersion身份或六件套声明不一致")
        hashes = manifest.get("snapshot_sha256")
        if not isinstance(hashes, dict):
            raise ValueError(f"{product_id}缺少六件套哈希")
        product_dir = manifest_path.parent
        for key, filename in SNAPSHOT_FILES.items():
            snapshot_path = product_dir / filename
            if not snapshot_path.is_file() or _file_digest(snapshot_path) != hashes.get(key + "_sha256"):
                raise ValueError(f"{product_id}的{filename}缺失或哈希不一致")
        product = _read_product_literal(product_dir / SNAPSHOT_FILES["optionreg"], product_id)
        identity = product.get("identity", {})
        name = identity.get("name_zh")
        if identity.get("entry_status") is not True or not isinstance(name, str) or not name:
            raise ValueError(f"{product_id}的归档OptionReg身份或入库状态无效")
        list_text = (product_dir / SNAPSHOT_FILES["optionlist"]).read_text(encoding="utf-8")
        if re.search(rf"^\|\s*\d+\s*\|\s*{re.escape(name)}\s*\|\s*{re.escape(product_id)}\s*\|.*\|\s*已录入\s*\|$", list_text, re.MULTILINE) is None:
            raise ValueError(f"{product_id}的归档OptionList身份不一致")
        lib_text = (product_dir / SNAPSHOT_FILES["optionlib"]).read_text(encoding="utf-8")
        if re.search(rf"^###\s+{re.escape(product_id)}\s+{re.escape(name)}\s*$", lib_text, re.MULTILINE) is None:
            raise ValueError(f"{product_id}的归档OptionLib身份不一致")
        default_terms = json.loads((product_dir / SNAPSHOT_FILES["default_terms"]).read_text(encoding="utf-8"))
        expected_terms = {key: value for key, value in product.get("terms", {}).items() if key not in RULE_TERM_KEYS}
        if _canonical(default_terms) != _canonical(expected_terms):
            raise ValueError(f"{product_id}的归档默认条款与OptionReg不一致")
        payoff = json.loads((product_dir / SNAPSHOT_FILES["default_json"]).read_text(encoding="utf-8"))
        if payoff.get("name_zh") != name or _canonical(payoff.get("terms")) != _canonical(product.get("terms")) or _canonical(payoff.get("paths")) != _canonical(product.get("paths")):
            raise ValueError(f"{product_id}的归档Payoff JSON与OptionReg条款或路径不一致")


def _read_product_literal(path: Path, product_id: str) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PRODUCT" for target in node.targets):
            product = ast.literal_eval(node.value)
            if isinstance(product, dict) and product.get("identity", {}).get("product_id") == product_id:
                return product
            break
    raise ValueError("ProductVersion的OptionReg产品快照无效")


def validate_published_catalog(
    root: str | Path,
    version: str,
    *,
    versions_root: str | Path | None = None,
    require_source_match: bool = True,
) -> dict[str, Any]:
    """验证正式Catalog、65个ProductVersion、六件套及当前源一致性。"""
    root_path = _project_root(root)
    _validate_version(version)
    archive_root = Path(versions_root).expanduser().resolve() if versions_root else root_path / "versions"
    version_root = archive_root / version
    catalog_path = version_root / "knowledger" / "catalog-version.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"正式CatalogVersion不存在或无效：{catalog_path}") from error
    _published_manifest(catalog, "CatalogVersion", "catalog_version", version)
    products = catalog.get("products")
    if not isinstance(products, dict):
        raise ValueError("CatalogVersion必须按产品顺序映射65个真实产品ID")
    expected_ids = list(products)
    if (
        len(expected_ids) != 65
        or any(re.fullmatch(r"[2-9]\.\d+|10\.\d+", product_id) is None for product_id in expected_ids)
        or expected_ids != sorted(expected_ids, key=lambda product_id: tuple(map(int, product_id.split("."))))
    ):
        raise ValueError("CatalogVersion必须按OptionList顺序映射65个真实产品ID")
    if require_source_match:
        current_ids = [item["product_id"] for item in _optionlist_products(root_path / "references" / "optionlist.md")]
        if expected_ids != current_ids:
            raise ValueError("CatalogVersion产品ID与当前OptionList不一致")
    if catalog.get("ordered_product_version_map") != products:
        raise ValueError("CatalogVersion有序产品映射不一致")
    _validate_product_manifests(version_root, catalog, expected_ids)

    if require_source_match:
        expected_sources = {name: _file_digest(root_path / "references" / name) for name in ("optionlist.md", "optionlib.md", "optionreg.py")}
        if catalog.get("source_file_sha256") != expected_sources:
            raise ValueError("CatalogVersion与当前三份权威源不一致")
    return catalog


def _load_product_snapshot(
    version_root: Path,
    catalog: Mapping[str, Any],
    product_id: str,
    product_version: str,
) -> dict[str, Any]:
    products = catalog["products"]
    if products.get(product_id) != product_version:
        raise ValueError("显式ProductVersion不属于所选Catalog")
    manifest_path = version_root / catalog["product_manifest_refs"][product_id]
    product_dir = manifest_path.parent
    product = _read_product_literal(product_dir / SNAPSHOT_FILES["optionreg"], product_id)
    return {
        "catalog_version": catalog["catalog_version"],
        "product_version": product_version,
        "product": product,
        "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
        "default_json_path": product_dir / SNAPSHOT_FILES["default_json"],
        "default_svg_path": product_dir / SNAPSHOT_FILES["default_svg"],
        "default_terms_path": product_dir / SNAPSHOT_FILES["default_terms"],
    }


def load_published_catalog_snapshots(
    root: str | Path,
    version: str,
    *,
    versions_root: str | Path | None = None,
    require_source_match: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """验证正式Catalog并读取其65份不可变产品快照。"""
    root_path = _project_root(root)
    archive_root = Path(versions_root).expanduser().resolve() if versions_root else root_path / "versions"
    version_root = archive_root / version
    catalog = validate_published_catalog(
        root_path, version, versions_root=archive_root, require_source_match=require_source_match,
    )
    snapshots = {
        product_id: _load_product_snapshot(version_root, catalog, product_id, product_version)
        for product_id, product_version in catalog["products"].items()
    }
    return catalog, snapshots


def activate_catalog(root: str | Path, version: str, *, versions_root: str | Path | None = None) -> Path:
    """验证后原子切换current；可再次指向旧正式版本完成回滚。"""
    root_path = _project_root(root)
    archive_root = Path(versions_root).expanduser().resolve() if versions_root else root_path / "versions"
    catalog = validate_published_catalog(root_path, version, versions_root=archive_root, require_source_match=False)
    catalog_path = archive_root / version / "knowledger" / "catalog-version.json"
    pointer = {
        "pointer_type": "KnowledgerCatalogCurrent",
        "catalog_version": version,
        "catalog_manifest_sha256": _file_digest(catalog_path),
        "published_at": catalog["published_at"],
    }
    archive_root.mkdir(parents=True, exist_ok=True)
    current = archive_root / "current.json"
    handle, temporary_name = tempfile.mkstemp(prefix=".current-", suffix=".json", dir=archive_root)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(_json_bytes(pointer))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, current)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return current


def load_current_catalog(root: str | Path, *, versions_root: str | Path | None = None) -> dict[str, Any] | None:
    """无current时返回None保留开发态unversioned；存在时严格验证，失败不降级。"""
    root_path = _project_root(root)
    archive_root = Path(versions_root).expanduser().resolve() if versions_root else root_path / "versions"
    current = archive_root / "current.json"
    if not current.exists():
        return None
    try:
        pointer = json.loads(current.read_text(encoding="utf-8"))
        if set(pointer) != {"pointer_type", "catalog_version", "catalog_manifest_sha256", "published_at"} or pointer.get("pointer_type") != "KnowledgerCatalogCurrent":
            raise ValueError("current CatalogVersion指针字段或类型无效")
        version = str(pointer["catalog_version"])
    except (OSError, json.JSONDecodeError, KeyError) as error:
        raise ValueError("current CatalogVersion指针无效") from error
    catalog = validate_published_catalog(root_path, version, versions_root=archive_root, require_source_match=False)
    catalog_path = archive_root / version / "knowledger" / "catalog-version.json"
    if pointer.get("catalog_manifest_sha256") != _file_digest(catalog_path):
        raise ValueError("current指针与CatalogVersion manifest哈希不一致")
    if pointer.get("published_at") != catalog.get("published_at"):
        raise ValueError("current指针与CatalogVersion签发时间不一致")
    return catalog


def load_current_product_snapshot(
    root: str | Path,
    product_id: str,
    product_version: str,
    *,
    versions_root: str | Path | None = None,
) -> dict[str, Any]:
    """仅经已验证current Catalog读取正式ProductVersion六件套。"""
    root_path = _project_root(root)
    archive_root = Path(versions_root).expanduser().resolve() if versions_root else root_path / "versions"
    catalog = load_current_catalog(root_path, versions_root=archive_root)
    if catalog is None:
        raise ValueError("没有正式current Catalog，显式ProductVersion不可执行")
    return _load_product_snapshot(archive_root / str(catalog["catalog_version"]), catalog, product_id, product_version)


def load_packaged_product_snapshot(
    package_root: str | Path,
    product_id: str,
    product_version: str,
) -> dict[str, Any]:
    """读取Skill中已选正式Catalog，不依赖开发仓库或current指针。"""
    root = Path(package_root).expanduser().resolve()
    package_root = root / "scripts"
    catalog_path = package_root / "knowledger" / "catalog-version.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("发行包缺少有效的正式CatalogVersion") from error
    version = str(catalog.get("catalog_version", ""))
    _validate_version(version)
    _published_manifest(catalog, "CatalogVersion", "catalog_version", version)
    products = catalog.get("products")
    if not isinstance(products, dict):
        raise ValueError("发行包CatalogVersion缺少产品映射")
    expected_ids = list(products)
    if (
        len(expected_ids) != 65
        or any(re.fullmatch(r"[2-9]\.\d+|10\.\d+", item) is None for item in expected_ids)
        or expected_ids != sorted(expected_ids, key=lambda item: tuple(map(int, item.split("."))))
        or catalog.get("ordered_product_version_map") != products
    ):
        raise ValueError("发行包CatalogVersion产品映射无效")
    _validate_product_manifests(package_root, catalog, expected_ids)
    return _load_product_snapshot(package_root, catalog, product_id, product_version)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledger版本候选构建与正式版本复核")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-candidate")
    build.add_argument("--root", default=".")
    build.add_argument("--output", required=True)
    build.add_argument("--proposed-version", default="v1.0")
    build.add_argument("--built-at", required=True)
    integrity = subparsers.add_parser("verify-candidate-integrity")
    integrity.add_argument("--candidate", required=True)
    freshness = subparsers.add_parser("verify-candidate-freshness")
    freshness.add_argument("--root", default=".")
    freshness.add_argument("--candidate", required=True)
    formal = subparsers.add_parser("verify-published")
    formal.add_argument("--root", default=".")
    formal.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    if args.command == "build-candidate":
        build_candidate(args.root, args.output, version=args.proposed_version, built_at=args.built_at)
    elif args.command == "verify-candidate-integrity":
        verify_candidate_integrity(args.candidate)
    elif args.command == "verify-candidate-freshness":
        verify_candidate_freshness(args.root, args.candidate)
    else:
        validate_published_catalog(args.root, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
