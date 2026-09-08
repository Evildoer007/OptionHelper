"""Bind App-owned outer resources to their source and packaged bytes."""

from __future__ import annotations

from hashlib import sha256
from fnmatch import fnmatch
from importlib import metadata
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
from typing import Iterable


FORBIDDEN_DIRECTORY_NAMES = frozenset({
    ".optionhelper",
    "credentials",
    "data",
    "history",
    "result",
})
CACHE_DIRECTORY_NAMES = frozenset({
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
})

BACKEND_DESKTOP_COMMON_MODULES = (
    "desktop.common",
    "desktop.common.paths",
)
BACKEND_DESKTOP_COMMON_SOURCES = (
    "products/app/desktop/common/__init__.py",
    "products/app/desktop/common/paths.py",
)
VERIFICATION_FIXTURE_SOURCE = "products/app/config/verification-fixture.json"
VERIFICATION_FIXTURE_RESOURCE = "config/verification-fixture.json"
VERIFICATION_FIXTURE_SCHEMA = "optionhelper.verification-fixture-identity"

MACOS_SOURCE_MAPPINGS = (
    ("products/app/frontend", "Contents/Resources/frontend"),
    ("assets/icons/optionhelper-app-icon-tile-light.icns", "Contents/Resources/OptionHelper.icns"),
    ("assets/icons/optionhelper-app-icon-tile-light.svg", "Contents/Resources/assets/icons/optionhelper-app-icon-tile-light.svg"),
    ("assets/icons/optionhelper-app-icon-tile-dark.svg", "Contents/Resources/assets/icons/optionhelper-app-icon-tile-dark.svg"),
    ("assets/icons/optionhelper-app-icon-tile-dark.icns", "Contents/Resources/assets/icons/optionhelper-app-icon-tile-dark.icns"),
    ("packaging/app/macos/assets/optionhelper-dmg-background.png", "Contents/Resources/installer/optionhelper-dmg-background.png"),
    (VERIFICATION_FIXTURE_SOURCE, f"Contents/Resources/{VERIFICATION_FIXTURE_RESOURCE}"),
)
MACOS_PAYLOAD_ROOTS = (
    "Contents/Resources/frontend",
    "Contents/Resources/assets",
    "Contents/Resources/installer",
    "Contents/Resources/LICENSES",
    "Contents/Resources/OptionHelper.icns",
    f"Contents/Resources/{VERIFICATION_FIXTURE_RESOURCE}",
)

