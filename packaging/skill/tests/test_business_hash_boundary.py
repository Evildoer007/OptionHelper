"""Packaging verifies delivered files, not mutable business-source hashes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_builder(name: str):
    skill_packaging = ROOT / "packaging" / "skill"
    if str(skill_packaging) not in sys.path:
        sys.path.insert(0, str(skill_packaging))
    spec = importlib.util.spec_from_file_location(name, skill_packaging / "build_skill.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_pricer_business_evidence_manifest_is_outside_the_skill_payload() -> None:
    source_map = json.loads(
        (ROOT / "packaging" / "skill" / "package-source-map.json").read_text(encoding="utf-8")
    )
    pricer_tree = next(
        item for item in source_map["trees"]
        if item["source"] == "modules/pricer/src"
    )

    assert "fair_parameter_evidence_manifest.json" in pricer_tree["exclude"]


def test_packaging_has_no_pricer_business_hash_protocol() -> None:
    production_files = (
        ROOT / "packaging" / "skill" / "build_skill.py",
        ROOT / "packaging" / "skill" / "verify_skill.py",
        ROOT / "packaging" / "app" / "verify_capability.py",
        ROOT / "packaging" / "build_current.py",
        ROOT / "packaging" / "source_snapshot.py",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in production_files)

    assert "pricer_evidence" not in combined
    assert "fair_parameter_evidence_manifest" not in combined
    assert not (ROOT / "packaging" / "skill" / "pricer_evidence.py").exists()


def test_skill_file_integrity_protocol_remains_enabled() -> None:
    builder = (ROOT / "packaging" / "skill" / "build_skill.py").read_text(encoding="utf-8")
    verifier = (ROOT / "packaging" / "skill" / "verify_skill.py").read_text(encoding="utf-8")

    assert "content_tree_entries(staged)" in builder
    assert '"content_hashes": hashes' in builder
    assert "tree_hash(entries)" in builder
    assert "_manifest_errors(root, entries)" in verifier


def test_skill_build_ignores_an_obsolete_business_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builder = _load_builder("optionhelper_business_hash_boundary_builder")
    repository = tmp_path / "repository"
    source_map_path = repository / "packaging" / "skill" / "package-source-map.json"
    contract = repository / "contract-source" / "contract.py"
    pricer = repository / "modules" / "pricer" / "src"
    source_map_path.parent.mkdir(parents=True)
    contract.parent.mkdir(parents=True)
    pricer.mkdir(parents=True)
    contract.write_text("CONTRACT = True\n", encoding="utf-8")
    (pricer / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pricer / "fair_parameter_evidence_manifest.json").write_text(
        '{"source_sha256":"deliberately-stale"}\n',
        encoding="utf-8",
    )
    for relative in (
        "modules/designer/src/comparison_renderer.py",
        "modules/designer/assets/templates/multicard-standard.template.json",
        "modules/designer/assets/templates/multicard.html",
        "modules/designer/assets/templates/multireport-standard.template.json",
        "modules/designer/assets/templates/multireport.html",
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "\n", encoding="utf-8")

    source_map = {
        "catalog": {"target": "scripts/knowledger/catalog-version.json"},
        "files": [],
        "trees": [
            {"source": "contract-source", "target": "scripts/runtime/contracts", "exclude": []},
            {
                "source": "modules/pricer/src",
                "target": "scripts/modules/pricer",
                "exclude": ["fair_parameter_evidence_manifest.json"],
            },
        ],
    }
    source_map_path.write_text(json.dumps(source_map), encoding="utf-8")
    monkeypatch.setattr(builder, "load_source_map", lambda _path: source_map)
    monkeypatch.setattr(
        builder,
        "_working_tree_catalog",
        lambda _root: {"catalog_version": "development", "products": {}},
    )
    monkeypatch.setattr(builder, "_protocol_id", lambda _root: "test-protocol")
    monkeypatch.setattr(builder, "_source_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "_module_hashes", lambda _entries: {})
    monkeypatch.setattr(builder, "verify_skill", lambda _root: [])
    monkeypatch.setattr(builder, "verify_source_snapshot", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "probe_runtime", lambda _root: [])

    skill = builder.build_skill(tmp_path / "output", candidate=True, repo_root=repository)

    assert (skill / "scripts" / "modules" / "pricer" / "service.py").is_file()
    assert not (skill / "scripts" / "modules" / "pricer" / "fair_parameter_evidence_manifest.json").exists()
    manifest = json.loads((skill / "capability-manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_hashes"]
    assert manifest["content_tree_hash"]
