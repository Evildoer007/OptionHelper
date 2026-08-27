"""Reviewer输出的权限边界与冲突检查。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import RecommendationValidationError


def critique_candidates(research: Mapping[str, Any], critic: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    proposals = research.get("proposals", ())
    reviews = critic.get("reviews", ())
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise RecommendationValidationError("Selector.proposals必须为数组")
    if isinstance(reviews, (str, bytes)) or not isinstance(reviews, Sequence):
        raise RecommendationValidationError("Reviewer.reviews必须为数组")
    proposed_ids = {
        str(item.get("product_id", "")).strip()
        for item in proposals if isinstance(item, Mapping) and str(item.get("product_id", "")).strip()
    }
    result: dict[str, Mapping[str, Any]] = {}
    for item in reviews:
        if not isinstance(item, Mapping):
            raise RecommendationValidationError("Reviewer.review必须为对象")
        product_id = str(item.get("product_id", "")).strip()
        if not product_id:
            raise RecommendationValidationError("Reviewer.review.product_id不能为空")
        if product_id not in proposed_ids:
            raise RecommendationValidationError(f"Reviewer越权新增候选：{product_id}")
        if product_id in result:
            raise RecommendationValidationError(f"Reviewer重复审阅产品：{product_id}")
        result[product_id] = dict(item)
    missing = sorted(proposed_ids - set(result))
    if missing:
        raise RecommendationValidationError(f"Reviewer漏审候选：{','.join(missing)}")
    return result
