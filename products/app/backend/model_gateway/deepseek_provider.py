"""One generic OpenAI-compatible completion adapter for the local App.

The provider preset belongs to settings UI only.  Runtime uses one neutral
adapter plus the chosen Base URL and model name, so adding a compatible service
does not require a new credential or completion code path.
"""

from __future__ import annotations

from .step_instructions import recommender_step_instruction

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..errors import UnavailableCapabilityError, ValidationError
from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import ModelServiceSettings
from .https_transport import open_verified_https
from .request_control import ModelRequestControl

MAX_MODEL_RESPONSE_BYTES = 512 * 1024


def complete_openai_compatible_with_metadata(
    settings: ModelServiceSettings,
    secret_ref: SecretRef,
    messages: list[dict[str, str]],
    *,
    resolve_secret: Callable[[SecretRef], str],
    opener: Callable[..., Any] = urlopen,
    request_control: ModelRequestControl | None = None,
) -> dict[str, Any]:
    """Return text and authentic usage when the compatible service supplies it."""

    if request_control is not None:
        request_control.raise_if_cancelled()
    endpoint = _completion_endpoint(settings.endpoint)
    token = resolve_secret(secret_ref)
    model_name = settings.model_name.strip()
    if not model_name:
        raise ValidationError("模型名不能为空，请在设置中心明确配置。")
    payload = json.dumps(
        {"model": model_name, "messages": messages, "stream": False},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout = request_control.remaining_seconds() if request_control is not None else 45
        with open_verified_https(request, timeout=timeout, opener=opener) as response:
            # Read one byte beyond the ceiling so an upstream server cannot
            # make the local App allocate an unbounded non-streaming body.
            try:
                raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
            except TypeError:
                # Small provider doubles and older urllib-compatible adapters
                # expose ``read()`` without a size argument. Keep that test
                # and adapter contract working while retaining the bounded
                # read for real HTTP responses.
                raw = response.read()
    except HTTPError as error:
        raise model_http_error(error.code) from error
    except (URLError, TimeoutError) as error:
        raise model_network_error(timed_out=isinstance(error, TimeoutError)) from error
    if request_control is not None:
        request_control.raise_if_cancelled()
    if not isinstance(raw, bytes) or len(raw) > MAX_MODEL_RESPONSE_BYTES:
        raise ValidationError("模型服务响应超过允许上限")

    try:
        value = json.loads(raw.decode("utf-8"))
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("模型服务返回格式无可用文本") from error
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("模型服务未返回有效文本")
    return {"text": content.strip(), "usage": _provider_usage(value.get("usage"))}


def complete_openai_compatible(
    settings: ModelServiceSettings,
    secret_ref: SecretRef,
    messages: list[dict[str, str]],
    *,
    resolve_secret: Callable[[SecretRef], str],
    opener: Callable[..., Any] = urlopen,
    request_control: ModelRequestControl | None = None,
) -> str:
    """Return one non-streaming compatible completion without logging a key."""

    return str(complete_openai_compatible_with_metadata(
        settings,
        secret_ref,
        messages,
        resolve_secret=resolve_secret,
        opener=opener,
        request_control=request_control,
    )["text"])


def _provider_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {
        key: int(value[key])
        for key in allowed
        if isinstance(value.get(key), int) and not isinstance(value.get(key), bool) and int(value[key]) >= 0
    }
    return result or None


def _completion_endpoint(endpoint: str) -> str:
    value = validate_openai_base_url(endpoint).rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def validate_openai_base_url(endpoint: str) -> str:
    value = endpoint.strip()
    if not value or any(ord(character) < 33 for character in value):
        raise ValidationError("模型Base URL格式无效")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise ValidationError("模型Base URL必须是无账号、查询参数和片段的完整https地址")
    return value


def model_http_error(status_code: int) -> UnavailableCapabilityError:
    """Project one upstream status into a safe, actionable App failure.

    Provider response bodies are deliberately ignored because they can echo
    request content or credentials.  The HTTP status is sufficient for the
    user-facing recovery categories used by Settings and OptChat.
    """

    if status_code in {401, 403}:
        return UnavailableCapabilityError(
            "模型凭据",
            "请重新粘贴有效API Key并保存，然后再次测试连接。",
            failure_code="model_credential_rejected",
            stage="model",
            message="模型API Key未通过验证。",
        )
    if status_code == 402:
        return UnavailableCapabilityError(
            "模型账户额度",
            "请在模型服务商账户中补充余额或恢复可用额度后重试。",
            failure_code="model_balance_unavailable",
            stage="model",
            message="模型账户余额或可用额度不足。",
        )
    if status_code == 404:
        return UnavailableCapabilityError(
            "模型服务配置",
            "请核对Base URL和模型ID，或先重新获取模型目录。",
            failure_code="model_not_found",
            stage="model",
            message="当前模型或API地址不可用。",
        )
    if status_code == 429:
        return UnavailableCapabilityError(
            "模型服务限流",
            "请等待请求额度或并发恢复后重试。",
            failure_code="model_rate_limited",
            stage="model",
            message="模型服务当前请求过多或额度已受限。",
        )
    if status_code in {400, 422}:
        return UnavailableCapabilityError(
            "模型请求",
            "请核对所选模型及其接口兼容性后重试。",
            failure_code="model_request_rejected",
            stage="model",
            message="模型服务拒绝了当前请求。",
        )
    if 500 <= status_code <= 599:
        return UnavailableCapabilityError(
            "模型服务上游",
            "请稍后重试；若持续失败，请检查服务商运行状态。",
            failure_code="model_upstream_unavailable",
            stage="model",
            message="模型服务上游暂时不可用。",
        )
    return UnavailableCapabilityError(
        "模型服务上游",
        "请检查模型服务商状态、Base URL和账户权限后重试。",
        failure_code="model_http_failure",
        stage="model",
        message="模型服务请求未完成。",
    )


def model_network_error(*, timed_out: bool = False) -> UnavailableCapabilityError:
    """Return a public network failure without reflecting socket details."""

    return UnavailableCapabilityError(
        "模型服务网络",
        "请检查当前设备网络、代理和Base URL后重试。",
        failure_code="model_request_timeout" if timed_out else "model_network_unavailable",
        stage="model",
        message="模型服务连接超时。" if timed_out else "无法连接模型服务。",
    )


def discover_openai_models(
    endpoint: str,
    token: str | None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, str]]:
    """Read a bounded OpenAI-compatible ``GET /models`` response.

    The caller may supply a one-shot form key.  It is used only to build this
    request and is never persisted or included in an error message.
    """

    base = validate_openai_base_url(endpoint).rstrip("/")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    request = Request(f"{base}/models", headers=headers, method="GET")
    try:
        with open_verified_https(request, timeout=20, opener=opener) as response:
            raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise model_http_error(error.code) from error
    except (URLError, TimeoutError) as error:
        raise model_network_error(timed_out=isinstance(error, TimeoutError)) from error
    if not isinstance(raw, bytes) or len(raw) > MAX_MODEL_RESPONSE_BYTES:
        raise ValidationError("模型目录响应超过允许上限")
    try:
        entries = json.loads(raw.decode("utf-8")).get("data", [])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("模型目录返回格式无效") from error
    if not isinstance(entries, list):
        raise ValidationError("模型目录未返回data数组")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
            continue
        seen.add(model_id)
        name = entry.get("name") or entry.get("display_name") or model_id
        found.append({"model_id": model_id, "display_name": str(name)})
    return found


