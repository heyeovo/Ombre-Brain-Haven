import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from conversation_slice_engine import ConversationSliceEngine
from conversation_slice_store import ConversationSliceStore
from daily_review_engine import DailyReviewEngine
from gateway_state import GatewayStateStore
from gateway import GatewayService


class FakeRouter:
    is_configured = True
    model = "claude-test"

    def __init__(self):
        self._create_message = AsyncMock()

    @staticmethod
    def choice():
        return {"engine": "api", "model": "claude-test"}


class ConversationSliceEngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "gateway_state.db")
        self.state = GatewayStateStore(self.db_path)
        self.state.save_cc_persona({"id": "ombre", "name": "Ombre", "base_prompt": "记住真实经历。"})
        self.state.record_conversation_turn(
            profile_id="default",
            session_id="session-1",
            round_id=1,
            user_text="今天去了海边。",
            assistant_text="海风让你觉得很放松。",
            created_at=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
        )
        self.router = FakeRouter()
        self.daily = DailyReviewEngine(
            {"daily_review": {"model": "claude-test", "base_url": "https://relay", "api_key": "key"}},
            self.state,
            message_client=self.router,
        )
        self.slices = ConversationSliceStore(self.db_path)
        self.engine = ConversationSliceEngine(
            {}, self.state, message_client=self.router,
            daily_review_engine=self.daily, slice_store=self.slices,
        )
        snapshot = self.slices.build_source_snapshot(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        )
        self.message_ids = [item["message_id"] for item in snapshot["messages"]]

    def tearDown(self):
        self.temp_dir.cleanup()

    def model_output(self, *, summary="我今天去了海边，海风让我觉得很放松。", daily="我今天去了海边，心情很放松。"):
        return json.dumps({
            "daily_review": {"content": daily} if daily is not None else None,
            "session_slices": [{
                "session_id": "session-1",
                "slices": [{
                    "source_start_message_id": self.message_ids[0],
                    "source_end_message_id": self.message_ids[-1],
                    "source_message_ids": self.message_ids,
                    "summary": summary,
                    "event_time_start": None,
                    "event_time_end": None,
                    "boundary_reason": "day_end",
                }],
                "ignored_source_ranges": [],
                "zero_slice_reason": "",
            }],
        }, ensure_ascii=False)

    async def test_daily_bundle_reads_once_and_saves_outputs_independently(self):
        original = self.state.list_daily_review_turns
        calls = 0

        def counted(**kwargs):
            nonlocal calls
            calls += 1
            return original(**kwargs)

        self.state.list_daily_review_turns = counted
        self.router._create_message.return_value = self.model_output()
        result = await self.engine.generate_daily_bundle(
            profile_id="default", persona_id="ombre", review_date="2026-09-12",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, 1)
        self.assertEqual(self.router._create_message.await_count, 1)
        self.assertEqual(len(self.state.list_daily_reviews(
            profile_id="default", persona_id="ombre", start_date="2026-09-12", end_date="2026-09-12",
        )), 1)
        active = self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        )
        self.assertEqual(len(active), 1)

    async def test_existing_user_review_uses_slice_only_without_overwrite(self):
        self.state.upsert_daily_review(
            profile_id="default", persona_id="ombre", review_date="2026-09-12",
            content="用户亲手修改的版本", edited_by_user=True, preserve_user_edit=False,
        )
        self.router._create_message.return_value = self.model_output(daily=None)
        result = await self.engine.generate_daily_bundle(
            profile_id="default", persona_id="ombre", review_date="2026-09-12",
        )
        self.assertEqual(result["status"], "slice_only")
        review = self.state.list_daily_reviews(
            profile_id="default", persona_id="ombre", start_date="2026-09-12", end_date="2026-09-12",
        )[0]
        self.assertEqual(review["content"], "用户亲手修改的版本")
        self.assertEqual(len(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        )), 1)

    async def test_invalid_slice_does_not_rollback_daily_review_and_queues_recovery(self):
        self.router._create_message.return_value = self.model_output(summary="没有第一人称的摘要")
        result = await self.engine.generate_daily_bundle(
            profile_id="default", persona_id="ombre", review_date="2026-09-12",
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["daily_review"]["status"], "created")
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        ), [])
        recovery = self.slices.list_tasks(
            profile_id="default", trigger_types=("slice_recovery",), statuses=("queued",),
        )
        self.assertEqual(len(recovery), 1)

    async def test_backfill_requires_displayed_estimate_and_does_not_execute(self):
        estimate = self.engine.estimate_backfill(
            profile_id="default", persona_id="ombre", session_id="session-1",
            start_date="2026-09-12", end_date="2026-09-12",
        )
        self.assertEqual(estimate["message_count"], 2)
        self.assertGreater(estimate["estimated_input_tokens"], 0)
        self.assertEqual(estimate["estimated_call_count"], 1)
        self.assertEqual(self.slices.list_tasks(profile_id="default"), [])
        with self.assertRaisesRegex(ValueError, "estimate changed"):
            self.engine.create_backfill_tasks(estimate, estimate_signature="wrong")
        tasks = self.engine.create_backfill_tasks(
            estimate, estimate_signature=estimate["estimate_signature"],
        )
        self.assertEqual(tasks[0]["status"], "queued")
        self.assertEqual(self.router._create_message.await_count, 0)

    async def test_queued_task_with_changed_source_is_not_sent_to_model(self):
        task = self.engine.enqueue_scope(
            profile_id="default", persona_id="ombre", session_id="session-1",
            chat_day="2026-09-12", trigger_type="slice_recovery",
        )
        self.state.record_conversation_turn(
            profile_id="default", session_id="session-1", round_id=2,
            user_text="后来又去吃了晚饭。", assistant_text="这是当天新增的经历。",
            created_at=datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
        )
        result = await self.engine.run_task(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["task"]["error_code"], "source_changed")
        self.assertEqual(result["replacement_task"]["status"], "queued")
        self.assertEqual(self.router._create_message.await_count, 0)

    def test_raw_exit_only_queues_missing_slice(self):
        service = object.__new__(GatewayService)
        service.state_store = self.state
        service.conversation_slice_store = self.slices
        service._enqueue_raw_exit_slice_tasks(
            profile_id="default", persona_id="ombre", session_id="session-1",
            before_state={"rolling_context": {"day_modes": {"2026-09-12": "raw"}}},
            after_state={"rolling_context": {"day_modes": {"2026-09-12": "omit"}}},
        )
        tasks = self.slices.list_tasks(profile_id="default", trigger_types=("raw_exit",))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "queued")
        self.assertEqual(self.router._create_message.await_count, 0)

    async def test_review_and_source_lookup_use_permanent_message_ids(self):
        self.router._create_message.return_value = self.model_output()
        await self.engine.generate_daily_bundle(
            profile_id="default", persona_id="ombre", review_date="2026-09-12",
        )
        item = self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        )[0]
        source = self.slices.get_slice_source(profile_id="default", slice_id=item["slice_id"])
        self.assertEqual([row["message_id"] for row in source["messages"]], self.message_ids)
        reviewed = self.slices.review_slice(
            profile_id="default", slice_id=item["slice_id"], review_status="rejected",
            review_reason="emotion", review_note="情绪写重了",
        )
        self.assertEqual(reviewed["review_status"], "rejected")
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12",
        ), [])


if __name__ == "__main__":
    unittest.main()
