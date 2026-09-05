"""Reporter受控选择的唯一RecommendationSet事实构建。"""

from __future__ import annotations

from typing import Any, Mapping

from .models import ReporterError, require_identifier


def build_host_selection_source_refs(source: Mapping[str, Any], tenant_id: str) -> dict[str, Any]:
    """从Host已鉴权候选生成一个完整、确定性的选择证据包。"""

    source_id = require_identifier(source.get("source_id"), "source.source_id")
    task_id = require_identifier(source.get("task_id"), "source.task_id")
    analysis_case_id = require_identifier(source.get("analysis_case_id"), "source.analysis_case_id")
    raw_candidates = source.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ReporterError("Host选择证据缺少候选")

    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise ReporterError("Host选择证据含无效候选")
        candidate_id = require_identifier(raw.get("candidate_id"), "candidate.candidate_id")
        product_id = require_identifier(raw.get("product_id"), "candidate.product_id")
        rule_revision = raw.get("rule_revision")
        if not isinstance(rule_revision, int) or isinstance(rule_revision, bool) or rule_revision <= 0:
            raise ReporterError("candidate.rule_revision必须为正整数")
        # The Host selection is an identity and authorization boundary, not a
        # second recommender.  Keep public candidate facts already frozen by
        # the authoritative recommendation/product source.  Replacing them
        # here used to erase risks, suitability and terms and introduced an
        # internal Host sentence into customer-facing reports.
        candidate = {
            "candidate_id": candidate_id,
            "product_id": product_id,
            "rule_revision": rule_revision,
            **{key: raw.get(key) for key in (
                "source_candidate_id", "product_name", "analysis_basis_id",
                "underlyings", "currency", "price_convention", "supplemental_sections",
            ) if raw.get(key) is not None},
            "rank": int(raw.get("rank") or 1),
            "reason": str(raw.get("reason") or "").strip(),
            "suitable_for": list(raw.get("suitable_for", [])) if isinstance(raw.get("suitable_for"), list) else [],
            "not_suitable_for": list(raw.get("not_suitable_for", [])) if isinstance(raw.get("not_suitable_for"), list) else [],
            "main_risks": list(raw.get("main_risks", [])) if isinstance(raw.get("main_risks"), list) else [],
            "library_status": str(raw.get("library_status") or "ready"),
            "key_terms": list(raw.get("key_terms", [])) if isinstance(raw.get("key_terms"), list) else [],
            "evidence_refs": list(raw.get("evidence_refs", [])) if isinstance(raw.get("evidence_refs"), list) else [],
        }
        candidates.append(candidate)

    run_id = require_identifier(source.get("selection_run_id") or source_id, "source.selection_run_id")
    recommendation = {
        "schema": "optionhelper.recommendation-set",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "run_id": run_id,
        "candidates": candidates,
    }
    return {
        "evidence_refs": {"recommendation_set": {
            "source_id": source_id,
            "run_id": run_id,
            "payload": recommendation,
        }},
        "module_run_refs": {},
    }


__all__ = ["build_host_selection_source_refs"]