# Compatibility import for existing callers while the App source migrates to
# the neutral name.  It is an alias, not a separate vendor-specific path.
complete = complete_openai_compatible


def decide_openai_compatible(
    settings: ModelServiceSettings,
    secret_ref: SecretRef,
    context: Mapping[str, Any],
    *,
    resolve_secret: Callable[[SecretRef], str],
    opener: Callable[..., Any] = urlopen,
    request_control: ModelRequestControl | None = None,
) -> dict[str, Any]:
    """Return one normalized Agent decision from an OpenAI-compatible model.

    Compatible providers often return a semantically correct JSON object with
    ``result`` or a fenced JSON block.  This boundary accepts only those narrow
    transport variations and normalizes them to the App's strict protocol;
    AgentDecision remains the final schema and safety validator.
    """

    recommender_step = context.get("operation") == "recommender_fixed_step"
    instruction = recommender_step_instruction() if recommender_step else (
        "你是OptionHelper的受控任务路由器。只返回一个JSON对象，不要Markdown、解释或推理。"
        "必须严格使用以下三种之一："
        '{"action":"final","text":"回复文本","fact_refs":[]};'
        '{"action":"ask_user","question":"需要补充的信息"};'
        '{"action":"call_tool","tool":"工具目录中的完整名称","arguments":{}}。'
        "不得添加其他字段，不得编造金融数字，不得输出凭据。"
        "普通问候直接使用final；需要金融计算时只能选择tool_catalog中存在的工具。"
        "final和ask_user的文字不得出现工具名、运行引用或内部字段名。"
        "推荐结果需要确认时，不生成独立审批动作；Host会根据Recommender状态直接生成确认问题。"
        "对话上下文中已确认的标的、方向、期限、风险约束和交付偏好必须复用，不得重复询问。"
        "不得要求用户提供内部文件、文件路径、运行编号或字段名；缺少研究条件时，只询问会改变下一步执行的必要条件，并将相关问题合并为一次自然提问。"
        "能够采用明确保守假设时先给阶段性判断，不要为了默认值新增一轮确认。"
        "当前任务已有可用正式分析结果时，用户说简报、简单报告或研究简报，调用reporter.run并传arguments:{\"kind\":\"card\"}；"
        "当前任务已有可用正式分析结果时，用户说报告、详细报告、深度报告、完整报告或完整研究报告，调用reporter.run并传arguments:{\"kind\":\"report\"}。"
        "当前任务已有可用正式分析结果时，用户说参考报价、报价表或quote，调用reporter.run并传arguments:{\"kind\":\"quote\"}。"
        "若用户同时提出新的标的、市场观点、期限或条款并要求Card、Report或Quote，先调用recommender.run形成候选和新的合同版本；用户确认合同后，由正常计算流程自动生成所要求交付，不能把缺少历史运行结果当成Quote不可用。"
        "用户明确要求横向对比，或上下文中存在至少两个需要并列比较的已验证候选时，使用card或report的comparison模式生成多结构研究简报或多结构完整研究报告；不得拆成多份单结构文件，也不得改成Quote。"
        "recommender.run只生成或确认候选，返回后必须由主Agent决定下一步；已确认候选需要计算或交付时调用recommendation_delivery.run，Recommender本身不生成报告。"
        "正式交付首次生成时，由Reporter冻结已验证结果，再由Designer按固定模板生成；不得自行编写HTML、PDF、Python或图表。"
        "同一任务已有正式交付后，用户补要研究简报、完整研究报告、参考报价、多结构研究简报、多结构完整研究报告或PDF时，优先复用匹配的冻结事实集重新渲染；只有参考报价重新组合快照或用户修改合同时才形成新的交付事实集，不得仅为切换呈现形式重跑模型、取数、定价或回测。"
        "参考报价首次请求可随推荐和收益结构计算直接生成；后续可从已保存合同版本中选择主结构、备选结构或不同参数版本组合，不得手写报价表。"
        "完整研究报告的七个固定章节为核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示；HTML宽屏使用左侧章节目录。"
        "普通研究对话不主动展示JSON、工具名、内部字段、文件路径或环境名称；只有用户明确要求JSON或进行系统集成时才展示原始JSON。"
        "确需询问交付形式时，只说明研究简报和完整研究报告的专业定位，不在选项中解释格式、目录或图表删减规则。"
        "请求交付时只向reporter.run提供kind、format、title或delivery_mode，由App选择受控来源；"
        "若当前缺少可用分析结果，应使用ask_user自然询问标的、产品结构、期限或所需分析，不得暴露内部术语、文件或实现细节。"
    )
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), default=str)},
    ]
    if recommender_step:
        from .openai_compatible_stream import stream_openai_compatible

        parts = []
        for event in stream_openai_compatible(
            settings, secret_ref, messages, resolve_secret=resolve_secret,
            opener=opener, request_control=request_control,
        ):
            if request_control is not None:
                request_control.refresh_deadline(90.0)
            if event.get("type") == "text_delta":
                parts.append(str(event.get("delta", "")))
        raw = "".join(parts)
    else:
        raw = complete_openai_compatible(
            settings, secret_ref, messages, resolve_secret=resolve_secret,
            opener=opener, request_control=request_control,
        )
    value = _json_object(raw)
    if recommender_step:
        if set(value) != {"action", "result"} or value.get("action") != "final" or not isinstance(value.get("result"), Mapping):
            raise ValidationError("Recommender步骤必须返回{action:'final',result:{...}}")
        return {"action": "final", "result": dict(value["result"])}
    action = str(value.get("action", "")).strip()
    if action == "final" and "text" not in value:
        alias = value.get("result", value.get("message", value.get("content")))
        if isinstance(alias, str) and set(value).issubset({"action", "result", "message", "content", "fact_refs"}):
            value = {"action": "final", "text": alias, "fact_refs": value.get("fact_refs", [])}
    elif action == "ask_user" and "question" not in value:
        alias = value.get("text", value.get("message"))
        if isinstance(alias, str) and set(value).issubset({"action", "text", "message"}):
            value = {"action": "ask_user", "question": alias}
    return value



def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValidationError("模型未返回有效的结构化JSON决策") from error
    if not isinstance(value, dict):
        raise ValidationError("模型决策必须为JSON对象")
    return value
