"""Release gates for the Core/App v2 hosted-tool protocol."""

from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from build_skill import load_source_map
from verify_skill import _formal_compute_protocol_errors


def _mapped_target(source: str) -> str | None:
    config = load_source_map()
    for entry in config["files"]:
        if entry["source"] == source:
            return str(entry["target"])
    source_path = PurePosixPath(source)
    for entry in config["trees"]:
        tree = PurePosixPath(str(entry["source"]))
        try:
            relative = source_path.relative_to(tree)
        except ValueError:
            continue
        return (PurePosixPath(str(entry["target"])) / relative).as_posix()
    return None


def test_source_map_covers_v2_host_and_portable_resources() -> None:
    expected = {
        "core/tool_entry.py": "scripts/tool_entry.py",
        "core/src/runtime/protocol/models.py": "scripts/runtime/protocol/models.py",
        "core/src/runtime/protocol/module_host.py": "scripts/runtime/protocol/module_host.py",
        "core/src/runtime/adapters/local_store.py": "scripts/runtime/adapters/local_store.py",
        "core/src/runtime/protocol/schemas/caller-context.schema.json": "scripts/runtime/protocol/schemas/caller-context.schema.json",
        "core/src/runtime/protocol/schemas/module-host-context.schema.json": "scripts/runtime/protocol/schemas/module-host-context.schema.json",
        "core/src/runtime/protocol/schemas/run-ref.schema.json": "scripts/runtime/protocol/schemas/run-ref.schema.json",
        "core/src/runtime/browser/module_host_bridge.js": "assets/pages/module-host-bridge.js",
        "modules/datafetcher/page/datafetcher.js": "assets/pages/datafetcher/datafetcher.js",
        "modules/reporter/src/artifact_validator.py": "scripts/modules/reporter/artifact_validator.py",
        "modules/reporter/src/evidence_resolver.py": "scripts/modules/reporter/evidence_resolver.py",
        "modules/reporter/src/export_service.py": "scripts/modules/reporter/export_service.py",
        "modules/reporter/src/models.py": "scripts/modules/reporter/models.py",
        "modules/reporter/src/report_unit_builder.py": "scripts/modules/reporter/report_unit_builder.py",
        "modules/designer/assets/vendor/echarts.min.js": "assets/designer/vendor/echarts.min.js",
    }
    assert {source: _mapped_target(source) for source in expected} == expected


def test_formal_probe_executes_v2_context_security_checks(monkeypatch, tmp_path: Path) -> None:
    captured: list[str] = []

    def completed(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("verify_skill.subprocess.run", completed)
    assert _formal_compute_protocol_errors(tmp_path, sys.executable, {}, str(tmp_path)) == []
    probe = captured[0]
    assert 'expected = ["module", "request", "caller_context", "host_context", "result_store", "data_store"]' in probe
    for required in (
        "CallerContext(",
        "ModuleHostContext(",
        'capabilities=("module.catalog",)',
        'request_policy=("module.catalog",)',
        'request_id="release-probe-idempotency-0001"',
        'assert caller.request_id == "release-probe-idempotency-0001"',
        "module.run calling catalog was not rejected",
        "module.catalog calling run was not rejected",
        "formal run did not retain module.run authorization",
        'protocol_version="v1.2"',
        "LocalResultStore(",
        "commit_module_run(",
        "expected_artifact_manifest_hash",
        "new ModuleRunRef accepted legacy five-field shape",
        "committed RunRef external artifact anchor mismatch",
        "tenant rejection was not enforced",
    ):
        assert required in probe


def test_app_capability_fixture_runs_the_v12_acceptance_gate() -> None:
    fixture = (ROOT / "products" / "app" / "tests" / "capability_fixture.py").read_text(encoding="utf-8")
    assert "_assert_v12_capability" in fixture
    assert "_assert_v12_capability(skill)" in fixture
    assert 'manifest.get("protocol_version") != "v1.2"' in fixture
    assert "_assert_app_host_idempotency" in fixture
    assert "_assert_app_host_idempotency(skill)" in fixture
    assert "Module Host request was already used" in fixture
    assert "candidate=True" in fixture
    assert 'catalog_version="v1.0", repo_root=ROOT' not in fixture


def test_app_verifier_checks_v12_owned_adapters() -> None:
    verifier = (ROOT / "packaging" / "app" / "verify_app.py").read_text(encoding="utf-8")
    assert "backend/reporter_adapter.py" in verifier
    assert "backend/stores/result_store.py" in verifier
    assert "expected_artifact_manifest_hash" in verifier
