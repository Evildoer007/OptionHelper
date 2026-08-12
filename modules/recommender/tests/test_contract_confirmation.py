"""候选条款确认、覆盖与续接的Recommender专项回归。"""

from __future__ import annotations

import hashlib
import unittest

from modules.recommender.candidate_builder import build_candidates
from modules.recommender.executor import execute
from modules.recommender.interaction import merge_confirmed_constraints
from modules.recommender.models import (
    AuditEvent,
    CandidateContract,
    EvidenceRef,
    ModelCapability,
    ModuleRunRef,
    RecommendationCandidate,
    RecommendationSet,
    RecommendationValidationError,
)
from modules.recommender.service import RecommenderService


def _evidence(source: str) -> EvidenceRef:
    excerpt = f"{source}受控证据"
    return EvidenceRef(
        evidence_id=f"ev_{source}", product_id="2.1", catalog_version="v1.0",
        source=source, section="2.1", material_status="ready",
        excerpt_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(), excerpt=excerpt,
        identity={"product_id": "2.1"} if source == "optionlist" else {},
        entry_status=True if source == "optionreg_status" else None,
    )


def _candidate(*, candidate_id: str = "recommend_1_candidate_01", rank: int = 1) -> RecommendationCandidate:
    return RecommendationCandidate(
        candidate_id=candidate_id, product_id="2.1", underlyings=("000905.SH",), rank=rank,
        reason="与客户确认的上涨及波动率上升判断一致。", suitable_for=("接受本金波动。",),
        not_suitable_for=("需要保本。",), main_risks=("标的下跌。",), library_status="ready",
        evidence_refs=(_evidence("optionlist"), _evidence("optionlib"), _evidence("optionreg_status")),
    )


def _resolved_contract(*, product_id: str = "2.1", underlyings: list[str] | None = None) -> dict:
    return {
        "identity": {"product_id": product_id, "underlyings": underlyings or ["000905.SH"]},
        "terms": {"T": 0.25, "K": 100.0},
        "term_sources": {"T": "override", "K": "default"},
        "contract_fingerprint": "a" * 64,
    }


def _contract(*, candidate_id: str = "recommend_1_candidate_01", display_terms: list[dict] | None = None) -> dict:
    value = {
        "candidate_id": candidate_id, "product_id": "2.1", "underlyings": ["000905.SH"],
        "catalog_version": "v1.0", "evidence_ref_ids": ["ev_optionlist", "ev_optionlib", "ev_optionreg_status"],
        "contract_fingerprint": "a" * 64, "resolved_contract": _resolved_contract(),
    }
    if display_terms is not None:
        value["display_terms"] = display_terms
    return value


class _Runner:
    class Audit:
        def append(self, *_args, **_kwargs) -> None:
            pass

    def __init__(self) -> None:
        self.audit = self.Audit()

    def run(self, _role: str, _payload: dict) -> dict:
        return {"tool_requests": [{"candidate_id": "recommend_1_candidate_01", "module": "payoffer"}]}


class _NoRunner(_Runner):
    def run(self, _role: str, _payload: dict) -> dict:
        raise AssertionError("纯交付请求不得规划或调用计算模块")


class _Tool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, module: str, body: dict) -> dict:
        self.calls.append((module, dict(body)))
        return {
            "status": "succeeded", "run_id": "payoffer_1", "candidate_id": "recommend_1_candidate_01",
            "catalog_version": "v1.0", "contract_fingerprint": "a" * 64,
        }


class _Agent:
    def capability(self) -> ModelCapability:
        return ModelCapability("test", structured_output=True, tool_calling=True)

    def run_step(self, role: str, _payload: dict) -> dict:
        if role.endswith("Intent"):
            return {"confirmed_constraints": {}, "missing_information": [], "next_question": None, "research_queries": ["看涨期权"]}
        if role.endswith("Research"):
            return {"proposals": [{
                "product_id": "2.1", "product_name": "看涨期权", "underlyings": ["000905.SH"],
                "reason": "符合客户观点。", "suitable_for": ["上涨判断"], "not_suitable_for": ["保本需求"],
                "main_risks": ["标的下跌"], "library_status": "ready",
                "evidence_ref_ids": ["ev_optionlist", "ev_optionlib", "ev_optionreg_status"], "missing_inputs": [],
            }]}
        if role.endswith("Critic"):
            return {"reviews": [{
                "product_id": "2.1", "hard_reject": False, "rejection_reason": None,
                "additional_not_suitable_for": [], "additional_risks": [], "rank_adjustment": 0,
            }]}
        raise AssertionError(role)


