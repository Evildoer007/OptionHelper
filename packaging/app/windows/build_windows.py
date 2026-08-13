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
WINDOWS_APP_ICON = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
WINDOWS_APP_ICON_RELATIVE = Path("assets/icons/optionhelper-app-icon-tile-light.ico")
WINDOWS_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
WINDOWS_ICON_LAYER_COUNT = len(WINDOWS_ICON_SIZES)
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))
if str(ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(ROOT / "packaging"))

from verify_skill import content_tree_entries, tree_hash, verify_skill
from release_contract import RELEASE_VERSION, require_release_version


class WindowsBuildError(RuntimeError):
    pass


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
)
PDF_RUNTIME_VERSION = "5.0.0"
PILLOW_RUNTIME_VERSION = "12.3.0"
EXCLUDED_BACKEND_MODULES = (
    "IPython", "PySide6", "cv2", "datasets", "debugpy", "h5py",
    "jedi", "keras", "matplotlib", "pyarrow", "pytest", "sklearn", "tensorflow",
    "tensorboard", "torch", "torchaudio", "torchvision", "transformers",
    "pandas.tests", "numpy.tests", "scipy.tests", "numba.tests",
)
MAX_BACKEND_BYTES = 600 * 1024 * 1024
MAX_WINDOWS_APP_BYTES = 750 * 1024 * 1024
MINIMUM_BUILD_PYTHON = (3, 11)
PYINSTALLER_VERSION = "6.21.0"


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise WindowsBuildError(f"命令失败：{' '.join(command)}\n{completed.stdout}")


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


def verified_capability_icon(capability_root: Path) -> Path:
    source_icon = WINDOWS_APP_ICON
    capability_icon = capability_root / WINDOWS_APP_ICON_RELATIVE
    _validate_application_icon(source_icon)
    _validate_application_icon(capability_icon)
    if _hash(source_icon) != _hash(capability_icon):
        raise WindowsBuildError("当次Capability的Windows ICO与受控源哈希不一致")
    return capability_icon


def backend_build_command(workspace: Path, icon: Path = WINDOWS_APP_ICON) -> list[str]:
    _validate_application_icon(icon)
    launcher = APP_ROOT / "desktop" / "macos" / "backend_launcher.py"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "OptionHelperBackend", "--distpath", str(workspace / "pyinstaller-dist"),
        "--workpath", str(workspace / "pyinstaller-work"), "--specpath", str(workspace / "pyinstaller-spec"),
        "--paths", str(APP_ROOT), "--icon", str(icon),
        "--exclude-module", "runtime", "--exclude-module", "modules",
    ]
    for module in NUMERIC_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    for module in PDF_RUNTIME_MODULES:
        command.extend(("--hidden-import", module))
    command.extend(("--copy-metadata", "reportlab"))
    for module in EXCLUDED_BACKEND_MODULES:
        command.extend(("--exclude-module", module))
    command.append(str(launcher))
    return command


def shell_build_command(output: Path, icon: Path = WINDOWS_APP_ICON) -> list[str]:
    _validate_application_icon(icon)
    return [
        "dotnet", "publish", str(APP_ROOT / "desktop" / "windows" / "OptionHelper.Windows.csproj"),
        "--configuration", "Release", "--runtime", "win-x64", "--self-contained", "true",
        f"-p:ApplicationIcon={icon}", "--output", str(output),
    ]


def copy_application_icon(resources: Path, icon: Path = WINDOWS_APP_ICON) -> Path:
    _validate_application_icon(icon)
    target = resources / "icons" / "OptionHelper.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon, target)
    if target.read_bytes() != icon.read_bytes():
        raise WindowsBuildError("Windows安装物内的应用图标与受控ICO不一致")
    return target


def verify_executable_icon(executable: Path, icon: Path = WINDOWS_APP_ICON) -> None:
    """On the Windows build host, compare the executable icon with the controlled ICO."""
    _validate_application_icon(icon)
    if not executable.is_file():
        raise WindowsBuildError(f"缺少待验图标的Windows EXE：{executable}")
    script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$embedded = [System.Drawing.Icon]::ExtractAssociatedIcon($args[0])
