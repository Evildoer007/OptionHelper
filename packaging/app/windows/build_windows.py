#!/usr/bin/env python3
"""Build the Windows OptionHelper portable App from one verified Capability."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib import metadata
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
SKILL_PACKAGING = ROOT / "packaging" / "skill"
APP_PACKAGING = ROOT / "packaging" / "app"
WINDOWS_APP_ICON = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
WINDOWS_APP_ICON_RELATIVE = Path("assets/icons/optionhelper-app-icon-tile-light.ico")
WINDOWS_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
WINDOWS_ICON_LAYER_COUNT = len(WINDOWS_ICON_SIZES)
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))
if str(APP_PACKAGING) not in sys.path:
    sys.path.insert(0, str(APP_PACKAGING))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from verify_skill import content_tree_entries, tree_hash
from verify_capability import verify_app_capability
from release_contract import APP_VERSION, RELEASE_VERSION, require_app_version, app_platform_versions
from platform_payload import (
    BACKEND_DESKTOP_COMMON_MODULES,
    WINDOWS_ALLOWED_RESOURCE_ENTRIES,
    WINDOWS_BUILD_INPUTS,
    WINDOWS_PAYLOAD_ROOTS,
    WINDOWS_SOURCE_MAPPINGS,
    WINDOWS_STRICT_DIRECTORY_PAYLOADS,
    PlatformPayloadError,
    assert_outer_resource_layout,
    build_outer_payload_manifest,
    stage_python_docx_templates,
    stage_verification_fixture_definition,
)
from python_runtime_licenses import (
    PythonRuntimeLicenseError,
    stage_pyinstaller_dependency_licenses,
    verify_python_runtime_licenses,
)

AGENT_RUNTIME_PACKAGING = ROOT / "packaging" / "app" / "agent_runtime"
if str(AGENT_RUNTIME_PACKAGING) not in sys.path:
    sys.path.insert(0, str(AGENT_RUNTIME_PACKAGING))
from build_runtime import (  # noqa: E402
    ALLOW_SOURCE_RUNTIME_FALLBACK_ENV,
    REQUIRE_NATIVE_RUNTIME_ENV,
    RuntimeBuildError,
    prepare_staged_runtime,
    resolve_runtime_candidate,
)
from name_boundary import assert_agent_runtime_delivery_clean, assert_name_boundary_clean


class WindowsBuildError(RuntimeError):
    pass


AGENT_RUNTIME_LEGAL_FILES = (
    "OptionHelper-Agent-Runtime-LICENSE.txt",
    "OptionHelper-Agent-Runtime-NOTICES.txt",
    "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md",
)
REQUIRED_CAPABILITY_LICENSES = (
    "ReportLab-LICENSE.txt",
    "Pillow-LICENSE.txt",
    "pypdf-LICENSE.txt",
    "python-docx-LICENSE.txt",
    "openpyxl-LICENSE.txt",
)
WINDOWS_SIGNING_CERT_SHA1_ENV = "OPTIONHELPER_WINDOWS_SIGNING_CERT_SHA1"
WINDOWS_TIMESTAMP_URL = "https://timestamp.digicert.com"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WindowsBuildError(f"环境变量{name}必须是布尔值")


def _runtime_required(explicit: bool | None, *, formal: bool) -> bool:
    if explicit is not None:
        return explicit
    return _env_flag(REQUIRE_NATIVE_RUNTIME_ENV, formal)


def _source_fallback_enabled(explicit: bool | None, *, required: bool) -> bool:
    if required:
        return False
    if explicit is not None:
        return explicit
    return _env_flag(ALLOW_SOURCE_RUNTIME_FALLBACK_ENV, True)


def _assert_source_name_boundary(*, repo_root: Path = ROOT) -> None:
    app_root = repo_root / "products" / "app"
    try:
        assert_name_boundary_clean(
            repo_root,
            paths=(app_root / "backend", app_root / "frontend", app_root / "runtime"),
        )
    except AssertionError as error:
        raise WindowsBuildError(str(error)) from error


def _progress(message: str) -> None:
    print(f"[platform] {message}", flush=True)


NUMERIC_RUNTIME_MODULES = ("numpy", "pandas", "scipy", "numba")
PDF_RUNTIME_MODULES = (
    "reportlab",
    "reportlab.pdfgen.canvas",
    "reportlab.pdfbase.pdfmetrics",
    "reportlab.pdfbase.cidfonts",
    "PIL",
    "PIL.Image",
    "PIL.ImageColor",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "pypdf",
    "docx",
    "bs4",
    "bs4.builder",
    "bs4.builder._htmlparser",
    "soupsieve",
    "lxml",
    "lxml.etree",
    "openpyxl",
)
PDF_RUNTIME_VERSION = "5.0.0"
PILLOW_RUNTIME_VERSION = "12.3.0"
PYPDF_RUNTIME_VERSION = "6.16.0"
PYTHON_DOCX_RUNTIME_VERSION = "1.2.0"
BEAUTIFULSOUP_RUNTIME_VERSION = "4.15.0"
SOUPSIEVE_RUNTIME_VERSION = "2.9.1"
LXML_RUNTIME_VERSION = "6.1.1"
OPENPYXL_RUNTIME_VERSION = "3.1.5"
DOCUMENT_RUNTIME_DISTRIBUTIONS = {
    "pypdf": PYPDF_RUNTIME_VERSION,
    "python-docx": PYTHON_DOCX_RUNTIME_VERSION,
    "beautifulsoup4": BEAUTIFULSOUP_RUNTIME_VERSION,
    "soupsieve": SOUPSIEVE_RUNTIME_VERSION,
    "lxml": LXML_RUNTIME_VERSION,
    "openpyxl": OPENPYXL_RUNTIME_VERSION,
}
EXCLUDED_BACKEND_MODULES = (
    "IPython", "PySide6", "cv2", "datasets", "debugpy", "h5py",
    "jedi", "keras", "matplotlib", "pyarrow", "pytest", "sklearn", "tensorflow",
    "tensorboard", "torch", "torchaudio", "torchvision", "transformers",
    "pandas.tests", "numpy.tests", "scipy.tests", "numba.tests",
)
MAX_BACKEND_BYTES = 600 * 1024 * 1024
MAX_WINDOWS_APP_BYTES = 750 * 1024 * 1024
EXTERNAL_COMMAND_TIMEOUT_SECONDS = 900
MINIMUM_BUILD_PYTHON = (3, 12)
PYINSTALLER_VERSION = "6.21.0"


def backend_runtime_modules(*, app_root: Path = APP_ROOT) -> tuple[str, ...]:
    """Return every App backend module that must survive freezing."""

    modules: set[str] = set()
    source_root = app_root / "backend"
    for source in source_root.rglob("*.py"):
        relative = source.relative_to(app_root).with_suffix("")
        parts = relative.parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.add(".".join(parts))
    return tuple(sorted(modules))


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise WindowsBuildError(f"命令超时：{' '.join(command)}\n{output}") from error
    except OSError as error:
        raise WindowsBuildError(f"无法启动构建工具{command[0]}：{error}") from error
    if completed.returncode:
        raise WindowsBuildError(f"命令失败：{' '.join(command)}\n{completed.stdout}")



def _run_powershell(script: str, *arguments: str) -> None:
    """Pass paths as literal script arguments, never as PowerShell source."""
    with tempfile.TemporaryDirectory(prefix="optionhelper-powershell-") as directory:
        path = Path(directory) / "verify.ps1"
        path.write_text('$ErrorActionPreference = "Stop"\n' + script, encoding="utf-8-sig")
        _run([
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(path), *arguments,
        ])

def formal_signing_thumbprint() -> str:
    """Require an explicit real Authenticode certificate for formal builds."""

    thumbprint = os.environ.get(WINDOWS_SIGNING_CERT_SHA1_ENV, "").strip().upper()
    if len(thumbprint) != 40 or any(character not in "0123456789ABCDEF" for character in thumbprint):
        raise WindowsBuildError(
            f"Windows正式发布必须通过{WINDOWS_SIGNING_CERT_SHA1_ENV}指定40位证书SHA-1指纹"
        )
    if shutil.which("signtool") is None:
        raise WindowsBuildError("Windows正式发布缺少signtool，禁止生成unsigned正式归档")
    return thumbprint


def sign_and_verify_executable(executable: Path, thumbprint: str) -> None:
    """Apply and verify Authenticode; unsigned output is never promoted."""

    _run([
        "signtool", "sign", "/sha1", thumbprint, "/fd", "SHA256",
        "/td", "SHA256", "/tr", WINDOWS_TIMESTAMP_URL, str(executable),
    ])
    _run(["signtool", "verify", "/pa", "/all", "/v", str(executable)])
    script = (
        "$signature=Get-AuthenticodeSignature -LiteralPath $args[0];"
        "if($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate){exit 2};"
        "if($signature.SignerCertificate.Thumbprint -ne $args[1]){exit 3}"
    )
    _run_powershell(script, str(executable), thumbprint)


def _validate_application_icon(icon: Path = WINDOWS_APP_ICON) -> int:
    try:
        data = icon.read_bytes()
    except OSError as error:
        raise WindowsBuildError(f"缺少Windows应用图标：{icon}") from error
    if len(data) < 6 or data[:4] != b"\x00\x00\x01\x00":
        raise WindowsBuildError("Windows应用图标不是有效ICO")
    layer_count = int.from_bytes(data[4:6], "little")
    if layer_count != WINDOWS_ICON_LAYER_COUNT or len(data) < 6 + 16 * layer_count:
        raise WindowsBuildError(f"Windows应用图标必须包含{WINDOWS_ICON_LAYER_COUNT}层")
    sizes: list[int] = []
    directory_end = 6 + 16 * layer_count
    for index in range(layer_count):
        entry = data[6 + 16 * index:22 + 16 * index]
        width = entry[0] or 256
        height = entry[1] or 256
        payload_size = int.from_bytes(entry[8:12], "little")
        payload_offset = int.from_bytes(entry[12:16], "little")
        if width != height or payload_size <= 0 or payload_offset < directory_end or payload_offset + payload_size > len(data):
            raise WindowsBuildError("Windows应用图标目录项无效")
        sizes.append(width)
    if tuple(sizes) != WINDOWS_ICON_SIZES:
        raise WindowsBuildError(f"Windows应用图标层级必须为{WINDOWS_ICON_SIZES}")
    return layer_count


def verified_capability_icon(
    capability_root: Path,
    *,
    source_icon: Path = WINDOWS_APP_ICON,
) -> Path:
    capability_icon = capability_root / WINDOWS_APP_ICON_RELATIVE
    _validate_application_icon(source_icon)
    _validate_application_icon(capability_icon)
    if _hash(source_icon) != _hash(capability_icon):
        raise WindowsBuildError("当次Capability的Windows ICO与受控源哈希不一致")
    return capability_icon


def backend_build_command(
    workspace: Path,
    icon: Path = WINDOWS_APP_ICON,
    *,
    app_root: Path = APP_ROOT,
) -> list[str]:
    _validate_application_icon(icon)
    launcher = app_root / "desktop" / "macos" / "backend_launcher.py"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "OptionHelperBackend", "--distpath", str(workspace / "pyinstaller-dist"),
        "--workpath", str(workspace / "pyinstaller-work"), "--specpath", str(workspace / "pyinstaller-spec"),
        "--paths", str(app_root), "--icon", str(icon),
        "--exclude-module", "runtime", "--exclude-module", "modules",
    ]
    for module in backend_runtime_modules(app_root=app_root):
        command.extend(("--hidden-import", module))
    for module in BACKEND_DESKTOP_COMMON_MODULES:
        command.extend(("--hidden-import", module))
    for module in NUMERIC_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    for module in PDF_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    command.extend(("--copy-metadata", "reportlab"))
    # Keep python-docx's base document and header/footer/style XML templates in
    # the frozen backend; these resources are opened lazily during rendering.
    command.extend(("--collect-data", "docx"))
    command.extend(("--collect-data", "certifi"))
    # JSON Schema format validation imports this grammar lazily at first use.
    command.extend(("--collect-data", "rfc3987_syntax"))
    command.extend(("--collect-data", "lark"))
    for module in EXCLUDED_BACKEND_MODULES:
        command.extend(("--exclude-module", module))
    command.append(str(launcher))
    return command


def shell_build_command(
    output: Path,
    icon: Path = WINDOWS_APP_ICON,
    *,
    app_root: Path = APP_ROOT,
    intermediate_root: Path | None = None,
) -> list[str]:
    _validate_application_icon(icon)
    command = [
        "dotnet", "publish", str(app_root / "desktop" / "windows" / "OptionHelper.Windows.csproj"),
        "--configuration", "Release", "--runtime", "win-x64", "--self-contained", "true",
        f"-p:ApplicationIcon={icon}", "--output", str(output),
        f"-p:Version={APP_VERSION.removeprefix('v').replace('.alpha', '-alpha')}",
        f"-p:AssemblyVersion={app_platform_versions(APP_VERSION)[0]}.0",
        f"-p:FileVersion={app_platform_versions(APP_VERSION)[0]}.0",
        f"-p:InformationalVersion={APP_VERSION.removeprefix('v')}",
    ]
    if intermediate_root is not None:
        command.extend((
            f"-p:BaseIntermediateOutputPath={intermediate_root / 'obj'}/",
            f"-p:BaseOutputPath={intermediate_root / 'bin'}/",
        ))
    return command


def copy_application_icon(resources: Path, icon: Path = WINDOWS_APP_ICON) -> Path:
    _validate_application_icon(icon)
    target = resources / "icons" / "OptionHelper.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon, target)
    if target.read_bytes() != icon.read_bytes():
        raise WindowsBuildError("Windows安装物内的应用图标与受控ICO不一致")
    return target


def _verify_icon_resource_group(group: bytes, read_icon, source: bytes) -> None:
    """Compare ICO layers with RT_GROUP_ICON/RT_ICON without image decoding.

    PE group entries replace the ICO file offset with a resource ID. IDs and
    entry order are not image identity; compare metadata and payloads instead.
    """
    from collections import Counter
    import struct

    count = int.from_bytes(source[4:6], "little")
    if len(group) != 6 + 14 * count or group[:6] != source[:6]:
        raise WindowsBuildError("EXE图标资源目录或层数与源ICO不一致")
    expected = []
    actual = []
    for index in range(count):
        entry = source[6 + index * 16:22 + index * 16]
        size, offset = struct.unpack_from("<II", entry, 8)
        expected.append((entry[:12], source[offset:offset + size]))
        embedded = group[6 + index * 14:20 + index * 14]
        resource_id = struct.unpack_from("<H", embedded, 12)[0]
        payload = read_icon(resource_id)
        if len(payload) != int.from_bytes(embedded[8:12], "little"):
            raise WindowsBuildError(f"EXE图标资源{resource_id}长度不一致")
        actual.append((embedded[:12], payload))
    if Counter(actual) != Counter(expected):
        raise WindowsBuildError("EXE图标资源内容与源ICO不一致")


def verify_executable_icon(executable: Path, icon: Path = WINDOWS_APP_ICON) -> None:
    """Verify every embedded icon layer through Win32 resources, not GDI+."""
    import ctypes
    from ctypes import wintypes

    _validate_application_icon(icon)
    if not executable.is_file():
        raise WindowsBuildError(f"缺少待验图标的Windows EXE：{executable}")
    if os.name != "nt":
        raise WindowsBuildError("EXE图标资源校验必须在Windows构建主机执行")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, pointer, pointer, pointer, ctypes.c_ssize_t)
    signatures = {
        "LoadLibraryExW": ([wintypes.LPCWSTR, pointer, wintypes.DWORD], pointer),
        "EnumResourceNamesW": ([pointer, pointer, callback_type, ctypes.c_ssize_t], wintypes.BOOL),
        "FindResourceW": ([pointer, pointer, pointer], pointer),
        "SizeofResource": ([pointer, pointer], wintypes.DWORD),
        "LoadResource": ([pointer, pointer], pointer),
        "LockResource": ([pointer], pointer),
        "FreeLibrary": ([pointer], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result

    def failure(stage):
        return WindowsBuildError(f"读取EXE图标资源失败：{stage}；{ctypes.WinError(ctypes.get_last_error())}")

    # LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE: never run EXE code.
    module = kernel.LoadLibraryExW(str(executable.resolve()), None, 0x22)
    if not module:
        raise failure("LoadLibraryExW")
    try:
        names = []

        @callback_type
        def collect(_module, _kind, name, _parameter):
            names.append(name if name <= 0xffff else ctypes.wstring_at(name))
            return True

        if not kernel.EnumResourceNamesW(module, 14, collect, 0):
            raise failure("RT_GROUP_ICON")
        if not names:
            raise WindowsBuildError("EXE未包含应用图标")

        def read_resource(kind, name):
            key = ctypes.cast(ctypes.c_wchar_p(name), pointer) if isinstance(name, str) else name
            resource = kernel.FindResourceW(module, key, kind)
            if not resource:
                raise failure(f"FindResourceW({kind}, {name})")
            size = kernel.SizeofResource(module, resource)
            loaded = kernel.LoadResource(module, resource)
            address = kernel.LockResource(loaded) if loaded else None
            if not size or not address:
                raise failure(f"LoadResource({kind}, {name})")
            return ctypes.string_at(address, size)

        source = icon.read_bytes()
        for name in names:
            _verify_icon_resource_group(read_resource(14, name), lambda resource_id: read_resource(3, resource_id), source)
    finally:
        kernel.FreeLibrary(module)


def verify_installer_payload(installer: Path, app: Path) -> None:
    """Verify every ZIP member against the accepted package, not only EXEs."""
    expected = {
        path.relative_to(app.parent).as_posix(): path
        for path in app.rglob("*") if path.is_file()
    }
    try:
        with zipfile.ZipFile(installer) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise WindowsBuildError("Windows ZIP文件集合与验收后的应用不一致")
            for name, source in expected.items():
                digest = sha256()
                with archive.open(name) as member:
                    for block in iter(lambda: member.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != _hash(source):
                    raise WindowsBuildError(f"Windows ZIP文件内容与验收后的应用不一致：{name}")
    except (OSError, zipfile.BadZipFile) as error:
        raise WindowsBuildError(f"Windows ZIP读取或CRC校验失败：{error}") from error


def _verify_backend(package: Path) -> None:
    size = sum(path.stat().st_size for path in package.rglob("*") if path.is_file())
    if size > MAX_BACKEND_BYTES:
        raise WindowsBuildError(f"App Host体积{size / 1024 / 1024:.1f}MB超过600MB上限")
    internal = package / "_internal"
    forbidden = [name for name in EXCLUDED_BACKEND_MODULES if "." not in name and (internal / name).exists()]
    if forbidden:
        raise WindowsBuildError("App Host包含禁止的无关依赖：" + ", ".join(forbidden))
    # Pure-Python packages may live inside PyInstaller's PYZ archive.  The
    # packaged executable is therefore checked with --probe-pdf-runtime after
    # all verified resources have been staged instead of inferring availability
    # from a physical _internal/reportlab directory.


def _copy_tree(source: Path, target: Path, *, extra_ignored: tuple[str, ...] = ()) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        patterns = shutil.ignore_patterns(*extra_ignored)(_directory, names)
        return {
            name for name in names
            if name in {"__pycache__", ".pytest_cache", ".DS_Store"}
            or name.endswith(".pyc")
            or name in patterns
        }

    shutil.copytree(source, target, ignore=ignore)


def _copy_agent_runtime_licenses(destination: Path, *, license_root: Path = ROOT / "LICENSES") -> None:
    destination.mkdir(parents=True)
    for name in AGENT_RUNTIME_LEGAL_FILES:
        source = license_root / name
        if not source.is_file():
            raise WindowsBuildError(f"缺少Agent Runtime发行声明：{source}")
        shutil.copy2(source, destination / name)


def _verify_required_licenses(resources: Path, *, license_root: Path = ROOT / "LICENSES") -> None:
    for name in REQUIRED_CAPABILITY_LICENSES:
        source = license_root / name
        packaged = resources / "LICENSES" / "capability" / name
        if not source.is_file() or not packaged.is_file():
            raise WindowsBuildError(f"Windows App缺少必需第三方许可证：{name}")
        if packaged.read_bytes() != source.read_bytes():
            raise WindowsBuildError(f"Windows App第三方许可证与受控来源不一致：{name}")


def _assert_staged_capability(source: Path, staged: Path) -> None:
    if content_tree_entries(source) != content_tree_entries(staged):
        raise WindowsBuildError("Windows App内置Capability不是当次Skill的逐字节副本")
    if (source / "capability-manifest.json").read_bytes() != (staged / "capability-manifest.json").read_bytes():
        raise WindowsBuildError("Windows App内置Capability Manifest不是当次Skill的逐字节副本")


def _manifest(
    app_version: str,
    capability: dict[str, object],
    capability_manifest: Path,
    icon: Path = WINDOWS_APP_ICON,
    agent_runtime: dict[str, object] | None = None,
    signing_thumbprint: str | None = None,
    formal_release: bool = False,
    outer_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    layer_count = _validate_application_icon(icon)
    if formal_release and app_version != APP_VERSION:
        raise WindowsBuildError(f"Windows正式App Manifest版本必须为{APP_VERSION}")
    normalized_thumbprint = (signing_thumbprint or "").strip().upper()
    if formal_release and (
        len(normalized_thumbprint) != 40
        or any(character not in "0123456789ABCDEF" for character in normalized_thumbprint)
    ):
        raise WindowsBuildError("Windows正式App Manifest不得记录unsigned签名")
    return {
        "app_version": app_version,
        "build_version": APP_VERSION,
        "release_status": "formal_release" if formal_release else "local_candidate",
        "formal_release": formal_release,
        "bundle_id": "com.optionhelper.app",
        "platform": "windows-x86_64",
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": _hash(capability_manifest),
        "capability_content_tree_hash": capability["content_tree_hash"],
        "shared_payload_hash": capability["shared_payload_hash"],
        "protocol_id": capability["protocol_id"],
        "design_system_id": capability["design_system_id"],
        "shell_language": "csharp",
        "build_tool": "dotnet",
        "signing": (
            {
                "method": "authenticode",
                "certificate_thumbprint": normalized_thumbprint,
                "status": "valid",
            }
            if formal_release else
            {"method": "unsigned", "status": "local_candidate"}
        ),
        "agent_runtime": agent_runtime or {
            "status": "disabled",
            "support_status": "development_only",
            "resource_path": None,
            "manifest_path": None,
        },
        "outer_payload": outer_payload or {},
        "application_icon": {
            "source": WINDOWS_APP_ICON_RELATIVE.as_posix(),
            "packaged_path": "Resources/icons/OptionHelper.ico",
            "sha256": _hash(icon),
            "format": "ico",
            "layer_count": layer_count,
            "embedded_executables": [
                "OptionHelper.exe",
                "Resources/backend/OptionHelperBackend/OptionHelperBackend.exe",
            ],
        },
    }


def build_windows(
    app_version: str,
    capability_root: Path,
    *,
    dist_root: Path,
    versions_root: Path = ROOT / "versions",
    agent_runtime_path: Path | None = None,
    require_native_runtime: bool | None = None,
    allow_source_runtime_fallback: bool | None = None,
    formal_release: bool = False,
    repo_root: Path = ROOT,
) -> dict[str, Path]:
    try:
        require_app_version(app_version)
    except ValueError as error:
        raise WindowsBuildError(str(error)) from error
    repo_root = repo_root.expanduser().resolve()
    app_root = repo_root / "products" / "app"
    app_packaging = repo_root / "packaging" / "app"
    application_icon_source = repo_root / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
    license_root = repo_root / "LICENSES"
    runtime_source_root = app_root / "runtime" / "optionhelper_agent_runtime"
    formal_context = formal_release or versions_root.resolve() == (ROOT / "versions").resolve()
    signing_thumbprint = formal_signing_thumbprint() if formal_context else None
    native_runtime_required = _runtime_required(
        require_native_runtime,
        formal=formal_context,
    )
    source_fallback_enabled = _source_fallback_enabled(
        allow_source_runtime_fallback,
        required=native_runtime_required,
    )
    if native_runtime_required:
        _assert_source_name_boundary(repo_root=repo_root)
    _progress("正在验证当次Capability")
    check_prerequisites(capability_root)
    if verify_app_capability(capability_root):
        raise WindowsBuildError("App Capability未通过验收")
    capability_manifest = capability_root / "capability-manifest.json"
    capability = json.loads(capability_manifest.read_text(encoding="utf-8"))
    if tree_hash(content_tree_entries(capability_root)) != capability.get("content_tree_hash"):
        raise WindowsBuildError("Capability目录树哈希与Manifest不一致")
    application_icon = verified_capability_icon(
        capability_root,
        source_icon=application_icon_source,
    )

    release_root = versions_root.resolve() / (RELEASE_VERSION if formal_context else app_version)
    if release_root.exists() and not release_root.is_dir():
        raise WindowsBuildError("Windows构建记录路径不是目录")
    if not release_root.exists():
        if versions_root.resolve() == (ROOT / "versions").resolve():
            raise WindowsBuildError("Windows正式归档只能由受控发行事务创建")
        release_root.mkdir(parents=True, exist_ok=False)
    installer_name = f"OptionHelper-{app_version}-windows-x86_64.zip"
    archive_installer = release_root / installer_name
    if archive_installer.exists() or (release_root / "app-manifest-windows.json").exists():
        raise WindowsBuildError(f"Windows App历史版本已存在；不得覆盖已签发的{RELEASE_VERSION}文件")

    with tempfile.TemporaryDirectory(prefix="optionhelper-windows-") as temporary_name:
        temporary = Path(temporary_name)
        environment = dict(os.environ)
        environment["PYINSTALLER_CONFIG_DIR"] = str(temporary / "pyinstaller-config")
        _progress("正在构建后端运行时")
        _run(
            backend_build_command(temporary, application_icon, app_root=app_root),
            cwd=repo_root,
            env=environment,
        )
        backend = temporary / "pyinstaller-dist" / "OptionHelperBackend"
        if not (backend / "OptionHelperBackend.exe").is_file():
            raise WindowsBuildError("PyInstaller未生成OptionHelperBackend.exe")
        try:
            stage_python_docx_templates(backend)
        except PlatformPayloadError as error:
            raise WindowsBuildError(str(error)) from error
        _verify_backend(backend)
        verify_executable_icon(backend / "OptionHelperBackend.exe", application_icon)
        if signing_thumbprint is not None:
            sign_and_verify_executable(backend / "OptionHelperBackend.exe", signing_thumbprint)

        _progress("正在构建Windows应用壳")
        shell = temporary / "shell"
        _run(
            shell_build_command(
                shell,
                application_icon,
                app_root=app_root,
                intermediate_root=temporary / "dotnet-intermediate",
            ),
            cwd=repo_root,
        )
        if not (shell / "OptionHelper.exe").is_file():
            raise WindowsBuildError("dotnet未生成OptionHelper.exe")
        verify_executable_icon(shell / "OptionHelper.exe", application_icon)
        if signing_thumbprint is not None:
            sign_and_verify_executable(shell / "OptionHelper.exe", signing_thumbprint)
        _run([str(shell / "OptionHelper.exe"), "--check-webview2"], cwd=shell)

        _progress("正在组装应用资源")
        app = temporary / "OptionHelper"
        resources = app / "Resources"
        app.mkdir()
        for path in shell.iterdir():
            target = app / path.name
            if path.is_dir():
                _copy_tree(path, target)
            else:
                shutil.copy2(path, target)
        _copy_tree(backend, resources / "backend" / "OptionHelperBackend")
        _copy_tree(app_root / "frontend", resources / "frontend", extra_ignored=("*.md",))
        brand_icons = resources / "assets" / "icons"
        brand_icons.mkdir(parents=True)
        for name in ("optionhelper-app-icon-tile-light.svg", "optionhelper-app-icon-tile-dark.svg"):
            shutil.copy2(repo_root / "assets" / "icons" / name, brand_icons / name)
        try:
            stage_verification_fixture_definition(
                repo_root,
                app,
                resources_relative="Resources",
            )
        except PlatformPayloadError as error:
            raise WindowsBuildError(str(error)) from error
        _copy_tree(capability_root, resources / "capability" / "option-helper")
        _assert_staged_capability(capability_root, resources / "capability" / "option-helper")
        _copy_tree(
            license_root,
            resources / "LICENSES" / "capability",
            extra_ignored=("README.md", *AGENT_RUNTIME_LEGAL_FILES),
        )
        _copy_agent_runtime_licenses(
            resources / "LICENSES" / "agent-runtime",
            license_root=license_root,
        )
        _verify_required_licenses(resources, license_root=license_root)
        try:
            runtime_candidate = resolve_runtime_candidate(
                "windows-x64",
                explicit=agent_runtime_path,
                repository_root=repo_root,
                formal=formal_context,
            )
            runtime_summary = prepare_staged_runtime(
                resources,
                "windows-x64",
                runtime_candidate,
                required=native_runtime_required,
                allow_source_fallback=source_fallback_enabled,
                source_root=runtime_source_root,
                require_source_provenance=native_runtime_required,
            )
        except RuntimeBuildError as error:
            raise WindowsBuildError(str(error)) from error
        _run([
            str(resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe"),
            "--probe-pdf-runtime", "--resource-dir", str(resources),
        ])
        _run([
            str(resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe"),
            "--probe-compute-worker", "--resource-dir", str(resources),
        ])
        staged_icon = copy_application_icon(resources, application_icon)
        acceptance_entry = app / "verify-windows.py"
        shutil.copy2(app_packaging / "windows" / "verify_windows.py", acceptance_entry)
        verification_root = app / "verification"
        verification_root.mkdir()
        shutil.copy2(app_packaging / "platform_payload.py", verification_root / "platform_payload.py")
        shutil.copy2(app_packaging / "python_runtime_licenses.py", verification_root / "python_runtime_licenses.py")
        try:
            license_inventory = stage_pyinstaller_dependency_licenses(
                temporary / "pyinstaller-work" / "OptionHelperBackend" / "Analysis-00.toc",
                resources / "LICENSES" / "python-runtime",
                requirements_lock=repo_root / "core" / "requirements.lock",
                repository_license_root=license_root,
            )
            verify_python_runtime_licenses(resources / "LICENSES" / "python-runtime", license_inventory)
            (resources / "LICENSES" / "THIRD_PARTY.md").write_text(
                "# 第三方许可证\n\n"
                "- Capability许可证见`capability/`。\n"
                "- Agent Runtime许可证、NOTICE和源码映射见`agent-runtime/`。\n"
                "- PyInstaller冻结Python依赖的许可证与NOTICE见`python-runtime/`清单。\n",
                encoding="utf-8",
                newline="",
            )
            outer_payload = build_outer_payload_manifest(
                repo_root,
                app,
                source_mappings=WINDOWS_SOURCE_MAPPINGS,
                payload_roots=WINDOWS_PAYLOAD_ROOTS,
                build_inputs=WINDOWS_BUILD_INPUTS,
                strict_directories=WINDOWS_STRICT_DIRECTORY_PAYLOADS,
            )
            assert_outer_resource_layout(
                app,
                resources_relative="Resources",
                allowed_entries=WINDOWS_ALLOWED_RESOURCE_ENTRIES,
            )
        except (PlatformPayloadError, PythonRuntimeLicenseError) as error:
            raise WindowsBuildError(str(error)) from error
        manifest_version = app_version if formal_context else "development"
        manifest = _manifest(
            manifest_version,
            capability,
            capability_manifest,
            application_icon,
            agent_runtime=runtime_summary,
            signing_thumbprint=signing_thumbprint,
            formal_release=formal_context,
            outer_payload=outer_payload,
        )
        (resources / "app-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="")
        required = (app / "OptionHelper.exe", resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe", resources / "frontend" / "optchat" / "index.html", staged_icon)
        if not all(path.is_file() for path in required):
            raise WindowsBuildError("Windows App缺少必需资源")
        app_size = sum(path.stat().st_size for path in app.rglob("*") if path.is_file())
        if app_size > MAX_WINDOWS_APP_BYTES:
            raise WindowsBuildError(f"Windows App体积{app_size / 1024 / 1024:.1f}MB超过750MB上限")

        _progress("正在运行Windows后端/API与静态包验收")
        _run([sys.executable, str(acceptance_entry), str(app), "--source-root", str(repo_root)], cwd=temporary)
        # Runtime acceptance must not mutate the payload that will be archived.
        from platform_payload import verify_outer_payload_manifest
        verify_outer_payload_manifest(
            repo_root, app, outer_payload,
            source_mappings=WINDOWS_SOURCE_MAPPINGS,
            payload_roots=WINDOWS_PAYLOAD_ROOTS,
            build_inputs=WINDOWS_BUILD_INPUTS,
            strict_directories=WINDOWS_STRICT_DIRECTORY_PAYLOADS,
        )

        _progress("正在验证安装物")
        dist_root.mkdir(parents=True, exist_ok=True)
        staged_zip = temporary / installer_name
        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(temporary).as_posix())
        verify_installer_payload(staged_zip, app)
        try:
            assert_agent_runtime_delivery_clean(resources)
        except AssertionError as error:
            raise WindowsBuildError(str(error)) from error
        output = dist_root / installer_name
        shutil.copy2(staged_zip, output)
        shutil.copy2(staged_zip, archive_installer)
        checksum = release_root / f"{installer_name}.sha256"
        checksum.write_text(f"{_hash(archive_installer)}  {installer_name}\n", encoding="utf-8", newline="")
        app_manifest = release_root / "app-manifest-windows.json"
        app_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="")
        shutil.copy2(capability_manifest, release_root / "app-capability-manifest-windows.json")
        release_manifest = release_root / "platform-release-manifest-windows.json"
        release_manifest.write_text(json.dumps({
            "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}" if formal_context else "optionhelper.platform-candidate-manifest", "app_version": manifest_version,
            "platform": "windows", "architecture": "x86_64", "installer": {
                "filename": installer_name, "sha256": _hash(archive_installer), "size": archive_installer.stat().st_size,
            }, "app_manifest_hash": _hash(app_manifest), "capability_version": manifest["capability_version"],
            "catalog_version": manifest["catalog_version"], "capability_manifest_hash": manifest["capability_manifest_hash"],
            "capability_content_tree_hash": manifest["capability_content_tree_hash"],
            "shared_payload_hash": manifest["shared_payload_hash"], "protocol_id": manifest["protocol_id"],
            "design_system_id": manifest["design_system_id"],
            "signing_identity": signing_thumbprint or "unsigned",
            "application_icon": manifest["application_icon"],
            "agent_runtime": manifest["agent_runtime"],
            "agent_runtime_status": manifest["agent_runtime"].get("status"),
            "package_static_acceptance": "passed",
            "backend_api_acceptance": "passed",
            "native_interaction_acceptance": "not_run",
            "signature_status": "authenticode_validated" if formal_context else "unsigned_local_candidate",
            "notarized": False,
            "release_status": "formal_release" if formal_context else "local_candidate",
            "formal_distribution_status": "ready" if formal_context else "formal_distribution_unavailable",
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="")
    return {"installer": output, "manifest": app_manifest, "release_manifest": release_manifest}


def check_prerequisites(capability_root: Path) -> None:
    if platform.system() != "Windows":
        raise WindowsBuildError("Windows App只能在Windows构建机封装")
    if sys.version_info < MINIMUM_BUILD_PYTHON:
        raise WindowsBuildError("App构建需要Python 3.12或更高版本")
    try:
        pyinstaller_version = metadata.version("PyInstaller")
    except metadata.PackageNotFoundError as error:
        raise WindowsBuildError("当前Python环境缺少PyInstaller") from error
    if pyinstaller_version != PYINSTALLER_VERSION:
        raise WindowsBuildError(f"PyInstaller必须固定为{PYINSTALLER_VERSION}，当前为{pyinstaller_version}")
    try:
        pdf_version = metadata.version("reportlab")
    except metadata.PackageNotFoundError as error:
        raise WindowsBuildError(f"Windows App构建缺少PDF运行组件ReportLab {PDF_RUNTIME_VERSION}") from error
    if pdf_version != PDF_RUNTIME_VERSION:
        raise WindowsBuildError(f"ReportLab必须固定为{PDF_RUNTIME_VERSION}，当前为{pdf_version}")
    try:
        from PIL import __version__ as pillow_version
    except ImportError as error:
        raise WindowsBuildError(f"Windows App构建缺少PDF图像组件Pillow {PILLOW_RUNTIME_VERSION}") from error
    if pillow_version != PILLOW_RUNTIME_VERSION:
        raise WindowsBuildError(f"Pillow必须固定为{PILLOW_RUNTIME_VERSION}，当前为{pillow_version}")
    for package, expected in DOCUMENT_RUNTIME_DISTRIBUTIONS.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError as error:
            raise WindowsBuildError(f"Windows App构建缺少文档解析组件{package} {expected}") from error
        if actual != expected:
            raise WindowsBuildError(f"{package}必须固定为{expected}，当前为{actual}")
    if not (capability_root / "capability-manifest.json").is_file():
        raise WindowsBuildError("缺少已验证Capability")
    if shutil.which("dotnet") is None:
        raise WindowsBuildError("缺少dotnet SDK 8，无法构建WebView2 Windows壳")
    if shutil.which("powershell") is None:
        raise WindowsBuildError("缺少PowerShell，无法执行Windows签名和凭据权限验收")
    try:
        installed = subprocess.run(
            ["dotnet", "--list-sdks"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise WindowsBuildError("检查dotnet SDK 8超时") from error
    if installed.returncode or not any(line.lstrip().startswith("8.") for line in installed.stdout.splitlines()):
        raise WindowsBuildError("需要dotnet SDK 8，当前环境不满足Windows App构建前置")
    assert_outer_resource_layout,
