"""Natural-language delivery instructions passed to OptChat's model."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.model_gateway.deepseek_provider import decide_openai_compatible
from backend.secrets.secret_ref import SecretRef
from backend.settings.settings_models import ModelServiceSettings


class _Response:
    def __init__(self, content: str) -> None:
        self._body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


class OptChatDeliveryLanguageTests(unittest.TestCase):
    def test_model_instruction_maps_user_delivery_words_without_internal_names(self) -> None:
        captured: dict = {}

        def opener(request, *, timeout):
            self.assertEqual(timeout, 45)
            captured.update(json.loads(request.data.decode("utf-8")))
            return _Response('{"action":"final","text":"已收到。","fact_refs":[]}')

        decide_openai_compatible(
            ModelServiceSettings("openai-compatible", "https://api.example.com/v1", model_name="model-a"),
            SecretRef("keychain", "model/test"),
            {"latest_message": "请生成简报", "tool_catalog": [{"name": "reporter.run"}]},
            resolve_secret=lambda _ref: "test-key",
            opener=opener,
        )
        instruction = captured["messages"][0]["content"]
        self.assertIn("研究简报", instruction)
        self.assertIn("完整研究报告", instruction)
        self.assertIn('"kind":"card"', instruction)
        self.assertIn('"kind":"report"', instruction)
        self.assertIn("不得重复询问", instruction)
        self.assertIn("不主动展示JSON", instruction)
        self.assertIn("不在选项中解释格式、目录或图表删减规则", instruction)
        self.assertNotIn("Card", instruction)
        self.assertNotIn("Report", instruction)


if __name__ == "__main__":
    unittest.main()
