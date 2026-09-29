"""Desk research reads verified assets; it never updates pricing or task terms."""
from __future__ import annotations

from dataclasses import asdict, replace
from io import BytesIO
from typing import Any, Callable, Mapping

import pandas as pd

from .config import DataFetcherConfig
from .dashboard import SECTIONS, build_dashboard, panel
from .models import DataRequest, deep_thaw
from .request_validator import RequestValidationError
from .calendar_service import calendar_evidence_for_history, CalendarValidationError
from .providers.base import ProviderError
from .providers.ifind_dashboard import IFindDashboardProvider
from .providers.ifind_http import IFindDownloadError


def read_dashboard(request: Mapping[str, Any], caller, *, secret_ref=None, secret_port=None,
                   selected_provider: str = "ifind_http",
                   cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    # The App alone supplies identity. Browser inputs only select an existing asset.
    from .service import read_data_asset, _is_host_secret_ref

    unknown = set(request) - {"task_id", "data_asset_id", "asset_id", "sections"}
    if unknown:
        raise RequestValidationError("Dashboard请求包含未支持的字段")
    reference_id = request.get("data_asset_id")
    selected = request.get("asset_id")
    sections = request.get("sections", ["prices"])
    if not isinstance(reference_id, str) or not reference_id:
        raise RequestValidationError("Dashboard需要已获取的行情资产")
    if selected is not None and (not isinstance(selected, str) or not selected):
        raise RequestValidationError("Dashboard标的无效")
    if not isinstance(sections, list) or not sections or len(sections) > len(SECTIONS) or any(not isinstance(s, str) or s not in SECTIONS for s in sections):
        raise RequestValidationError("Dashboard板块无效")
    def check():
        if cancelled and cancelled():
            raise InterruptedError("Dashboard请求已停止")
    check()
    reference, content = read_data_asset(reference_id, caller=caller)
    if reference.media_type != "text/csv" or not {"date", "asset_id", "close"}.issubset(reference.normalized_fields):
        raise RequestValidationError("Dashboard只支持日频行情资产")
    metadata = deep_thaw(asdict(reference))
    if metadata.get("price_convention", {}).get("frequency") != "1d":
        raise RequestValidationError("Dashboard只支持日频行情资产")
    frame = pd.read_csv(BytesIO(content), dtype={"date": str, "asset_id": str})
    base = DataFetcherConfig.from_runtime()
    sessions = ()
    calendar = metadata.get("coverage", {}).get("calendar_ref")
    if isinstance(calendar, dict) and calendar.get("data_asset_id"):
        try:
            calendar_ref, _ = read_data_asset(calendar["data_asset_id"], caller=caller)
            if calendar_ref.content_hash != calendar.get("content_hash"):
                raise CalendarValidationError("交易日历版本不一致")
            history_request = DataRequest(tuple(reference.asset_ids), str(frame.date.min()), str(frame.date.max()), tuple(reference.normalized_fields))
            evidence = calendar_evidence_for_history(calendar_ref, history_request, caller, base)
            sessions = evidence.dates_by_asset.get(selected or reference.asset_ids[0], ())
        except (FileNotFoundError, CalendarValidationError):
            # Cached history remains inspectable when its calendar has expired.
            sessions = ()
    dashboard = build_dashboard(frame, metadata, asset_id=selected, sessions=sessions)
    host_connection = _is_host_secret_ref(secret_ref) and callable(secret_port)
    token_provider = selected_provider in {"tinyshare", "tushare"} or getattr(secret_ref, "provider", "").lower() in {"tinyshare", "tushare"}
    configured = host_connection and not token_provider
    # History support does not imply valuation/NAV/futures support. Advertise
    # only sections this selected connection can actually request; do not send
    # Tushare credentials to iFinD or describe a working history feed as offline.
    enhancement_unavailable = None
    if base.offline:
        enhancement_unavailable = panel("unavailable", "当前为离线模式，仅展示已有行情。", code="offline")
    elif token_provider:
        enhancement_unavailable = panel("unavailable", "当前Tushare适配器尚未接入该增强数据接口，已有行情仍可使用。", code="provider_not_supported")
    elif not configured:
        enhancement_unavailable = panel("unavailable", "尚未配置增强数据连接，已有行情仍可使用。", code="connection_unavailable")
    if enhancement_unavailable:
        dashboard["available_sections"] = [key for key in dashboard["available_sections"] if key in {"prices", "details"}]
    config = replace(base, ifind_secret_ref=secret_ref if configured else None,
                     ifind_secret_port=secret_port if configured else None)
    provider = IFindDashboardProvider(config, cancelled=cancelled)
    prices = dashboard["sections"]["prices"]
    for section in dict.fromkeys(sections):
        check()
        if section == "prices":
            continue
        if not dashboard["as_of"]:
            result = panel("unavailable", "没有可用的行情日期，无法对齐增强数据。")
        elif enhancement_unavailable:
            result = dict(enhancement_unavailable)
        else:
            try:
                result = provider.section(section, dashboard["asset_id"], dashboard["asset_class"],
                                          prices["start_date"], dashboard["as_of"], prices=prices["series"])
            except InterruptedError:
                raise
            except (IFindDownloadError, ProviderError) as error:
                code = getattr(error, "category", None) or getattr(error, "reason_code", "provider_unavailable")
                result = panel("unavailable", "该板块取数未完成，请检查连接、数据权限后重试。", code=code)
            except (ValueError, KeyError, TypeError):
                result = panel("unavailable", "数据源返回格式不符合该板块要求。", code="invalid_response")
        dashboard["sections"][section] = result
    check()
    return {"ok": True, "module": "datafetcher", "status": "complete", "dashboard": dashboard}