# The App Host is a separate delivery boundary from the shared Capability.
# Keep its source closure explicit so a newly imported package cannot be
# present only in one developer's untracked worktree.  The audit package is
# intentionally listed as its own build input because app_server imports it at
# runtime and both native platforms freeze the same Python Host.
APP_BACKEND_SOURCE_INPUTS = (
    "products/app/backend/__init__.py",
    "products/app/backend/agent_runtime",
    "products/app/backend/app_server.py",
    "products/app/backend/attachments",
    "products/app/backend/audit",
    "products/app/backend/authorization",
    "products/app/backend/capability_service.py",
    "products/app/backend/datafetcher_adapter.py",
    "products/app/backend/errors.py",
    "products/app/backend/identity",
    "products/app/backend/model_gateway",
    "products/app/backend/page_registry.py",
    "products/app/backend/product_rule_revision.py",
    "products/app/backend/report_editor.py",
    "products/app/backend/reporter_adapter.py",
    "products/app/backend/report_delivery.py",
    "products/app/backend/secrets",
    "products/app/backend/settings",
    "products/app/backend/stores",
    "products/app/backend/task_runtime",
    "products/app/backend/tool_gateway.py",
    "products/app/backend/verification_fixture.py",
)
MACOS_BUILD_INPUTS = (
    *APP_BACKEND_SOURCE_INPUTS,
    "products/app/config/app-defaults.yaml",
    "products/app/config/logging.yaml",
    VERIFICATION_FIXTURE_SOURCE,
    *BACKEND_DESKTOP_COMMON_SOURCES,
    "products/app/desktop/macos",
    "core/requirements.lock",
)
MACOS_ALLOWED_RESOURCE_ENTRIES = frozenset({
    "OptionHelper.icns", "LICENSES", "agent-runtime", "app-manifest.json",
    "assets", "backend", "capability", "config", "frontend", "installer",
})
WINDOWS_SOURCE_MAPPINGS = (
    ("products/app/frontend", "Resources/frontend"),
    ("assets/icons/optionhelper-app-icon-tile-light.ico", "Resources/icons/OptionHelper.ico"),
    ("packaging/app/windows/verify_windows.py", "verify-windows.py"),
    ("packaging/app/platform_payload.py", "verification/platform_payload.py"),
    ("packaging/app/python_runtime_licenses.py", "verification/python_runtime_licenses.py"),
    (VERIFICATION_FIXTURE_SOURCE, f"Resources/{VERIFICATION_FIXTURE_RESOURCE}"),
)
WINDOWS_PAYLOAD_ROOTS = (
    "Resources/frontend",
    "Resources/icons",
    "Resources/LICENSES",
    "verify-windows.py",
    "verification",
    f"Resources/{VERIFICATION_FIXTURE_RESOURCE}",
)
WINDOWS_BUILD_INPUTS = (
    *APP_BACKEND_SOURCE_INPUTS,
    "products/app/config/app-defaults.yaml",
    "products/app/config/logging.yaml",
    VERIFICATION_FIXTURE_SOURCE,
    *BACKEND_DESKTOP_COMMON_SOURCES,
    "products/app/desktop/macos/backend_launcher.py",
    "products/app/desktop/windows",
    "core/requirements.lock",
)
WINDOWS_ALLOWED_RESOURCE_ENTRIES = frozenset({
    "LICENSES", "agent-runtime", "app-manifest.json", "backend", "capability",
    "config", "frontend", "icons",
})
MACOS_STRICT_DIRECTORY_PAYLOADS = (
    ("backend", "Contents/Resources/backend"),
    ("agent-runtime", "Contents/Resources/agent-runtime"),
)
WINDOWS_STRICT_DIRECTORY_PAYLOADS = (
    ("backend", "Resources/backend"),
    ("agent-runtime", "Resources/agent-runtime"),
)
SOURCE_EXCLUDES = {
    "products/app/frontend": ("*.md",),
}
PYTHON_DOCX_TEMPLATE_TARGET = Path("_internal/docx/templates")
PYTHON_DOCX_PARTS_ANCHOR_TARGET = Path("_internal/docx/parts/optionhelper-resource-path.anchor")
PYTHON_DOCX_PARTS_ANCHOR_BYTES = b"OptionHelper python-docx relative resource path anchor.\n"
PYTHON_DOCX_REQUIRED_TEMPLATES = frozenset({
    "default.docx",
    "default-header.xml",
    "default-footer.xml",
    "default-settings.xml",
    "default-styles.xml",
    "default-docx-template/word/numbering.xml",
    "default-docx-template/word/styles.xml",
})


class PlatformPayloadError(RuntimeError):
    pass


def _read_verification_fixture_definition(path: Path) -> str:
    if path.is_symlink():
        raise PlatformPayloadError("Fixture身份定义不得为符号链接")
    if not path.is_file():
        raise PlatformPayloadError(f"Fixture身份定义不存在：{path}")
    if path.stat().st_size > 4096:
        raise PlatformPayloadError("Fixture身份定义超过大小限制")
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformPayloadError("Fixture身份定义不是有效UTF-8 JSON") from error
    if not isinstance(definition, dict) or set(definition) != {"schema", "principal_label"}:
        raise PlatformPayloadError("Fixture身份定义字段无效")
    if definition.get("schema") != VERIFICATION_FIXTURE_SCHEMA:
        raise PlatformPayloadError("Fixture身份定义Schema无效")
    principal_label = definition.get("principal_label")
    if not isinstance(principal_label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}", principal_label):
        raise PlatformPayloadError("Fixture身份定义principal_label无效")
    return principal_label


