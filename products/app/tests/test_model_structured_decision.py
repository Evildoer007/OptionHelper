from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.agent_runtime.agent_loop import AgentDecision
from backend.model_gateway.deepseek_provider import decide_openai_compatible
from backend.secrets.secret_ref import SecretRef
from backend.settings.settings_models import ModelServiceSettings


class FakeResponse:
    def __init__(self, content: str) -> None:
        self._body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


class StructuredDecisionTests(unittest.TestCase):
    def decide(self, content: str):
        captured = {}

        def opener(request, timeout):
            self.assertEqual(timeout, 45)
            captured.update(json.loads(request.data.decode()))
            return FakeResponse(content)

        value = decide_openai_compatible(
            ModelServiceSettings("openai-compatible", "https://api.example.com/v1", model_name="model-a"),
            SecretRef("keychain", "model/test"),
            {"latest_message": "你好", "tool_catalog": []},
            resolve_secret=lambda _ref: "test-key",
            opener=opener,
        )
        self.assertIn("普通问候直接使用final", captured["messages"][0]["content"])
        return value

    def test_accepts_strict_final(self):
        value = self.decide('{"action":"final","text":"你好，我可以帮你研究期权结构。","fact_refs":[]}')
        self.assertEqual(AgentDecision.from_mapping(value).text, "你好，我可以帮你研究期权结构。")

    def test_normalizes_common_result_alias_and_json_fence(self):
        value = self.decide('```json\n{"action":"final","result":"你好"}\n```')
        self.assertEqual(value, {"action": "final", "text": "你好", "fact_refs": []})
        self.assertEqual(AgentDecision.from_mapping(value).text, "你好")

    def test_recommender_step_uses_its_own_action_result_protocol(self):
        captured = {}

        def opener(request, timeout):
            captured.update(json.loads(request.data.decode()))
            return FakeResponse('{"action":"final","result":{"confirmed_constraints":{},"missing_information":[],"next_question":null,"research_queries":["看涨期权"]}}')

        value = decide_openai_compatible(
            ModelServiceSettings("openai-compatible", "https://api.example.com/v1", model_name="model-a"),
            SecretRef("keychain", "model/test"),
            {"operation": "recommender_fixed_step", "role": "SingleAgent.Intent", "input": {"required_output": {}}},
            resolve_secret=lambda _ref: "test-key",
            opener=opener,
        )

        self.assertEqual(value["action"], "final")
        self.assertIn("result", value)
        self.assertIn("recommender_fixed_step", captured["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
