"""Shadow mode must expose missing local runtime dependencies as start failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from products.app.backend.agent_runtime.runtime_subprocess import (
    RuntimeSubprocessError,
    RuntimeSubprocessTransport,
)


@pytest.fixture(scope="session")
def verified_capability_root() -> None:
    """Override the unrelated App-wide package build for this transport unit test."""


def test_shadow_source_runtime_reports_a_missing_node_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.cjs"
    runtime.write_text("process.exit(0);\n", encoding="utf-8", newline="")
    missing_node = tmp_path / "missing-node"
    monkeypatch.setenv("OPTIONHELPER_AGENT_RUNTIME_MODE", "shadow")

    transport = RuntimeSubprocessTransport(
        scope={"task_id": "shadow-missing-node"},
        runtime_path=runtime,
        node_binary=str(missing_node),
        session_root=tmp_path / "sessions",
    )

    with pytest.raises(
        RuntimeSubprocessError,
        match=r"runtime process start failed:.*missing-node",
    ):
        transport.initialize()
    assert transport.process is None


def test_shadow_runtime_reports_a_missing_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_runtime = tmp_path / "missing-runtime.cjs"
    monkeypatch.setenv("OPTIONHELPER_AGENT_RUNTIME_MODE", "shadow")

    transport = RuntimeSubprocessTransport(
        scope={"task_id": "shadow-missing-entrypoint"},
        runtime_path=missing_runtime,
        node_binary=str(tmp_path / "node"),
        session_root=tmp_path / "sessions",
    )

    with pytest.raises(RuntimeSubprocessError, match=r"runtime entrypoint not found"):
        transport.initialize()
    assert transport.process is None