def read_packaged_verification_fixture_identity(
    app_root: Path,
    *,
    resources_relative: str,
) -> str:
    """Read the one packaged verification identity and reject sibling files."""

    config_root = app_root / resources_relative / "config"
    if config_root.is_symlink() or not config_root.is_dir():
        raise PlatformPayloadError("Fixture身份目录不存在或为符号链接")
    entries = sorted(path.name for path in config_root.iterdir())
    if entries != [Path(VERIFICATION_FIXTURE_RESOURCE).name]:
        raise PlatformPayloadError("Fixture身份目录文件集合无效")
    return _read_verification_fixture_definition(config_root / Path(VERIFICATION_FIXTURE_RESOURCE).name)


def stage_verification_fixture_definition(
    repository_root: Path,
    app_root: Path,
    *,
    resources_relative: str,
) -> str:
    """Copy the authoritative fixture identity as one exact packaged resource."""

    source = repository_root / VERIFICATION_FIXTURE_SOURCE
    principal_label = _read_verification_fixture_definition(source)
    config_root = app_root / resources_relative / "config"
    if config_root.exists() or config_root.is_symlink():
        raise PlatformPayloadError("Fixture身份目录已存在")
    config_root.mkdir(parents=True)
    target = config_root / Path(VERIFICATION_FIXTURE_RESOURCE).name
    shutil.copy2(source, target)
    if target.read_bytes() != source.read_bytes():
        raise PlatformPayloadError("Fixture身份定义复制后字节不一致")
    if read_packaged_verification_fixture_identity(
        app_root,
        resources_relative=resources_relative,
    ) != principal_label:
        raise PlatformPayloadError("Fixture身份定义复制后身份不一致")
    return principal_label


