"""The development launcher must never require or create a formal release."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
PACKAGING_APP = ROOT / "packaging" / "app"
if str(PACKAGING_APP) not in sys.path:
    sys.path.insert(0, str(PACKAGING_APP))

import run_development_app


def test_development_capability_uses_only_the_working_tree_candidate(tmp_path: Path) -> None:
    capability = tmp_path / "skill" / "option-helper"
    capability.mkdir(parents=True)
    repository = tmp_path / "repository"
    formal_release = repository / "versions" / "v1.0"
    formal_release.mkdir(parents=True)
    sentinel = formal_release / "immutable-release.txt"
    sentinel.write_bytes(b"formal-release-must-not-change")

    with (
        patch.object(run_development_app, "ROOT", repository),
        patch.object(run_development_app, "build_skill", return_value=capability) as build,
        patch.object(run_development_app, "verify_skill", return_value=[]),
        patch.object(run_development_app, "verify_source_snapshot", return_value=[]),
        patch.object(run_development_app, "probe_runtime", return_value=[]),
        patch.object(run_development_app, "validated_capability", return_value=capability),
        patch.object(run_development_app, "verify_app", return_value=[]),
    ):
        result = run_development_app.build_development_capability(tmp_path, "v1.0")

    assert result == capability
    build.assert_called_once_with(tmp_path / "skill", candidate=True, repo_root=repository)
    assert sentinel.read_bytes() == b"formal-release-must-not-change"
    assert {path.name for path in formal_release.iterdir()} == {sentinel.name}
