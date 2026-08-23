"""Recommender流程配置，不保存模型或数据凭据。"""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RecommenderConfig:
    agent_mode: str = "auto"
    max_candidates: int = 3
    max_agent_rounds: int = 4
    multi_agent_preset: str = "sequential-deliberation"
    max_research_queries: int = 5
    port_timeout_seconds: float = 20.0
    model_gateway_url: str | None = None
    knowledger_url: str | None = None
    tool_gateway_url: str | None = None

    def __post_init__(self) -> None:
        if self.agent_mode not in {"auto", "single", "multi"}:
            raise ValueError("agent_mode必须为auto、single或multi")
        if not 1 <= self.max_candidates <= 3:
            raise ValueError("max_candidates必须位于1至3")
        if self.max_agent_rounds < 1:
            raise ValueError("max_agent_rounds必须为正数")
        if self.multi_agent_preset not in {
            "sequential-deliberation",
            "independent-council",
        }:
            raise ValueError("multi_agent_preset未启用")

    @classmethod
    def from_environment(cls) -> "RecommenderConfig":
        return cls(
            agent_mode=os.environ.get("OPTIONHELPER_RECOMMENDER_AGENT_MODE", "auto").strip().lower(),
            model_gateway_url=os.environ.get("OPTIONHELPER_MODEL_GATEWAY_URL") or None,
            knowledger_url=os.environ.get("OPTIONHELPER_KNOWLEDGER_URL") or None,
            tool_gateway_url=os.environ.get("OPTIONHELPER_TOOL_GATEWAY_URL") or None,
            port_timeout_seconds=float(os.environ.get("OPTIONHELPER_RECOMMENDER_TIMEOUT", "20")),
        )
