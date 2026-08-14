"""通过Knowledger正式端口锁定同一CatalogVersion的推荐证据。"""

from __future__ import annotations

from typing import Mapping, Sequence

from .models import EvidenceRef, RecommendationValidationError
from .ports import KnowledgePort


class EvidenceRetrievalError(RuntimeError):
    pass


def retrieve_evidence(
    port: KnowledgePort,
    *,
    catalog_version: str,
    queries: Sequence[str],
    limit_per_query: int = 8,
) -> tuple[EvidenceRef, ...]:
    clean_queries = tuple(dict.fromkeys(str(item).strip() for item in queries if str(item).strip()))
    if not clean_queries:
        raise EvidenceRetrievalError("Research未提供有效检索查询")
    evidence: dict[str, EvidenceRef] = {}
    for query in clean_queries:
        response = port.search({
            "catalog_version": catalog_version,
            "queries": [query],
            "sources": ["optionlist", "optionlib", "optionreg_status"],
            "limit": limit_per_query,
        })
        returned_version = str(response.get("catalog_version", "")).strip()
        if returned_version != catalog_version:
            raise EvidenceRetrievalError(
                f"Knowledger返回catalog_version={returned_version or '<empty>'}，任务固定为{catalog_version}"
            )
        rows = response.get("evidence", ())
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise EvidenceRetrievalError("Knowledger响应evidence必须为数组")
        for row in rows:
            if not isinstance(row, Mapping):
                raise EvidenceRetrievalError("Knowledger证据项必须为对象")
            try:
                item = EvidenceRef.from_mapping(row, expected_catalog_version=catalog_version)
            except RecommendationValidationError as error:
                raise EvidenceRetrievalError(str(error)) from error
            existing = evidence.get(item.evidence_id)
            if existing is not None and existing != item:
                raise EvidenceRetrievalError(f"证据ID冲突：{item.evidence_id}")
            evidence[item.evidence_id] = item
    return tuple(evidence[key] for key in sorted(evidence))
