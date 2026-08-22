import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ImportOnlyAsyncClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


sys.modules.setdefault(
    "httpx",
    SimpleNamespace(AsyncClient=ImportOnlyAsyncClient, HTTPError=Exception),
)

from daily_review_engine import DAILY_REVIEW_HARD_CONSTRAINTS, DailyReviewEngine  # noqa: E402


class DailyReviewEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_product_prompt_is_layered_inside_hard_constraints(self):
        class Store:
            def list_daily_reviews(self, **kwargs):
                return []

            def list_daily_review_turns(self, **kwargs):
                return [{
                    "session_id": "chat-1", "mode": "chat", "session_title": "闲聊",
                    "user_text": "今天散步了", "assistant_text": "风很舒服",
                }]

            def get_cc_persona(self, persona_id):
                return {"id": persona_id, "base_prompt": "协作者基础提示", "prompt_modules": []}

            def upsert_daily_review(self, **kwargs):
                return kwargs

        engine = DailyReviewEngine(
            {"daily_review": {"model": "m", "base_url": "https://relay", "api_key": "k"}},
            Store(),
            prompt_resolver=lambda name: "用户日回顾偏好" if name == "daily_review" else "",
        )
        engine._create_message = AsyncMock(return_value="今天一起散了步。")
        await engine.generate(profile_id="default", persona_id="ombre", review_date="2026-08-08")
        call = engine._create_message.await_args.kwargs
        self.assertIn("协作者基础提示", call["system"])
        self.assertIn(DAILY_REVIEW_HARD_CONSTRAINTS, call["system"])
        self.assertIn("用户日回顾偏好", call["user"])
        self.assertNotIn("用户日回顾偏好", call["system"])

    def test_anthropic_content_reads_text_blocks(self):
        self.assertEqual(
            DailyReviewEngine._anthropic_content({
                "content": [
                    {"type": "thinking", "thinking": "内部思考"},
                    {"type": "text", "text": "日回顾正文"},
                ]
            }),
            "日回顾正文",
        )

    async def test_create_message_uses_anthropic_v1_messages(self):
        engine = DailyReviewEngine({"daily_review": {
            "model": "claude-test", "base_url": "https://relay.example", "api_key": "test-key",
        }}, SimpleNamespace())

        class FakeResponse:
            is_success = True
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"content": [{"type": "text", "text": "生成结果"}]}

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        client = FakeClient()
        with mock.patch("daily_review_engine.httpx.AsyncClient", return_value=client):
            result = await engine._create_message(
                system="完整身份提示", user="日回顾材料", max_tokens=900, temperature=0.5,
            )
        self.assertEqual(result, "生成结果")
        url, request = client.calls[0]
        self.assertEqual(url, "https://relay.example/v1/messages")
        self.assertEqual(request["json"]["system"], "完整身份提示")
        self.assertEqual(request["json"]["messages"], [{"role": "user", "content": "日回顾材料"}])
        self.assertFalse(request["json"]["stream"])
        self.assertEqual(request["headers"]["x-api-key"], "test-key")

    def test_continuity_reference_uses_only_two_exact_previous_calendar_days(self):
        class Store:
            @staticmethod
            def list_daily_reviews(**kwargs):
                return [
                    {"review_date": "2026-08-08", "content": "当天旧稿，不应读取"},
                    {"review_date": "2026-08-07", "content": "前一天"},
                    {"review_date": "2026-08-06", "content": "前两天"},
                    {"review_date": "2026-08-05", "content": "更早内容，不应补位"},
                ]

        engine = DailyReviewEngine({}, Store())
        reference = engine._continuity_reference(
            profile_id="default", persona_id="ombre", target=date(2026, 8, 8),
        )
        self.assertLess(reference.index("2026-08-06"), reference.index("2026-08-07"))
        self.assertIn("前两天", reference)
        self.assertIn("前一天", reference)
        self.assertNotIn("当天旧稿", reference)
        self.assertNotIn("更早内容", reference)

    async def test_within_budget_keeps_all_work_text_and_only_calls_final_review_once(self):
        class Store:
            @staticmethod
            def list_daily_reviews(**kwargs):
                return []

            @staticmethod
            def list_daily_review_turns(**kwargs):
                return [
                    {
                        "session_id": "work-1", "mode": "work", "session_title": "工作",
                        "user_text": f"工作问题{i}", "assistant_text": f"工作回答{i}",
                    }
                    for i in range(12)
                ]

            @staticmethod
            def get_cc_persona(persona_id):
                return {"id": persona_id, "name": "言之", "user_name": "小羊", "base_prompt": "你是言之。"}

            @staticmethod
            def upsert_daily_review(**kwargs):
                return kwargs

        engine = DailyReviewEngine(
            {
                "daily_review": {
                    "model": "claude-test",
                    "base_url": "https://relay.example",
                    "api_key": "test-key",
                    "work_tail_turns": 10,
                }
            },
            state_store=Store(),
        )
        engine._create_message = AsyncMock(return_value="今天继续推进了工作。")
        await engine.generate(profile_id="default", persona_id="ombre", review_date="2026-08-08")

        self.assertEqual(engine._create_message.await_count, 1)
        final_call = engine._create_message.await_args.kwargs
        self.assertIn("工作问题0", final_call["user"])
        self.assertIn("工作问题11", final_call["user"])
        self.assertNotIn("较早对话脉络摘要", final_call["user"])

    async def test_over_budget_summarizes_older_work_and_keeps_recent_turns_verbatim(self):
        engine = DailyReviewEngine(
            {"daily_review": {"model": "m", "base_url": "https://relay", "api_key": "k"}},
            state_store=SimpleNamespace(),
        )
        engine.max_input_chars = 2000
        engine.work_tail_turns = 3
        engine._create_message = AsyncMock(return_value="较早阶段摘要")
        persona = {
            "name": "言之",
            "user_name": "小羊",
            "base_prompt": "你是言之。",
            "purpose": "你和小羊有持续关系。",
            "prompt_modules": [{"name": "关系", "content": "认真记得彼此。", "enabled_by_default": True}],
        }
        turns = [
            {
                "session_id": "work-1", "mode": "work", "session_title": "工作",
                "user_text": f"问题{i}-" + "甲" * 180, "assistant_text": f"回答{i}-" + "乙" * 180,
            }
            for i in range(12)
        ]

        material, session_ids = await engine._materials(turns, persona)

        self.assertEqual(session_ids, ["work-1"])
        self.assertIn("较早对话脉络摘要", material)
        self.assertIn("问题11-", material)
        self.assertNotIn("问题0-", material)
        self.assertLessEqual(len(material), engine.max_input_chars)
        self.assertEqual(engine._create_message.await_count, 1)
        summary_call = engine._create_message.await_args.kwargs
        self.assertIn("你是言之。", summary_call["system"])
        self.assertIn("认真记得彼此。", summary_call["system"])

    async def test_over_budget_keeps_every_work_window_and_summarizes_each_older_section(self):
        engine = DailyReviewEngine(
            {"daily_review": {"model": "m", "base_url": "https://relay", "api_key": "k"}},
            state_store=SimpleNamespace(),
        )
        engine.max_input_chars = 4000
        engine.work_tail_turns = 2
        engine._create_message = AsyncMock(side_effect=["窗口一摘要", "窗口二摘要"])
        persona = {"name": "言之", "user_name": "小羊", "base_prompt": "你是言之。"}
        turns = [
            *[
                {
                    "session_id": "work-1", "mode": "work", "session_title": "项目一",
                    "user_text": f"一问{i}-" + "甲" * 220, "assistant_text": f"一答{i}-" + "乙" * 220,
                }
                for i in range(8)
            ],
            *[
                {
                    "session_id": "work-2", "mode": "work", "session_title": "项目二",
                    "user_text": f"二问{i}-" + "丙" * 220, "assistant_text": f"二答{i}-" + "丁" * 220,
                }
                for i in range(8)
            ],
        ]

        material, session_ids = await engine._materials(turns, persona)

        self.assertEqual(session_ids, ["work-1", "work-2"])
        self.assertIn("【工作窗口：项目一】", material)
        self.assertIn("【工作窗口：项目二】", material)
        self.assertIn("一问7-", material)
        self.assertIn("二问7-", material)
        self.assertIn("窗口一摘要", material)
        self.assertIn("窗口二摘要", material)
        self.assertEqual(engine._create_message.await_count, 2)
        self.assertLessEqual(len(material), engine.max_input_chars)

    async def test_manual_edit_requires_explicit_override_before_regeneration(self):
        class Store:
            def __init__(self):
                self.upsert_args = None

            def list_daily_reviews(self, **kwargs):
                return [{"review_date": "2026-08-08", "content": "手动版", "edited_by_user": True}]

            def list_daily_review_turns(self, **kwargs):
                return [{
                    "session_id": "chat-1", "mode": "chat", "session_title": "闲聊",
                    "user_text": "昨天的对话", "assistant_text": "昨天的回复",
                }]

            def get_cc_persona(self, persona_id):
                return {"id": persona_id, "name": "言之", "base_prompt": "你是言之。"}

            def upsert_daily_review(self, **kwargs):
                self.upsert_args = kwargs
                return {"review_date": kwargs["review_date"], "content": kwargs["content"], "edited_by_user": False}

        store = Store()
        engine = DailyReviewEngine({"daily_review": {
            "model": "claude-test", "base_url": "https://relay.example", "api_key": "test-key",
        }}, store)
        engine._create_message = AsyncMock(return_value="重新生成的日回顾")

        protected = await engine.generate(
            profile_id="default", persona_id="ombre", review_date="2026-08-08", force=True,
        )
        self.assertEqual(protected["status"], "protected")
        self.assertEqual(engine._create_message.await_count, 0)

        created = await engine.generate(
            profile_id="default", persona_id="ombre", review_date="2026-08-08",
            force=True, override_user_edit=True,
        )
        self.assertEqual(created["status"], "created")
        self.assertFalse(store.upsert_args["preserve_user_edit"])

    async def test_review_day_uses_four_am_to_next_four_am(self):
        class Store:
            def __init__(self):
                self.turn_args = None

            def list_daily_reviews(self, **kwargs):
                return []

            def list_daily_review_turns(self, **kwargs):
                self.turn_args = kwargs
                return [{
                    "session_id": "chat-1", "mode": "chat", "session_title": "跨夜闲聊",
                    "user_text": "凌晨还在聊", "assistant_text": "我在。",
                }]

            def get_cc_persona(self, persona_id):
                return {"id": persona_id, "base_prompt": "你是言之。"}

            def upsert_daily_review(self, **kwargs):
                return kwargs

        store = Store()
        engine = DailyReviewEngine({}, store)
        engine._create_message = AsyncMock(return_value="跨夜回顾")
        await engine.generate(profile_id="default", persona_id="ombre", review_date="2026-08-12")
        self.assertEqual(store.turn_args["start_at"].isoformat(), "2026-08-12T04:00:00+08:00")
        self.assertEqual(store.turn_args["end_at"].isoformat(), "2026-08-13T04:00:00+08:00")

    async def test_run_due_waits_until_configured_minute(self):
        engine = DailyReviewEngine({"daily_review": {"daily_hour": 4, "daily_minute": 30}}, SimpleNamespace())
        engine.generate = AsyncMock(return_value={"status": "created"})
        engine.state_store.list_cc_personas = lambda: [{"id": "ombre"}]
        early = await engine.run_due(now=datetime.fromisoformat("2026-08-13T04:29:00+08:00"))
        due = await engine.run_due(now=datetime.fromisoformat("2026-08-13T04:30:00+08:00"))
        self.assertEqual(early, [])
        self.assertEqual(due, [{"status": "created"}])
        self.assertEqual(engine.generate.await_args.kwargs["review_date"], "2026-08-12")


if __name__ == "__main__":
    unittest.main()
