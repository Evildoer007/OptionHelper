"""Frozen Agent Runtime build tools contain locks, never workstation modules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    app_packaging = ROOT / "packaging" / "app"
    if str(app_packaging) not in sys.path:
        sys.path.insert(0, str(app_packaging))
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / "source_snapshot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_frozen_build_tools_preserve_locks_without_local_node_modules(tmp_path: Path) -> None:
    snapshot_module = _load("optionhelper_postject_snapshot")

    with snapshot_module.frozen_source_snapshot(
        ROOT,
        tmp_path / "snapshot",
        selectors=(snapshot_module.SnapshotSelector("packaging/app/agent_runtime"),),
    ) as snapshot:
        tools = snapshot.agent_runtime_build_tools
        assert (tools / "package.json").is_file()
        assert (tools / "package-lock.json").is_file()
        assert not (tools / "node_modules").exists()


def test_snapshot_rejects_a_symlink_outside_the_selected_closure(tmp_path: Path) -> None:
    snapshot_module = _load("optionhelper_snapshot_symlink_escape")
    repository = tmp_path / "repo"
    selected = repository / "packaging" / "app" / "agent_runtime"
    selected.mkdir(parents=True)
    outside = repository / "outside.js"
    outside.write_text("module.exports = true;\n", encoding="utf-8", newline="")
    (selected / "tool").symlink_to("../../../outside.js")

    with pytest.raises(snapshot_module.SourceSnapshotError, match="符号链接.*闭包"):
        with snapshot_module.frozen_source_snapshot(
            repository,
            tmp_path / "snapshot",
            selectors=(snapshot_module.SnapshotSelector("packaging/app/agent_runtime"),),
        ):
            pytest.fail("越界符号链接不得进入冻结构建工具")


@pytest.mark.parametrize(
    ("link_kind", "message"),
    (
        ("absolute", "符号链接目标不在冻结闭包"),
        ("broken", "断裂符号链接"),
        ("directory", "符号链接目标不是文件"),
    ),
)
def test_snapshot_rejects_unsafe_symlink_shapes(
    tmp_path: Path,
    link_kind: str,
    message: str,
) -> None:
    snapshot_module = _load(f"optionhelper_snapshot_{link_kind}_symlink")
    repository = tmp_path / "repo"
    selected = repository / "tools"
    selected.mkdir(parents=True)
    target = selected / "target.js"
    target.write_text("module.exports = true;\n", encoding="utf-8", newline="")
    link = selected / "tool"
    if link_kind == "absolute":
        link.symlink_to(target.resolve())
    elif link_kind == "broken":
        link.symlink_to("missing.js")
    else:
        directory = selected / "package"
        directory.mkdir()
        link.symlink_to("package", target_is_directory=True)

    with pytest.raises(snapshot_module.SourceSnapshotError, match=message):
        with snapshot_module.frozen_source_snapshot(
            repository,
            tmp_path / "snapshot",
            selectors=(snapshot_module.SnapshotSelector("tools"),),
        ):
            pytest.fail("不安全符号链接不得进入冻结构建工具")


def test_snapshot_detects_same_content_symlink_retargeting(tmp_path: Path) -> None:
    snapshot_module = _load("optionhelper_snapshot_symlink_retarget")
    repository = tmp_path / "repo"
    selected = repository / "tools"
    selected.mkdir(parents=True)
    for name in ("a.js", "b.js"):
        (selected / name).write_text("same content\n", encoding="utf-8", newline="")
    link = selected / "tool"
    link.symlink_to("a.js")

    with snapshot_module.frozen_source_snapshot(
        repository,
        tmp_path / "snapshot",
        selectors=(snapshot_module.SnapshotSelector("tools"),),
    ) as snapshot:
        link.unlink()
        link.symlink_to("b.js")
        with pytest.raises(
            snapshot_module.SourceSnapshotError,
            match=r"构建期间源码发生变化：tools/tool",
        ):
            snapshot.verify_current()
