#!/usr/bin/env python3
"""Validate the App-only capability assembled around the shared payload."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from verify_skill import (
    HASH_SPEC_ID,
    MODULES,
    PAGE_MODULES,
    PROTOCOL_ID,
    _code_errors,
    _link_errors,
    _module_hashes,
    _source_manifest_errors,
    content_tree_entries,
    shared_payload_entries,
    tree_hash,
)
_FORBIDDEN = (
    "SKILL.md",
    "README.md",
    "scripts/environment_check.py",
    "scripts/requirements.lock",
    "references/context.md",
    "references/knowledger-manager.md",
)


def _hashes(entries: list[dict[str, object]]) -> dict[str, str]:
    return {str(item["path"]): str(item["sha256"]) for item in entries}


def verify_app_capability(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    errors: list[str] = []
    try:
        entries = content_tree_entries(root)
    except Exception as error:
        return [f"App Capability目录无效：{error}"]
    manifest_path = root / "capability-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"App Capability Manifest无效：{error}"]

    actual_hashes = _hashes(entries)
    allowed_top_level = {"scripts", "assets", "references", "LICENSES", "capability-manifest.json"}
    actual_top_level = {path.name for path in root.iterdir()}
    errors.extend(
        f"App Capability发行根目录存在未允许项：{name}"
        for name in sorted(actual_top_level - allowed_top_level)
    )
    errors.extend(_code_errors(root))
    errors.extend(_link_errors(root))
    errors.extend(_source_manifest_errors(root, manifest))
    required = {
        "scripts/tool_entry.py",
        "scripts/module_host.py",
        "scripts/knowledger/optionreg.py",
        "scripts/knowledger/source-manifest.json",
        "scripts/runtime/protocol/tool_catalog.py",
        "references/optionlist.md",
        "references/optionlib.md",
        "assets/pages/module-host-bridge.js",
        "assets/pages/module-host-presentation.css",
        "assets/pages/module-host-presentation.js",
        "assets/pages/date-input-control.js",
        "assets/pages/date-input-control.css",
        "assets/pages/plotly-chart-system.js",
        "assets/pages/vendor/plotly-optionhelper.min.js",
        "assets/icons/optionhelper-app-icon-tile-light.svg",
        "assets/icons/optionhelper-app-icon-tile-dark.svg",
        "assets/icons/optionhelper-app-icon-tile-light.icns",
        "assets/icons/optionhelper-app-icon-tile-dark.icns",
        "assets/icons/optionhelper-app-icon-tile-light.ico",
        "assets/icons/optionhelper-app-icon-tile-dark.ico",
        "assets/designer/vendor/echarts.min.js",
        "scripts/modules/designer/comparison_renderer.py",
        "assets/designer/templates/multicard-standard.template.json",
        "assets/designer/templates/multicard.html",
        "assets/designer/templates/multireport-standard.template.json",
        "assets/designer/templates/multireport.html",
    }
    required.update(f"assets/pages/{module}/{module}.html" for module in PAGE_MODULES)
    errors.extend(f"App Capability缺少资源：{path}" for path in sorted(required - set(actual_hashes)))
    errors.extend(f"App Capability混入Skill专属资源：{path}" for path in _FORBIDDEN if (root / path).exists())
    references = root / "references"
    allowed_references = {"optionlist.md", "optionlib.md"}
    actual_references = {path.name for path in references.iterdir()} if references.is_dir() else set()
    if actual_references != allowed_references:
        errors.append(
            "App Capability references必须精确为运行所需OptionList与OptionLib："
            f"{sorted(actual_references)}"
        )
    figures = root / "assets" / "payoffer" / "figures"
    if not figures.is_dir():
        errors.append("App Capability缺少Payoffer默认资产")
    else:
        json_names = {path.stem for path in (figures / "json").glob("*.json")}
        svg_names = {path.stem for path in (figures / "svg").glob("*.svg")}
        if len(json_names) != 65 or json_names != svg_names:
            errors.append("App Capability Payoffer默认JSON/SVG必须为同名65对")
    optionlist = root / "references" / "optionlist.md"
    if not optionlist.is_file():
        errors.append("App Capability缺少65产品OptionList")
    else:
        product_ids = re.findall(
            r"^\|\s*\d+\s*\|.*?\|\s*(\d+\.\d+)\s*\|",
            optionlist.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if len(product_ids) != 65 or len(set(product_ids)) != 65:
            errors.append("App Capability OptionList必须登记65个唯一产品")

    if manifest.get("package_kind") != "app":
        errors.append("App Capability package_kind必须为app")
    if tuple(manifest.get("modules", ())) != MODULES:
        errors.append("App Capability七模块清单无效")
    if tuple(manifest.get("page_modules", ())) != PAGE_MODULES:
        errors.append("App Capability五页面清单无效")
    if manifest.get("hash_spec_id") != HASH_SPEC_ID:
        errors.append("App Capability哈希规范无效")
    if manifest.get("protocol_id") != PROTOCOL_ID:
        errors.append("App Capability协议标识无效")
    skill_content_tree_hash = manifest.get("skill_content_tree_hash")
    if skill_content_tree_hash is None:
        errors.append("App Capability缺少Skill内容树绑定")
    elif not isinstance(skill_content_tree_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", skill_content_tree_hash):
        errors.append("App Capability Skill内容树哈希无效")
    skill_manifest_hash = manifest.get("skill_manifest_hash")
    if skill_manifest_hash is None:
        errors.append("App Capability缺少Skill Manifest绑定")
    elif not isinstance(skill_manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", skill_manifest_hash):
        errors.append("App Capability Skill Manifest哈希无效")
    if manifest.get("content_tree_entries") != entries:
        errors.append("App Capability内容树记录不一致")
    if manifest.get("content_hashes") != actual_hashes:
        errors.append("App Capability文件哈希不一致")
    if manifest.get("content_tree_hash") != tree_hash(entries):
        errors.append("App Capability内容树总哈希不一致")
    if manifest.get("module_content_hashes") != _module_hashes(entries):
        errors.append("App Capability模块哈希不一致")

    shared_entries = shared_payload_entries(entries)
    shared_hashes = _hashes(shared_entries)
    if manifest.get("shared_payload_entries") != shared_entries:
        errors.append("App Capability共享载荷记录不一致")
    if manifest.get("shared_content_hashes") != shared_hashes:
        errors.append("App Capability共享载荷文件哈希不一致")
    if manifest.get("shared_payload_hash") != tree_hash(shared_entries):
        errors.append("App Capability共享载荷总哈希不一致")
    if "scripts/knowledger/source-manifest.json" not in shared_hashes:
        errors.append("App Capability共享载荷缺少Catalog哈希")

    source_map = Path(__file__).with_name("capability-source-map.json")
    if manifest.get("app_source_map_hash") != sha256(source_map.read_bytes()).hexdigest():
        errors.append("App Capability专属Source Map哈希不一致")
    return errors


if __name__ == "__main__":
    problems = verify_app_capability(Path(sys.argv[1]))
    if problems:
        raise SystemExit("\n".join(problems))
    print("App capability verified")
