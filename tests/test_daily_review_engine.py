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

from daily_review_engine import DailyReviewEngine  # noqa: E402


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="较早阶段已经完成范围确认，正在实现。"))]
        )


class DailyReviewEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_keeps_full_text_and_work_uses_summary_plus_tail(self):
        engine = DailyReviewEngine(
            {
                "daily_review": {
                    "model": "claude-test",
                    "base_url": "https://relay.example",
                    "api_key": "test-key",
                    "work_tail_turns": 10,
                }
            },
            state_store=SimpleNamespace(),
        )
        completions = FakeCompletions()
        engine.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        persona = {
            "name": "言之",
            "user_name": "小羊",
            "base_prompt": "你是言之。",
            "purpose": "你和小羊有持续关系。",
            "prompt_modules": [{"name": "关系", "content": "认真记得彼此。", "enabled_by_default": True}],
        }
        turns = [
            {
                "session_id": "chat-1", "mode": "chat", "session_title": "闲聊",
                "user_text": "闲聊最早原文", "assistant_text": "闲聊回复",
            },
            *[
                {
                    "session_id": "work-1", "mode": "work", "session_title": "工作",
                    "user_text": f"工作问题{i}", "assistant_text": f"工作回答{i}",
                }
                for i in range(12)
            ],
        ]

        material, session_ids = await engine._materials(turns, persona)

        self.assertEqual(session_ids, ["chat-1", "work-1"])
        self.assertIn("闲聊最早原文", material)
        self.assertIn("较早对话脉络摘要", material)
        self.assertIn("工作问题11", material)
        self.assertNotIn("工作问题0\n", material)
        self.assertEqual(len(completions.calls), 1)
        self.assertIn("你是言之。", completions.calls[0]["messages"][0]["content"])
        self.assertIn("认真记得彼此。", completions.calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
