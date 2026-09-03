from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
import textwrap
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_runtime_target_names_are_stable_and_platform_specific() -> None:
    module = _load("agent_runtime_packaging_names", "packaging/app/agent_runtime/build_runtime.py")

    assert module.expected_artifact_name("macos-arm64") == "optionhelper-agent-runtime-macos-arm64"
    assert module.expected_artifact_name("windows-x64") == "optionhelper-agent-runtime-windows-x64.exe"
    assert module.expected_artifact_name("windows-x64", with_extension=False) == "optionhelper-agent-runtime-windows-x64"
    assert module.SEA_SENTINEL == "NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2"


def test_plan_is_declarative_and_does_not_create_an_unverified_artifact(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_plan", "packaging/app/agent_runtime/build_runtime.py")
    output = tmp_path / "runtime-output"

    plan = module.make_build_plan("macos-arm64", output_root=output)

    assert plan.artifact == output / "optionhelper-agent-runtime-macos-arm64"
    assert plan.support_status == "unverified_local_candidate"
    assert not output.exists()


def test_preflight_rejects_cross_platform_build_before_tool_resolution(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_host", "packaging/app/agent_runtime/build_runtime.py")

    with pytest.raises(module.RuntimeBuildError, match="不匹配|交叉构建"):
        module.preflight_runtime(
            "macos-arm64",
            output_root=tmp_path,
            host_system="linux",
            host_machine="x86_64",
            node_path=tmp_path / "node",
            npm_path=tmp_path / "npm",
        )


def test_preflight_fails_closed_when_npm_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("agent_runtime_packaging_tools", "packaging/app/agent_runtime/build_runtime.py")
    node = _executable(tmp_path / "node")
    monkeypatch.setattr(module, "_node_version", lambda _path: (24, 18, 0))

    with pytest.raises(module.RuntimeBuildError, match="npm|依赖"):
        module.preflight_runtime(
            "macos-arm64",
            output_root=tmp_path / "output",
            host_system="darwin",
            host_machine="arm64",
            node_path=node,
            npm_path=tmp_path / "missing-npm",
            codesign_path=tmp_path / "missing-codesign",
        )


def test_sea_entry_is_built_only_from_the_local_runtime_source(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_entry", "packaging/app/agent_runtime/build_runtime.py")
    source = tmp_path / "runtime"
    entrypoint = source / "src" / "entrypoint" / "runtime.ts"
    esbuild = source / "node_modules" / "esbuild" / "bin" / "esbuild"
    entrypoint.parent.mkdir(parents=True)
    esbuild.parent.mkdir(parents=True)
    entrypoint.write_text("import 'node:sqlite';\n", encoding="utf-8")
    esbuild.write_text("", encoding="utf-8")
    destination = tmp_path / "entry.cjs"
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(command: list[str], *, cwd: Path | None = None) -> None:
        calls.append((list(command), cwd))
        destination.write_text(
            "// optionhelper-agent-runtime\n// node:sqlite\n" + "x" * 1_100,
            encoding="utf-8",
        )

    module._run = fake_run

    entry = module._create_sea_entry(source, destination, node=tmp_path / "node")
    content = entry.read_text(encoding="utf-8")

    assert entry == destination
    assert entry.stat().st_size > 1_000
    assert "optionhelper-agent-runtime" in content
    assert "node:sqlite" in content
    assert calls[0][1] == source
    assert str(entrypoint) in calls[0][0]


def test_runtime_name_boundary_covers_runtime_owned_surfaces() -> None:
    module = _load("agent_runtime_name_boundary", "packaging/app/agent_runtime/name_boundary.py")

    module.assert_name_boundary_clean(ROOT)
    expected_coverage = {
        ROOT / "products" / "app" / "runtime",
        ROOT / "products" / "app" / "backend" / "agent_runtime",
        ROOT / "products" / "app" / "frontend" / "optchat",
        ROOT / "packaging" / "app",
    }
    roots = set(module.runtime_scan_roots(ROOT))
    assert expected_coverage <= roots
    assert ROOT / "products" / "app" / "config" / "app-defaults.yaml" in roots
    assert ROOT / "products" / "app" / "config" / "logging.yaml" in roots


def test_full_product_audit_reports_existing_terms_without_treating_them_as_runtime_clean() -> None:
    module = _load("agent_runtime_name_boundary_full_audit", "packaging/app/agent_runtime/name_boundary.py")

    findings = module.scan_name_boundary(ROOT, paths=(ROOT / "products" / "app",))

    assert findings
    assert all(module.is_preexisting_product_surface(ROOT, item.path) for item in findings)


def test_runtime_name_boundary_allows_only_the_three_legal_files(tmp_path: Path) -> None:
    module = _load("agent_runtime_name_boundary_fixture", "packaging/app/agent_runtime/name_boundary.py")
    safe = tmp_path / "safe.txt"
    unsafe = tmp_path / "unsafe.txt"
    safe.write_text("ordinary runtime text\n", encoding="utf-8")
    unsafe.write_text("deep" + "seek marker\n", encoding="utf-8")

    findings = module.scan_name_boundary(tmp_path, paths=(safe, unsafe), legal_files=())

    assert [(item.path, item.marker) for item in findings] == [(unsafe, "deep" + "seek")]
    assert module.LEGAL_FILES == frozenset(
        {
            Path("LICENSES/OptionHelper-Agent-Runtime-LICENSE.txt"),
            Path("LICENSES/OptionHelper-Agent-Runtime-NOTICES.txt"),
            Path("LICENSES/OptionHelper-Agent-Runtime-SOURCE-MAPPING.md"),
        }
    )


def test_macos_license_bundle_copies_agent_runtime_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load("agent_runtime_macos_licenses", "packaging/app/macos/build_macos.py")
    capability = tmp_path / "capability"
    (capability / "LICENSES").mkdir(parents=True)
    (capability / "LICENSES" / "Capability-LICENSE.txt").write_text("capability\n", encoding="utf-8")
    site_packages = tmp_path / "site-packages"
    pyinstaller_package = site_packages / "PyInstaller" / "__init__.py"
    pyinstaller_package.parent.mkdir(parents=True)
    pyinstaller_package.write_text("", encoding="utf-8")
    pyinstaller_license = (
        site_packages
        / f"pyinstaller-{module.PYINSTALLER_VERSION}.dist-info"
        / "licenses"
        / "COPYING.txt"
    )
    pyinstaller_license.parent.mkdir(parents=True)
    pyinstaller_license.write_text("PyInstaller\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "PyInstaller", SimpleNamespace(__file__=str(pyinstaller_package)))
    monkeypatch.setattr(module, "assert_build_python", lambda: None)

    destination = tmp_path / "bundle-licenses"
    module.copy_licenses(destination, capability)

    for name in (
        "OptionHelper-Agent-Runtime-LICENSE.txt",
        "OptionHelper-Agent-Runtime-NOTICES.txt",
        "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md",
    ):
        assert (destination / "agent-runtime" / name).read_bytes() == (ROOT / "LICENSES" / name).read_bytes()
    assert "Agent Runtime" in (destination / "THIRD_PARTY.md").read_text(encoding="utf-8")


def test_runtime_candidate_requires_target_name_and_matching_manifest(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_candidate", "packaging/app/agent_runtime/build_runtime.py")
    candidate = tmp_path / module.expected_artifact_name("macos-arm64")
    candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    manifest = module.write_runtime_manifest(
        candidate,
        "macos-arm64",
        host_system="darwin",
        host_machine="arm64",
    )

    verified = module.verify_runtime_candidate(
        candidate,
        "macos-arm64",
        manifest_path=manifest,
        host_system="darwin",
        host_machine="arm64",
    )
    assert verified["support_status"] == "local_verified"

    candidate.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(module.RuntimeBuildError, match="哈希或大小"):
        module.verify_runtime_candidate(
            candidate,
            "macos-arm64",
            manifest_path=manifest,
            host_system="darwin",
            host_machine="arm64",
        )


def test_runtime_manifest_binds_current_source_tree_and_lock(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_source_provenance", "packaging/app/agent_runtime/build_runtime.py")
    source = tmp_path / "runtime-source"
    (source / "src").mkdir(parents=True)
    (source / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (source / "package.json").write_text('{"name":"@optionhelper/agent-runtime"}\n', encoding="utf-8")
    (source / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (source / "src" / "runtime.ts").write_text("export const current = true;\n", encoding="utf-8")
    (source / "test").mkdir()
    (source / "test" / "ignored.test.ts").write_text("throw new Error('not shipped');\n", encoding="utf-8")
    artifact = tmp_path / module.expected_artifact_name("macos-arm64")
    artifact.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    artifact.chmod(0o755)

    manifest_path = module.write_runtime_manifest(
        artifact,
        "macos-arm64",
        source_root=source,
        host_system="darwin",
        host_machine="arm64",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["package_lock_sha256"] == module._sha256(source / "package-lock.json")
    assert manifest["source_tree_hash"]
    assert set(manifest["source_content_hashes"]) == {
        "package-lock.json", "package.json", "src/runtime.ts", "tsconfig.json",
    }
    module.verify_runtime_candidate(
        artifact,
        "macos-arm64",
        source_root=source,
        require_source_provenance=True,
        host_system="darwin",
        host_machine="arm64",
    )
    (source / "src" / "runtime.ts").write_text("export const current = false;\n", encoding="utf-8")
    with pytest.raises(module.RuntimeBuildError, match="源码|source"):
        module.verify_runtime_candidate(
            artifact,
            "macos-arm64",
            source_root=source,
            require_source_provenance=True,
            host_system="darwin",
            host_machine="arm64",
        )


def test_formal_runtime_resolution_rejects_environment_and_dist_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = _load("agent_runtime_packaging_formal_resolution", "packaging/app/agent_runtime/build_runtime.py")
    stale = tmp_path / "stale"
    stale.write_text("old", encoding="utf-8")
    repository = tmp_path / "repo"
    (repository / "dist").mkdir(parents=True)
    (repository / "dist" / module.expected_artifact_name("macos-arm64")).write_text("old", encoding="utf-8")
    monkeypatch.setenv(module.RUNTIME_ARTIFACT_ENV, str(stale))

    assert module.resolve_runtime_candidate(
        "macos-arm64", repository_root=repository, formal=True,
    ) is None
    assert module.resolve_runtime_candidate(
        "macos-arm64", explicit=stale, repository_root=repository, formal=True,
    ) == stale.resolve()


def test_locked_node_dependency_preflight_does_not_install_into_source_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load("agent_runtime_dependency_restore", "packaging/app/agent_runtime/restore_dependencies.py")
    before = {
        root: (root / "node_modules").stat().st_mtime_ns
        for root in module.DEPENDENCY_ROOTS
        if (root / "node_modules").exists()
    }
    module.restore_locked_dependencies(npm="/controlled/npm")

    assert before == {
        root: (root / "node_modules").stat().st_mtime_ns
        for root in before
    }


def test_locked_node_dependency_preflight_rejects_missing_npm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load("agent_runtime_missing_npm_preflight", "packaging/app/agent_runtime/restore_dependencies.py")
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    with pytest.raises(module.DependencyRestoreError, match="npm"):
        module.restore_locked_dependencies()


def test_formal_app_runtime_gate_rejects_missing_candidate(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_formal_gate", "packaging/app/agent_runtime/build_runtime.py")

    with pytest.raises(module.RuntimeBuildError, match="正式.*缺少.*原生Agent运行时"):
        module.prepare_staged_runtime(
            tmp_path / "Resources",
            "macos-arm64",
            None,
            required=True,
        )


def test_source_runtime_fallback_is_development_only_and_explicit(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_source_fallback", "packaging/app/agent_runtime/build_runtime.py")
    summary = module.prepare_staged_runtime(
        tmp_path / "Resources",
        "macos-arm64",
        None,
        required=False,
        allow_source_fallback=True,
    )

    assert summary == {
        "status": "disabled",
        "support_status": "development_only",
        "fallback": "source_explicit",
        "resource_path": None,
        "manifest_path": None,
    }
    with pytest.raises(module.RuntimeBuildError, match="显式开启"):
        module.prepare_staged_runtime(
            tmp_path / "Resources-2",
            "macos-arm64",
            None,
            required=False,
            allow_source_fallback=False,
        )


def test_staged_runtime_is_optional_but_verified_when_present(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_stage", "packaging/app/agent_runtime/build_runtime.py")
    resources = tmp_path / "Resources"
    disabled = module.stage_runtime_candidate(resources, "macos-arm64", None)
    assert disabled == {"status": "disabled", "resource_path": None, "manifest_path": None}

    candidate = tmp_path / module.expected_artifact_name("macos-arm64")
    candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    module.write_runtime_manifest(candidate, "macos-arm64", host_system="darwin", host_machine="arm64")
    summary = module.stage_runtime_candidate(
        resources,
        "macos-arm64",
        candidate,
        host_system="darwin",
        host_machine="arm64",
    )
    assert summary["status"] == "local_verified"
    module.verify_staged_runtime(resources, "macos-arm64", summary)
    staged = resources / str(summary["resource_path"])
    staged.write_bytes(b"tampered")
    with pytest.raises(module.RuntimeBuildError, match="哈希或大小"):
        module.verify_staged_runtime(resources, "macos-arm64", summary)


def test_runtime_process_probe_requires_clean_direct_executable_shutdown(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_probe", "packaging/app/agent_runtime/build_runtime.py")
    executable = tmp_path / "optionhelper-agent-runtime"
    executable.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys
            for line in sys.stdin:
                request = json.loads(line)
                if request["method"] == "runtime.initialize":
                    result = {"rootSessionId": request["params"]["rootSessionId"], "version": "test", "protocolVersion": "2.0"}
                elif request["method"] == "runtime.capabilities":
                    result = {"protocolVersion": "2.0", "capabilities": {
                        "persistentSessions": True,
                        "nativeToolLoop": True,
                        "streaming": True,
                        "oneShotSubagent": True,
                        "continuableSubagent": True,
                    }}
                else:
                    result = {"status": "stopped"}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    result = module.probe_runtime_process(executable)
    assert result["status"] == "local_verified"
    assert result["support_status"] == "local_verified"
    assert result["hash"]["status"] == "local_verified"


def test_runtime_source_requires_locked_build_dependencies_and_no_runtime_dependencies(tmp_path: Path) -> None:
    module = _load("agent_runtime_packaging_source_closure", "packaging/app/agent_runtime/build_runtime.py")
    source = tmp_path / "runtime"
    (source / "src" / "entrypoint").mkdir(parents=True)
    (source / "src" / "entrypoint" / "runtime.ts").write_text("import 'node:fs';\n", encoding="utf-8")
    (source / "tsconfig.json").write_text("{}", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps({
            "name": "@optionhelper/agent-runtime",
            "dependencies": {"external-package": "1.0.0"},
            "devDependencies": {"esbuild": "0.25.12"},
        }),
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {"devDependencies": {"esbuild": "0.25.12"}}},
    }), encoding="utf-8")

    with pytest.raises(module.RuntimeBuildError, match="外部运行依赖"):
        module._validate_source(source)

    (source / "package.json").write_text(json.dumps({
        "name": "@optionhelper/agent-runtime",
        "devDependencies": {"esbuild": "^0.25.12"},
    }), encoding="utf-8")
    (source / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {"devDependencies": {"esbuild": "^0.25.12"}}},
    }), encoding="utf-8")
    with pytest.raises(module.RuntimeBuildError, match="精确锁定"):
        module._validate_source(source)

    (source / "package.json").write_text(json.dumps({
        "name": "@optionhelper/agent-runtime",
        "devDependencies": {"esbuild": "0.25.12"},
    }), encoding="utf-8")
    (source / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {"devDependencies": {"esbuild": "0.25.12"}}},
    }), encoding="utf-8")
    module._validate_source(source)
    assert not (source / "node_modules").exists()


def test_name_boundary_allows_provider_and_legal_files_but_scans_zip_members(tmp_path: Path) -> None:
    module = _load("agent_runtime_name_boundary_artifacts", "packaging/app/agent_runtime/name_boundary.py")
    provider = tmp_path / "backend" / "model_gateway" / "provider.py"
    provider.parent.mkdir(parents=True)
    provider.write_text("provider DeepSeek endpoint\n", encoding="utf-8")
    legal = tmp_path / "LICENSES" / "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md"
    legal.parent.mkdir(parents=True)
    legal.write_text("upstream deepseek-harness and @deepseek-ai\n", encoding="utf-8")
    unsafe = tmp_path / "frontend" / "runtime-name.txt"
    unsafe.parent.mkdir(parents=True)
    unsafe.write_text("dsh deepseek-harness @deepseek-ai\n", encoding="utf-8")
    signature_index = tmp_path / "OptionHelper.app" / "Contents" / "_CodeSignature" / "CodeResources"
    signature_index.parent.mkdir(parents=True)
    signature_index.write_text("Resources/app/backend/model_gateway/deepseek_provider.py\n", encoding="utf-8")
    archive = tmp_path / "OptionHelper.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("OptionHelper/Resources/frontend/deepseek-harness.txt", "ordinary")

    findings = module.scan_name_boundary(tmp_path, paths=(provider, legal, unsafe, signature_index, archive))
    assert {(item.marker, item.location) for item in findings} >= {
        ("dsh", "content"),
        ("deepseek-harness", "content"),
        ("@deepseek-ai", "content"),
        ("deepseek-harness", "path"),
    }
    assert not any(item.path in {provider, legal, signature_index} for item in findings)