if ($null -eq $embedded) { throw 'EXE未包含应用图标' }
$source = [System.Drawing.Icon]::new([string]$args[1], [int]$embedded.Width, [int]$embedded.Height)
$actual = $embedded.ToBitmap()
$expected = $source.ToBitmap()
if ($actual.Width -ne $expected.Width -or $actual.Height -ne $expected.Height) { throw '图标尺寸不一致' }
for ($y = 0; $y -lt $actual.Height; $y++) {
  for ($x = 0; $x -lt $actual.Width; $x++) {
    if ($actual.GetPixel($x, $y).ToArgb() -ne $expected.GetPixel($x, $y).ToArgb()) { throw '图标像素不一致' }
  }
}
$actual.Dispose(); $expected.Dispose(); $source.Dispose(); $embedded.Dispose()
""".strip()
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(executable), str(icon)])


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


def _copy_tree(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {"__pycache__", ".pytest_cache", ".DS_Store"} or name.endswith(".pyc")}

    shutil.copytree(source, target, ignore=ignore)


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
) -> dict[str, object]:
    layer_count = _validate_application_icon(icon)
    return {
        "app_version": app_version,
        "bundle_id": "com.optionhelper.app",
        "platform": "windows-x86_64",
        "capability_version": capability["capability_version"],
        "catalog_version": capability["catalog_version"],
        "capability_manifest_hash": _hash(capability_manifest),
        "capability_content_tree_hash": capability["content_tree_hash"],
        "protocol_version": capability["protocol_version"],
        "design_system_version": capability["design_system_version"],
        "shell_language": "csharp",
        "build_tool": "dotnet",
        "signing": {"method": "unsigned", "notarized": False},
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
) -> dict[str, Path]:
    try:
        require_release_version(app_version)
    except ValueError as error:
        raise WindowsBuildError(str(error)) from error
    _progress("正在验证当次Capability")
    check_prerequisites(capability_root)
    if verify_skill(capability_root):
        raise WindowsBuildError("Capability未通过verify_skill")
    capability_manifest = capability_root / "capability-manifest.json"
    capability = json.loads(capability_manifest.read_text(encoding="utf-8"))
    if tree_hash(content_tree_entries(capability_root)) != capability.get("content_tree_hash"):
        raise WindowsBuildError("Capability目录树哈希与Manifest不一致")
    application_icon = verified_capability_icon(capability_root)

    release_root = versions_root.resolve() / app_version
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
        _run(backend_build_command(temporary, application_icon), cwd=ROOT, env=environment)
        backend = temporary / "pyinstaller-dist" / "OptionHelperBackend"
        if not (backend / "OptionHelperBackend.exe").is_file():
            raise WindowsBuildError("PyInstaller未生成OptionHelperBackend.exe")
        _verify_backend(backend)
        verify_executable_icon(backend / "OptionHelperBackend.exe", application_icon)

        _progress("正在构建Windows应用壳")
        shell = temporary / "shell"
        _run(shell_build_command(shell, application_icon), cwd=ROOT)
        if not (shell / "OptionHelper.exe").is_file():
            raise WindowsBuildError("dotnet未生成OptionHelper.exe")
        verify_executable_icon(shell / "OptionHelper.exe", application_icon)
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
        _copy_tree(APP_ROOT / "backend", resources / "app" / "backend")
        _copy_tree(APP_ROOT / "config", resources / "app" / "config")
        _copy_tree(APP_ROOT / "frontend", resources / "frontend")
        _copy_tree(capability_root, resources / "capability" / "option-helper")
        _assert_staged_capability(capability_root, resources / "capability" / "option-helper")
        _copy_tree(ROOT / "core" / "src" / "runtime", resources / "runtime")
        _copy_tree(ROOT / "LICENSES", resources / "LICENSES" / "capability")
        _run([
            str(resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe"),
            "--probe-pdf-runtime", "--resource-dir", str(resources),
        ])
        staged_icon = copy_application_icon(resources, application_icon)
        manifest = _manifest(app_version, capability, capability_manifest, application_icon)
        (resources / "app-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        required = (app / "OptionHelper.exe", resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe", resources / "frontend" / "optchat" / "index.html", staged_icon)
        if not all(path.is_file() for path in required):
            raise WindowsBuildError("Windows App缺少必需资源")
        app_size = sum(path.stat().st_size for path in app.rglob("*") if path.is_file())
        if app_size > MAX_WINDOWS_APP_BYTES:
            raise WindowsBuildError(f"Windows App体积{app_size / 1024 / 1024:.1f}MB超过750MB上限")

        _progress("正在验证安装物")
        dist_root.mkdir(parents=True, exist_ok=True)
        staged_zip = temporary / installer_name
        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(temporary).as_posix())
        with zipfile.ZipFile(staged_zip) as archive:
            damaged = archive.testzip()
            if damaged is not None:
                raise WindowsBuildError(f"Windows ZIP成员CRC无效：{damaged}")
            expected_executables = {
                "OptionHelper/OptionHelper.exe": app / "OptionHelper.exe",
                "OptionHelper/Resources/backend/OptionHelperBackend/OptionHelperBackend.exe": resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe",
            }
            for member, source in expected_executables.items():
                if archive.read(member) != source.read_bytes():
                    raise WindowsBuildError(f"Windows ZIP内EXE与图标验收后的文件不一致：{member}")
        output = dist_root / installer_name
        shutil.copy2(staged_zip, output)
        shutil.copy2(staged_zip, archive_installer)
        checksum = release_root / f"{installer_name}.sha256"
        checksum.write_text(f"{_hash(archive_installer)}  {installer_name}\n", encoding="utf-8")
        app_manifest = release_root / "app-manifest-windows.json"
        app_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        release_manifest = release_root / "platform-release-manifest-windows.json"
        release_manifest.write_text(json.dumps({
            "schema": f"optionhelper.platform-release-manifest/{RELEASE_VERSION}", "app_version": app_version,
            "platform": "windows", "architecture": "x86_64", "installer": {
                "filename": installer_name, "sha256": _hash(archive_installer), "size": archive_installer.stat().st_size,
            }, "app_manifest_hash": _hash(app_manifest), "capability_version": manifest["capability_version"],
            "catalog_version": manifest["catalog_version"], "capability_manifest_hash": manifest["capability_manifest_hash"],
            "capability_content_tree_hash": manifest["capability_content_tree_hash"], "protocol_version": manifest["protocol_version"],
            "design_system_version": manifest["design_system_version"], "signing_identity": "unsigned",
            "application_icon": manifest["application_icon"],
            "signature_status": "unsigned_local_candidate", "notarized": False, "release_status": "local_candidate",
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"installer": output, "manifest": app_manifest, "release_manifest": release_manifest}


def check_prerequisites(capability_root: Path) -> None:
    if platform.system() != "Windows":
        raise WindowsBuildError("Windows App只能在Windows构建机封装")
    if sys.version_info < MINIMUM_BUILD_PYTHON:
        raise WindowsBuildError("App构建需要Python 3.11或更高版本")
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
    if not (capability_root / "capability-manifest.json").is_file():
        raise WindowsBuildError("缺少已验证Capability")
    if shutil.which("dotnet") is None:
        raise WindowsBuildError("缺少dotnet SDK 8，无法构建WebView2 Windows壳")
    if shutil.which("powershell") is None:
        raise WindowsBuildError("缺少PowerShell，无法验证Windows EXE应用图标")
    installed = subprocess.run(["dotnet", "--list-sdks"], text=True, capture_output=True)
    if installed.returncode or not any(line.lstrip().startswith("8.") for line in installed.stdout.splitlines()):
        raise WindowsBuildError("需要dotnet SDK 8，当前环境不满足Windows App构建前置")
