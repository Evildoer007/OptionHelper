"""macOS DMG icon-layout contracts without creating a disk image."""

from __future__ import annotations

from pathlib import Path
import plistlib
import sys
import tempfile
import types

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
MACOS_PACKAGING = ROOT / "packaging" / "app" / "macos"
if str(MACOS_PACKAGING) not in sys.path:
    sys.path.insert(0, str(MACOS_PACKAGING))

import build_macos as builder
import render_dmg_background as renderer


VALID_FINDER_METADATA = (
    b"OptionHelper OptionHelper.app optionhelper-dmg-background.png "
    b"/OptionHelper.app/Contents/Resources/installer/optionhelper-dmg-background.png"
)


def make_bundle(root: Path) -> Path:
    bundle = root / "OptionHelper.app"
    background = bundle / builder.DMG_BUNDLE_BACKGROUND
    background.parent.mkdir(parents=True)
    background.write_bytes(builder.DMG_BACKGROUND.read_bytes())
    return bundle


def install_metadata_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    def write_metadata(mountpoint: Path) -> Path:
        metadata = mountpoint / ".DS_Store"
        metadata.write_bytes(VALID_FINDER_METADATA)
        return metadata

    monkeypatch.setattr(builder, "_write_finder_metadata", write_metadata)


def test_dmg_layout_contains_branded_background_and_drag_targets() -> None:
    assert renderer.WINDOW_SIZE == builder.DMG_WINDOW_SIZE
    with Image.open(builder.DMG_BACKGROUND) as background:
        assert background.size == builder.DMG_WINDOW_SIZE
        assert background.getpixel((20, 40)) == (247, 244, 240)

    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)

        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")

        assert (layout / "OptionHelper.app" / "Contents").is_dir()
        assert (layout / "Applications").is_symlink()
        assert {path.name for path in layout.iterdir()} == {"OptionHelper.app", "Applications"}
        assert (layout / "OptionHelper.app" / builder.DMG_BUNDLE_BACKGROUND).read_bytes() == builder.DMG_BACKGROUND.read_bytes()


def test_finder_metadata_matches_window_and_icon_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    records: dict[tuple[str, str], object] = {}

    class Section:
        def __init__(self, name: str) -> None:
            self.name = name

        def __setitem__(self, code: str, value: object) -> None:
            records[(self.name, code)] = value

    class Store:
        def __init__(self, path: str) -> None:
            self.path = Path(path)

        def __enter__(self) -> "Store":
            return self

        def __exit__(self, *_args: object) -> None:
            self.path.write_bytes(VALID_FINDER_METADATA)

        def __getitem__(self, name: str) -> Section:
            return Section(name)

    class DSStore:
        @staticmethod
        def open(path: str, _mode: str) -> Store:
            return Store(path)

    class AliasValue:
        def to_bytes(self) -> bytes:
            return b"background-alias"

    class Alias:
        @staticmethod
        def for_file(_path: str) -> AliasValue:
            return AliasValue()

    monkeypatch.setitem(sys.modules, "ds_store", types.SimpleNamespace(DSStore=DSStore))
    monkeypatch.setitem(sys.modules, "mac_alias", types.SimpleNamespace(Alias=Alias))
    with tempfile.TemporaryDirectory() as temporary_name:
        mountpoint = Path(temporary_name)
        make_bundle(mountpoint)
        metadata = builder._write_finder_metadata(mountpoint)
        assert metadata.is_file()
        builder._validate_finder_metadata_hygiene(metadata.read_bytes())
        assert records[(".", "bwsp")]["WindowBounds"] == "{{160, 140}, {760, 460}}"
        assert records[(".", "icvp")]["iconSize"] == float(builder.DMG_ICON_SIZE)
        assert records[(".", "icvp")]["backgroundImageAlias"] == b"background-alias"
        for name, position in builder.DMG_ICON_POSITIONS.items():
            assert records[(name, "Iloc")] == position


def test_hdiutil_plist_parser_binds_mountpoint_and_device() -> None:
    output = plistlib.dumps({
        "system-entities": [{
            "dev-entry": "/dev/disk77s1",
            "mount-point": "/Volumes/OptionHelper-build-a1b2c3",
        }],
    }).decode()
    assert builder._attached_mountpoint(output) == Path("/Volumes/OptionHelper-build-a1b2c3")
    assert builder._attached_device(output) == "/dev/disk77s1"


def test_hdiutil_parser_rejects_unstructured_output() -> None:
    with pytest.raises(builder.MacOSBuildError, match="结构化信息"):
        builder._attached_mountpoint("/dev/disk77s1\tApple_APFS\t/Volumes/OptionHelper-build-a1b2c3\n")


