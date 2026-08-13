"""One generic OpenAI-compatible completion adapter for the local App.

The provider preset belongs to settings UI only.  Runtime uses one neutral
adapter plus the chosen Base URL and model name, so adding a compatible service
does not require a new credential or completion code path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..errors import UnavailableCapabilityError, ValidationError
from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import ModelServiceSettings


MAX_MODEL_RESPONSE_BYTES = 512 * 1024


def complete_openai_compatible(
    settings: ModelServiceSettings,
    secret_ref: SecretRef,
    messages: list[dict[str, str]],
    *,
    resolve_secret: Callable[[SecretRef], str],
    opener: Callable[..., Any] = urlopen,
) -> str:
    """Return one non-streaming compatible completion without logging a key."""

    endpoint = _completion_endpoint(settings.endpoint)
    token = resolve_secret(secret_ref)
    model_name = settings.model_name.strip()
    if not model_name:
        raise ValidationError("模型名不能为空，请由管理员在设置中心明确配置。")
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
        with opener(request, timeout=45) as response:
            # Read one byte beyond the ceiling so an upstream server cannot
            # make the local App allocate an unbounded non-streaming body.
            raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code in {401, 403}:
            raise UnavailableCapabilityError("模型凭据", "模型服务拒绝了当前API Key，请重新粘贴并保存后再测试连接。") from error
        raise UnavailableCapabilityError("模型服务上游", f"模型服务返回HTTP {error.code}，请检查模型名称、Base URL或稍后重试。") from error
    except (URLError, TimeoutError) as error:
        raise UnavailableCapabilityError("模型服务网络", "无法连接模型服务，请检查网络和Base URL后重试。") from error
    if not isinstance(raw, bytes) or len(raw) > MAX_MODEL_RESPONSE_BYTES:
        raise ValidationError("模型服务响应超过允许上限")

    try:
        value = json.loads(raw.decode("utf-8"))
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("模型服务返回格式无可用文本") from error
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("模型服务未返回有效文本")
    return content.strip()


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
) -> dict[str, Any]:
    """Return one normalized Agent decision from an OpenAI-compatible model.

    Compatible providers often return a semantically correct JSON object with
    ``result`` or a fenced JSON block.  This boundary accepts only those narrow
    transport variations and normalizes them to the App's strict protocol;
    AgentDecision remains the final schema and safety validator.
    """

    recommender_step = context.get("operation") == "recommender_fixed_step"
    instruction = _recommender_step_instruction() if recommender_step else (
        "你是OptionHelper的受控任务路由器。只返回一个JSON对象，不要Markdown、解释或推理。"
        "必须严格使用以下四种之一："
        '{"action":"final","text":"回复文本","fact_refs":[]};'
        '{"action":"ask_user","question":"需要补充的信息"};'
        '{"action":"call_tool","tool":"工具目录中的完整名称","arguments":{}};'
        '{"action":"request_approval","message":"需要确认的操作"}。'
        "不得添加其他字段，不得编造金融数字，不得输出凭据。"
        "普通问候直接使用final；需要金融计算时只能选择tool_catalog中存在的工具。"
        "final、ask_user和request_approval的文字不得出现工具名、运行引用或内部字段名。"
        "对话上下文中已确认的标的、方向、期限、风险约束和交付偏好必须复用，不得重复询问。"
        "不得要求用户提供内部文件、文件路径、运行编号或字段名；缺少研究条件时，只询问会改变下一步执行的必要条件，并将相关问题合并为一次自然提问。"
        "能够采用明确保守假设时先给阶段性判断，不要为了默认值新增一轮确认。"
        "用户说简报、简单报告或研究简报时，必须调用reporter.run，并传arguments:{\"kind\":\"card\"}；"
        "用户说报告、详细报告、深度报告、完整报告或完整研究报告时，必须调用reporter.run，并传arguments:{\"kind\":\"report\"}。"
        "正式报告不得自行编写HTML、PDF、Python或图表；只能由Reporter整理已验证结果，再由Designer按固定模板生成。"
        "完整研究报告的八个固定章节为核心结论、研究逻辑、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示；不提供目录版。"
        "用户要求两份时，复用同一组正式结果分别生成card和report，不重复运行上游模块。"
        "普通研究对话不主动展示JSON、工具名、内部字段、文件路径或环境名称；只有用户明确要求JSON或进行系统集成时才展示原始JSON。"
        "确需询问交付形式时，只说明研究简报和完整研究报告的专业定位，不在选项中解释格式、目录或图表删减规则。"
        "请求报告时只向reporter.run提供kind、format、html_report_layout或title，由App选择受控来源；"
        "若当前缺少可用分析结果，应使用ask_user自然询问标的、产品结构、期限或所需分析，不得暴露内部术语、文件或实现细节。"
    )
    raw = complete_openai_compatible(
        settings,
        secret_ref,
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), default=str)},
        ],
        resolve_secret=resolve_secret,
        opener=opener,
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


def _recommender_step_instruction() -> str:
    return (
        "你正在执行OptionHelper的recommender_fixed_step。只返回一个JSON对象，不要Markdown、解释或推理。"
        "固定格式为{\"action\":\"final\",\"result\":{...}}，result必须且只能满足输入required_output声明的结构。"
        "Intent只能提取用户已经明确表达的事实，不能把已有confirmed_constraints列为缺失；"
        "Research只能从输入evidence选择产品并引用evidence_id；Critic只能审阅已有产品；"
        "Executor只能规划已批准候选的允许工具，绝不生成金融数值、条款、路径或运行成功。"
        "不得输出reasoning、analysis、secret、token、文件路径或内部运行引用。"
    )


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
