#!/usr/bin/env python3
"""Assemble an App capability from a verified Skill shared payload and App UI."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_skill import _copy_file, _copy_tree
from verify_skill import (
    MODULES,
    PAGE_MODULES,
    _module_hashes,
    content_tree_entries,
    is_credential_file,
    is_development_artifact_path,
    is_finder_copy_name,
    shared_payload_entries,
    tree_hash,
    verify_skill,
)
from verify_capability import verify_app_capability


SOURCE_MAP = Path(__file__).with_name("capability-source-map.json")


class AppCapabilityBuildError(RuntimeError):
    pass


def _source_map_path(repo_root: Path) -> Path:
    return repo_root / "packaging" / "app" / "capability-source-map.json"


_PAGE_TREES = {
    (f"modules/{module}/page", f"assets/pages/{module}")
    for module in PAGE_MODULES
}
_FILE_MAPPINGS = {
    "core/module_host.py": "scripts/module_host.py",
    "core/src/runtime/browser/module_host_bridge.js": "assets/pages/module-host-bridge.js",
    "core/src/runtime/browser/module_host_presentation.css": "assets/pages/module-host-presentation.css",
    "core/src/runtime/browser/module_host_presentation.js": "assets/pages/module-host-presentation.js",
    "core/src/runtime/browser/date_input_control.js": "assets/pages/date-input-control.js",
    "core/src/runtime/browser/date_input_control.css": "assets/pages/date-input-control.css",
    "core/src/runtime/browser/plotly_chart_system.js": "assets/pages/plotly-chart-system.js",
    "core/src/runtime/browser/vendor/plotly-optionhelper.min.js": "assets/pages/vendor/plotly-optionhelper.min.js",
    "assets/icons/optionhelper-app-icon-tile-light.svg": "assets/icons/optionhelper-app-icon-tile-light.svg",
    "assets/icons/optionhelper-app-icon-tile-dark.svg": "assets/icons/optionhelper-app-icon-tile-dark.svg",
    "assets/icons/optionhelper-app-icon-tile-light.icns": "assets/icons/optionhelper-app-icon-tile-light.icns",
    "assets/icons/optionhelper-app-icon-tile-dark.icns": "assets/icons/optionhelper-app-icon-tile-dark.icns",
    "assets/icons/optionhelper-app-icon-tile-light.ico": "assets/icons/optionhelper-app-icon-tile-light.ico",
    "assets/icons/optionhelper-app-icon-tile-dark.ico": "assets/icons/optionhelper-app-icon-tile-dark.ico",
    "LICENSES/LibreChat-LICENSE.txt": "LICENSES/LibreChat-LICENSE.txt",
    "LICENSES/Plotly.js-LICENSE.txt": "LICENSES/Plotly.js-LICENSE.txt",
    "LICENSES/ThinkingOrbs-LICENSE.txt": "LICENSES/ThinkingOrbs-LICENSE.txt",
    "LICENSES/Bloub-LICENSE.txt": "LICENSES/Bloub-LICENSE.txt",
    "LICENSES/Bloub-SOURCE.md": "LICENSES/Bloub-SOURCE.md",
}


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AppCapabilityBuildError(f"{label}必须是非空POSIX相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AppCapabilityBuildError(f"{label}包含不安全路径：{value}")
    return path.as_posix()


def _validate_app_source_map(config: object) -> dict[str, object]:
    """Fail before copying if the App-only allowlist can escape its package."""

    if not isinstance(config, dict) or set(config) != {"schema", "page_modules", "files", "trees"}:
        raise AppCapabilityBuildError("App Capability Source Map字段无效")
    if config["schema"] != "optionhelper.app-capability-source-map":
        raise AppCapabilityBuildError("App Capability Source Map无效")
    if not isinstance(config["page_modules"], list) or tuple(config["page_modules"]) != PAGE_MODULES:
        raise AppCapabilityBuildError("App Capability Source Map五页面清单无效")
    files = config["files"]
    trees = config["trees"]
    if not isinstance(files, list) or not files or not isinstance(trees, list):
        raise AppCapabilityBuildError("App Capability Source Map白名单必须是数组")

    sources: list[str] = []
    targets: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"source", "target"}:
            raise AppCapabilityBuildError("App Capability Source Map文件条目无效")
        source = _relative(item["source"], "files.source")
        target = _relative(item["target"], "files.target")
        if _FILE_MAPPINGS.get(source) != target:
            raise AppCapabilityBuildError(f"App Capability文件映射越界：{source} -> {target}")
        if any(is_development_artifact_path(path) for path in (source, target)):
            raise AppCapabilityBuildError(f"App Capability不得包含开发内容：{source} -> {target}")
        if any(is_finder_copy_name(part) for path in (source, target) for part in PurePosixPath(path).parts):
            raise AppCapabilityBuildError(f"App Capability不得包含Finder副本：{source} -> {target}")
        if is_credential_file(PurePosixPath(source).name) or is_credential_file(PurePosixPath(target).name):
            raise AppCapabilityBuildError(f"App Capability不得包含凭据：{source} -> {target}")
        sources.append(source)
        targets.append(target)

    tree_pairs: set[tuple[str, str]] = set()
    tree_sources: list[str] = []
    for item in trees:
        if not isinstance(item, dict) or not set(item).issubset({"source", "target", "transform"}) or not {"source", "target"}.issubset(item):
            raise AppCapabilityBuildError("App Capability Source Map目录条目无效")
        source = _relative(item["source"], "trees.source")
        target = _relative(item["target"], "trees.target")
        pair = (source, target)
        if pair not in _PAGE_TREES:
            raise AppCapabilityBuildError(f"App Capability页面目录映射越界：{source} -> {target}")
        transform = item.get("transform")
        if transform not in {None, "page_theme_links"}:
            raise AppCapabilityBuildError(f"App Capability目录转换无效：{transform}")
        if transform == "page_theme_links" and pair != ("modules/backtester/page", "assets/pages/backtester"):
            raise AppCapabilityBuildError("页面主题转换只能用于回测页面")
        tree_pairs.add(pair)
        tree_sources.append(source)
        targets.append(target)
    if tree_pairs != _PAGE_TREES or len(trees) != len(_PAGE_TREES):
        raise AppCapabilityBuildError("App Capability必须精确映射五个页面目录")

    if len(sources) != len(set(sources)) or len(tree_sources) != len(set(tree_sources)):
        raise AppCapabilityBuildError("App Capability Source Map存在重复来源")
    if set(zip(sources, (str(item["target"]) for item in files), strict=True)) != set(_FILE_MAPPINGS.items()):
        raise AppCapabilityBuildError("App Capability必须精确映射正式文件白名单")
    if len(targets) != len(set(targets)):
        raise AppCapabilityBuildError("App Capability Source Map存在重复目标")
    for left in targets:
        for right in targets:
            if left != right and right.startswith(f"{left}/"):
                raise AppCapabilityBuildError(f"App Capability Source Map目标重叠：{left} 与 {right}")
    for source in sources:
        for tree_source in tree_sources:
            if source.startswith(f"{tree_source}/"):
                raise AppCapabilityBuildError(f"App Capability同一源文件重复映射：{source}")
    return config


def _replace(staged: Path, target: Path, replace: bool) -> Path:
    if target.exists() and not replace:
        raise AppCapabilityBuildError(f"App Capability已存在：{target}")
    backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except BaseException:
        if backup.exists():
            os.replace(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return target


def build_app_capability(
    output_root: Path,
    skill_root: Path,
    *,
    repo_root: Path = ROOT,
    replace: bool = False,
) -> Path:
    skill_root = skill_root.expanduser().resolve()
    skill_errors = verify_skill(skill_root)
    if skill_errors:
        raise AppCapabilityBuildError("共享Skill未通过验收：\n" + "\n".join(skill_errors))
    skill_manifest = json.loads((skill_root / "capability-manifest.json").read_text(encoding="utf-8"))
    shared_hashes = skill_manifest.get("shared_content_hashes")
    if not isinstance(shared_hashes, dict) or not shared_hashes:
        raise AppCapabilityBuildError("共享Skill未声明shared_content_hashes")
    source_map_path = _source_map_path(repo_root)
    config = _validate_app_source_map(json.loads(source_map_path.read_text(encoding="utf-8")))

    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / "option-helper"
    staging_parent = Path(tempfile.mkdtemp(prefix=".app-capability-staging-", dir=output_root))
    staged = staging_parent / "option-helper"
    staged.mkdir()
    try:
        for relative, expected_hash in sorted(shared_hashes.items()):
            source = skill_root / relative
            if not source.is_file() or source.is_symlink():
                raise AppCapabilityBuildError(f"共享载荷文件无效：{relative}")
            if sha256(source.read_bytes()).hexdigest() != expected_hash:
                raise AppCapabilityBuildError(f"共享载荷文件哈希漂移：{relative}")
            target_path = staged / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_path)

        for item in config["files"]:
            _copy_file(repo_root / item["source"], staged / item["target"])
        for item in config["trees"]:
            _copy_tree(
                repo_root / item["source"],
                staged / item["target"],
                ["**/__pycache__/**", "**/*.pyc", ".DS_Store"],
                transform=item.get("transform"),
            )

        entries = content_tree_entries(staged)
        hashes = {str(item["path"]): str(item["sha256"]) for item in entries}
        shared_entries = shared_payload_entries(entries)
        final_shared_hashes = {
            str(item["path"]): str(item["sha256"])
            for item in shared_entries
        }
        if final_shared_hashes != shared_hashes:
            raise AppCapabilityBuildError("App组装后的共享载荷与已验证Skill不一致")
        contract_entries = [item for item in entries if str(item["path"]).startswith("scripts/runtime/contracts/")]
        manifest = {
            key: skill_manifest[key]
            for key in (
                "manifest_schema", "package_status", "capability_version", "release_status",
                "formal_release", "execution_scope", "catalog_version", "catalog_source",
                "protocol_id", "design_system_id", "hash_spec_id",
            )
        }
        manifest.update({
            "package_kind": "app",
            "modules": list(MODULES),
            "page_modules": list(PAGE_MODULES),
            "contract_core_hash": tree_hash(contract_entries),
            "tool_catalog_hash": hashes.get("scripts/runtime/protocol/tool_catalog.py"),
            "content_tree_hash": tree_hash(entries),
            "content_hashes": hashes,
            "content_tree_entries": entries,
            "module_content_hashes": _module_hashes(entries),
            "shared_payload_hash": tree_hash(shared_entries),
            "shared_payload_entries": shared_entries,
            "shared_content_hashes": final_shared_hashes,
            "skill_content_tree_hash": skill_manifest["content_tree_hash"],
            "skill_manifest_hash": sha256((skill_root / "capability-manifest.json").read_bytes()).hexdigest(),
            "app_source_map_hash": sha256(source_map_path.read_bytes()).hexdigest(),
        })
        if "published_by" in skill_manifest:
            manifest["published_by"] = skill_manifest["published_by"]
        if "published_at" in skill_manifest:
            manifest["published_at"] = skill_manifest["published_at"]
        (staged / "capability-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        errors = verify_app_capability(staged)
        if errors:
            raise AppCapabilityBuildError("App Capability未通过验收：\n" + "\n".join(errors))
        return _replace(staged, target, replace)
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)
