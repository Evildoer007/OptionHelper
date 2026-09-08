#!/usr/bin/env python3
"""从明确白名单生成未签发的OptionHelper Skill候选包。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from fnmatch import fnmatch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "packaging" / "skill") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging" / "skill"))

from verify_skill import (
    HASH_SPEC_ID,
    MODULES,
    SKILL_PAGE_MODULES,
    SkillVerificationError,
    _module_hashes,
    _tool_catalog_protocol_id,
    content_tree_entries,
    is_development_artifact_path,
    is_credential_file,
    probe_runtime,
    shared_payload_entries,
    tree_hash,
    verify_skill,
    verify_zip,
)
SOURCE_MAP = Path(__file__).with_name("package-source-map.json")
_SAFE_TOP_LEVEL = ("references", "scripts", "assets", "LICENSES")
_BANNED_SOURCE_ROOTS = {"blueprint", "data", "dist", "evals", "history", "products", "result"}
_EXCLUDED_PATH_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".optionhelper", "blueprint", "history", "result"}
_FROZEN_NUMERICAL_ASSET = "engines/pricing_core/data/rand_normal.npy"


def _finder_copy_name(name: str) -> bool:
    return bool(re.search(r"\s\d+(?:\.[^.]+)+$", name))


def _candidate_conflicts(output_root: Path, name: str) -> list[Path]:
    pattern = re.compile(rf"^{re.escape(name)}\s\d+(?:\.zip)?$")
    return sorted(path for path in output_root.iterdir() if pattern.fullmatch(path.name))


if str(ROOT / "core" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "core" / "src"))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from knowledge_snapshot import (
    load_published_catalog_snapshots,
    registry_product_revisions,
    validate_published_catalog,
)
from release_contract import skill_archive_name
from release_contract import CAPABILITY_MANIFEST_SCHEMA, DEVELOPMENT_ID, PROTOCOL_ID, RELEASE_VERSION, SKILL_VERSION, require_release_version


class SkillBuildError(RuntimeError):
    pass


def _source_map_path(repo_root: Path) -> Path:
    return repo_root / "packaging" / "skill" / "package-source-map.json"


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SkillBuildError(f"{label}必须是非空POSIX相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillBuildError(f"{label}包含不安全路径：{value}")
    return path.as_posix()


def _target_allowed(target: str) -> bool:
    return target in {"SKILL.md", "README.md"} or any(
        target == root or target.startswith(f"{root}/") for root in _SAFE_TOP_LEVEL
    )


def _validate_source_map(config: object) -> dict[str, object]:
    if not isinstance(config, dict):
        raise SkillBuildError("Source Map必须是JSON对象")
    expected_keys = {"schema", "modules", "page_modules", "catalog", "files", "trees"}
    if set(config) != expected_keys:
        raise SkillBuildError(f"Source Map字段必须精确为{sorted(expected_keys)}")
    if config["schema"] != "optionhelper.skill-source-map":
        raise SkillBuildError("Source Map Schema无效")
    if not isinstance(config["modules"], list) or not isinstance(config["page_modules"], list):
        raise SkillBuildError("Source Map模块清单必须是数组")
    if tuple(config["modules"]) != MODULES or tuple(config["page_modules"]) != SKILL_PAGE_MODULES:
        raise SkillBuildError("Skill Source Map必须登记固定七模块且不得包含App页面")
    catalog = config["catalog"]
    if not isinstance(catalog, dict) or set(catalog) != {"source_root", "file_name", "target"}:
        raise SkillBuildError("Source Map.catalog无效")
    if _relative(catalog["source_root"], "catalog.source_root") != "versions":
        raise SkillBuildError("知识源清单只能来自versions/{version}/knowledger")
    if catalog["file_name"] != "knowledger/source-manifest.json" or _relative(catalog["target"], "catalog.target") != "scripts/knowledger/source-manifest.json":
        raise SkillBuildError("知识源清单目标必须固定为scripts/knowledger/source-manifest.json")
    targets: list[str] = [str(catalog["target"])]
    for kind in ("files", "trees"):
        entries = config[kind]
        if not isinstance(entries, list) or not entries:
            raise SkillBuildError(f"Source Map.{kind}必须是非空数组")
        for item in entries:
            if not isinstance(item, dict):
                raise SkillBuildError(f"Source Map.{kind}包含非对象项")
            allowed = {"source", "target", "transform"} if kind == "files" else {"source", "target", "exclude", "transform"}
            if not set(item).issubset(allowed) or not {"source", "target"}.issubset(item):
                raise SkillBuildError(f"Source Map.{kind}条目字段无效")
            source = _relative(item["source"], f"{kind}.source")
            target = _relative(item["target"], f"{kind}.target")
            if source.split("/", 1)[0] in _BANNED_SOURCE_ROOTS:
                raise SkillBuildError(f"Source Map不得读取生成或历史目录：{source}")
            if is_development_artifact_path(source) or is_development_artifact_path(target):
                raise SkillBuildError(f"Source Map不得包含开发内容：{source} -> {target}")
            if not _target_allowed(target) or target == "capability-manifest.json":
                raise SkillBuildError(f"Source Map目标不在Skill发行范围：{target}")
            if kind == "files":
                transform = item.get("transform")
                if transform not in {None, "skill_links"}:
                    raise SkillBuildError(f"不支持的文档转换：{transform}")
                if transform == "skill_links" and source != "SKILL.md":
                    raise SkillBuildError("只有根SKILL.md可以转换相对链接")
            else:
                excludes = item.get("exclude", [])
                if not isinstance(excludes, list) or not all(isinstance(value, str) and value for value in excludes):
                    raise SkillBuildError("树白名单exclude必须是字符串数组")
                transform = item.get("transform")
                if transform not in {None, "page_theme_links"}:
                    raise SkillBuildError(f"不支持的目录转换：{transform}")
                if transform == "page_theme_links" and (source, target) != (
                    "modules/backtester/page",
                    "assets/pages/backtester",
                ):
                    raise SkillBuildError("页面主题路径转换只能用于回测页面")
            targets.append(target)
    if len(targets) != len(set(targets)):
        raise SkillBuildError("Source Map存在重复目标")
    for left in targets:
        for right in targets:
            if left != right and right.startswith(f"{left}/"):
                raise SkillBuildError(f"Source Map目标重叠：{left} 与 {right}")
    for file_entry in config["files"]:
        assert isinstance(file_entry, dict)
        file_source = PurePosixPath(str(file_entry["source"]))
        for tree_entry in config["trees"]:
            assert isinstance(tree_entry, dict)
            tree_source = PurePosixPath(str(tree_entry["source"]))
            try:
                relative = file_source.relative_to(tree_source)
            except ValueError:
                continue
            if not _excluded(Path(relative.as_posix()), list(tree_entry.get("exclude", []))):
                raise SkillBuildError(
                    f"Source Map同一源文件被复制到多个发行路径：{file_source}"
                )
    return config


def load_source_map(path: Path = SOURCE_MAP) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillBuildError(f"无法读取Source Map：{error}") from error
    return _validate_source_map(config)


def _record(path: Path, root: Path, *, provenance_path: str | None = None) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SkillBuildError(f"白名单源文件不存在或为符号链接：{path}")
    if is_credential_file(path.name):
        raise SkillBuildError(f"白名单源文件不得为凭据：{path}")
    relative = provenance_path or path.relative_to(root).as_posix()
    digest = sha256(path.read_bytes()).hexdigest()
    return {
        "path": relative,
        "size": path.stat().st_size,
        "sha256": digest,
        "executable": bool(path.stat().st_mode & stat.S_IXUSR),
    }


def _catalog_archive_root(
    repo_root: Path,
    config: dict[str, object],
    versions_root: Path | None,
) -> Path:
    if versions_root is not None:
        return versions_root.expanduser().resolve()
    catalog = config["catalog"]
    assert isinstance(catalog, dict)
    return (repo_root / str(catalog["source_root"])).resolve()


def _published_provenance(path: Path, archive_root: Path) -> str:
    """Return an archive-stable source identity without exposing its host path."""
    try:
        relative = path.relative_to(archive_root).as_posix()
    except ValueError as error:
        raise SkillBuildError(f"正式Catalog来源不在受控versions_root内：{path}") from error
    return (PurePosixPath("versions") / PurePosixPath(relative)).as_posix()


def _excluded(relative: Path, patterns: list[str]) -> bool:
    text = relative.as_posix()
    # ``data`` normally remains excluded from a portable Skill.  The one
    # exception is the checked-in deterministic MC matrix required by the
    # Pricer numerical core.  It is not market history and never changes at
    # runtime; releasing it avoids a hidden local-file dependency.
    if "data" in relative.parts and text != _FROZEN_NUMERICAL_ASSET:
        return True
    if is_development_artifact_path(relative):
        return True
    if any(part in _EXCLUDED_PATH_PARTS or _finder_copy_name(part) for part in relative.parts):
        return True
    if relative.suffix == ".pyc" or relative.name == ".DS_Store" or is_credential_file(relative.name):
        return True
    if _finder_copy_name(relative.name):
        return True
    return any(fnmatch(text, pattern) or fnmatch(relative.name, pattern) for pattern in patterns)


def _copy_file(source: Path, target: Path, *, transform: str | None = None) -> None:
    if source.is_symlink() or not source.is_file():
        raise SkillBuildError(f"白名单源文件不存在或为符号链接：{source}")
    if is_credential_file(source.name):
        raise SkillBuildError(f"白名单源文件不得为凭据：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if transform == "skill_links":
        text = source.read_text(encoding="utf-8")
        text = text.replace("](CONTEXT.md)", "](references/context.md)")
        for module in MODULES:
            text = text.replace(f"](modules/{module}/module-guide.md)", f"](references/module-guides/{module}.md)")
        target.write_text(text, encoding="utf-8", newline="")
        shutil.copymode(source, target)
        return
    if transform == "page_theme_links":
        text = source.read_text(encoding="utf-8")
        source_link = "../../../designer/assets/themes/designer-token-vars.css"
        target_link = "../../../designer/themes/designer-token-vars.css"
        if source_link not in text:
            raise SkillBuildError("回测页面缺少可转换的Designer主题链接")
        target.write_text(text.replace(source_link, target_link), encoding="utf-8", newline="")
        shutil.copymode(source, target)
        return
    shutil.copy2(source, target)


def _tree_files(source: Path, patterns: list[str]) -> list[Path]:
    if source.is_symlink() or not source.is_dir():
        raise SkillBuildError(f"白名单源目录不存在或为符号链接：{source}")
    files: list[Path] = []
    for directory, names, filenames in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in [*names, *filenames]:
            if (parent / name).is_symlink():
                raise SkillBuildError(f"白名单源目录包含符号链接：{parent / name}")
        for name in filenames:
            item = parent / name
            relative = item.relative_to(source)
            if _excluded(relative, patterns):
                continue
            files.append(item)
    return sorted(files)


def _copy_tree(source: Path, target: Path, patterns: list[str], *, transform: str | None = None) -> list[Path]:
    files = _tree_files(source, patterns)
    for item in files:
        relative = item.relative_to(source).as_posix()
        item_transform = transform if transform == "page_theme_links" and relative == "ui/style.css" else None
        _copy_file(item, target / item.relative_to(source), transform=item_transform)
    return files


def _resolve_catalog(
    repo_root: Path,
    config: dict[str, object],
    catalog_version: str,
    *,
    versions_root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    try:
        require_release_version(catalog_version)
    except ValueError as error:
        raise SkillBuildError(str(error)) from error
    catalog = config["catalog"]
    assert isinstance(catalog, dict)
    archive_root = _catalog_archive_root(repo_root, config, versions_root)
    source = archive_root / catalog_version / str(catalog["file_name"])
    if source.is_symlink() or not source.is_file():
        raise SkillBuildError(f"缺少已签发知识源清单：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SkillBuildError(f"知识源清单不是有效JSON：{source}") from error
    if payload.get("release_version") != catalog_version:
        raise SkillBuildError("知识源清单文件名、内容与所选版本不一致")
    if "release_id" in payload:
        raise SkillBuildError("知识源清单不得包含release_id")
    if (
        payload.get("manifest_type") != "KnowledgeSourceManifest"
        or payload.get("formal_release") is not True
        or payload.get("executable") is not True
        or "candidate_status" in payload
    ):
        raise SkillBuildError("技术候选知识源清单不得用于Skill构建，必须先完成正式签发")
    if not isinstance(payload.get("published_by"), str) or not payload["published_by"].strip() or not isinstance(payload.get("published_at"), str):
        raise SkillBuildError("正式知识源清单必须记录published_by与published_at")
    if not isinstance(payload.get("products"), dict) or len(payload["products"]) != 65:
        raise SkillBuildError("知识源清单必须含65个产品修订映射")
    try:
        validate_published_catalog(repo_root, catalog_version, versions_root=archive_root)
    except ValueError as error:
        raise SkillBuildError(f"知识源清单正式版本链校验失败：{error}") from error
    return source, payload


def _protocol_id(repo_root: Path) -> str:
    source = repo_root / "core" / "src" / "runtime" / "protocol" / "tool_catalog.py"
    protocol_id = _tool_catalog_protocol_id(source)
    if protocol_id is None:
        raise SkillBuildError("无法从Tool Catalog确定唯一protocol_id")
    if protocol_id != PROTOCOL_ID:
        raise SkillBuildError(
            f"Core Tool Catalog协议标识必须为{PROTOCOL_ID}，当前为{protocol_id}"
        )
    return protocol_id


def _source_records(
    repo_root: Path,
    config: dict[str, object],
    catalog_path: Path | None,
    *,
    versions_root: Path | None = None,
) -> list[dict[str, object]]:
    records: dict[str, dict[str, object]] = {}

    def add(path: Path) -> None:
        record = _record(path, repo_root)
        records[str(record["path"])] = record

    for item in config["files"]:
        assert isinstance(item, dict)
        add(repo_root / str(item["source"]))
    for item in config["trees"]:
        assert isinstance(item, dict)
        source = repo_root / str(item["source"])
        for path in _tree_files(source, list(item.get("exclude", []))):
            add(path)
    if catalog_path is not None:
        archive_root = _catalog_archive_root(repo_root, config, versions_root)

        def add_published(path: Path) -> None:
            record = _record(
                path,
                repo_root,
                provenance_path=_published_provenance(path, archive_root),
            )
            records[str(record["path"])] = record

        add_published(catalog_path)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        version_root = catalog_path.parents[1]
        for relative in catalog["product_manifest_refs"].values():
            manifest_path = version_root / str(relative)
            add_published(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for filename in manifest["snapshot_files"].values():
                add_published(manifest_path.parent / filename)
    return sorted(records.values(), key=lambda item: str(item["path"]))


def _source_output_paths(
    repo_root: Path,
    config: dict[str, object],
    catalog_path: Path | None,
    *,
    versions_root: Path | None = None,
) -> set[str]:
    paths = {str(config["catalog"]["target"])}
    for item in config["files"]:
        assert isinstance(item, dict)
        _record(repo_root / str(item["source"]), repo_root)
        paths.add(str(item["target"]))
    for item in config["trees"]:
        assert isinstance(item, dict)
        source = repo_root / str(item["source"])
        target = PurePosixPath(str(item["target"]))
        for path in _tree_files(source, list(item.get("exclude", []))):
            paths.add((target / path.relative_to(source).as_posix()).as_posix())
    if catalog_path is not None:
        _published_provenance(catalog_path, _catalog_archive_root(repo_root, config, versions_root))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        version_root = catalog_path.parents[1]
        for relative in catalog["product_manifest_refs"].values():
            manifest_path = version_root / str(relative)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            package_dir = PurePosixPath("scripts") / PurePosixPath(str(relative)).parent
            paths.add((package_dir / "product-manifest.json").as_posix())
            paths.update((package_dir / filename).as_posix() for filename in manifest["snapshot_files"].values())
    return paths


def _working_tree_catalog(repo_root: Path) -> dict[str, object]:
    """以当前references生成仅开发环境可执行的技术候选Catalog，不触碰versions历史。"""
    sources = {
        name: sha256((repo_root / "references" / name).read_bytes()).hexdigest()
        for name in ("optionlist.md", "optionlib.md", "optionreg.py")
    }
    optionlist = (repo_root / "references" / "optionlist.md").read_text(encoding="utf-8")
    product_ids = re.findall(r"^\|\s*\d+\s*\|.*?\|\s*(\d+\.\d+)\s*\|", optionlist, re.MULTILINE)
    if len(product_ids) != 65 or len(set(product_ids)) != 65:
        raise SkillBuildError("working-tree Catalog必须从OptionList解析出65个唯一产品ID")
    revisions = registry_product_revisions(repo_root / "references" / "optionreg.py")
    if list(revisions) != product_ids:
        raise SkillBuildError("working-tree知识源修订映射与OptionList顺序不一致")
    return {
        "manifest_type": "KnowledgeSourceCandidate",
        "release_status": "technical_candidate",
        "formal_release": False,
        "execution_scope": "development_only",
        "executable": True,
        "release_version": DEVELOPMENT_ID,
        "catalog_source": "working-tree",
        "product_count": 65,
        "product_ids": product_ids,
        "product_revisions": revisions,
        "source_file_sha256": sources,
    }


def _copy_published_catalog_snapshots(
    repo_root: Path,
    catalog_version: str,
    staged: Path,
    *,
    versions_root: Path | None = None,
) -> None:
    """把正式产品源快照装入包，并以其Payoff资产覆盖活目录副本。"""
    try:
        catalog, snapshots = load_published_catalog_snapshots(
            repo_root, catalog_version, versions_root=versions_root
        )
    except ValueError as error:
        raise SkillBuildError(f"无法读取正式产品源快照：{error}") from error
    for product_id, snapshot in snapshots.items():
        manifest_source = Path(snapshot["default_json_path"]).parent / "product-manifest.json"
        package_dir = staged / "scripts" / "knowledger" / "products" / product_id
        package_dir.mkdir(parents=True, exist_ok=True)
        _copy_file(manifest_source, package_dir / "product-manifest.json")
        manifest = snapshot["manifest"]
        for filename in manifest["snapshot_files"].values():
            _copy_file(manifest_source.parent / filename, package_dir / filename)
        name = str(snapshot["product"]["identity"]["name_zh"])
        _copy_file(Path(snapshot["default_json_path"]), staged / "assets" / "payoffer" / "figures" / "json" / f"{name}.json")
        _copy_file(Path(snapshot["default_svg_path"]), staged / "assets" / "payoffer" / "figures" / "svg" / f"{name}.svg")


def verify_source_snapshot(
    skill_root: Path,
    *,
    repo_root: Path = ROOT,
    versions_root: Path | None = None,
) -> list[str]:
    """确认候选Manifest仍对应当前Source Map选择的开发源。"""
    try:
        manifest = json.loads((skill_root / "capability-manifest.json").read_text(encoding="utf-8"))
        repo_root = repo_root.expanduser().resolve()
        source_map_path = _source_map_path(repo_root)
        source_map_hash = sha256(source_map_path.read_bytes()).hexdigest()
        config = load_source_map(source_map_path)
        technical = manifest.get("release_status") == "technical_candidate"
        if technical:
            catalog_path = None
            expected_catalog = _working_tree_catalog(repo_root)
        else:
            catalog_path, expected_catalog = _resolve_catalog(
                repo_root,
                config,
                str(manifest["catalog_version"]),
                versions_root=versions_root,
            )
        records = _source_records(repo_root, config, catalog_path, versions_root=versions_root)
        expected_paths = _source_output_paths(repo_root, config, catalog_path, versions_root=versions_root)
        actual_paths = {str(item["path"]) for item in content_tree_entries(skill_root)}
    except (KeyError, OSError, json.JSONDecodeError, SkillBuildError, SkillVerificationError) as error:
        return [f"无法核对候选来源快照：{error}"]
    hashes = {str(item["path"]): str(item["sha256"]) for item in records}
    errors: list[str] = []
    if manifest.get("source_map_hash") != source_map_hash:
        errors.append("候选Source Map已与当前白名单漂移")
    if manifest.get("source_tree_hash") != tree_hash(records):
        errors.append("候选Source Tree Hash已与当前白名单源漂移")
    if manifest.get("source_content_hashes") != hashes:
        errors.append("候选Source文件哈希已与当前白名单源漂移")
    if actual_paths != expected_paths:
        errors.append("候选文件集合已与当前白名单映射漂移")
    if technical:
        try:
            actual_catalog = json.loads((skill_root / str(config["catalog"]["target"])).read_text(encoding="utf-8"))
        except (KeyError, OSError, json.JSONDecodeError) as error:
            errors.append(f"无法读取working-tree Catalog：{error}")
        else:
            if actual_catalog != expected_catalog:
                errors.append("working-tree Catalog已与当前唯一references源漂移")
    return errors


def _replace_candidate(staged: Path, target: Path, replace: bool) -> Path:
    conflicts = _candidate_conflicts(target.parent, target.name)
    if conflicts:
        raise SkillBuildError(
            "候选目录存在同步冲突副本，需先可恢复迁移："
            + ", ".join(path.name for path in conflicts)
        )
    if target.exists() and not replace:
        raise SkillBuildError(f"候选目录已存在：{target}；使用--replace整体重建生成目录")
    backup: Path | None = None
    if target.exists():
        if target.is_symlink() or not target.is_dir() or target.name != "option-helper":
            raise SkillBuildError(f"拒绝替换非候选目录：{target}")
        backup = target.parent / f".option-helper-backup-{uuid.uuid4().hex}"
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except OSError:
        if backup and backup.exists():
            os.replace(backup, target)
        raise
    conflicts = _candidate_conflicts(target.parent, target.name)
    if conflicts:
        os.replace(target, staged)
        if backup and backup.exists():
            os.replace(backup, target)
        raise SkillBuildError(
            "候选目录出现同步冲突副本，已回滚："
            + ", ".join(path.name for path in conflicts)
        )
    if backup:
        shutil.rmtree(backup)
    return target


def build_skill(
    output_root: Path,
    *,
    catalog_version: str | None = None,
    candidate: bool = False,
    replace: bool = False,
    repo_root: Path = ROOT,
    versions_root: Path | None = None,
) -> Path:
    repo_root = repo_root.expanduser().resolve()
    required_designer_sources = {
        "modules/designer/src/comparison_renderer.py": "scripts/modules/designer/comparison_renderer.py",
        "modules/designer/assets/templates/multicard-standard.template.json": "assets/designer/templates/multicard-standard.template.json",
        "modules/designer/assets/templates/multicard.html": "assets/designer/templates/multicard.html",
        "modules/designer/assets/templates/multireport-standard.template.json": "assets/designer/templates/multireport-standard.template.json",
        "modules/designer/assets/templates/multireport.html": "assets/designer/templates/multireport.html",
    }
    missing_designer_sources = [
        source for source in required_designer_sources if not (repo_root / source).is_file()
    ]
    if missing_designer_sources:
        raise SkillBuildError(
            "缺少Designer正式多结构发行源文件：" + ", ".join(missing_designer_sources)
        )
    source_map_path = _source_map_path(repo_root)
    config = load_source_map(source_map_path)
    if candidate == (catalog_version is not None):
        raise SkillBuildError("必须且只能选择--candidate或--catalog-version")
    catalog_path: Path | None
    if candidate:
        catalog_path = None
        catalog_payload = _working_tree_catalog(repo_root)
        catalog_source = "working-tree"
    else:
        assert catalog_version is not None
        catalog_path, catalog_payload = _resolve_catalog(
            repo_root, config, catalog_version, versions_root=versions_root
        )
        catalog_source = _published_provenance(
            catalog_path,
            _catalog_archive_root(repo_root, config, versions_root),
        )
    output_root = output_root.expanduser().resolve()
    if output_root == output_root.anchor:
        raise SkillBuildError("拒绝把候选构建到文件系统根目录")
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / "option-helper"
    if target.exists() and not replace:
        raise SkillBuildError(f"候选目录已存在：{target}；使用--replace整体重建生成目录")
    staging_parent = Path(tempfile.mkdtemp(prefix=".option-helper-staging-", dir=output_root))
    staged = staging_parent / "option-helper"
    staged.mkdir()
    try:
        source_records = _source_records(repo_root, config, catalog_path, versions_root=versions_root)
        for item in config["files"]:
            assert isinstance(item, dict)
            source = repo_root / str(item["source"])
            _copy_file(source, staged / str(item["target"]), transform=item.get("transform"))
        for item in config["trees"]:
            assert isinstance(item, dict)
            source = repo_root / str(item["source"])
            _copy_tree(
                source,
                staged / str(item["target"]),
                list(item.get("exclude", [])),
                transform=item.get("transform"),
            )
        (staged / "assets/designer/templates").mkdir(parents=True, exist_ok=True)
        catalog = config["catalog"]
        assert isinstance(catalog, dict)
        catalog_target = staged / str(catalog["target"])
        if catalog_path is None:
            catalog_target.parent.mkdir(parents=True, exist_ok=True)
            catalog_target.write_text(json.dumps(catalog_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
        else:
            _copy_file(catalog_path, catalog_target)
            _copy_published_catalog_snapshots(
                repo_root,
                str(catalog_version),
                staged,
                versions_root=versions_root,
            )
        entries = content_tree_entries(staged)
        hashes = {str(item["path"]): str(item["sha256"]) for item in entries}
        shared_entries = shared_payload_entries(entries)
        shared_hashes = {str(item["path"]): str(item["sha256"]) for item in shared_entries}
        contract_entries = [item for item in entries if str(item["path"]).startswith("scripts/runtime/contracts/")]
        if not contract_entries:
            raise SkillBuildError("候选包缺少共享合同核心")
        manifest = {
            "manifest_schema": CAPABILITY_MANIFEST_SCHEMA,
            "package_status": "candidate",
            "capability_version": DEVELOPMENT_ID if candidate else str(catalog_version),
            "release_status": "technical_candidate" if candidate else "candidate_from_published_catalog",
            "formal_release": False,
            "execution_scope": "development_only" if candidate else "candidate_verification",
            "catalog_version": DEVELOPMENT_ID if candidate else catalog_version,
            "catalog_source": catalog_source,
            "protocol_id": _protocol_id(repo_root),
            "design_system_id": "optionhelper.design-system",
            "hash_spec_id": HASH_SPEC_ID,
            "package_kind": "skill",
            "skill_version": SKILL_VERSION,
            "modules": list(MODULES),
            "page_modules": list(SKILL_PAGE_MODULES),
            "contract_core_hash": tree_hash(contract_entries),
            "tool_catalog_hash": hashes.get("scripts/runtime/protocol/tool_catalog.py"),
            "content_tree_hash": tree_hash(entries),
            "content_hashes": hashes,
            "content_tree_entries": entries,
            "module_content_hashes": _module_hashes(entries),
            "shared_payload_hash": tree_hash(shared_entries),
            "shared_payload_entries": shared_entries,
            "shared_content_hashes": shared_hashes,
            "source_map_hash": sha256(source_map_path.read_bytes()).hexdigest(),
            "source_tree_hash": tree_hash(source_records),
            "source_content_hashes": {str(item["path"]): str(item["sha256"]) for item in source_records},
        }
        (staged / "capability-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
        errors = [
            *verify_skill(staged),
            *verify_source_snapshot(staged, repo_root=repo_root, versions_root=versions_root),
        ]
        if not errors:
            errors.extend(probe_runtime(staged))
        if errors:
            raise SkillBuildError("候选包未通过构建验收：\n" + "\n".join(errors))
        return _replace_candidate(staged, target, replace)
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)


def write_zip(skill_root: Path) -> Path:
    skill_root = skill_root.expanduser().resolve()
    errors = verify_skill(skill_root)
    if errors:
        raise SkillBuildError("候选目录未通过打包验收：\n" + "\n".join(errors))
    archive = skill_root.parent / skill_archive_name()
    conflicts = _candidate_conflicts(skill_root.parent, skill_root.name)
    if conflicts:
        raise SkillBuildError(
            "候选ZIP存在同步冲突副本，需先可恢复迁移："
            + ", ".join(path.name for path in conflicts)
        )
    temporary = archive.with_name(f".option-helper-{uuid.uuid4().hex}.zip")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted((item for item in skill_root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(skill_root).as_posix()):
            relative = path.relative_to(skill_root).as_posix()
            info = zipfile.ZipInfo(f"option-helper/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(temporary, archive)
    errors = verify_zip(archive)
    if errors:
        raise SkillBuildError("候选ZIP未通过解压验收：\n" + "\n".join(errors))
    conflicts = _candidate_conflicts(skill_root.parent, skill_root.name)
    if conflicts:
        raise SkillBuildError("候选ZIP出现同步冲突副本：" + ", ".join(path.name for path in conflicts))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description="构建未签发的OptionHelper Skill候选包")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "candidates" / "skill")
    catalog_mode = parser.add_mutually_exclusive_group(required=True)
    catalog_mode.add_argument("--catalog-version", help=f"已签发知识源清单，仅允许{RELEASE_VERSION}")
    catalog_mode.add_argument("--candidate", action="store_true", help="从当前references构建仅开发环境可执行的技术候选")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    skill = build_skill(args.output, catalog_version=args.catalog_version, candidate=args.candidate, replace=args.replace)
    print(skill)
    if args.zip:
        print(write_zip(skill))


if __name__ == "__main__":
    main()
