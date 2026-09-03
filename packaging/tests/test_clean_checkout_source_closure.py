"""Clean source snapshots must not depend on workstation-only build trees."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_source_snapshot(name: str):
    app_packaging = ROOT / "packaging" / "app"
    if str(app_packaging) not in sys.path:
        sys.path.insert(0, str(app_packaging))
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / "source_snapshot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_snapshot_prunes_machine_only_directories_at_every_depth(tmp_path: Path) -> None:
    snapshot_module = _load_source_snapshot("optionhelper_clean_checkout_pruning")
    repository = tmp_path / "repository"
    source = repository / "source"
    (source / "nested").mkdir(parents=True)
    (source / "keep.txt").write_text("kept\n", encoding="utf-8", newline="")
    (source / "nested" / "keep.py").write_text("VALUE = 1\n", encoding="utf-8", newline="")

    forbidden_directories = (
        "node_modules",
        ".optionhelper-agent-sessions",
        "dist",
        "build",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    )
    for directory in forbidden_directories:
        generated = source / "nested" / directory
        generated.mkdir(parents=True)
        (generated / "machine-only.bin").write_bytes(b"local")
    (source / "nested" / "cached.pyc").write_bytes(b"compiled")

    with snapshot_module.frozen_source_snapshot(
        repository,
        tmp_path / "snapshot",
        selectors=(snapshot_module.SnapshotSelector("source"),),
    ) as snapshot:
        selected = {str(record["path"]) for record in snapshot.source_records}
        assert selected == {"source/keep.txt", "source/nested/keep.py"}
        assert not any(
            part in snapshot_module.ALWAYS_EXCLUDED_PARTS
            for path in selected
            for part in Path(path).parts
        )


def test_agent_runtime_snapshot_uses_only_versioned_source_and_lockfiles() -> None:
    snapshot_module = _load_source_snapshot("optionhelper_clean_checkout_runtime")
    selectors = (
        snapshot_module.SnapshotSelector("packaging/app/agent_runtime"),
        snapshot_module.SnapshotSelector(
            "products/app/runtime/optionhelper_agent_runtime",
            ("test/**",),
        ),
    )
    selected = snapshot_module._selected_files(ROOT, selectors)
    selected_paths = set(selected)

    assert {
        "packaging/app/agent_runtime/package.json",
        "packaging/app/agent_runtime/package-lock.json",
        "products/app/runtime/optionhelper_agent_runtime/package.json",
        "products/app/runtime/optionhelper_agent_runtime/package-lock.json",
        "products/app/runtime/optionhelper_agent_runtime/src/entrypoint/runtime.ts",
    }.issubset(selected_paths)
    assert all(
        not snapshot_module.ALWAYS_EXCLUDED_PARTS.intersection(Path(path).parts)
        for path in selected_paths
    )

    tracked = set(
        subprocess.run(
            ["git", "ls-files", "-z", "--", *[selector.path for selector in selectors]],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").split("\0")
    )
    tracked.discard("")
    assert selected_paths.issubset(tracked)


def test_agent_runtime_restores_locked_dependencies_only_in_the_temporary_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_module_path = ROOT / "packaging" / "app" / "agent_runtime" / "build_runtime.py"
    spec = importlib.util.spec_from_file_location(
        "optionhelper_clean_checkout_runtime_build",
        runtime_module_path,
    )
    assert spec is not None and spec.loader is not None
    runtime_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime_module
    spec.loader.exec_module(runtime_module)
    commands: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path | None = None) -> None:
        assert cwd is not None
        root = Path(command[command.index("--prefix") + 1])
        commands.append((list(command), root))
        assert root == cwd
        if root.name == "runtime-source":
            executable = root / "node_modules" / "esbuild" / "bin" / "esbuild"
        else:
            postject_name = "postject.cmd" if runtime_module.os.name == "nt" else "postject"
            executable = root / "node_modules" / ".bin" / postject_name
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8", newline="")
        executable.chmod(0o755)

    monkeypatch.setattr(runtime_module, "_run", fake_run)
    workspace = tmp_path / "locked-build"
    runtime_source, postject = runtime_module._prepare_locked_build_workspace(
        ROOT / "products" / "app" / "runtime" / "optionhelper_agent_runtime",
        workspace,
        npm=Path("/controlled/npm"),
        build_tools_source=ROOT / "packaging" / "app" / "agent_runtime",
    )

    assert runtime_source == workspace / "runtime-source"
    postject_name = "postject.cmd" if runtime_module.os.name == "nt" else "postject"
    assert postject == workspace / "build-tools" / "node_modules" / ".bin" / postject_name
    assert len(commands) == 2
    assert all(command[command.index("ci") - 1] == "/controlled/npm" for command, _root in commands)
    assert all(root.is_relative_to(workspace) for _command, root in commands)
    assert not any(root.is_relative_to(ROOT) for _command, root in commands)
    assert not (runtime_source / "dist").exists()
    assert not (runtime_source / "test").exists()


def test_app_audit_imports_and_native_shell_fallback_are_in_the_default_closure() -> None:
    snapshot_module = _load_source_snapshot("optionhelper_clean_checkout_app_closure")
    from platform_payload import (
        APP_BACKEND_SOURCE_INPUTS,
        MACOS_BUILD_INPUTS,
        WINDOWS_BUILD_INPUTS,
    )

    selected = snapshot_module._selected_files(
        ROOT,
        snapshot_module.default_snapshot_selectors(ROOT),
    )
    backend_sources = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "products" / "app" / "backend").rglob("*.py")
        if "__pycache__" not in path.parts
    }

    assert {
        "products/app/backend/audit/__init__.py",
        "products/app/backend/audit/audit_models.py",
        "products/app/backend/audit/audit_service.py",
        "products/app/desktop/macos/OptionHelperApp.m",
    }.issubset(selected)
    assert backend_sources.issubset(selected)
    assert "products/app/backend/audit" in APP_BACKEND_SOURCE_INPUTS
    assert set(APP_BACKEND_SOURCE_INPUTS).issubset(MACOS_BUILD_INPUTS)
    assert set(APP_BACKEND_SOURCE_INPUTS).issubset(WINDOWS_BUILD_INPUTS)
    assert "products/app/backend" not in MACOS_BUILD_INPUTS
    assert "products/app/backend" not in WINDOWS_BUILD_INPUTS
    backend_root = ROOT / "products" / "app" / "backend"
    actual_backend_inputs = {
        path.relative_to(ROOT).as_posix()
        for path in backend_root.iterdir()
        if path.name != "__pycache__"
    }
    assert set(APP_BACKEND_SOURCE_INPUTS) == actual_backend_inputs