def _safe_manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PlatformPayloadError(f"交付目录Manifest路径越界：{value}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PlatformPayloadError(f"交付目录Manifest路径越界：{value}")
    return path.as_posix()


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(
    root: Path,
    *,
    exclude_patterns: Iterable[str] = (),
    ignore_transient: bool = False,
) -> dict[str, Path]:
    if root.is_file():
        return {root.name: root}
    if not root.is_dir():
        raise PlatformPayloadError(f"交付资源不存在：{root}")
    values: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ignore_transient:
            if any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
                continue
            if path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
        if any(fnmatch(relative.as_posix(), pattern) or fnmatch(path.name, pattern) for pattern in exclude_patterns):
            continue
        values[relative.as_posix()] = path
    return values


def _strict_files(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise PlatformPayloadError(f"严格交付目录不存在或为符号链接：{root}")
    values: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PlatformPayloadError(f"严格交付目录不得包含符号链接：{path}")
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        parts = {part.casefold() for part in PurePosixPath(relative).parts}
        if FORBIDDEN_DIRECTORY_NAMES.intersection(parts):
            raise PlatformPayloadError(f"严格交付目录含禁止目录：{relative}")
        if CACHE_DIRECTORY_NAMES.intersection(parts):
            raise PlatformPayloadError(f"严格交付目录含缓存目录：{relative}")
        if not path.is_file():
            continue
        if (
            path.name == ".DS_Store"
            or path.suffix in {".pyc", ".pyo"}
        ):
            raise PlatformPayloadError(f"严格交付目录含缓存文件：{relative}")
        values[relative] = path
    if not values:
        raise PlatformPayloadError(f"严格交付目录为空：{root}")
    return values


def _python_docx_template_source() -> Path:
    try:
        distribution = metadata.distribution("python-docx")
    except metadata.PackageNotFoundError as error:
        raise PlatformPayloadError("构建环境缺少python-docx") from error
    return Path(distribution.locate_file("docx/templates")).resolve()


def _python_docx_template_files(root: Path) -> dict[str, Path]:
    files = _strict_files(root)
    missing = sorted(PYTHON_DOCX_REQUIRED_TEMPLATES.difference(files))
    if missing:
        raise PlatformPayloadError("python-docx模板源缺少：" + ", ".join(missing))
    return files


def verify_python_docx_templates(
    backend_package: Path,
    *,
    source_root: Path | None = None,
) -> Path:
    """Bind python-docx's lazy file lookups to exact frozen runtime bytes."""

    source = source_root.resolve() if source_root is not None else _python_docx_template_source()
    expected = _python_docx_template_files(source)
    target = backend_package / PYTHON_DOCX_TEMPLATE_TARGET
    actual = _strict_files(target)
    if set(actual) != set(expected):
        missing = sorted(set(expected).difference(actual))
        extra = sorted(set(actual).difference(expected))
        details: list[str] = []
        if missing:
            details.append("缺少：" + ", ".join(missing))
        if extra:
            details.append("多出：" + ", ".join(extra))
        raise PlatformPayloadError("python-docx冻结模板文件集合不一致；" + "；".join(details))
    for relative, packaged in actual.items():
        if packaged.stat().st_size != expected[relative].stat().st_size or _hash(packaged) != _hash(expected[relative]):
            raise PlatformPayloadError(f"python-docx冻结模板与构建环境不一致：{relative}")
    anchor = backend_package / PYTHON_DOCX_PARTS_ANCHOR_TARGET
    if anchor.is_symlink() or not anchor.is_file() or anchor.read_bytes() != PYTHON_DOCX_PARTS_ANCHOR_BYTES:
        raise PlatformPayloadError(f"python-docx冻结资源路径锚点无效：{anchor}")
    for relative in ("default-comments.xml", "default-footer.xml", "default-header.xml", "default-settings.xml", "default-styles.xml"):
        through_parts = anchor.parent / ".." / "templates" / relative
        try:
            runtime_bytes = through_parts.read_bytes()
        except OSError as error:
            raise PlatformPayloadError(f"python-docx冻结相对路径不可访问：parts/../templates/{relative}") from error
        if runtime_bytes != expected[relative].read_bytes():
            raise PlatformPayloadError(f"python-docx冻结相对路径内容不一致：parts/../templates/{relative}")
    return target


def stage_python_docx_templates(
    backend_package: Path,
    *,
    source_root: Path | None = None,
) -> Path:
    """Materialize the complete template tree at python-docx's frozen lookup path."""

    if backend_package.is_symlink() or not backend_package.is_dir():
        raise PlatformPayloadError(f"冻结后端目录不存在或为符号链接：{backend_package}")
    source = source_root.resolve() if source_root is not None else _python_docx_template_source()
    _python_docx_template_files(source)
    target = backend_package / PYTHON_DOCX_TEMPLATE_TARGET
    if target.is_symlink():
        raise PlatformPayloadError(f"python-docx冻结模板目录不得为符号链接：{target}")
    if target.exists():
        if not target.is_dir():
            raise PlatformPayloadError(f"python-docx冻结模板路径不是目录：{target}")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=False)
    anchor = backend_package / PYTHON_DOCX_PARTS_ANCHOR_TARGET
    if anchor.parent.is_symlink():
        raise PlatformPayloadError(f"python-docx冻结parts目录不得为符号链接：{anchor.parent}")
    anchor.parent.mkdir(parents=True, exist_ok=True)
    if anchor.is_symlink() or (anchor.exists() and not anchor.is_file()):
        raise PlatformPayloadError(f"python-docx冻结资源路径锚点无效：{anchor}")
    anchor.write_bytes(PYTHON_DOCX_PARTS_ANCHOR_BYTES)
    return verify_python_docx_templates(backend_package, source_root=source)


def build_strict_directory_manifest(root: Path) -> dict[str, object]:
    """Describe every regular file in one generated runtime directory."""

    files = _strict_files(root)
    return {
        "schema": "optionhelper.strict-directory-manifest",
        "files": [
            {
                "path": relative,
                "sha256": _hash(path),
                "size": path.stat().st_size,
            }
            for relative, path in files.items()
        ],
    }


def verify_strict_directory_manifest(root: Path, manifest: object) -> None:
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}:
        raise PlatformPayloadError("严格交付目录Manifest无效")
    if manifest.get("schema") != "optionhelper.strict-directory-manifest":
        raise PlatformPayloadError("严格交付目录Manifest Schema无效")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise PlatformPayloadError("严格交付目录Manifest文件集合为空")
    expected: dict[str, tuple[str, int]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise PlatformPayloadError("严格交付目录Manifest文件记录无效")
        relative = _safe_manifest_path(record.get("path"))
        digest = record.get("sha256")
        size = record.get("size")
        if (
            relative in expected
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise PlatformPayloadError("严格交付目录Manifest文件记录无效")
        expected[relative] = (digest, size)
    actual = _strict_files(root)
    if set(actual) != set(expected):
        raise PlatformPayloadError("严格交付目录文件集合与Manifest不一致")
    for relative, path in actual.items():
        digest, size = expected[relative]
        if path.stat().st_size != size or _hash(path) != digest:
            raise PlatformPayloadError(f"严格交付目录文件哈希不一致：{relative}")


def assert_source_tree_clean(root: Path) -> None:
    if not root.exists():
        raise PlatformPayloadError(f"交付来源不存在：{root}")
    for path in (root, *root.rglob("*")) if root.is_dir() else (root,):
        relative = path.relative_to(root) if path != root else Path(path.name)
        forbidden = FORBIDDEN_DIRECTORY_NAMES.intersection(part.casefold() for part in relative.parts)
        if forbidden:
            raise PlatformPayloadError(f"交付来源含禁止目录：{path}")
        if path.is_symlink():
            raise PlatformPayloadError(f"交付来源不得包含符号链接：{path}")


def _mapped_hashes(
    repository_root: Path,
    app_root: Path,
    source_mappings: Iterable[tuple[str, str]],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for source_relative, payload_relative in source_mappings:
        source = repository_root / source_relative
        payload = app_root / payload_relative
        assert_source_tree_clean(source)
        source_files = _files(
            source,
            exclude_patterns=SOURCE_EXCLUDES.get(source_relative, ()),
            ignore_transient=True,
        )
        payload_files = _files(payload)
        if source.is_file() and payload.is_file():
            source_hash = _hash(source)
            if _hash(payload) != source_hash:
                raise PlatformPayloadError(f"交付来源与包内字节不一致：{source_relative}")
            if source_relative in hashes:
                raise PlatformPayloadError(f"交付来源重复绑定：{source_relative}")
            hashes[source_relative] = source_hash
            continue
        if set(source_files) != set(payload_files):
            raise PlatformPayloadError(f"交付来源与包内文件集合不一致：{source_relative}")
        for relative, source_file in source_files.items():
            payload_file = payload_files[relative]
            source_hash = _hash(source_file)
            if _hash(payload_file) != source_hash:
                raise PlatformPayloadError(f"交付来源与包内字节不一致：{source_relative}/{relative}")
            key = (Path(source_relative) / relative).as_posix() if source.is_dir() else source_relative
            if key in hashes:
                raise PlatformPayloadError(f"交付来源重复绑定：{key}")
            hashes[key] = source_hash
    return dict(sorted(hashes.items()))


def _source_hashes(
    repository_root: Path,
    source_mappings: Iterable[tuple[str, str]],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for source_relative, _payload_relative in source_mappings:
        source = repository_root / source_relative
        assert_source_tree_clean(source)
        for relative, source_file in _files(
            source,
            exclude_patterns=SOURCE_EXCLUDES.get(source_relative, ()),
            ignore_transient=True,
        ).items():
            key = (Path(source_relative) / relative).as_posix() if source.is_dir() else source_relative
            if key in hashes:
                raise PlatformPayloadError(f"交付来源重复绑定：{key}")
            hashes[key] = _hash(source_file)
    return dict(sorted(hashes.items()))


def _build_input_hashes(repository_root: Path, roots: Iterable[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for source_relative in roots:
        source = repository_root / source_relative
        assert_source_tree_clean(source)
        for relative, source_file in _files(source, ignore_transient=True).items():
            key = (Path(source_relative) / relative).as_posix() if source.is_dir() else source_relative
            if key in hashes:
                raise PlatformPayloadError(f"App构建输入重复绑定：{key}")
            hashes[key] = _hash(source_file)
    return dict(sorted(hashes.items()))


def assert_outer_resource_layout(
    app_root: Path,
    *,
    resources_relative: str,
    allowed_entries: Iterable[str],
) -> None:
    resources = app_root / resources_relative
    if not resources.is_dir():
        raise PlatformPayloadError(f"App缺少外层Resources目录：{resources}")
    allowed = set(allowed_entries)
    unexpected = sorted(path.name for path in resources.iterdir() if path.name not in allowed)
    if unexpected:
        raise PlatformPayloadError("App外层Resources存在未映射项：" + ", ".join(unexpected))
    inspected = tuple(
        resources / name for name in allowed
        if name not in {"capability", "app-manifest.json"}
        and (resources / name).exists()
    )
    for root in inspected:
        for path in (root, *root.rglob("*")) if root.is_dir() else (root,):
            relative = path.relative_to(resources)
            parts = {part.casefold() for part in relative.parts}
            if FORBIDDEN_DIRECTORY_NAMES.intersection(parts):
                raise PlatformPayloadError(f"App外层Resources含禁止目录：{relative.as_posix()}")
            if path.name in {".DS_Store"} or path.suffix == ".pyc" or "__pycache__" in parts or ".pytest_cache" in parts:
                raise PlatformPayloadError(f"App外层Resources含缓存文件：{relative.as_posix()}")


def _payload_hashes(app_root: Path, payload_roots: Iterable[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative_root in payload_roots:
        root = app_root / relative_root
        assert_source_tree_clean(root)
        for relative, path in _files(root).items():
            key = (Path(relative_root) / relative).as_posix() if root.is_dir() else relative_root
            if key in hashes:
                raise PlatformPayloadError(f"交付资源重复绑定：{key}")
            hashes[key] = _hash(path)
    return dict(sorted(hashes.items()))


def build_outer_payload_manifest(
    repository_root: Path,
    app_root: Path,
    *,
    source_mappings: Iterable[tuple[str, str]],
    payload_roots: Iterable[str],
    build_inputs: Iterable[str] = (),
    strict_directories: Iterable[tuple[str, str]] = (),
) -> dict[str, object]:
    mappings = tuple(source_mappings)
    roots = tuple(payload_roots)
    return {
        "source_mappings": [
            {
                "source": source,
                "target": target,
                "exclude": list(SOURCE_EXCLUDES.get(source, ())),
            }
            for source, target in mappings
        ],
        "source_content_hashes": _mapped_hashes(repository_root, app_root, mappings),
        "build_input_hashes": _build_input_hashes(repository_root, tuple(build_inputs)),
        "content_hashes": _payload_hashes(app_root, roots),
        "strict_directory_manifests": {
            label: build_strict_directory_manifest(app_root / relative)
            for label, relative in strict_directories
        },
    }


def verify_outer_payload_manifest(
    repository_root: Path,
    app_root: Path,
    manifest: object,
    *,
    source_mappings: Iterable[tuple[str, str]],
    payload_roots: Iterable[str],
    build_inputs: Iterable[str] = (),
    strict_directories: Iterable[tuple[str, str]] = (),
    verify_sources: bool = True,
) -> None:
    if not isinstance(manifest, dict) or set(manifest) != {
        "source_mappings", "source_content_hashes", "build_input_hashes", "content_hashes",
        "strict_directory_manifests",
    }:
        raise PlatformPayloadError("App Manifest中的外层资源绑定无效")
    mappings = tuple(source_mappings)
    expected_mappings = [
        {
            "source": source,
            "target": target,
            "exclude": list(SOURCE_EXCLUDES.get(source, ())),
        }
        for source, target in mappings
    ]
    if manifest.get("source_mappings") != expected_mappings:
        raise PlatformPayloadError("App Manifest中的外层资源映射不一致")
    if verify_sources and manifest.get("source_content_hashes") != _source_hashes(repository_root, mappings):
        raise PlatformPayloadError("App外层资源源码哈希不一致")
    if verify_sources and manifest.get("build_input_hashes") != _build_input_hashes(
        repository_root, tuple(build_inputs)
    ):
        raise PlatformPayloadError("App Host构建输入源码哈希不一致")
    if manifest.get("content_hashes") != _payload_hashes(app_root, tuple(payload_roots)):
        raise PlatformPayloadError("App外层交付资源哈希不一致")
    strict = manifest.get("strict_directory_manifests")
    directories = tuple(strict_directories)
    if not isinstance(strict, dict) or set(strict) != {label for label, _relative in directories}:
        raise PlatformPayloadError("App严格交付目录Manifest集合不一致")
    for label, relative in directories:
        verify_strict_directory_manifest(app_root / relative, strict[label])
