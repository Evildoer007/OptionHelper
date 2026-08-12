from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _prepare_current_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_module("test_build_current_contract", "packaging/build_current.py")
    repository = tmp_path / "repo"
    fresh_skill = tmp_path / "fresh-skill" / "option-helper"
    fresh_skill.mkdir(parents=True)
    (fresh_skill / "capability-manifest.json").write_text(
        json.dumps({"content_tree_hash": "fresh-tree-hash"}) + "\n",
        encoding="utf-8",
    )

    def write_zip(skill: Path) -> Path:
        archive = tmp_path / "fresh-skill" / "option-helper.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.write(
                skill / "capability-manifest.json",
                "option-helper/capability-manifest.json",
            )
        return archive

    monkeypatch.setattr(module, "ROOT", repository)
    def build_skill(output: Path, **kwargs):
        assert output.name == "skill"
        assert kwargs == {"candidate": True, "repo_root": repository}
        return fresh_skill

    monkeypatch.setattr(module, "build_skill", build_skill)
    monkeypatch.setattr(module, "verify_skill", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "verify_source_snapshot", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "probe_runtime", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "verify_app", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "write_zip", write_zip)
    monkeypatch.setattr(module, "verify_zip", lambda *_args, **_kwargs: [])
    return module, repository, fresh_skill


def _platform_artifacts(
    root: Path,
    version: str,
    *,
    tree_hash: str = "fresh-tree-hash",
    skill_manifest: Path,
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    dmg = root.parent.parent / f"OptionHelper-{version}-macOS-arm64.dmg"
    dmg.write_bytes(b"dmg")
    manifest_hash = sha256(skill_manifest.read_bytes()).hexdigest()
    app_manifest = root / "app-manifest.json"
    app_manifest.write_text(
        json.dumps({
            "capability_content_tree_hash": tree_hash,
            "capability_manifest_hash": manifest_hash,
        }) + "\n",
        encoding="utf-8",
    )
    release_manifest = root / "platform-release-manifest.json"
    release_manifest.write_text(
        json.dumps({
            "capability_content_tree_hash": tree_hash,
            "capability_manifest_hash": manifest_hash,
            "installer": {
                "filename": dmg.name,
                "sha256": sha256(dmg.read_bytes()).hexdigest(),
                "size": dmg.stat().st_size,
            },
        }) + "\n",
        encoding="utf-8",
    )
    return {"dmg": dmg, "manifest": app_manifest, "release_manifest": release_manifest}


def test_build_current_rejects_app_capability_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _repository, fresh_skill = _prepare_current_build(monkeypatch, tmp_path)

    def build_macos(version: str, capability: Path, *, dist_root: Path, versions_root: Path):
        artifacts = _platform_artifacts(
            versions_root / version,
            version,
            tree_hash="stale-tree-hash",
            skill_manifest=fresh_skill / "capability-manifest.json",
        )
        (dist_root / artifacts["dmg"].name).write_bytes(artifacts["dmg"].read_bytes())
        artifacts["dmg"] = dist_root / artifacts["dmg"].name
        return artifacts

    monkeypatch.setattr(module, "build_macos", build_macos)

    with pytest.raises(module.CurrentBuildError, match="content_tree_hash|Capability"):
        module.build_current("v1.0", "macos")


def test_build_current_rejects_formal_compute_protocol_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _repository, _fresh_skill = _prepare_current_build(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "probe_runtime", lambda *_args, **_kwargs: ["正式计算协议探测失败：stale call_tool"])

    with pytest.raises(module.CurrentBuildError, match="正式计算协议"):
        module.build_current("v1.0", "macos")


def test_validated_capability_rejects_finder_copy_without_touching_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_build_app_no_stage", "packaging/app/build_app.py")
    skill = tmp_path / "skill" / "option-helper"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("canonical", encoding="utf-8")
    conflict = skill / "SKILL 2.md"
    conflict.write_text("recoverable", encoding="utf-8")
    monkeypatch.setattr(module, "verify_skill", lambda _root: [])
    with pytest.raises(module.AppBuildError, match="同步冲突副本"):
        module.validated_capability(skill)
    assert conflict.read_text(encoding="utf-8") == "recoverable"


def test_build_current_uses_the_manifest_inside_the_skill_zip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _repository, fresh_skill = _prepare_current_build(monkeypatch, tmp_path)

    def stale_zip(_skill: Path) -> Path:
        archive = tmp_path / "fresh-skill" / "option-helper.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(
                "option-helper/capability-manifest.json",
                json.dumps({"content_tree_hash": "zip-tree-hash"}) + "\n",
            )
        return archive

    def build_macos(version: str, capability: Path, *, dist_root: Path, versions_root: Path):
        artifacts = _platform_artifacts(
            versions_root / version,
            version,
            skill_manifest=fresh_skill / "capability-manifest.json",
        )
        (dist_root / artifacts["dmg"].name).write_bytes(artifacts["dmg"].read_bytes())
        artifacts["dmg"] = dist_root / artifacts["dmg"].name
        return artifacts

    monkeypatch.setattr(module, "write_zip", stale_zip)
    monkeypatch.setattr(module, "build_macos", build_macos)

    with pytest.raises(module.CurrentBuildError, match="content_tree_hash|Capability"):
        module.build_current("v1.0", "macos")