def test_dmg_background_contains_branded_visual_pixel_contract() -> None:
    assert renderer.TITLE == "将OptionHelper拖入Applications完成安装"
    assert renderer.FOOTER == "拖动OptionHelper到Applications后即可开始使用"
    with Image.open(builder.DMG_BACKGROUND) as background:
        image = background.convert("RGB")
        assert image.getpixel((20, 1)) == (200, 16, 46)
        assert image.getpixel((300, 235)) == (200, 16, 46)
        assert image.getpixel((510, 235)) == (200, 16, 46)
        left_glow = image.getpixel((185, 235))
        right_glow = image.getpixel((575, 235))
        assert left_glow[0] > left_glow[2]
        assert right_glow[2] > right_glow[0]

        title_pixels = [
            image.getpixel((x, y))
            for y in range(30, 65)
            for x in range(140, 620)
        ]
        footer_pixels = [
            image.getpixel((x, y))
            for y in range(405, 435)
            for x in range(200, 560)
        ]
        assert sum(sum(pixel) < 600 for pixel in title_pixels) > 100
        assert sum(sum(pixel) < 600 for pixel in footer_pixels) > 100


def test_dmg_verifier_rejects_hidden_files_other_than_finder_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        dmg = temporary / "OptionHelper.dmg"
        dmg.write_bytes(b"dmg")

        def fake_run(command: list[str], **_kwargs: object) -> str:
            if command[:2] == ["hdiutil", "attach"]:
                mountpoint = Path(command[command.index("-mountpoint") + 1])
                resources = mountpoint / "OptionHelper.app" / "Contents" / "Resources"
                capability = resources / "capability" / "option-helper"
                capability.mkdir(parents=True)
                (capability / "capability-manifest.json").write_text("{}\n", encoding="utf-8")
                (mountpoint / "Applications").symlink_to("/Applications", target_is_directory=True)
                (mountpoint / ".DS_Store").write_bytes(b"finder-layout")
                (mountpoint / ".fseventsd").mkdir()
                background = mountpoint / "OptionHelper.app" / builder.DMG_BUNDLE_BACKGROUND
                background.parent.mkdir(parents=True, exist_ok=True)
                background.write_bytes(builder.DMG_BACKGROUND.read_bytes())
            return ""

        monkeypatch.setattr(builder, "run", fake_run)
        monkeypatch.setattr(builder, "verified_capability", lambda _root: {"content_tree_hash": "tree"})
        monkeypatch.setattr(builder, "file_hash", lambda _path: "manifest")
        with pytest.raises(builder.MacOSBuildError, match="非安装入口"):
            builder.verify_dmg_install_layout(
                dmg,
                expected_content_tree_hash="tree",
                expected_manifest_hash="manifest",
            )


def test_dmg_build_writes_finder_metadata_before_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)
        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")
        output = temporary / "OptionHelper.dmg"
        mountpoint = temporary / "mounted"
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> str:
            commands.append(command)
            if command[:2] == ["hdiutil", "create"]:
                source = Path(command[command.index("-srcfolder") + 1])
                assert not (source / ".DS_Store").exists()
                Path(command[-1]).write_bytes(b"writable")
            elif command[:2] == ["hdiutil", "attach"]:
                mountpoint.mkdir()
                return "attached"
            elif command[:2] == ["diskutil", "info"]:
                if command[-1] == str(mountpoint):
                    return "Device Identifier: disk-test\n"
                return f"Mount Point: {mountpoint}\n"
            elif command[:2] == ["hdiutil", "convert"]:
                Path(command[command.index("-o") + 1]).write_bytes(b"compressed")
            return ""

        monkeypatch.setattr(builder, "_attached_mountpoint", lambda _output: mountpoint)
        monkeypatch.setattr(builder, "run", fake_run)
        install_metadata_writer(monkeypatch)
        builder.build_styled_dmg(layout, output, temporary)

        assert output.read_bytes() == b"compressed"
        assert [command[:2] for command in commands] == [
            ["hdiutil", "create"],
            ["hdiutil", "attach"],
            ["diskutil", "info"],
            ["diskutil", "rename"],
            ["diskutil", "info"],
            ["sync"],
            ["hdiutil", "detach"],
            ["hdiutil", "convert"],
        ]
        assert "UDRW" in commands[0]
        assert commands[0][commands[0].index("-fs") + 1] == "HFS+"
        assert "UDZO" in commands[-1]
        assert "-noautoopen" in commands[1]
        assert "-nobrowse" not in commands[1]
        assert not any(command[:2] == ["osascript", "-e"] for command in commands)


