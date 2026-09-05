"""OptionHelper Recommender模块。"""

from .candidate_builder import create_term_variant
from .interaction import classify_term_change
from .models import (
    CandidateSelectionSpec,
    EvaluationRecord,
    RankingDecision,
    RankingSpec,
    RECOMMENDATION_SET_SCHEMA,
    RecommendationCase,
    RecommendationCandidate,
    RecommendationSet,
    RouteDecision,
)
from .service import (
    RecommenderService,
    RecommenderUnavailable,
    call_tool,
    capability,
    confirmation_inputs,
    recommend,
    recommend_fixed,
)

__all__ = (
    "CandidateSelectionSpec", "EvaluationRecord", "RankingDecision", "RankingSpec",
    "RECOMMENDATION_SET_SCHEMA", "RecommendationCase", "RecommendationCandidate", "RecommendationSet", "RouteDecision",
    "RecommenderService", "RecommenderUnavailable", "call_tool", "capability", "classify_term_change",
    "confirmation_inputs", "create_term_variant", "recommend", "recommend_fixed",
)