def test_build_current_stages_the_fresh_verified_skill_not_legacy_app_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, repository, fresh_skill = _prepare_current_build(monkeypatch, tmp_path)
    legacy = repository / "products" / "app" / "capability" / "option-helper"
    legacy.mkdir(parents=True)
    (legacy / "STALE").write_text("legacy\n", encoding="utf-8")
    seen: list[Path] = []

    def build_macos(version: str, capability: Path, *, dist_root: Path, versions_root: Path):
        seen.append(capability.resolve())
        artifacts = _platform_artifacts(
            versions_root / version,
            version,
            skill_manifest=fresh_skill / "capability-manifest.json",
        )
        (dist_root / artifacts["dmg"].name).write_bytes(artifacts["dmg"].read_bytes())
        artifacts["dmg"] = dist_root / artifacts["dmg"].name
        return artifacts

    monkeypatch.setattr(module, "build_macos", build_macos)
    module.build_current("v1.0", "macos")

    assert seen == [fresh_skill.resolve()]
    assert seen[0] != legacy.resolve()
    assert not (repository / "versions").exists()


def test_dist_replacement_failure_restores_previous_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_build_current_rollback", "packaging/build_current.py")
    repository = tmp_path / "repo"
    dist = repository / "dist"
    dist.mkdir(parents=True)
    expected = {
        "option-helper.zip": b"old-skill",
        "OptionHelper-v1.0-macOS-arm64.dmg": b"old-dmg",
    }
    for name, payload in expected.items():
        (dist / name).write_bytes(payload)
    missing_stage = tmp_path / "missing-stage"
    monkeypatch.setattr(module, "ROOT", repository)

    with pytest.raises(FileNotFoundError):
        module._replace_dist(missing_stage)

    assert {path.name: path.read_bytes() for path in dist.iterdir()} == expected
    assert not list(repository.glob(".dist-backup-*"))


def test_dist_backup_cleanup_failure_rolls_back_to_previous_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_build_current_cleanup_rollback", "packaging/build_current.py")
    repository = tmp_path / "repo"
    dist = repository / "dist"
    stage = tmp_path / "stage"
    dist.mkdir(parents=True)
    stage.mkdir()
    old = {
        "option-helper.zip": b"old-skill",
        "OptionHelper-v1.0-macOS-arm64.dmg": b"old-dmg",
    }
    new = {
        "option-helper.zip": b"new-skill",
        "OptionHelper-v1.0-macOS-arm64.dmg": b"new-dmg",
    }
    for name, payload in old.items():
        (dist / name).write_bytes(payload)
    for name, payload in new.items():
        (stage / name).write_bytes(payload)
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(
        module.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )

    with pytest.raises(PermissionError, match="cleanup denied"):
        module._replace_dist(stage)

    assert {path.name: path.read_bytes() for path in dist.iterdir()} == old
    assert {path.name: path.read_bytes() for path in stage.iterdir()} == new
    assert not list(repository.glob(".dist-backup-*"))


def test_windows_build_does_not_replace_the_locked_macos_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _repository, fresh_skill = _prepare_current_build(monkeypatch, tmp_path)

    def build_windows(version: str, capability: Path, *, dist_root: Path, versions_root: Path):
        release_root = versions_root / version
        release_root.mkdir(parents=True, exist_ok=True)
        installer = dist_root / f"OptionHelper-{version}-windows-x86_64.zip"
        installer.write_bytes(b"windows")
        manifest_hash = sha256((fresh_skill / "capability-manifest.json").read_bytes()).hexdigest()
        app_manifest = release_root / "app-manifest-windows.json"
        app_manifest.write_text(json.dumps({
            "capability_content_tree_hash": "fresh-tree-hash",
            "capability_manifest_hash": manifest_hash,
        }) + "\n", encoding="utf-8")
        release_manifest = release_root / "platform-release-manifest-windows.json"
        release_manifest.write_text(json.dumps({
            "capability_content_tree_hash": "fresh-tree-hash",
            "capability_manifest_hash": manifest_hash,
            "installer": {
                "filename": installer.name,
                "sha256": sha256(installer.read_bytes()).hexdigest(),
                "size": installer.stat().st_size,
            },
        }) + "\n", encoding="utf-8")
        return {
            "installer": installer,
            "manifest": app_manifest,
            "release_manifest": release_manifest,
        }

    monkeypatch.setattr(module, "build_windows", build_windows)

    def reject_dist_replacement(_staged: Path) -> None:
        raise AssertionError("Windows候选不得替换锁定为macOS交付物的dist")

    monkeypatch.setattr(module, "_replace_dist", reject_dist_replacement)
    module.build_current("v1.0", "windows")