def test_dmg_build_failure_does_not_create_output(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)
        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")
        output = temporary / "OptionHelper.dmg"

        def fail_run(*_args: object, **_kwargs: object) -> str:
            raise builder.MacOSBuildError("hdiutil失败")

        monkeypatch.setattr(builder, "run", fail_run)
        with pytest.raises(builder.MacOSBuildError, match="hdiutil失败"):
            builder.build_styled_dmg(layout, output, temporary)
        assert not output.exists()


def test_dmg_build_removes_partial_compressed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)
        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")
        output = temporary / "OptionHelper.dmg"
        mountpoint = temporary / "mounted"

        def fail_after_convert(command: list[str], **_kwargs: object) -> str:
            if command[:2] == ["hdiutil", "create"]:
                Path(command[-1]).write_bytes(b"writable")
            elif command[:2] == ["hdiutil", "attach"]:
                mountpoint.mkdir()
                return "attached"
            elif command[:2] == ["diskutil", "info"]:
                if command[-1] == str(mountpoint):
                    return "Device Identifier: disk-test\n"
                return f"Mount Point: {mountpoint}\n"
            elif command[:2] == ["hdiutil", "convert"]:
                output.write_bytes(b"partial")
                raise builder.MacOSBuildError("压缩失败")
            return ""

        monkeypatch.setattr(builder, "_attached_mountpoint", lambda _output: mountpoint)
        monkeypatch.setattr(builder, "run", fail_after_convert)
        install_metadata_writer(monkeypatch)
        with pytest.raises(builder.MacOSBuildError, match="压缩失败"):
            builder.build_styled_dmg(layout, output, temporary)
        assert not output.exists()


def test_dmg_build_rejects_metadata_generation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)
        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")
        output = temporary / "OptionHelper.dmg"
        mountpoint = temporary / "mounted"

        def no_finder_metadata(command: list[str], **_kwargs: object) -> str:
            if command[:2] == ["hdiutil", "create"]:
                Path(command[-1]).write_bytes(b"writable")
            elif command[:2] == ["hdiutil", "attach"]:
                mountpoint.mkdir()
                return "attached"
            elif command[:2] == ["diskutil", "info"]:
                if command[-1] == str(mountpoint):
                    return "Device Identifier: disk-test\n"
                return f"Mount Point: {mountpoint}\n"
            return ""

        monkeypatch.setattr(builder, "_attached_mountpoint", lambda _output: mountpoint)
        monkeypatch.setattr(builder, "run", no_finder_metadata)
        monkeypatch.setattr(builder, "_write_finder_metadata", lambda _path: (_ for _ in ()).throw(
            builder.MacOSBuildError("DMG布局元数据生成失败")
        ))
        with pytest.raises(builder.MacOSBuildError, match="布局元数据生成失败"):
            builder.build_styled_dmg(layout, output, temporary)
        assert not output.exists()


def test_finder_metadata_hygiene_allows_backing_image_bookmark() -> None:
    metadata = (
        b"OptionHelper OptionHelper.app optionhelper-dmg-background.png "
        b"/OptionHelper.app/Contents/Resources/installer/optionhelper-dmg-background.png "
        b"/private/tmp/build/OptionHelper-writable.dmg"
    )
    builder._validate_finder_metadata_hygiene(metadata)


def test_finder_metadata_hygiene_rejects_temporary_volume_name() -> None:
    metadata = (
        b"OptionHelper-build-deadbeef OptionHelper.app optionhelper-dmg-background.png "
        b"/OptionHelper.app/Contents/Resources/installer/optionhelper-dmg-background.png"
    )
    with pytest.raises(builder.MacOSBuildError, match="临时卷"):
        builder._validate_finder_metadata_hygiene(metadata)


def test_dmg_build_cleanup_does_not_mask_detach_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        bundle = make_bundle(temporary)
        layout = builder.prepare_dmg_layout(bundle, temporary / "layout")
        output = temporary / "OptionHelper.dmg"
        mountpoint = temporary / "mounted"

        def fail_detach(command: list[str], **_kwargs: object) -> str:
            if command[:2] == ["hdiutil", "create"]:
                Path(command[-1]).write_bytes(b"writable")
            elif command[:2] == ["hdiutil", "attach"]:
                mountpoint.mkdir()
                return "attached"
            elif command[:2] == ["diskutil", "info"]:
                if command[-1] == str(mountpoint):
                    return "Device Identifier: disk-test\n"
                return f"Mount Point: {mountpoint}\n"
            elif command[:2] == ["hdiutil", "detach"]:
                raise builder.MacOSBuildError("卸载失败")
            return ""

        monkeypatch.setattr(builder, "_attached_mountpoint", lambda _output: mountpoint)
        monkeypatch.setattr(builder, "run", fail_detach)
        install_metadata_writer(monkeypatch)
        recovery = tmp_path / "recovered-writable.dmg"

        def preserve_work_disk(writable: Path) -> Path:
            recovery.write_bytes(writable.read_bytes())
            return recovery

        monkeypatch.setattr(builder, "_preserve_writable_image", preserve_work_disk)
        with pytest.raises(builder.MacOSBuildError, match="卸载失败"):
            builder.build_styled_dmg(layout, output, temporary)
        assert not output.exists()
        assert list(temporary.glob("*-writable.dmg"))
        assert recovery.read_bytes() == b"writable"


