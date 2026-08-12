"""Reporter受控选择的唯一RecommendationSet与Catalog事实构建。"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .models import ReporterError, require_identifier, require_text, stable_hash


def build_host_selection_source_refs(source: Mapping[str, Any], tenant_id: str) -> dict[str, Any]:
    """从Host已鉴权候选生成一个完整、确定性的选择证据包。"""

    source_id = require_identifier(source.get("source_id"), "source.source_id")
    task_id = require_identifier(source.get("task_id"), "source.task_id")
    analysis_case_id = require_identifier(source.get("analysis_case_id"), "source.analysis_case_id")
    catalog_version = str(source.get("catalog_version") or "app-host-selection")
    raw_candidates = source.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ReporterError("Host选择证据缺少候选")

    candidates: list[dict[str, Any]] = []
    product_refs: dict[str, dict[str, str]] = {}
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise ReporterError("Host选择证据含无效候选")
        candidate = dict(raw)
        candidate_id = require_identifier(candidate.get("candidate_id"), "candidate.candidate_id")
        product_id = require_identifier(candidate.get("product_id"), "candidate.product_id")
        # Product versions are immutable evidence labels, not path segments.
        # Runtime contracts may use a qualified form such as
        # ``unversioned:<content-hash>``; preserve it exactly so the selected
        # candidate can be matched against the committed contract snapshot.
        product_version = require_text(candidate.get("product_version"), "candidate.product_version")
        candidate.pop("module_run_refs", None)
        # The Host selection is an identity and authorization boundary, not a
        # second recommender.  Keep public candidate facts already frozen by
        # the authoritative recommendation/product source.  Replacing them
        # here used to erase risks, suitability and terms and introduced an
        # internal Host sentence into customer-facing reports.
        candidate.update({
            "rank": int(candidate.get("rank") or 1),
            "reason": str(candidate.get("reason") or "").strip(),
            "suitable_for": list(candidate.get("suitable_for", [])) if isinstance(candidate.get("suitable_for"), list) else [],
            "not_suitable_for": list(candidate.get("not_suitable_for", [])) if isinstance(candidate.get("not_suitable_for"), list) else [],
            "main_risks": list(candidate.get("main_risks", [])) if isinstance(candidate.get("main_risks"), list) else [],
            "library_status": str(candidate.get("library_status") or "ready"),
            "key_terms": list(candidate.get("key_terms", [])) if isinstance(candidate.get("key_terms"), list) else [],
            "evidence_refs": list(candidate.get("evidence_refs", [])) if isinstance(candidate.get("evidence_refs"), list) else [],
        })
        candidates.append(candidate)
        product_refs[candidate_id] = {
            "product_id": product_id,
            "product_version": product_version,
            "content_hash": stable_hash({"product_id": product_id, "product_version": product_version}),
        }

    catalog_hash = stable_hash({"catalog_version": catalog_version, "product_versions": product_refs})
    run_id = f"selection-{hashlib.sha256(source_id.encode()).hexdigest()[:24]}"
    recommendation = {
        "schema": "optionhelper.recommendation-set/v2",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "run_id": run_id,
        "catalog_version": catalog_version,
        "catalog_content_hash": catalog_hash,
        "candidates": candidates,
    }
    return {
        "product_version_refs": product_refs,
        "catalog_version_ref": {"catalog_version": catalog_version, "content_hash": catalog_hash},
        "evidence_refs": {"recommendation_set": {
            "source_id": "app-result-selection",
            "run_id": run_id,
            "payload": recommendation,
            "expected_semantic_result_hash": stable_hash(recommendation),
        }},
        "module_run_refs": {},
    }


__all__ = ["build_host_selection_source_refs"]
