#!/usr/bin/env python3
"""验证OptionHelper Skill候选包的结构、内容树和安装边界。"""

from __future__ import annotations

import argparse
import ast
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
from threading import Thread
from typing import Callable, Mapping
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
import zipfile


MODULES = ("datafetcher", "recommender", "payoffer", "pricer", "backtester", "reporter", "designer")
PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")
HASH_SPEC_VERSION = "content-tree-sha256-nfc-v1"
PROTOCOL_VERSION = "v1.2"
_TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".html", ".js", ".css", ".command", ".bat", ".lock"}
_LOCAL_ENVIRONMENT_MARKER = "Machine" + "Learning"
_SECRET_PATTERNS = (
    re.compile(r"(?i)(access[_ -]?token|refresh[_ -]?token|password|passwd|api[_ -]?key|client[_ -]?secret)\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"[0-9a-f]{40,}\.signs_[A-Za-z0-9_-]+"),
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_HTML_LINK = re.compile(r"\b(?:href|src)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_CSS_LINK = re.compile(r"(?:@import\s+(?:url\()?|url\()\s*['\"]?([^'\"\)\s]+)", re.IGNORECASE)
_CREDENTIAL_NAMES = {".env", "credentials.json", "credentials.yaml", "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_DEVELOPMENT_PATH_PARTS = frozenset({"dev", "development", "docs", "eval", "evals", "example", "examples", "test", "tests"})


class SkillVerificationError(RuntimeError):
    pass


def is_credential_file(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _CREDENTIAL_NAMES or lowered.startswith(".env.") or lowered.endswith(_CREDENTIAL_SUFFIXES)


def is_finder_copy_name(name: str) -> bool:
    return bool(re.search(r"\s\d+(?:\.[^.]+)+$", name))


def is_development_artifact_path(path: str | Path) -> bool:
    """Return whether a package-relative path belongs to development-only material."""
    return any(part.casefold() in _DEVELOPMENT_PATH_PARTS for part in Path(path).parts)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    raw = path.relative_to(root).as_posix()
    normalized = unicodedata.normalize("NFC", raw)
    if raw != normalized:
        raise SkillVerificationError(f"路径不是NFC规范：{raw}")
    return normalized


def _walk_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise SkillVerificationError(f"Skill根目录无效或为符号链接：{root}")
    seen: dict[str, str] = {}
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in [*names, *filenames]:
            child = parent / name
            if child.is_symlink():
                raise SkillVerificationError(f"Skill包不得包含符号链接：{_relative(root, child)}")
            relative = _relative(root, child)
            key = relative.casefold()
            if key in seen and seen[key] != relative:
                raise SkillVerificationError(f"Skill包存在大小写冲突：{seen[key]} 与 {relative}")
            seen[key] = relative
        files.extend(parent / name for name in filenames if (parent / name).is_file())
    return sorted(files, key=lambda item: _relative(root, item))


def content_tree_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in _walk_files(root):
        relative = _relative(root, path)
        if relative == "capability-manifest.json":
            continue
        mode = path.stat().st_mode
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "executable": bool(mode & stat.S_IXUSR),
            }
        )
    return entries


def tree_hash(entries: list[dict[str, object]]) -> str:
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _hashes(entries: list[dict[str, object]]) -> dict[str, str]:
    return {str(item["path"]): str(item["sha256"]) for item in entries}


def _selected_entries(entries: list[dict[str, object]], module: str) -> list[dict[str, object]]:
    prefixes = [f"scripts/modules/{module}/", f"assets/pages/{module}/"]
    exact = {f"references/module-guides/{module}.md"}
    if module == "payoffer":
        prefixes.append("assets/payoffer/")
    if module == "designer":
        prefixes.append("assets/designer/")
    if module == "reporter":
        prefixes.append("assets/reporter/")
    return [item for item in entries if str(item["path"]) in exact or any(str(item["path"]).startswith(prefix) for prefix in prefixes)]


def _module_hashes(entries: list[dict[str, object]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for module in MODULES:
        selected = _selected_entries(entries, module)
        if not selected:
            raise SkillVerificationError(f"模块{module}没有进入候选包")
        result[module] = tree_hash(selected)
    return result


def _frontmatter_errors(skill: Path) -> list[str]:
    lines = skill.read_text(encoding="utf-8").splitlines()
    if len(lines) > 500:
        return ["SKILL.md超过500行"]
    if not lines or lines[0] != "---":
        return ["SKILL.md缺少YAML Frontmatter"]
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return ["SKILL.md Frontmatter未闭合"]
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if ":" not in line:
            return ["SKILL.md Frontmatter只能包含简单name和description字段"]
        key, value = line.split(":", 1)
        if key in fields:
            return [f"SKILL.md Frontmatter重复字段：{key}"]
        fields[key.strip()] = value.strip()
    errors: list[str] = []
    if set(fields) != {"name", "description"}:
        errors.append("SKILL.md Frontmatter只能包含name和description")
    if fields.get("name") != "option-helper":
        errors.append("SKILL.md名称必须为option-helper")
    if not fields.get("description"):
        errors.append("SKILL.md description不能为空")
    return errors


def _link_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _walk_files(root):
        if path.suffix.lower() not in {".md", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        pattern = _MARKDOWN_LINK if path.suffix == ".md" else _HTML_LINK if path.suffix == ".html" else _CSS_LINK
        for raw in pattern.findall(text):
            target = raw.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith(("#", "data:", "mailto:", "tel:", "javascript:", "http:", "https:", "//")):
                continue
            if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", target):
                errors.append(f"绝对资源链接：{_relative(root, path)} -> {raw}")
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                errors.append(f"资源链接越出Skill根目录：{_relative(root, path)} -> {raw}")
                continue
            if not resolved.exists():
                errors.append(f"资源链接不存在：{_relative(root, path)} -> {raw}")
    return errors


def _code_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _walk_files(root):
        relative = _relative(root, path)
        parts = relative.split("/")
        if any(is_finder_copy_name(part) for part in parts):
            errors.append(f"候选包包含Finder副本：{relative}")
        if is_development_artifact_path(relative):
            errors.append(f"候选包包含开发内容：{relative}")
        if "__pycache__" in parts or path.suffix == ".pyc" or path.name == ".DS_Store":
            errors.append(f"候选包包含运行缓存：{relative}")
        if is_credential_file(path.name):
            errors.append(f"候选包包含凭据文件：{relative}")
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _LOCAL_ENVIRONMENT_MARKER in text:
            errors.append(f"发行包不得指定本机Python环境名：{relative}")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"疑似明文Secret：{relative}")
        if re.search(r"(?:/Users/|/opt/anaconda|[A-Za-z]:\\Users\\)", text):
            errors.append(f"发行包包含机器绝对路径：{relative}")
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as error:
            errors.append(f"Python语法错误：{relative}:{error.lineno}")
            continue
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
        imports.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        if relative.startswith("scripts/runtime/"):
            for module_name in imports:
                if module_name == "modules" or module_name.startswith("modules."):
                    errors.append(f"共享运行时反向依赖模块：{relative} -> {module_name}")
        if relative.startswith("scripts/modules/"):
            module = parts[2] if len(parts) > 2 else ""
            for module_name in imports:
                top = module_name.split(".", 1)[0]
                if top in {"assets", "core", "products", "references"}:
                    errors.append(f"模块依赖物理目录：{relative} -> {module_name}")
                if top == "modules" and not (module_name == f"modules.{module}" or module_name.startswith(f"modules.{module}.")):
                    errors.append(f"模块越界依赖：{relative} -> {module_name}")
    return errors


def _structure_errors(root: Path) -> list[str]:
    errors: list[str] = []
    allowed = {"SKILL.md", "README.md", "capability-manifest.json", "references", "scripts", "assets", "LICENSES"}
    actual = {path.name for path in root.iterdir()} if root.is_dir() else set()
    errors.extend(f"发行根目录存在未允许项：{name}" for name in sorted(actual - allowed))
    required = [
        "SKILL.md", "README.md", "capability-manifest.json", "references/context.md", "references/optionlist.md",
        "references/optionlib.md", "references/knowledger-manager.md", "scripts/tool_entry.py",
        "scripts/module_host.py", "scripts/environment_check.py", "scripts/start-pages.command",
        "scripts/start-pages.bat", "scripts/requirements.lock", "scripts/knowledger/__init__.py",
        "scripts/knowledger/optionreg.py", "scripts/knowledger/catalog-version.json", "scripts/runtime",
        "scripts/modules", "assets/icons", "assets/payoffer/figures/json", "assets/payoffer/figures/svg",
        "assets/designer/themes", "assets/designer/vendor", "LICENSES",
    ]
    for module in MODULES:
        required.extend((f"references/module-guides/{module}.md", f"scripts/modules/{module}/__init__.py", f"scripts/modules/{module}/service.py", f"scripts/modules/{module}/config.py", f"scripts/modules/{module}/models.py"))
    for module in PAGE_MODULES:
        required.extend((f"assets/pages/{module}/{module}.html", f"assets/pages/{module}/{module}.css", f"assets/pages/{module}/{module}.js"))
    errors.extend(f"缺少{relative}" for relative in required if not (root / relative).exists())
    modules_root = root / "scripts" / "modules"
    if modules_root.is_dir():
        actual_modules = {path.name for path in modules_root.iterdir() if path.is_dir()}
        if actual_modules != set(MODULES):
            errors.append(f"内部模块集合错误：{sorted(actual_modules)}")
    pages_root = root / "assets" / "pages"
    if pages_root.is_dir():
        actual_pages = {path.name for path in pages_root.iterdir() if path.is_dir()}
        if actual_pages != set(PAGE_MODULES):
            errors.append(f"操作页面集合错误：{sorted(actual_pages)}")
    figures = root / "assets" / "payoffer" / "figures"
    if figures.is_dir():
        json_names = {path.stem for path in (figures / "json").glob("*.json")}
        svg_names = {path.stem for path in (figures / "svg").glob("*.svg")}
        if len(json_names) != 65 or json_names != svg_names:
            errors.append("Payoffer默认JSON/SVG必须为同名65对")
    nested_skills = [path for path in root.rglob("SKILL.md") if path != root / "SKILL.md"]
    errors.extend(f"内部模块不得包含嵌套SKILL：{_relative(root, path)}" for path in nested_skills)
    return errors


def _store_boundary_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for name in ("data", "result"):
        if (root / name).exists():
            errors.append(f"Skill安装目录不得包含{name}/")
    bootstrap = root / "scripts" / "runtime" / "bootstrap.py"
    if bootstrap.is_file():
        text = bootstrap.read_text(encoding="utf-8", errors="ignore")
        if any(value not in text for value in ("OPTIONHELPER_RUNTIME_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT")):
            errors.append("运行时未声明外部Runtime、DataStore和ResultStore环境变量")
        if "Path.cwd() / \"data\"" in text or "Path.cwd() / \"result\"" in text:
            errors.append("共享Core仍可能把data/result写入启动目录，未满足外部Store边界")
    skill = root / "SKILL.md"
    if skill.is_file():
        text = skill.read_text(encoding="utf-8")
        if "environment_check.py" not in text or "OPTIONHELPER_DATA_ROOT" not in text or "OPTIONHELPER_RESULT_ROOT" not in text:
            errors.append("根SKILL未声明依赖与外部Store预检")
    return errors


def _launcher_errors(root: Path) -> list[str]:
    errors: list[str] = []
    command = root / "scripts" / "start-pages.command"
    batch = root / "scripts" / "start-pages.bat"
    if command.is_file():
        text = command.read_text(encoding="utf-8", errors="ignore")
        required = ("module_host.py", "OPTIONHELPER_PYTHON", "environment_check.py", "python3")
        if any(value not in text for value in required) or _LOCAL_ENVIRONMENT_MARKER in text:
            errors.append("macOS启动器必须采用可验证的通用Python策略调用module_host.py")
    if batch.is_file():
        text = batch.read_text(encoding="utf-8", errors="ignore")
        required = ("module_host.py", "OPTIONHELPER_PYTHON", "environment_check.py", "where python")
        if any(value not in text for value in required) or _LOCAL_ENVIRONMENT_MARKER in text:
            errors.append("Windows启动器必须采用可验证的通用Python策略调用module_host.py")
    return errors


def _capability_interface_errors(root: Path) -> list[str]:
    """检查12.1已冻结的跨模块Capability接口确实随包进入发行物。"""
    required = {
        "assets/pages/module-host-bridge.js": "module-host bridge",
        "assets/pages/datafetcher/datafetcher.js": "DataFetcher动态下载页面Bridge",
        "assets/designer/vendor/echarts.min.js": "Reporter portable ECharts资源",
        "scripts/tool_entry.py": "Core v2 Tool入口",
        "scripts/runtime/protocol/models.py": "CallerContext协议模型",
        "scripts/runtime/protocol/module_host.py": "ModuleHost v2协议",
        "scripts/runtime/adapters/local_host.py": "项目级本机Host适配",
        "scripts/runtime/adapters/local_store.py": "ModuleRunRef外部锚ResultStore",
        "scripts/runtime/protocol/schemas/caller-context.schema.json": "CallerContext Schema",
        "scripts/runtime/protocol/schemas/module-host-context.schema.json": "ModuleHostContext Schema",
        "scripts/runtime/protocol/schemas/run-ref.schema.json": "RunRef v1.2 Schema",
        "scripts/modules/pricer/engines/pricing_core/optionhelper_core.py": "Pricer pricing_core",
        "scripts/modules/datafetcher/market_conventions.py": "DataFetcher中国市场复权约定",
        "scripts/modules/reporter/artifact_validator.py": "Reporter portable资源校验",
        "scripts/modules/reporter/evidence_resolver.py": "Reporter RunRef外部锚解析",
        "scripts/modules/reporter/export_service.py": "Reporter portable资源落盘",
        "scripts/modules/reporter/models.py": "Reporter RunRef v1.2模型",
        "scripts/modules/reporter/report_unit_builder.py": "Reporter RunRef证据单元",
        "scripts/modules/reporter/service.py": "Reporter ResultSelectionPort适配",
    }
    errors = [f"缺少{label}：{relative}" for relative, label in required.items() if not (root / relative).is_file()]
    bridge = root / "assets" / "pages" / "module-host-bridge.js"
    if bridge.is_file():
        bridge_text = bridge.read_text(encoding="utf-8", errors="ignore")
        for token, label in (
            ("ModuleHostContext", "ModuleHostContext"),
            ("X-OptionHelper-Request-Id", "幂等request id"),
            ("dataAssetDownloadId", "DataFetcher动态下载"),
            ("optionhelper.module-download-error", "DataFetcher下载错误事件"),
        ):
            if token not in bridge_text:
                errors.append(f"module-host bridge未声明{label}")
    caller_schema = root / "scripts" / "runtime" / "protocol" / "schemas" / "caller-context.schema.json"
    if caller_schema.is_file():
        try:
            caller_payload = json.loads(caller_schema.read_text(encoding="utf-8"))
            if caller_payload.get("title") != "CallerContext" or "request_id" not in caller_payload.get("properties", {}):
                errors.append("CallerContext Schema未声明request_id幂等键")
        except json.JSONDecodeError:
            errors.append("CallerContext Schema不是有效JSON")
    schema = root / "scripts" / "runtime" / "protocol" / "schemas" / "module-host-context.schema.json"
    if schema.is_file():
        try:
            schema_payload = json.loads(schema.read_text(encoding="utf-8"))
            token_pattern = schema_payload.get("properties", {}).get("capability_token", {}).get("pattern", "")
            if schema_payload.get("title") != "ModuleHostContext":
                errors.append("ModuleHostContext Schema标题无效")
            if not str(token_pattern).startswith("^v2"):
                errors.append("ModuleHostContext Schema未锁定v2 capability_token")
        except json.JSONDecodeError:
            errors.append("ModuleHostContext Schema不是有效JSON")
    run_ref_schema = root / "scripts" / "runtime" / "protocol" / "schemas" / "run-ref.schema.json"
    if run_ref_schema.is_file():
        try:
            run_ref_payload = json.loads(run_ref_schema.read_text(encoding="utf-8"))
            module_refs = [
                item for item in run_ref_payload.get("oneOf", [])
                if isinstance(item, dict) and "module" in item.get("properties", {})
            ]
            required_fields = {
                "module", "tenant_id", "task_id", "run_id",
                "expected_semantic_result_hash", "expected_artifact_manifest_hash",
            }
            if len(module_refs) != 1 or set(module_refs[0].get("required", ())) != required_fields:
                errors.append("RunRef Schema未锁定ModuleRunRef v1.2六字段")
        except json.JSONDecodeError:
            errors.append("RunRef Schema不是有效JSON")
    conventions = root / "scripts" / "modules" / "datafetcher" / "market_conventions.py"
    if conventions.is_file():
        text = conventions.read_text(encoding="utf-8", errors="ignore")
        if 'adjustment: str = "auto"' not in text or "forward_adjusted_for_adj_fields" not in text:
            errors.append("DataFetcher未声明中国股票/ETF auto前复权约定")
    reporter = root / "scripts" / "modules" / "reporter" / "service.py"
    if reporter.is_file() and "ResultSelectionPort" not in reporter.read_text(encoding="utf-8", errors="ignore"):
        errors.append("Reporter未接入ResultSelectionPort")
    for relative in (
        "scripts/modules/reporter/artifact_validator.py",
        "scripts/modules/reporter/export_service.py",
    ):
        path = root / relative
        if path.is_file() and "portable_assets" not in path.read_text(encoding="utf-8", errors="ignore"):
            errors.append(f"Reporter portable资源协议缺失：{relative}")
    for relative in (
        "scripts/modules/reporter/evidence_resolver.py",
        "scripts/modules/reporter/models.py",
        "scripts/modules/reporter/report_unit_builder.py",
    ):
        path = root / relative
        if path.is_file() and "expected_artifact_manifest_hash" not in path.read_text(encoding="utf-8", errors="ignore"):
            errors.append(f"Reporter未绑定ModuleRunRef外部锚：{relative}")
    datafetcher_page = root / "assets" / "pages" / "datafetcher" / "datafetcher.js"
    if datafetcher_page.is_file() and "/api/assets/" not in datafetcher_page.read_text(encoding="utf-8", errors="ignore"):
        errors.append("DataFetcher页面未接入动态下载Bridge")
    tool_entry = root / "scripts" / "tool_entry.py"
    if tool_entry.is_file():
        tool_text = tool_entry.read_text(encoding="utf-8", errors="ignore")
        for token, label in (
            ("--project-request", "单行自然语言项目入口"),
            ("run_project_request", "项目级完整研究流程"),
            ("public_project_result", "人类友好公开结果投影"),
            ('or "continuous"', "完整HTML默认连续版式"),
        ):
            if token not in tool_text:
                errors.append(f"Tool入口未声明{label}")
    return errors


def _legacy_product_name_errors(root: Path) -> list[str]:
    """旧底座名仅允许作为pricing_core迁移清单中的baseline_id。"""
    errors: list[str] = []
    allowed = "scripts/modules/pricer/engines/pricing_core/baseline_manifest.json"
    for path in _walk_files(root):
        relative = _relative(root, path)
        if "option_pricing" in relative:
            errors.append(f"发行包路径包含旧底座名option_pricing：{relative}")
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "option_pricing" not in text:
            continue
        if relative == allowed:
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError:
                errors.append("Pricer迁移baseline_manifest不是有效JSON")
            else:
                baseline_id = manifest.get("baseline_id")
                if not isinstance(baseline_id, str) or "option_pricing" not in baseline_id:
                    errors.append("Pricer迁移baseline_manifest缺少旧baseline_id证据")
                elif "option_pricing" in text.replace(baseline_id, ""):
                    errors.append("Pricer迁移baseline_manifest只能在baseline_id保留旧名称")
            continue
        errors.append(f"发行包公开内容包含旧底座名option_pricing：{relative}")
    return errors


def _manifest_errors(root: Path, entries: list[dict[str, object]]) -> list[str]:
    path = root / "capability-manifest.json"
    if not path.is_file():
        return ["缺少capability-manifest.json"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"Capability Manifest不是有效JSON：{error}"]
    errors: list[str] = []
    required = {
        "manifest_schema_version", "package_status", "capability_version", "catalog_version", "catalog_source",
        "release_status", "formal_release", "execution_scope",
        "protocol_version", "design_system_version", "hash_spec_version", "modules", "page_modules",
        "contract_core_hash", "tool_catalog_hash", "content_tree_hash", "content_hashes", "content_tree_entries",
        "module_content_hashes", "source_map_hash", "source_tree_hash", "source_content_hashes",
    }
    errors.extend(f"Capability Manifest缺少字段：{field}" for field in sorted(required - set(manifest)))
    published = manifest.get("release_status") == "published"
    if published:
        catalog_version = manifest.get("catalog_version")
        if (
            manifest.get("package_status") != "published"
            or not isinstance(catalog_version, str)
            or not re.fullmatch(r"v\d+\.\d+", catalog_version)
            or manifest.get("capability_version") != catalog_version
            or manifest.get("formal_release") is not True
        ):
            errors.append("正式Capability Manifest状态无效")
        if not isinstance(manifest.get("published_by"), str) or not isinstance(manifest.get("published_at"), str):
            errors.append("正式Capability Manifest缺少签发信息")
        if manifest.get("design_system_version") != "v1.0":
            errors.append("正式Capability必须绑定当前Design System版本")
    elif manifest.get("package_status") != "candidate" or manifest.get("capability_version") != "candidate" or manifest.get("formal_release") is not False:
        errors.append("候选Capability Manifest状态无效")
    if manifest.get("hash_spec_version") != HASH_SPEC_VERSION:
        errors.append("内容树哈希规范版本不匹配")
    if tuple(manifest.get("modules", ())) != MODULES:
        errors.append("Manifest七模块顺序或集合不正确")
    if tuple(manifest.get("page_modules", ())) != PAGE_MODULES:
        errors.append("Manifest五个操作页面顺序或集合不正确")
    if "release_id" in manifest:
        errors.append("Capability Manifest不得包含release_id")
    actual_hashes = _hashes(entries)
    if manifest.get("content_tree_entries") != entries:
        errors.append("Capability内容树记录不一致")
    if manifest.get("content_hashes") != actual_hashes:
        errors.append("Capability文件哈希不一致")
    if manifest.get("content_tree_hash") != tree_hash(entries):
        errors.append("Capability目录树总哈希不一致")
    try:
        actual_module_hashes = _module_hashes(entries)
    except SkillVerificationError as error:
        errors.append(str(error))
        actual_module_hashes = {}
    if manifest.get("module_content_hashes") != actual_module_hashes:
        errors.append("模块内容哈希不一致")
    contract_entries = [item for item in entries if str(item["path"]).startswith("scripts/runtime/contracts/")]
    if not contract_entries or manifest.get("contract_core_hash") != tree_hash(contract_entries):
        errors.append("共享合同核心哈希不一致")
    if manifest.get("tool_catalog_hash") != actual_hashes.get("scripts/runtime/protocol/tool_catalog.py"):
        errors.append("Tool Catalog哈希不一致")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"Capability协议版本必须为{PROTOCOL_VERSION}")
    tool_catalog = root / "scripts" / "runtime" / "protocol" / "tool_catalog.py"
    if tool_catalog.is_file():
        catalog_versions = set(re.findall(
            r'"protocol_version"\s*:\s*"([^"]+)"',
            tool_catalog.read_text(encoding="utf-8", errors="ignore"),
        ))
        if catalog_versions != {PROTOCOL_VERSION}:
            errors.append(f"Tool Catalog协议版本必须唯一为{PROTOCOL_VERSION}")
    catalog_path = root / "scripts" / "knowledger" / "catalog-version.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if catalog.get("catalog_version") != manifest.get("catalog_version"):
            errors.append("CatalogVersion与Capability Manifest不一致")
        if "release_id" in catalog:
            errors.append("CatalogVersion不得包含release_id")
        technical = manifest.get("release_status") == "technical_candidate"
        if technical:
            expected_fields = {
                "manifest_type", "release_status", "formal_release", "execution_scope", "executable", "catalog_version",
                "catalog_source", "product_count", "product_ids", "source_file_sha256",
            }
            if set(catalog) != expected_fields:
                errors.append("working-tree Catalog字段不符合技术候选规范")
            if (
                catalog.get("manifest_type") != "CatalogTechnicalCandidate"
                or catalog.get("release_status") != "technical_candidate"
                or catalog.get("formal_release") is not False
                or catalog.get("execution_scope") != "development_only"
                or catalog.get("executable") is not True
                or catalog.get("catalog_version") != "unreleased"
                or catalog.get("catalog_source") != "working-tree"
                or manifest.get("catalog_version") != "unreleased"
                or manifest.get("catalog_source") != "working-tree"
                or manifest.get("execution_scope") != "development_only"
            ):
                errors.append("working-tree Catalog必须明确为仅开发环境可执行、不可签发技术候选")
            optionlist = (root / "references" / "optionlist.md").read_text(encoding="utf-8")
            expected_ids = re.findall(r"^\|\s*\d+\s*\|.*?\|\s*(\d+\.\d+)\s*\|", optionlist, re.MULTILINE)
            if catalog.get("product_count") != 65 or catalog.get("product_ids") != expected_ids:
                errors.append("working-tree Catalog未按OptionList顺序映射65个真实产品ID")
            expected_hashes = {
                "optionlist.md": _sha256(root / "references" / "optionlist.md"),
                "optionlib.md": _sha256(root / "references" / "optionlib.md"),
                "optionreg.py": _sha256(root / "scripts" / "knowledger" / "optionreg.py"),
            }
            if catalog.get("source_file_sha256") != expected_hashes:
                errors.append("working-tree Catalog来源哈希不一致")
        elif manifest.get("release_status") in {"candidate_from_published_catalog", "published"}:
            if manifest.get("execution_scope") not in {"candidate_verification", "production"}:
                errors.append("正式Catalog候选的execution_scope必须为candidate_verification")
            if (
                catalog.get("manifest_type") != "CatalogVersion"
                or catalog.get("formal_release") is not True
                or catalog.get("executable") is not True
                or "candidate_status" in catalog
            ):
                errors.append("正式Catalog候选必须包含已正式签发且可执行的CatalogVersion")
            if not isinstance(catalog.get("published_by"), str) or not catalog["published_by"].strip() or not isinstance(catalog.get("published_at"), str):
                errors.append("CatalogVersion缺少published_by或published_at")
            products = catalog.get("products")
            if not isinstance(products, dict) or len(products) != 65:
                errors.append("CatalogVersion必须包含65个产品版本映射")
            else:
                optionlist = (root / "references" / "optionlist.md").read_text(encoding="utf-8")
                expected_ids = re.findall(r"^\|\s*\d+\s*\|.*?\|\s*(\d+\.\d+)\s*\|", optionlist, re.MULTILINE)
                if len(expected_ids) != 65 or list(products) != expected_ids:
                    errors.append("CatalogVersion未按OptionList顺序映射65个真实产品ID")
            refs = catalog.get("product_manifest_refs")
            manifest_hashes = catalog.get("product_manifest_sha256")
            if not isinstance(refs, dict) or not isinstance(manifest_hashes, dict):
                errors.append("CatalogVersion缺少ProductVersion引用或manifest哈希")
            elif isinstance(products, dict):
                snapshot_files = {
                    "optionlist": "optionlist-fragment.md", "optionlib": "optionlib-fragment.md",
                    "optionreg": "optionreg-product.py", "default_terms": "default-terms.json",
                    "default_json": "default-payoff.json", "default_svg": "default-payoff.svg",
                }
                for product_id, product_version in products.items():
                    expected_ref = f"knowledger/products/{product_id}/product-version.json"
                    if refs.get(product_id) != expected_ref:
                        errors.append(f"{product_id}的ProductVersion引用无效")
                        continue
                    product_dir = root / "scripts" / "knowledger" / "products" / product_id
                    product_manifest_path = product_dir / "product-version.json"
                    try:
                        if _sha256(product_manifest_path) != manifest_hashes.get(product_id):
                            errors.append(f"{product_id}的ProductVersion manifest哈希不一致")
                        product_manifest = json.loads(product_manifest_path.read_text(encoding="utf-8"))
                        if (
                            product_manifest.get("manifest_type") != "ProductVersion"
                            or product_manifest.get("publication_status") != "published"
                            or product_manifest.get("formal_release") is not True
                            or product_manifest.get("executable") is not True
                            or product_manifest.get("product_id") != product_id
                            or product_manifest.get("product_version") != product_version
                            or product_manifest.get("snapshot_files") != snapshot_files
                        ):
                            errors.append(f"{product_id}的ProductVersion身份或六件套声明无效")
                            continue
                        hashes = product_manifest.get("snapshot_sha256")
                        for key, filename in snapshot_files.items():
                            snapshot_path = product_dir / filename
                            if not isinstance(hashes, dict) or _sha256(snapshot_path) != hashes.get(key + "_sha256"):
                                errors.append(f"{product_id}的{filename}缺失或哈希不一致")
                        payoff = json.loads((product_dir / snapshot_files["default_json"]).read_text(encoding="utf-8"))
                        name = payoff.get("name_zh")
                        live_json = root / "assets" / "payoffer" / "figures" / "json" / f"{name}.json"
                        live_svg = root / "assets" / "payoffer" / "figures" / "svg" / f"{name}.svg"
                        if _sha256(live_json) != _sha256(product_dir / snapshot_files["default_json"]):
                            errors.append(f"{product_id}的包内Payoff JSON未绑定签发快照")
                        if _sha256(live_svg) != _sha256(product_dir / snapshot_files["default_svg"]):
                            errors.append(f"{product_id}的包内Payoff SVG未绑定签发快照")
                    except (OSError, json.JSONDecodeError) as error:
                        errors.append(f"{product_id}的ProductVersion六件套无效：{error}")
        else:
            errors.append("Capability Manifest的release_status无效")
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"CatalogVersion无效：{error}")
    return errors


def verify_skill(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    errors: list[str] = []
    if root.name != "option-helper":
        errors.append("Skill根目录必须名为option-helper")
    try:
        entries = content_tree_entries(root)
    except SkillVerificationError as error:
        return [*errors, str(error)]
    errors.extend(_structure_errors(root))
    skill = root / "SKILL.md"
    if skill.is_file():
        errors.extend(_frontmatter_errors(skill))
    errors.extend(_code_errors(root))
    errors.extend(_capability_interface_errors(root))
    errors.extend(_legacy_product_name_errors(root))
    errors.extend(_link_errors(root))
    errors.extend(_store_boundary_errors(root))
    errors.extend(_launcher_errors(root))
    errors.extend(_manifest_errors(root, entries))
    return sorted(set(errors))


def _page_catalog_errors(output: str) -> list[str]:
    try:
        catalog = json.loads(output)
    except json.JSONDecodeError as error:
        return [f"module_host --list未返回JSON：{error}"]
    if not isinstance(catalog, dict) or tuple(catalog) != PAGE_MODULES:
        return ["module_host --list必须精确列出五个操作页面"]
    return [f"module_host页面不存在：{module}" for module in PAGE_MODULES if not isinstance(catalog[module], dict) or catalog[module].get("exists") is not True]


class _SilentPageHandler(SimpleHTTPRequestHandler):
    def log_message(self, _: str, *args: object) -> None:
        return


def _page_resources(root: Path, page: Path) -> list[Path]:
    pending = [page]
    resources: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in resources:
            continue
        resources.add(path)
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        pattern = _HTML_LINK if path.suffix.lower() == ".html" else _CSS_LINK
        for raw in pattern.findall(text):
            target = raw.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith(("#", "data:", "mailto:", "tel:", "javascript:", "http:", "https:", "//")):
                continue
            resolved = (root / target.lstrip("/")).resolve() if target.startswith("/") else (path.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                pending.append(resolved)
    return sorted(resources)


def _page_http_errors(root: Path) -> list[str]:
    handler = partial(_SilentPageHandler, directory=str(root))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as error:
        return [f"无法启动发行态页面HTTP校验服务：{error}"]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors: list[str] = []
    try:
        port = server.server_port
        for module in PAGE_MODULES:
            page = root / "assets" / "pages" / module / f"{module}.html"
            for resource in _page_resources(root, page):
                relative = quote(resource.relative_to(root).as_posix())
                try:
                    with urlopen(f"http://127.0.0.1:{port}/{relative}", timeout=5) as response:
                        if response.status != 200:
                            errors.append(f"页面资源HTTP状态异常：{resource.relative_to(root)}={response.status}")
                        else:
                            # 读取完整响应后再关闭连接，避免校验客户端提前断开导致
                            # 临时HTTP服务输出BrokenPipeError并污染发行日志。
                            response.read()
                except (HTTPError, URLError, OSError) as error:
                    errors.append(f"页面资源HTTP不可用：{resource.relative_to(root)}：{error}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return errors


def _formal_compute_protocol_errors(root: Path, python: str, environment: Mapping[str, str], cwd: str) -> list[str]:
    """Run the frozen Tool signature and input-shape gate inside the Skill.

    Importing is intentionally performed from the staged Skill, not from the
    development checkout. This prevents an App or ZIP from silently carrying
    a stale Tool entry even when the source-tree tests remain green.
    """
    probe = textwrap.dedent(
        """
        import importlib
        import inspect
        from dataclasses import replace
        from hashlib import sha256
        from pathlib import Path
        from tool_entry import call_tool, prepare_compute_request
        from tool_entry import ToolDispatchError
        from runtime.adapters.local_store import LocalResultStore
        from runtime.protocol.models import CallerContext, ModuleRunRef
        from runtime.protocol.module_host import ModuleHostContext

        expected = ["module", "request", "caller_context", "host_context", "result_store", "data_store"]
        assert list(inspect.signature(call_tool).parameters) == expected
        parameters = inspect.signature(call_tool).parameters
        assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in expected[2:])
        assert list(inspect.signature(prepare_compute_request).parameters) == ["module", "request", "data_refs", "resolved_contract", "data_store"]
        handler_parameters = {
            "payoffer": ["request", "host_context", "result_store", "tenant_id"],
            "pricer": ["request", "host_context", "result_store", "tenant_id", "data_store"],
            "backtester": ["request", "host_context", "result_store", "tenant_id", "data_store"],
        }
        for module_name, expected_handler in handler_parameters.items():
            handler = importlib.import_module(f"modules.{module_name}.service").call_tool
            assert list(inspect.signature(handler).parameters) == expected_handler

        result_store = LocalResultStore(Path.cwd() / "formal-probe-result")
        committed_ref = result_store.commit_module_run(
            module="pricer",
            tenant_id="release-probe-tenant",
            task_id="release-probe-task",
            run_id="release-probe-committed-run",
            files={
                "manifest.json": {"status": "succeeded"},
                "input_snapshot.json": {"probe": "v1.2"},
                "resolved_contract.json": {"contract_fingerprint": "a" * 64},
                "data_refs.json": {"items": []},
                "limitations.json": {"items": []},
                "result.json": {"present_value": 1.25},
            },
        )
        committed_dir = result_store.resolve_module_run(
            committed_ref, tenant_id="release-probe-tenant"
        )
        committed_manifest = committed_dir / "artifacts" / "artifact_manifest.json"
        assert committed_ref.expected_artifact_manifest_hash == sha256(
            committed_manifest.read_bytes()
        ).hexdigest(), "committed RunRef external artifact anchor mismatch"
        legacy_ref = {
            "module": committed_ref.module,
            "tenant_id": committed_ref.tenant_id,
            "task_id": committed_ref.task_id,
            "run_id": committed_ref.run_id,
            "expected_semantic_result_hash": committed_ref.expected_semantic_result_hash,
        }
        try:
            ModuleRunRef(**legacy_ref)
        except TypeError:
            pass
        else:
            raise AssertionError("new ModuleRunRef accepted legacy five-field shape")

        caller = CallerContext(
            tenant_id="release-probe-tenant",
            principal_id="release-probe-principal",
            role="admin",
            capabilities=("module.catalog",),
            session_id="release-probe-session",
            audience="option-helper-app",
            request_id="release-probe-idempotency-0001",
        )
        context = ModuleHostContext(
            session_ref="release-probe-session-ref",
            capability_token="v2.999999999999." + "1" * 64,
            analysis_case_id="release-probe-case",
            task_id="release-probe-task",
            candidate_id="release-probe-candidate",
            catalog_version="v1.0",
            contract_fingerprint="a" * 64,
            module="pricer",
            page_hash="b" * 64,
            capability_version="candidate",
            protocol_version="v1.2",
            context_id="mhc_release_probe_v2_0001",
            host_kind="app",
            request_policy=("module.catalog",),
            result_refs=(committed_ref,),
        )
        assert caller.request_id == "release-probe-idempotency-0001"
        assert context.module == "pricer" and context.host_kind == "app"
        catalog = call_tool("pricer", {"action": "catalog"}, caller_context=caller, host_context=context)
        assert isinstance(catalog, dict), "v2 catalog response must be a JSON object"

        run_caller = replace(
            caller, capabilities=("module.run",), request_id="release-probe-run-authorization"
        )
        run_context = replace(
            context, request_policy=("module.run",), context_id="mhc_release_probe_run_0001"
        )
        for denied_caller, denied_context, authority in (
            (run_caller, context, "CallerContext"),
            (caller, run_context, "ModuleHostContext"),
        ):
            try:
                call_tool(
                    "pricer", {"action": "catalog"},
                    caller_context=denied_caller,
                    host_context=denied_context,
                )
            except ToolDispatchError:
                pass
            else:
                raise AssertionError(f"module.run calling catalog was not rejected: {authority}")

        try:
            call_tool(
                "pricer", {"action": "run"},
                caller_context=replace(caller, request_id="release-probe-catalog-cannot-run"),
                host_context=context,
            )
        except ToolDispatchError:
            pass
        else:
            raise AssertionError("module.catalog calling run was not rejected")

        class ProbeDataStore:
            def read_bytes(self, *_args, **_kwargs):
                raise AssertionError("release probe must not read market data through the stub handler")

        pricer_service = importlib.import_module("modules.pricer.service")
        original_pricer_handler = pricer_service.call_tool
        pricer_service.call_tool = lambda request, **kwargs: {
            "ok": True,
            "action": request.get("action"),
            "tenant_id": kwargs.get("tenant_id"),
        }
        try:
            run_result = call_tool(
                "pricer", {"action": "run"},
                caller_context=run_caller,
                host_context=run_context,
                data_store=ProbeDataStore(),
            )
        finally:
            pricer_service.call_tool = original_pricer_handler
        assert run_result == {
            "ok": True,
            "action": "run",
            "tenant_id": caller.tenant_id,
        }, "formal run did not retain module.run authorization"

        cross_tenant = replace(
            context,
            result_refs=(replace(committed_ref, tenant_id="other-tenant"),),
        )
        try:
            call_tool(
                "pricer", {"action": "catalog"},
                caller_context=replace(caller, request_id="release-probe-tenant-check"),
                host_context=cross_tenant,
            )
        except ToolDispatchError:
            pass
        else:
            raise AssertionError("tenant rejection was not enforced")

        asset = "000905.SH"
        # The Pricer compiler may only derive its contract-start reference
        # price from a Host-injected, content-addressed DataAssetRef.  Keep
        # this release probe on that same production boundary instead of
        # weakening it with a data-less convenience path.
        market_payload = (
            b"date,asset_id,close,adj_close\\n"
            b"2024-01-02,000905.SH,5000.0,5000.0\\n"
            b"2024-01-03,000905.SH,5010.0,5010.0\\n"
            b"2024-01-04,000905.SH,5020.0,5020.0\\n"
            b"2024-01-05,000905.SH,5030.0,5030.0\\n"
        )

        class ProbeMarketDataStore:
            def read_bytes(self, data_ref, *, tenant_id):
                assert tenant_id == "protocol"
                assert data_ref.data_asset_id == "protocol-market"
                return market_payload

        ref = {
            "data_asset_id": "protocol-market",
            "storage_ref": "data:protocol:protocol-market:" + "a" * 64 + ":" + "b" * 64,
            "media_type": "text/csv", "schema_id": "market-history-v1",
            "asset_ids": [asset], "normalized_fields": ["date", "asset_id", "close", "adj_close"],
            "coverage": {"start": "2024-01-02", "end": "2024-01-05", "sessions": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"], "calendar_id": "CN-SSE", "calendar_version": "protocol-v1"},
            "row_count": 4, "price_convention": {"adjustment": "close_and_adj_close"},
            "content_hash": sha256(market_payload).hexdigest(), "lineage": {"probe": "formal-compute"},
            "tenant_id": "protocol", "created_by": "probe", "access_scope": ["read"], "partition_spec": {},
        }
        common = {
            "product_id": "2.1",
            "identity": {
                "underlyings": [asset],
                "contract_start_date": "2024-01-02",
                "reference_prices": {asset: 5000.0},
            },
            "term_overrides": {"K": 5100.0},
        }
        prepared = {
            "payoffer": prepare_compute_request("payoffer", common),
            "pricer": prepare_compute_request(
                "pricer",
                {**common, "pricing_config": {"valuation_date": "2024-01-05", "model_method": "black_scholes"}},
                data_refs=(ref,),
                data_store=ProbeMarketDataStore(),
            ),
            "backtester": prepare_compute_request("backtester", {**common, "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]}}, data_refs=(ref,)),
        }
        assert len({value["contract_fingerprint"] for value in prepared.values()}) == 1
        assert set(prepared["payoffer"]["request"]) == {"action", "payoff_input"}
        assert set(prepared["pricer"]["request"]) == {"action", "contract", "pricing_config", "market_data_refs"}
        assert set(prepared["backtester"]["request"]) == {"action", "contract", "backtest_config", "historical_data"}
        """
    )
    completed = subprocess.run(
        [python, "-c", probe], cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False,
    )
    if completed.returncode == 0:
        return []
    detail = (completed.stderr or completed.stdout).strip()
    return [f"正式计算协议探测失败：{detail}"]


def probe_runtime(
    root: Path,
    python: str = sys.executable,
    page_probe: Callable[[Path], list[str]] | None = None,
) -> list[str]:
    """探测可运行性；正式调用默认必须对五页面执行真实HTTP探测。"""
    root = root.expanduser().resolve()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    scripts_root = str(root / "scripts")
    environment["PYTHONPATH"] = scripts_root + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    with tempfile.TemporaryDirectory(prefix="optionhelper-store-") as store:
        data_root = Path(store) / "data"
        result_root = Path(store) / "result"
        runtime_root = Path(store) / "runtime"
        environment["OPTIONHELPER_RUNTIME_ROOT"] = str(runtime_root)
        environment["OPTIONHELPER_DATA_ROOT"] = str(data_root)
        environment["OPTIONHELPER_RESULT_ROOT"] = str(result_root)
        commands = [
            ("tool_entry", [python, str(root / "scripts" / "tool_entry.py"), "--list"]),
            ("module_host", [python, str(root / "scripts" / "module_host.py"), "--list"]),
            ("modules", [python, "-c", "import importlib; [importlib.import_module(f'modules.{name}.service') for name in " + repr(MODULES) + "]"]),
        ]
        errors: list[str] = []
        module_host_output: str | None = None
        for label, command in commands:
            completed = subprocess.run(command, cwd=store, env=environment, capture_output=True, text=True, check=False)
            if completed.returncode:
                errors.append(f"运行时探测失败：{' '.join(command[1:])}\n{completed.stderr.strip()}")
            elif label == "module_host":
                module_host_output = completed.stdout
        errors.extend(_formal_compute_protocol_errors(root, python, environment, store))
        if module_host_output is not None:
            errors.extend(_page_catalog_errors(module_host_output))
            errors.extend((page_probe or _page_http_errors)(root))
        rejected = subprocess.run(
            [
                python, str(root / "scripts" / "environment_check.py"), "--check-store",
                "--skill-root", str(root), "--data-root", str(root / "data"),
                "--result-root", str(result_root), "--runtime-root", str(root / ".optionhelper" / "runtime"),
            ],
            cwd=store, env=environment, capture_output=True, text=True, check=False,
        )
        if rejected.returncode == 0 or "inside_installation" not in rejected.stdout:
            errors.append("外部Store预检未真实拒绝安装目录内的DataStore")
        return errors


def verify_zip(archive: Path) -> list[str]:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        return [f"ZIP不存在：{archive}"]
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                return ["ZIP包含重复成员"]
            if any(name.startswith("/") or ".." in Path(name).parts or not name.startswith("option-helper/") for name in names):
                return ["ZIP成员越出option-helper根目录"]
            with tempfile.TemporaryDirectory(prefix="optionhelper-skill-") as temporary:
                bundle.extractall(temporary)
                for info in bundle.infolist():
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        (Path(temporary) / info.filename).chmod(mode)
                return verify_skill(Path(temporary) / "option-helper")
    except (OSError, zipfile.BadZipFile) as error:
        return [f"ZIP不可读取：{error}"]


def main() -> None:
    parser = argparse.ArgumentParser(description="验证OptionHelper Skill候选包")
    parser.add_argument("skill_root", type=Path)
    parser.add_argument("--zip", dest="archive", type=Path)
    parser.add_argument("--probe-runtime", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    errors = verify_skill(args.skill_root)
    if args.archive:
        errors.extend(verify_zip(args.archive))
    if args.probe_runtime and not errors:
        errors.extend(probe_runtime(args.skill_root, args.python))
    if errors:
        raise SkillVerificationError("\n".join(sorted(set(errors))))
    print("Skill candidate verified")


if __name__ == "__main__":
    main()
