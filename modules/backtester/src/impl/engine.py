"""Backtester唯一正式入口与结果封装。

入场、历史路径、逐笔账本与公共指标分别由同名职责文件拥有；本文件只编排
``ResolvedContract + BacktestConfig + HistoricalData -> BacktestResult``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime.contracts.contract_api import ResolvedContract
from runtime.contracts.contract_types import deep_thaw, semantic_hash

from ..branch_coverage import branch_coverage
from ..common_metrics import summarize_common_metrics
from ..entry_generator import BacktestInputError, BacktestUnsupportedError, ZeroValidSamplesError, entry_positions
from ..historical_data import HistoricalData
from ..metric_profile_map import MetricProfileSpec
from ..metric_profiles import metric_profile_hash, profile_for_product, specialized_metrics
from ..path_replay import aligned_history, assert_supported_schedule, contract_stop_position, entry_hv_feature, replay_path
from ..trade_ledger import HistoricalResolvedContract, TradeResult, build_trade_result, freeze_trade_contract
from .config import BacktestConfig


_ECONOMIC_CONVENTION = {
    "pnl_basis": "contract_cashflow_before_external_costs",
    "external_costs_modelled": False,
    "client_net_pnl_status": "not_modelled",
    "client_net_pnl_reason": "未建模交易费、资金成本、税费及客户特定现金流；不得将合同条款现金流损益表述为客户净损益。",
    "win_rate_numerator": "contract_cashflow_pnl_gt_zero",
    "win_rate_denominator": "valid_trade_count",
}


@dataclass(frozen=True)
class BacktestResult:
    """正式Backtester输出：公共统计、专属指标和可复算逐笔账本。"""

    contract: ResolvedContract
    config: BacktestConfig
    historical_data: HistoricalData
    metric_profile_spec: MetricProfileSpec
    trades: tuple[TradeResult, ...]
    skipped_entries: tuple[Mapping[str, str], ...]
    limitations: tuple[str, ...]
    execution_fingerprint: str
    ledger_hash: str
    metric_profile_hash: str

    @property
    def product_id(self) -> str:
        return self.contract.product_id

    @property
    def underlyings(self) -> tuple[str, ...]:
        return self.contract.underlyings

    def summary(self) -> dict[str, Any]:
        generic = summarize_common_metrics(
            self.trades,
            self.contract,
            include_annual=self.config.statistics_frequency == "year",
        )
        generic["common_metrics"]["skipped_count"] = len(self.skipped_entries)
        specialized = specialized_metrics(
            self.metric_profile_spec,
            self.trades,
            terms=self.contract.terms,
            event_summary=generic["event_summary"],
            monitor_summary=generic["monitor_summary"],
            outcome_summary=generic["outcome_summary"],
            three_outcome_summary=generic["three_outcome_summary"],
            conditional_summary=generic["conditional_summary"],
        )
        return {
            **generic["common_metrics"],
            "underlying_performance": generic["underlying_performance"],
            "event_summary": generic["event_summary"],
            "monitor_summary": generic["monitor_summary"],
            "outcome_summary": generic["outcome_summary"],
            "annual_summary": generic["annual_summary"],
            "metric_profile": {
                "profile_id": self.metric_profile_spec.profile_id,
                "display_name": self.metric_profile_spec.display_name,
                "metric_profile_hash": self.metric_profile_hash,
            },
            "specialized_metrics": specialized,
        }

    def to_dict(self) -> dict[str, Any]:
        summary = self.summary()
        data_ref = deep_thaw(self.historical_data.data_asset_ref)
        sample_definition = {
            "entry_rule": self.config.entry_rule,
            "entry_dates": list(self.config.entry_dates or ()),
            "entry_window": {"start_date": self.config.start_date, "end_date": self.config.end_date},
            "complete_tenor": self.config.complete_tenor,
            "missing_data_policy": self.config.missing_data_policy,
            "alignment_policy": self.config.alignment_policy,
            "return_denominator": self.config.return_denominator,
            "statistics_frequency": self.config.statistics_frequency,
            "contract_price_field": self.historical_data.contract_price_field,
            "contract_adjustment": self.historical_data.contract_adjustment,
            "entry_hv_price_field": self.historical_data.hv_price_field,
            "entry_hv_adjustment": self.historical_data.hv_adjustment,
            "entry_hv_window": self.config.entry_hv_window,
            "entry_hv_bins": list(self.config.entry_hv_bins or ()),
        }
        common_keys = (
            "sample_count", "skipped_count", "win_rate", "average_pnl", "median_pnl", "minimum_pnl", "maximum_pnl",
            "max_loss", "average_return", "median_return", "minimum_return", "maximum_return", "return_not_applicable_count",
            "return_distribution",
        )
        common_metrics = {key: summary[key] for key in common_keys}
        payload = {
            "product_id": self.product_id,
            "underlyings": list(self.underlyings),
            "contract_fingerprint": self.contract.contract_fingerprint,
            "backtest_config": self.config.to_dict(),
            "sample_definition": sample_definition,
            "sample_count": summary["sample_count"],
            "skipped_count": summary["skipped_count"],
            "skipped_entries": [dict(item) for item in self.skipped_entries],
            "common_metrics": common_metrics,
            "economic_convention": dict(_ECONOMIC_CONVENTION),
            "underlying_performance": summary["underlying_performance"],
            "metric_profile": summary["metric_profile"],
            "metric_profile_hash": self.metric_profile_hash,
            "specialized_metrics": summary["specialized_metrics"],
            "trade_ledger": [trade.to_dict() for trade in self.trades],
            "trade_ledger_ref": {"kind": "inline", "ledger_hash": self.ledger_hash, "trade_count": len(self.trades)},
            "ledger_hash": self.ledger_hash,
            "data_asset_ref": data_ref,
            "data_coverage": data_ref["coverage"],
            "event_summary": summary["event_summary"],
            "monitor_summary": summary["monitor_summary"],
            "outcome_summary": summary["outcome_summary"],
            "branch_coverage": branch_coverage(self.contract, self.trades),
            "annual_summary": summary["annual_summary"],
            "price_convention_evidence": {
                "contract_settlement": {"field": "close", "adjustment": "unadjusted"},
                "entry_hv": {
                    "field": self.historical_data.hv_price_field,
                    "adjustment": self.historical_data.hv_adjustment,
                    "window": self.config.entry_hv_window,
                    "bins": list(self.config.entry_hv_bins or ()),
                    "no_lookahead": True,
                },
            },
            "execution_fingerprint": self.execution_fingerprint,
            "limitations": list(self.limitations),
        }
        payload["semantic_result_hash"] = semantic_hash({
            "contract_fingerprint": payload["contract_fingerprint"],
            "ledger_hash": payload["ledger_hash"],
            "common_metrics": payload["common_metrics"],
            "economic_convention": payload["economic_convention"],
            "metric_profile": payload["metric_profile"],
            "specialized_metrics": payload["specialized_metrics"],
            "data_asset_ref": payload["data_asset_ref"],
        })
        return payload


def backtest(backtest_input: Any) -> BacktestResult:
    """Backtester唯一正式入口。"""
    from ..models import BacktestInput

    if not isinstance(backtest_input, BacktestInput):
        raise BacktestInputError("正式入口只接受BacktestInput")
    contract = backtest_input.contract
    config = backtest_input.backtest_config
    historical_data = backtest_input.historical_data
    historical_data.validate_integrity()
    profile_spec = profile_for_product(contract.product_id)
    if not profile_spec.supported:
        raise BacktestUnsupportedError(profile_spec.unsupported_reason or "metric_profile_unsupported")
    assert_supported_schedule(contract)
    history = aligned_history(historical_data, contract.underlyings, contract)
    skipped: list[dict[str, str]] = []
    trades: list[TradeResult] = []
    positions, missing_explicit = entry_positions(history.close.index, config)
    for entry_date in missing_explicit:
        _skip_or_reject(skipped, config, entry_date, "entry_date_not_in_aligned_trading_calendar")
    for ordinal, start in enumerate(positions, 1):
        entry_date = _date_text(history.close.index[start])
        entry_features = entry_hv_feature(
            historical_data, contract.underlyings, history.close.index[start],
            window=config.entry_hv_window, bins=config.entry_hv_bins,
        )
        stop, reason = contract_stop_position(history.close.index, start, contract, config.complete_tenor)
        if stop is None:
            _skip_or_reject(skipped, config, entry_date, reason or "insufficient_tenor")
            continue
        dates = history.close.index[start : stop + 1]
        values = history.close.iloc[start : stop + 1].to_numpy(dtype=float)
        if len(values) < 2:
            _skip_or_reject(skipped, config, entry_date, "insufficient_path")
            continue
        entry_spots = {asset: float(values[0, index]) for index, asset in enumerate(contract.underlyings)}
        fields = {name: matrix.iloc[start : stop + 1].to_numpy(dtype=float) for name, matrix in history.price_fields.items()}
        try:
            evaluation_contract, _ = freeze_trade_contract(contract, entry_date=entry_date, trading_dates=dates, entry_spots=entry_spots)
            replay = replay_path(evaluation_contract, dates=dates, values=values, price_fields=fields)
            final_contract, historical_contract = freeze_trade_contract(
                contract,
                entry_date=entry_date,
                trading_dates=replay.dates,
                entry_spots=entry_spots,
            )
            if not replay.outcome.cashflows:
                _skip_or_reject(skipped, config, entry_date, "shared_interpreter_returned_no_cashflows")
                continue
            trades.append(build_trade_result(
                trade_id=f"{contract.product_id}-{ordinal:05d}",
                contract=final_contract,
                historical_contract=historical_contract,
                replay=replay,
                historical_data=historical_data,
                config=config,
                entry_features=entry_features,
            ))
        except (BacktestInputError, ValueError) as error:
            if config.missing_data_policy == "reject":
                raise BacktestInputError(str(error)) from error
            skipped.append({"entry_date": entry_date, "reason": str(error)})
    if not trades:
        raise ZeroValidSamplesError("zero_valid_samples：没有满足完整期限、观察日与数据要求的有效入场样本")
    ledger_hash = semantic_hash([trade.to_dict() for trade in trades])
    profile_hash = metric_profile_hash(profile_spec)
    execution_fingerprint = semantic_hash({
        "contract_fingerprint": contract.contract_fingerprint,
        "backtest_config": config.to_dict(),
        "data_asset_id": historical_data.data_asset_ref["data_asset_id"],
        "data_content_hash": historical_data.data_asset_ref["content_hash"],
        "data_asset_ref_fingerprint": historical_data.data_asset_ref_fingerprint,
        "metric_profile_id": profile_spec.profile_id,
        "metric_profile_hash": profile_hash,
        "contract_price_field": historical_data.contract_price_field,
        "entry_hv_price_field": historical_data.hv_price_field,
    })
    limitations = tuple(dict.fromkeys((
        *historical_data.limitations,
        "no_nav_curve_is_generated",
    )))
    return BacktestResult(
        contract=contract,
        config=config,
        historical_data=historical_data,
        metric_profile_spec=profile_spec,
        trades=tuple(trades),
        skipped_entries=tuple(skipped),
        limitations=limitations,
        execution_fingerprint=execution_fingerprint,
        ledger_hash=ledger_hash,
        metric_profile_hash=profile_hash,
    )


def _date_text(value: Any) -> str:
    return str(value)[:10]


def _skip_or_reject(skipped: list[dict[str, str]], config: BacktestConfig, entry_date: str, reason: str) -> None:
    if config.missing_data_policy == "reject":
        raise BacktestInputError(f"{entry_date}：{reason}")
    skipped.append({"entry_date": entry_date, "reason": reason})


__all__ = (
    "BacktestInputError", "BacktestResult", "BacktestUnsupportedError", "HistoricalResolvedContract",
    "TradeResult", "ZeroValidSamplesError", "backtest",
)