class _Knowledge:
    def search(self, _payload: dict) -> dict:
        rows = []
        for source in ("optionlist", "optionlib", "optionreg_status"):
            evidence = _evidence(source)
            rows.append({
                "evidence_id": evidence.evidence_id, "product_id": evidence.product_id,
                "catalog_version": evidence.catalog_version, "source": evidence.source, "section": evidence.section,
                "library_status": evidence.material_status, "excerpt_hash": evidence.excerpt_hash,
                "excerpt": evidence.excerpt, "identity": dict(evidence.identity), "entry_status": evidence.entry_status,
            })
        return {"catalog_version": "v1.0", "evidence": rows}


def _case(**extra: object) -> dict:
    value: dict[str, object] = {
        "analysis_case_id": "case_1", "task_id": "task_1", "tenant_id": "tenant_1",
        "prompt": "000905.SH未来3个月上涨且波动率上升，最大亏损30%，接受本金波动。推荐期权产品。",
        "catalog_version": "v1.0", "run_id": "recommend_1",
        "confirmed_constraints": {
            "underlying": "000905.SH", "horizon": "3个月", "market_view": "上涨+波动率上升",
            "max_loss": "30%", "principal_fluctuation": True,
        },
    }
    value.update(extra)
    return value


class ContractConfirmationTests(unittest.TestCase):
    def test_resolved_contract_identity_must_bind_candidate(self) -> None:
        invalid = _contract()
        invalid["resolved_contract"] = _resolved_contract(product_id="3.3")

        with self.assertRaises(RecommendationValidationError):
            CandidateContract.from_mapping(invalid, candidate=_candidate())

    def test_stale_candidate_contract_cannot_be_silently_carried_into_execution(self) -> None:
        contracts = {"recommend_1_candidate_01": _contract(), "stale_candidate": _contract()}
        contracts["stale_candidate"] = {**contracts["stale_candidate"], "candidate_id": "stale_candidate"}

        with self.assertRaises(RecommendationValidationError):
            execute(
                (_candidate(),), approved_candidate_ids=("recommend_1_candidate_01",), requested_outputs=("payoff",),
                task_id="task_1", run_id="recommend_1", candidate_contracts=contracts,
                step_runner=_Runner(), tool_port=_Tool(),
            )

    def test_unapproved_candidate_contract_cannot_reach_executor(self) -> None:
        contracts = {
            "recommend_1_candidate_01": _contract(),
            "recommend_1_candidate_02": _contract(candidate_id="recommend_1_candidate_02"),
        }

        with self.assertRaises(RecommendationValidationError):
            execute(
                (_candidate(), _candidate(candidate_id="recommend_1_candidate_02", rank=2)),
                approved_candidate_ids=("recommend_1_candidate_01",), requested_outputs=("payoff",),
                task_id="task_1", run_id="recommend_1", candidate_contracts=contracts,
                step_runner=_Runner(), tool_port=_Tool(),
            )

    def test_resolved_terms_are_shown_and_wait_for_one_confirmation_before_running(self) -> None:
        tool = _Tool()
        service = RecommenderService(agent_port=_Agent(), knowledge_port=_Knowledge(), tool_port=tool)
        response = service.recommend_fixed(_case(
            requested_outputs=["payoff", "card"],
            candidate_contracts={"recommend_1_candidate_01": _contract(display_terms=[
                {"label": "期限", "value": "3个月", "source": "用户输入"},
                {"label": "行权价", "value": "100", "source": "拟采用参数"},
            ])},
        ))["recommendation_set"]

        self.assertEqual(response["status"], "pending_approval")
        self.assertEqual(response["candidates"][0]["candidate_status"], "pending_confirmation")
        self.assertEqual(response["candidates"][0]["key_terms"], [
            {"label": "期限", "value": "3个月", "source": "用户输入"},
            {"label": "行权价", "value": "100", "source": "拟采用参数"},
        ])
        self.assertIn("是否按此继续", response["next_question"])
        self.assertEqual(tool.calls, [])

    def test_explicit_dual_delivery_is_preserved_without_duplicate_analysis_runs(self) -> None:
        service = RecommenderService(agent_port=_Agent(), knowledge_port=_Knowledge())
        response = service.recommend_fixed(_case(
            prompt="000905.SH未来3个月上涨且波动率上升，最大亏损30%，接受本金波动。推荐产品，研究简报和完整研究报告都要HTML。",
        ))["recommendation_set"]

        self.assertEqual(response["requested_outputs"], ["card", "report"])

    def test_terminal_named_run_rejects_wrong_contract_binding(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            ModuleRunRef.from_tool_result(
                "payoffer",
                {
                    "status": "failed", "run_id": "run_wrong", "candidate_id": "other_candidate",
                    "catalog_version": "old", "contract_fingerprint": "b" * 64,
                },
                candidate_id="recommend_1_candidate_01", catalog_version="v1.0", contract_fingerprint="a" * 64,
            )

    def test_later_explicit_constraints_replace_loss_delivery_and_format(self) -> None:
        constraints = merge_confirmed_constraints({
            "max_loss": "30%", "output_type": "both", "format": "html",
        }, [
            "最大亏损改为20%。不要简报，只要完整研究报告。不要HTML，改成PDF。",
        ])

        self.assertEqual(constraints["max_loss"], "20%")
        self.assertEqual(constraints["output_type"], "report")
        self.assertEqual(constraints["format"], "pdf")

    def test_card_and_report_only_prevalidate_contract_without_running_modules(self) -> None:
        contract = _contract()
        contract["resolved_contract"] = _resolved_contract(product_id="3.3")

        with self.assertRaisesRegex(RecommendationValidationError, "ResolvedContract.identity.product_id"):
            execute(
                (_candidate(),), approved_candidate_ids=("recommend_1_candidate_01",),
                requested_outputs=("card", "report"), task_id="task_1", run_id="recommend_1",
                candidate_contracts={"recommend_1_candidate_01": contract}, step_runner=_NoRunner(), tool_port=None,
            )

    def test_card_and_report_only_mark_candidate_approved_without_running_modules(self) -> None:
        result = execute(
            (_candidate(),), approved_candidate_ids=("recommend_1_candidate_01",),
            requested_outputs=("card", "report"), task_id="task_1", run_id="recommend_1",
            candidate_contracts={"recommend_1_candidate_01": _contract()}, step_runner=_NoRunner(), tool_port=None,
        )

        self.assertEqual(result[0].candidate_status, "approved")
        self.assertEqual(result[0].module_run_refs, ())

    def test_dual_delivery_reuses_one_requested_analysis_run(self) -> None:
        tool = _Tool()
        execute(
            (_candidate(),), approved_candidate_ids=("recommend_1_candidate_01",),
            requested_outputs=("payoff", "card", "report"), task_id="task_1", run_id="recommend_1",
            candidate_contracts={"recommend_1_candidate_01": _contract()}, step_runner=_Runner(), tool_port=tool,
        )

        self.assertEqual([module for module, _ in tool.calls], ["payoffer"])

    def test_public_projection_excludes_internal_identifiers_and_exceptions(self) -> None:
        item = _candidate()
        recommendation = RecommendationSet(
            schema="optionhelper.recommendation-set/v1", task_id="task-internal", run_id="run-internal",
            analysis_case_id="analysis-internal", catalog_version="v1.0", route="recommendation",
            workflow_mode="single_agent", status="partial", primary_candidate_id=item.candidate_id,
            candidates=(item,), requested_outputs=("card", "report"),
            limitations=("CandidateContract不属于当前候选：stale",),
            audit_trail=(AuditEvent(1, "workflow", "failed", None, "single_agent", "a", None, {}),),
        )

        public = recommendation.to_public_dict()

        rendered = str(public)
        for forbidden in ("task-internal", "run-internal", "analysis-internal", "recommend_1_candidate_01", "CandidateContract", "audit"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(public["交付"], ["研究简报", "完整研究报告"])
        self.assertEqual(public["推荐结果"][0]["产品编号"], "2.1")

    def test_explicitly_opposite_product_is_rejected_even_when_critic_allows_it(self) -> None:
        evidence = tuple(
            EvidenceRef(
                evidence_id=f"ev_{source}", product_id="3.3", catalog_version="v1.0", source=source,
                section="3.3", material_status="ready", excerpt_hash=hashlib.sha256(source.encode()).hexdigest(),
                excerpt=source,
                identity={"product_id": "3.3", "name_zh": "熊市看跌价差"} if source == "optionlist" else {},
                entry_status=True if source == "optionreg_status" else None,
            )
            for source in ("optionlist", "optionlib", "optionreg_status")
        )
        candidates, rejected = build_candidates(
            {"proposals": [{
                "product_id": "3.3", "product_name": "熊市看跌价差", "underlyings": ["000905.SH"],
                "reason": "结构说明。", "suitable_for": ["看跌"], "not_suitable_for": ["看涨"],
                "main_risks": ["上涨"], "library_status": "ready",
                "evidence_ref_ids": [item.evidence_id for item in evidence],
            }]},
            {"reviews": [{"product_id": "3.3", "hard_reject": False}]}, evidence=evidence, run_id="recommend_1",
            confirmed_constraints={"market_view": "上涨+波动率上升"},
        )

        self.assertEqual(candidates, ())
        self.assertIn("方向", rejected[0]["reason"])

    def test_bear_call_spread_is_not_rejected_for_a_bearish_view(self) -> None:
        evidence = tuple(
            EvidenceRef(
                evidence_id=f"ev_{source}", product_id="3.4", catalog_version="v1.0", source=source,
                section="3.4", material_status="ready", excerpt_hash=hashlib.sha256(source.encode()).hexdigest(),
                excerpt=source,
                identity={"product_id": "3.4", "name_zh": "熊市看涨价差"} if source == "optionlist" else {},
                entry_status=True if source == "optionreg_status" else None,
            )
            for source in ("optionlist", "optionlib", "optionreg_status")
        )
        candidates, rejected = build_candidates(
            {"proposals": [{
                "product_id": "3.4", "product_name": "熊市看跌价差", "underlyings": ["000905.SH"],
                "reason": "符合偏空判断。", "suitable_for": ["看跌"], "not_suitable_for": ["看涨"],
                "main_risks": ["上涨"], "library_status": "ready",
                "evidence_ref_ids": [item.evidence_id for item in evidence],
            }]},
            {"reviews": [{"product_id": "3.4", "hard_reject": False}]}, evidence=evidence, run_id="recommend_1",
            confirmed_constraints={"market_view": "看跌"},
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(rejected, ())
        self.assertEqual(candidates[0].product_name, "熊市看涨价差")

    def test_parallel_delivery_request_means_both_deliveries(self) -> None:
        constraints = merge_confirmed_constraints({}, ["研究简报和完整研究报告，默认HTML即可。"])

        self.assertEqual(constraints["output_type"], "both")

    def test_delivery_negation_is_order_independent(self) -> None:
        only_report = merge_confirmed_constraints({}, ["完整研究报告，不要简报。"])
        only_card = merge_confirmed_constraints({}, ["研究简报，不要完整研究报告。"])

        self.assertEqual(only_report["output_type"], "report")
        self.assertEqual(only_card["output_type"], "card")

    def test_public_projection_redacts_internal_text_in_model_explanations(self) -> None:
        item = RecommendationCandidate(
            candidate_id="candidate_secret", product_id="2.1", underlyings=("000905.SH",), rank=1,
            reason="内部evidence_id=ev_secret，run_id=run_secret，analysis_case_id=case_secret。",
            suitable_for=("task_id=task_secret",), not_suitable_for=("candidate_id=candidate_secret",),
            main_risks=("审计audit_trail仅供内部使用",), library_status="ready",
        )
        recommendation = RecommendationSet(
            schema="optionhelper.recommendation-set/v1", task_id="task_1", run_id="run_1",
            analysis_case_id="case_1", catalog_version="v1.0", route="recommendation",
            workflow_mode="single_agent", status="candidate_ready", primary_candidate_id=item.candidate_id,
            candidates=(item,),
        )

        rendered = str(recommendation.to_public_dict()).lower()
        for forbidden in ("evidence_id", "ev_secret", "run_id", "run_secret", "analysis_case_id", "case_secret",
                          "task_id", "task_secret", "candidate_id", "candidate_secret", "audit_trail"):
            self.assertNotIn(forbidden, rendered)

    def test_public_projection_redacts_internal_product_name(self) -> None:
        item = RecommendationCandidate(
            candidate_id="candidate_1", product_id="2.1", underlyings=("000905.SH",), rank=1,
            reason="适配。", suitable_for=("看涨",), not_suitable_for=("保本",), main_risks=("下跌",),
            library_status="ready", product_name="内部run_id=run_secret",
        )
        recommendation = RecommendationSet(
            schema="optionhelper.recommendation-set/v1", task_id="task_1", run_id="run_1",
            analysis_case_id="case_1", catalog_version="v1.0", route="recommendation",
            workflow_mode="single_agent", status="candidate_ready", primary_candidate_id=item.candidate_id,
            candidates=(item,),
        )

        self.assertEqual(recommendation.to_public_dict()["推荐结果"][0]["产品名称"], "2.1")

    def test_public_projection_only_uses_fixed_term_sources(self) -> None:
        item = RecommendationCandidate(
            candidate_id="candidate_1", product_id="2.1", underlyings=("000905.SH",), rank=1,
            reason="适配。", suitable_for=("看涨",), not_suitable_for=("保本",), main_risks=("下跌",),
            library_status="ready", key_terms=({"label": "期限", "value": "3个月", "source": "internal_override"},),
        )
        recommendation = RecommendationSet(
            schema="optionhelper.recommendation-set/v1", task_id="task_1", run_id="run_1",
            analysis_case_id="case_1", catalog_version="v1.0", route="recommendation",
            workflow_mode="single_agent", status="candidate_ready", primary_candidate_id=item.candidate_id,
            candidates=(item,),
        )

        self.assertEqual(recommendation.to_public_dict()["推荐结果"][0]["拟采用条款"][0]["source"], "拟采用参数")

    def test_controlled_optionlist_name_prevents_research_rename_direction_bypass(self) -> None:
        evidence = tuple(
            EvidenceRef(
                evidence_id=f"ev_{source}", product_id="3.3", catalog_version="v1.0", source=source,
                section="3.3", material_status="ready", excerpt_hash=hashlib.sha256(source.encode()).hexdigest(),
                excerpt=source,
                identity={"product_id": "3.3", "name_zh": "熊市看跌价差"} if source == "optionlist" else {},
                entry_status=True if source == "optionreg_status" else None,
            )
            for source in ("optionlist", "optionlib", "optionreg_status")
        )
        candidates, rejected = build_candidates(
            {"proposals": [{
                "product_id": "3.3", "product_name": "看涨期权", "underlyings": ["000905.SH"],
                "reason": "x", "suitable_for": ["x"], "not_suitable_for": ["x"], "main_risks": ["x"],
                "library_status": "ready", "evidence_ref_ids": [item.evidence_id for item in evidence],
            }]},
            {"reviews": [{"product_id": "3.3", "hard_reject": False}]}, evidence=evidence, run_id="recommend_1",
            confirmed_constraints={"market_view": "上涨"},
        )

        self.assertEqual(candidates, ())
        self.assertIn("方向", rejected[0]["reason"])

    def test_missing_controlled_name_does_not_use_research_name_for_direction_rejection(self) -> None:
        evidence = tuple(
            EvidenceRef(
                evidence_id=f"ev_{source}", product_id="3.3", catalog_version="v1.0", source=source,
                section="3.3", material_status="ready", excerpt_hash=hashlib.sha256(source.encode()).hexdigest(),
                excerpt=source, identity={"product_id": "3.3"} if source == "optionlist" else {},
                entry_status=True if source == "optionreg_status" else None,
            )
            for source in ("optionlist", "optionlib", "optionreg_status")
        )
        candidates, rejected = build_candidates(
            {"proposals": [{
                "product_id": "3.3", "product_name": "熊市看跌价差", "underlyings": ["000905.SH"],
                "reason": "x", "suitable_for": ["x"], "not_suitable_for": ["x"], "main_risks": ["x"],
                "library_status": "ready", "evidence_ref_ids": [item.evidence_id for item in evidence],
            }]},
            {"reviews": [{"product_id": "3.3", "hard_reject": False}]}, evidence=evidence, run_id="recommend_1",
            confirmed_constraints={"market_view": "上涨"},
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(rejected, ())


if __name__ == "__main__":
    unittest.main()
