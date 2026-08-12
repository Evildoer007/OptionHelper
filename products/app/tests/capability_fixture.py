"""Session-lifetime verified Capability for App tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_skill as skill_builder
from build_app import validated_capability
from build_skill import verify_source_snapshot
from verify_skill import probe_runtime, verify_skill


_capability_root: Path | None = None


def capability_root() -> Path:
    if _capability_root is None:
        raise RuntimeError("App测试Capability尚未初始化；请通过pytest运行测试")
    return _capability_root


def _no_port_runtime_probe(root: Path) -> list[str]:
    """Keep process/import checks while bypassing a sandbox-only bind denial."""
    return probe_runtime(root, page_probe=lambda _root: [])


def _assert_v12_capability(root: Path) -> None:
    manifest = json.loads((root / "capability-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_version") != "v1.2":
        raise RuntimeError("App测试Capability协议版本不是v1.2")
    hashes = manifest.get("content_hashes", {})
    required = {
        "scripts/tool_entry.py",
        "scripts/runtime/adapters/local_store.py",
        "scripts/runtime/protocol/models.py",
        "scripts/runtime/protocol/module_host.py",
        "scripts/runtime/protocol/schemas/caller-context.schema.json",
        "scripts/runtime/protocol/schemas/module-host-context.schema.json",
        "scripts/runtime/protocol/schemas/run-ref.schema.json",
        "assets/pages/module-host-bridge.js",
        "assets/pages/datafetcher/datafetcher.js",
        "scripts/modules/reporter/artifact_validator.py",
        "scripts/modules/reporter/evidence_resolver.py",
        "scripts/modules/reporter/export_service.py",
        "scripts/modules/reporter/models.py",
        "scripts/modules/reporter/report_unit_builder.py",
        "assets/designer/vendor/echarts.min.js",
    }
    missing = sorted(required.difference(hashes)) if isinstance(hashes, dict) else sorted(required)
    if missing:
        raise RuntimeError("App测试Capability缺少v2发行资源：" + ",".join(missing))


def _assert_app_host_idempotency(root: Path) -> None:
    """Exercise the App-owned single-use request-id boundary on this Capability."""
    from products.app.backend.authorization.roles import Role
    from products.app.backend.errors import ValidationError
    from products.app.backend.identity.session_identity import SessionIdentity
    from products.app.backend.page_registry import PageRegistry

    registry = PageRegistry(root, token_secret=b"optionhelper-v2-fixture-secret")
    identity = SessionIdentity("fixture-principal", "fixture-tenant", Role.ADMIN, "fixture-session")
    context = registry.host_context(identity, "pricer", task_id="fixture-task", catalog_version="v1.0")
    request_id = "fixture-idempotency-request-0001"
    registry.validate_host_request(identity, "pricer", context, request_id)
    try:
        registry.validate_host_request(identity, "pricer", context, request_id)
    except ValidationError as error:
        if "Module Host request was already used" not in str(error):
            raise RuntimeError("App测试Capability幂等门禁返回了错误拒绝原因") from error
    else:
        raise RuntimeError("App测试Capability未拒绝重复Module Host request_id")


@pytest.fixture(scope="session", autouse=True)
def verified_capability_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    global _capability_root
    workspace = tmp_path_factory.mktemp("optionhelper-app-capability")
    if workspace.is_relative_to(ROOT):
        raise RuntimeError("App测试Capability必须构建在仓库外的系统临时目录")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(skill_builder, "probe_runtime", _no_port_runtime_probe)
        # App development tests must be reproducible from the current source
        # tree even when no immutable release has been signed yet.  Formal
        # CatalogVersion binding is exercised by the packaging/release gates;
        # this session fixture deliberately uses an ephemeral technical
        # candidate and never reads or writes ``versions/``.
        skill = skill_builder.build_skill(workspace / "skill", candidate=True, repo_root=ROOT)
    _assert_v12_capability(skill)
    _assert_app_host_idempotency(skill)
    errors = [*verify_skill(skill), *verify_source_snapshot(skill, repo_root=ROOT), *_no_port_runtime_probe(skill)]
    if errors:
        raise RuntimeError("App测试Capability未通过验收：\n" + "\n".join(errors))
    _capability_root = validated_capability(skill)
    try:
        yield _capability_root
    finally:
        _capability_root = None
