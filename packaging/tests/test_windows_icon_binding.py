"""Windows application-icon packaging contracts."""

from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "packaging" / "app" / "windows" / "build_windows.py"
SPEC = importlib.util.spec_from_file_location("test_windows_icon_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


def test_windows_icon_source_is_the_controlled_ten_layer_ico() -> None:
    icon = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.ico"
    data = icon.read_bytes()
    assert BUILDER.WINDOWS_APP_ICON == icon
    assert data[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(data[4:6], "little") == 10


def test_one_click_builder_checks_the_icon_and_powershell_before_building() -> None:
    script = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
    assert "assets\\icons\\optionhelper-app-icon-tile-light.ico" in script
    assert "where powershell" in script


def test_both_windows_executables_receive_the_controlled_icon() -> None:
    backend = BUILDER.backend_build_command(Path("workspace"))
    assert backend[backend.index("--icon") + 1] == str(BUILDER.WINDOWS_APP_ICON)

    shell = BUILDER.shell_build_command(Path("shell"))
    assert f"-p:ApplicationIcon={BUILDER.WINDOWS_APP_ICON}" in shell


def test_staged_installer_contains_an_exact_icon_copy(tmp_path: Path) -> None:
    resources = tmp_path / "Resources"
    staged = BUILDER.copy_application_icon(resources)
    assert staged == resources / "icons" / "OptionHelper.ico"
    assert staged.read_bytes() == BUILDER.WINDOWS_APP_ICON.read_bytes()


def test_manifest_binds_the_icon_hash_and_layer_count(tmp_path: Path) -> None:
    capability_manifest = tmp_path / "capability-manifest.json"
    capability_manifest.write_text("{}\n", encoding="utf-8")
    manifest = BUILDER._manifest(
        "v1.0",
        {
            "capability_version": "v1.0",
            "content_tree_hash": "tree",
            "protocol_version": "protocol",
        },
        capability_manifest,
    )
    assert manifest["application_icon"] == {
        "source": "assets/icons/optionhelper-app-icon-tile-light.ico",
        "packaged_path": "Resources/icons/OptionHelper.ico",
        "sha256": sha256(BUILDER.WINDOWS_APP_ICON.read_bytes()).hexdigest(),
        "format": "ico",
        "layer_count": 10,
        "embedded_executables": [
            "OptionHelper.exe",
            "Resources/backend/OptionHelperBackend/OptionHelperBackend.exe",
        ],
    }


def test_build_gate_checks_embedded_icon_against_the_source(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "OptionHelper.exe"
    executable.write_bytes(b"MZ")
    seen: list[list[str]] = []
    monkeypatch.setattr(BUILDER, "_run", lambda command, **_kwargs: seen.append(command))
    BUILDER.verify_executable_icon(executable)
    assert seen
    command = seen[0]
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert command[-2:] == [str(executable), str(BUILDER.WINDOWS_APP_ICON)]


def test_missing_invalid_and_mismatched_capability_icons_are_rejected(tmp_path: Path) -> None:
    capability = tmp_path / "capability"
    icon = capability / BUILDER.WINDOWS_APP_ICON_RELATIVE

    with pytest.raises(BUILDER.WindowsBuildError, match="缺少Windows应用图标"):
        BUILDER.verified_capability_icon(capability)

    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"not-ico")
    with pytest.raises(BUILDER.WindowsBuildError, match="有效ICO"):
        BUILDER.verified_capability_icon(capability)

    source = bytearray(BUILDER.WINDOWS_APP_ICON.read_bytes())
    source[4:6] = (9).to_bytes(2, "little")
    icon.write_bytes(source)
    with pytest.raises(BUILDER.WindowsBuildError, match="10层"):
        BUILDER.verified_capability_icon(capability)

    source = bytearray(BUILDER.WINDOWS_APP_ICON.read_bytes())
    source[-1] ^= 1
    icon.write_bytes(source)
    with pytest.raises(BUILDER.WindowsBuildError, match="哈希不一致"):
        BUILDER.verified_capability_icon(capability)


def test_injected_windows_build_stages_and_records_the_bound_icon(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    app_root = repository / "products" / "app"
    for relative in ("backend", "config", "frontend/optchat"):
        (app_root / relative).mkdir(parents=True)
    (app_root / "frontend" / "optchat" / "index.html").write_text("ok", encoding="utf-8")
    (app_root / "desktop" / "macos").mkdir(parents=True)
    (app_root / "desktop" / "macos" / "backend_launcher.py").write_text("", encoding="utf-8")
    (app_root / "desktop" / "windows").mkdir(parents=True)
    (app_root / "desktop" / "windows" / "OptionHelper.Windows.csproj").write_text("<Project />", encoding="utf-8")
    (repository / "core" / "src" / "runtime").mkdir(parents=True)
    (repository / "LICENSES").mkdir()

    source_icon = repository / BUILDER.WINDOWS_APP_ICON_RELATIVE
    source_icon.parent.mkdir(parents=True)
    shutil.copy2(BUILDER.WINDOWS_APP_ICON, source_icon)
    capability = tmp_path / "capability"
    capability_icon = capability / BUILDER.WINDOWS_APP_ICON_RELATIVE
    capability_icon.parent.mkdir(parents=True)
    shutil.copy2(source_icon, capability_icon)
    (capability / "SKILL.md").write_text("skill", encoding="utf-8")
    entries = BUILDER.content_tree_entries(capability)
    capability_manifest = {
        "capability_version": "v1.0",
        "content_tree_hash": BUILDER.tree_hash(entries),
        "protocol_version": "protocol",
    }
    (capability / "capability-manifest.json").write_text(json.dumps(capability_manifest), encoding="utf-8")

    monkeypatch.setattr(BUILDER, "ROOT", repository)
    monkeypatch.setattr(BUILDER, "APP_ROOT", app_root)
    monkeypatch.setattr(BUILDER, "WINDOWS_APP_ICON", source_icon)
    monkeypatch.setattr(BUILDER, "check_prerequisites", lambda _root: None)
    monkeypatch.setattr(BUILDER, "verify_skill", lambda _root: [])
    verified: list[tuple[Path, Path]] = []
    monkeypatch.setattr(BUILDER, "verify_executable_icon", lambda executable, icon: verified.append((executable, icon)))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> None:
        commands.append(command)
        if "PyInstaller" in command:
            backend = Path(command[command.index("--distpath") + 1]) / "OptionHelperBackend"
            backend.mkdir(parents=True)
            (backend / "OptionHelperBackend.exe").write_bytes(b"backend-exe")
            (backend / "_internal" / "reportlab").mkdir(parents=True)
        elif command[:2] == ["dotnet", "publish"]:
            shell = Path(command[command.index("--output") + 1])
            shell.mkdir(parents=True)
            (shell / "OptionHelper.exe").write_bytes(b"shell-exe")

    monkeypatch.setattr(BUILDER, "_run", fake_run)
    versions = tmp_path / "versions"
    (versions / "v1.0").mkdir(parents=True)
    outputs = BUILDER.build_windows("v1.0", capability, dist_root=tmp_path / "output", versions_root=versions)

    assert [path.name for path, _icon in verified] == ["OptionHelperBackend.exe", "OptionHelper.exe"]
    assert all(icon == capability_icon for _path, icon in verified)
    pyinstaller = next(command for command in commands if "PyInstaller" in command)
    dotnet = next(command for command in commands if command[:2] == ["dotnet", "publish"])
    assert pyinstaller[pyinstaller.index("--icon") + 1] == str(capability_icon)
    assert f"-p:ApplicationIcon={capability_icon}" in dotnet
    with zipfile.ZipFile(outputs["installer"]) as archive:
        assert archive.read("OptionHelper/Resources/icons/OptionHelper.ico") == source_icon.read_bytes()
        app_manifest = json.loads(archive.read("OptionHelper/Resources/app-manifest.json"))
        assert app_manifest["application_icon"]["sha256"] == sha256(source_icon.read_bytes()).hexdigest()
        assert archive.testzip() is None
    platform_manifest = json.loads(outputs["release_manifest"].read_text(encoding="utf-8"))
    assert platform_manifest["application_icon"] == app_manifest["application_icon"]
