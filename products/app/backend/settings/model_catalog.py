"""Built-in provider catalog for the local model settings surface.

The catalog contains only non-secret identifiers and endpoints.  A user still
chooses which models to enable and supplies the provider credential locally.
"""

from __future__ import annotations

from .settings_models import ModelCatalogEntry


TOKENHUB_ENDPOINT = "https://tokenhub.tencentmaas.com/v1"


def built_in_provider_catalog() -> dict[str, dict[str, object]]:
    """Return fresh descriptors so request handlers cannot mutate global state."""

    return {
        "tokenhub": {
            "display_name": "腾讯云TokenHub",
            "endpoint": TOKENHUB_ENDPOINT,
            "protocol": "openai-chat-completions",
            "models": (
                _model("hy3", "Hy3"),
                _model("glm-5.3", "GLM-5.3"),
                _model("glm-5.2", "GLM-5.2"),
                _model("glm-5.1", "GLM-5.1"),
                _model("glm-5v-turbo", "GLM-5V-Turbo"),
                _model("minimax-m3", "MiniMax-M3"),
                _model("kimi-k3", "Kimi-K3"),
                _model("kimi-k2.7-code", "Kimi-K2.7-Code"),
                _model("kimi-k2.6", "Kimi-K2.6"),
                _model("deepseek-v4-flash", "DeepSeek-V4-Flash"),
                _model("deepseek-v4-pro", "DeepSeek-V4-Pro"),
            ),
        },
        "deepseek": {
            "display_name": "DeepSeek",
            "endpoint": "https://api.deepseek.com/v1",
            "protocol": "openai-chat-completions",
            "models": (
                _model("deepseek-v4-flash", "DeepSeek-V4-Flash"),
                _model("deepseek-v4-pro", "DeepSeek-V4-Pro"),
            ),
        },
        "kimi": {
            "display_name": "Kimi",
            "endpoint": "https://api.moonshot.cn/v1",
            "protocol": "openai-chat-completions",
            "models": (
                _model("kimi-k2.6", "Kimi-K2.6"),
                _model("kimi-k2.5", "Kimi-K2.5"),
                _model("moonshot-v1-8k", "Moonshot-v1-8K"),
                _model("moonshot-v1-32k", "Moonshot-v1-32K"),
                _model("moonshot-v1-128k", "Moonshot-v1-128K"),
            ),
        },
        "zhipu": {
            "display_name": "智谱AI",
            "endpoint": "https://open.bigmodel.cn/api/paas/v4",
            "protocol": "openai-chat-completions",
            "models": (
                _model("glm-5.2", "GLM-5.2"),
                _model("glm-5.1", "GLM-5.1"),
                _model("glm-5-turbo", "GLM-5-Turbo"),
                _model("glm-5v-turbo", "GLM-5V-Turbo"),
            ),
        },
    }


def built_in_provider(provider_id: str) -> dict[str, object] | None:
    return built_in_provider_catalog().get(provider_id)


def _model(model_id: str, display_name: str) -> ModelCatalogEntry:
    return ModelCatalogEntry(model_id=model_id, display_name=display_name, enabled=False)