def test_macos_dmg_mount_rejects_stale_embedded_capability_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_macos_dmg_binding", "packaging/app/macos/build_macos.py")
    dmg = tmp_path / "OptionHelper-v1.0-macOS-arm64.dmg"
    dmg.write_bytes(b"dmg")

    def fake_run(command: list[str], **_kwargs) -> None:
        if len(command) > 1 and command[1] == "attach":
            mountpoint = Path(command[command.index("-mountpoint") + 1])
            resources = mountpoint / "OptionHelper.app" / "Contents" / "Resources"
            capability = resources / "capability" / "option-helper"
            capability.mkdir(parents=True)
            (capability / "capability-manifest.json").write_text(
                json.dumps({"content_tree_hash": "stale-tree-hash"}) + "\n",
                encoding="utf-8",
            )
            (resources / "app-manifest.json").write_text(
                json.dumps({"capability_content_tree_hash": "stale-tree-hash"}) + "\n",
                encoding="utf-8",
            )
            (mountpoint / "Applications").symlink_to("/Applications", target_is_directory=True)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        module,
        "verified_capability",
        lambda _root: {"content_tree_hash": "stale-tree-hash"},
    )
    monkeypatch.setattr(module, "file_hash", lambda _path: "stale-manifest-hash")

    with pytest.raises(module.MacOSBuildError, match="content_tree_hash"):
        module.verify_dmg_install_layout(
            dmg,
            expected_content_tree_hash="fresh-tree-hash",
            expected_manifest_hash="fresh-manifest-hash",
        )


def test_release_v1_has_no_finder_deletion_helper() -> None:
    module = _load_module("test_release_v1_no_delete", "packaging/release_v1.py")

    assert not hasattr(module, "remove_sync_conflicts")
    assert not hasattr(module, "_remove")


def test_skill_replace_rejects_finder_copy_without_deleting_it(tmp_path: Path) -> None:
    module = _load_module("test_build_skill_no_finder_delete", "packaging/skill/build_skill.py")
    output = tmp_path / "candidate"
    output.mkdir()
    staged = tmp_path / "staged-option-helper"
    staged.mkdir()
    conflict = output / "option-helper 2.zip"
    conflict.write_bytes(b"recoverable")

    with pytest.raises(module.SkillBuildError, match="同步冲突|Finder"):
        module._replace_candidate(staged, output / "option-helper", replace=True)

    assert conflict.read_bytes() == b"recoverable"
    assert staged.is_dir()


def test_release_layout_rejects_gitkeep_in_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_release_v1_layout", "packaging/release_v1.py")
    repository = tmp_path / "repo"
    dist = repository / "dist"
    archive = repository / "versions" / "v1.0"
    dist.mkdir(parents=True)
    archive.mkdir(parents=True)
    installer = "OptionHelper-v1.0-macOS-arm64.dmg"
    for name in ("option-helper.zip", installer, ".gitkeep"):
        (dist / name).write_bytes(b"delivery")
    for name in (
        "option-helper.zip",
        "capability-manifest.json",
        "knowledger",
        installer,
        f"{installer}.sha256",
        "app-manifest.json",
        "platform-release-manifest.json",
    ):
        path = archive / name
        if name == "knowledger":
            path.mkdir()
        else:
            path.write_bytes(b"archive")
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(module, "verify_zip", lambda *_args, **_kwargs: [])

    with pytest.raises(module.ReleaseError, match="非交付|gitkeep"):
        module.assert_final_layout("v1.0", platform="macos", dist=dist)


def test_release_dist_cleanup_failure_restores_previous_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module("test_release_v1_cleanup_rollback", "packaging/release_v1.py")
    repository = tmp_path / "repo"
    dist = repository / "dist"
    stage = tmp_path / "stage"
    dist.mkdir(parents=True)
    stage.mkdir()
    (dist / "option-helper.zip").write_bytes(b"old")
    (stage / "option-helper.zip").write_bytes(b"new")
    monkeypatch.setattr(module, "ROOT", repository)
    monkeypatch.setattr(
        module.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )

    with pytest.raises(PermissionError, match="cleanup denied"):
        module.replace_delivery_root(stage)

    assert (dist / "option-helper.zip").read_bytes() == b"old"
    assert (stage / "option-helper.zip").read_bytes() == b"new"
    assert not list(repository.glob(".dist-backup-*"))


def test_windows_one_click_declares_static_platform_and_dotnet8_checks() -> None:
    script = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8").lower()

    assert "packaging\\build_current.py" in script
    assert "packaging\\build-requirements.lock" in script
    assert "--check-dependencies" in script
    direct_dotnet_check = "dotnet" in script and "--list-sdks" in script and "8." in script
    delegated_preflight = "--preflight" in script
    assert direct_dotnet_check or delegated_preflight
    assert "windows" in script
    assert "verified" not in script and "真机" not in script