def test_mounted_dmg_requires_finder_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        dmg = temporary / "OptionHelper.dmg"
        dmg.write_bytes(b"dmg")

        def fake_run(command: list[str], **_kwargs: object) -> str:
            if command[:2] != ["hdiutil", "attach"]:
                return ""
            mountpoint = Path(command[command.index("-mountpoint") + 1])
            resources = mountpoint / "OptionHelper.app" / "Contents" / "Resources"
            capability = resources / "capability" / "option-helper"
            capability.mkdir(parents=True)
            (capability / "capability-manifest.json").write_text("{}\n", encoding="utf-8")
            (mountpoint / "Applications").symlink_to("/Applications", target_is_directory=True)
            background = mountpoint / "OptionHelper.app" / builder.DMG_BUNDLE_BACKGROUND
            background.parent.mkdir(parents=True, exist_ok=True)
            background.write_bytes(builder.DMG_BACKGROUND.read_bytes())
            return ""

        monkeypatch.setattr(builder, "run", fake_run)
        monkeypatch.setattr(builder, "verified_capability", lambda _root: {"content_tree_hash": "tree"})
        monkeypatch.setattr(builder, "file_hash", lambda _path: "manifest")
        with pytest.raises(builder.MacOSBuildError, match="Finder安装布局元数据"):
            builder.verify_dmg_install_layout(
                dmg,
                expected_content_tree_hash="tree",
                expected_manifest_hash="manifest",
            )


def test_mounted_dmg_preserves_validation_error_when_detach_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dmg = tmp_path / "OptionHelper.dmg"
    dmg.write_bytes(b"dmg")

    def fail_detach(command: list[str], **_kwargs: object) -> str:
        if command[:2] == ["hdiutil", "attach"]:
            mountpoint = Path(command[command.index("-mountpoint") + 1])
            (mountpoint / "OptionHelper.app").mkdir()
            (mountpoint / "Applications").symlink_to("/Applications", target_is_directory=True)
            return ""
        if command[:2] == ["hdiutil", "detach"]:
            raise builder.MacOSBuildError("卸载阶段失败")
        return ""

    monkeypatch.setattr(builder, "run", fail_detach)
    with pytest.raises(builder.MacOSBuildError, match="Skill根目录无效.*DMG卸载失败：卸载阶段失败"):
        builder.verify_dmg_install_layout(
            dmg,
            expected_content_tree_hash="tree",
            expected_manifest_hash="manifest",
        )


def test_dmg_verification_never_opens_finder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dmg = tmp_path / "OptionHelper.dmg"
    dmg.write_bytes(b"dmg")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> str:
        commands.append(command)
        if command[:2] == ["hdiutil", "attach"]:
            mountpoint = Path(command[command.index("-mountpoint") + 1])
            app = mountpoint / "OptionHelper.app"
            capability = app / "Contents" / "Resources" / "capability" / "option-helper"
            capability.mkdir(parents=True)
            (capability / "capability-manifest.json").write_text("{}\n", encoding="utf-8")
            background = app / builder.DMG_BUNDLE_BACKGROUND
            background.parent.mkdir(parents=True, exist_ok=True)
            background.write_bytes(builder.DMG_BACKGROUND.read_bytes())
            (mountpoint / "Applications").symlink_to("/Applications", target_is_directory=True)
            (mountpoint / ".DS_Store").write_bytes(VALID_FINDER_METADATA)
        return ""

    monkeypatch.setattr(builder, "run", fake_run)
    monkeypatch.setattr(builder, "verified_capability", lambda _root: {"content_tree_hash": "tree"})
    monkeypatch.setattr(builder, "file_hash", lambda _path: "manifest")
    builder.verify_dmg_install_layout(
        dmg,
        expected_content_tree_hash="tree",
        expected_manifest_hash="manifest",
    )
    assert not any(command[:2] == ["osascript", "-e"] for command in commands)
