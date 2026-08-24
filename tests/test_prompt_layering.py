import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ImportOnlyAsyncOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


sys.modules.setdefault("openai", SimpleNamespace(AsyncOpenAI=ImportOnlyAsyncOpenAI))
sys.modules.setdefault("yaml", SimpleNamespace(safe_load=lambda value: {}, safe_dump=lambda *args, **kwargs: ""))

from dehydrator import Dehydrator  # noqa: E402
from journey_weekly_engine import (  # noqa: E402
    WEEKLY_JOURNEY_HARD_CONSTRAINTS,
    WEEKLY_JOURNEY_PRODUCT_PROMPT,
    WeeklyJourneyEngine,
)


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=None,
            model="test-model",
        )


class PromptLayeringTest(unittest.IsolatedAsyncioTestCase):
    def test_weekly_transition_and_evidence_truth_are_not_editable_defaults(self):
        self.assertNotIn("才允许选择 transition", WEEKLY_JOURNEY_PRODUCT_PROMPT)
        self.assertNotIn("不得编造", WEEKLY_JOURNEY_PRODUCT_PROMPT)
        self.assertIn("才允许选择 transition", WEEKLY_JOURNEY_HARD_CONSTRAINTS)
        self.assertIn("不得编造", WEEKLY_JOURNEY_HARD_CONSTRAINTS)

    async def test_analyze_override_does_not_replace_hard_json_contract(self):
        completions = FakeCompletions(json.dumps({
            "domain": ["情感"], "valence": 0.6, "arousal": 0.4,
            "tags": ["散步"], "suggested_name": "一起散步",
            "memory_subject": "event", "memory_layer": "process_event",
        }, ensure_ascii=False))
        dehydrator = Dehydrator({}, prompt_resolver=lambda name: "已保存偏好")
        dehydrator.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        await dehydrator._api_analyze("今天一起散步", product_prompt_override="只关注具体事件")

        system = completions.calls[0]["messages"][0]["content"]
        self.assertIn("只关注具体事件", system)
        self.assertIn("不可覆盖的字段、白名单与输出协议", system)
        self.assertIn('"memory_layer"', system)

    async def test_analyze_uses_dehydration_runtime_parameters(self):
        completions = FakeCompletions(json.dumps({
            "domain": ["general"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "测试",
            "memory_subject": "event", "memory_layer": "process_event",
        }))
        dehydrator = Dehydrator({"dehydration": {
            "max_tokens": 640,
            "temperature": 0.35,
            "thinking_mode": "enabled",
        }})
        dehydrator.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        await dehydrator._api_analyze("测试正文")

        call = completions.calls[0]
        self.assertEqual(call["max_tokens"], 640)
        self.assertEqual(call["temperature"], 0.35)
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})

    async def test_analyze_defaults_do_not_send_thinking(self):
        completions = FakeCompletions(json.dumps({
            "domain": ["general"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "测试",
            "memory_subject": "event", "memory_layer": "process_event",
        }))
        dehydrator = Dehydrator({})
        dehydrator.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        await dehydrator._api_analyze("测试正文")

        call = completions.calls[0]
        self.assertEqual(call["max_tokens"], 1024)
        self.assertEqual(call["temperature"], 0.1)
        self.assertNotIn("extra_body", call)

    async def test_merge_override_keeps_structure_and_identity_constraints(self):
        completions = FakeCompletions("合并后的正文")
        dehydrator = Dehydrator({"identity": {"ai_name": "言之", "user_name": "小羊"}})
        dehydrator.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        result = await dehydrator._api_merge("旧正文", "新正文", product_prompt_override="尽量简短")

        self.assertEqual(result, "合并后的正文")
        system = completions.calls[0]["messages"][0]["content"]
        self.assertIn("尽量简短", system)
        self.assertIn("不可覆盖的合并与结构约束", system)
        self.assertIn("### reflection", system)

    async def test_weekly_product_prompt_is_below_fixed_snapshot_contract(self):
        class MessageClient:
            api_key = "key"
            base_url = "https://relay"
            model = "model"

            def __init__(self):
                self.call = None

            @staticmethod
            def _persona_system(persona):
                return "协作者基础提示"

            async def _create_message(self, **kwargs):
                self.call = kwargs
                return '{"candidate_type":"no_change","rationale":["无变化"],"evidence_bucket_ids":[],"proposal":{}}'

        class DailyStore:
            @staticmethod
            def get_cc_persona(persona_id):
                return {"id": persona_id}

        class AutomationStore:
            @staticmethod
            def ensure_schedule(**kwargs):
                return kwargs

        client = MessageClient()
        engine = WeeklyJourneyEngine(
            {"weekly_journey": {}}, AutomationStore(), SimpleNamespace(), DailyStore(),
            message_client=client,
            prompt_resolver=lambda name: "关系文风使用第一人称",
        )
        snapshot = {"persona": {"id": "yan-zhi"}, "materials": []}

        result = await engine._generate_raw_candidate(snapshot)

        self.assertEqual(result["candidate_type"], "no_change")
        self.assertIn("协作者基础提示", client.call["system"])
        self.assertIn(WEEKLY_JOURNEY_HARD_CONSTRAINTS, client.call["system"])
        self.assertIn("关系文风使用第一人称", client.call["user"])
        self.assertIn("weekly_journey_input", client.call["user"])


if __name__ == "__main__":
    unittest.main()
