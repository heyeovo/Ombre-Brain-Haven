import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from bark_notifications import BarkNotificationStore, BarkNotificationWorker
from gateway_state import GatewayStateStore


class BarkNotificationContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gateway_state.db")
        self.gateway = GatewayStateStore(self.db_path)
        self.store = BarkNotificationStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def save_ready_config(self, **overrides):
        changes = {
            "enabled": True,
            "server_url": "https://api.day.app",
            "device_key": "device-secret-1234",
            "dashboard_base_url": "https://dashboard.example.com",
            "segment_interval_ms": 1000,
            "max_segments": 8,
            **overrides,
        }
        return self.store.save_config(profile_id="default", changes=changes)

    def make_wake_schedule(self):
        schedule = self.gateway.get_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            create=True,
        )
        schedule = self.gateway.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={"bark_notification_enabled": True},
        )
        self.assertTrue(schedule["bark_notification_enabled"])

    def commit_wake(self, *, request_id="wake-1", assistant_text="第一段。\n第二段。", segments=None):
        if segments is None:
            segments = [
                {"kind": "text", "markdown": "第一段。\n"},
                {"kind": "text", "markdown": "第二段。"},
            ]
        return self.gateway.commit_conversation_turn(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            request_id=request_id,
            expected_last_round_id=0,
            user_text="",
            assistant_text=assistant_text,
            source="cc",
            turn_kind="agent_wake",
            lane_id="subscription",
            raw_json=json.dumps({"display_segments": {"version": 1, "segments": segments}}),
            agent_wake_update={
                "model_activity_at": "2026-09-02T00:00:00+00:00",
                "agent_wake": {"wake_id": request_id, "cause": "agent_schedule"},
            },
            created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )

    def outbox_rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM notification_outbox ORDER BY id")]
        finally:
            conn.close()

    def test_config_is_profile_scoped_masked_and_reinitialized_idempotently(self):
        public = self.save_ready_config(
            encryption_enabled=True,
            encryption_key="1234567890abcdef",
        )
        self.assertTrue(public["ready"])
        self.assertNotIn("device_key", public)
        self.assertNotIn("encryption_key", public)
        self.assertEqual(public["device_key_masked"], "devi...1234")
        self.assertEqual(public["encryption_key_masked"], "1234...cdef")
        self.assertFalse(self.store.get_public_config(profile_id="other")["has_device_key"])
        restarted = BarkNotificationStore(self.db_path)
        self.assertEqual(restarted.get_config(profile_id="default")["device_key"], "device-secret-1234")

    def test_visible_wake_enqueues_atomically_and_replay_is_idempotent(self):
        self.save_ready_config()
        self.make_wake_schedule()
        committed = self.commit_wake()
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["level"] for row in rows], ["active", "passive"])
        self.assertEqual(rows[0]["deep_link"], "https://dashboard.example.com/cc?session_id=session-1")
        replay = self.commit_wake()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.outbox_rows()), 2)
        self.assertEqual(rows[0]["turn_id"], committed["turn"]["id"])

        self.gateway.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="wake-noop", expected_last_round_id=1, user_text="", assistant_text="",
            source="cc", turn_kind="agent_wake", lane_id="subscription",
            raw_json=json.dumps({"display_segments": {"version": 1, "segments": []}}),
            agent_wake_update={"agent_wake": {"wake_id": "wake-noop"}},
        )
        self.assertEqual(len(self.outbox_rows()), 2)

    def test_segment_cap_atomic_placeholder_and_hidden_body(self):
        self.save_ready_config(max_segments=3)
        self.make_wake_schedule()
        segments = [
            {"kind": "text", "markdown": "一"},
            {"kind": "atomic", "markdown": "```py\nprint(1)\n```"},
            {"kind": "text", "markdown": "三"},
            {"kind": "text", "markdown": "四"},
        ]
        self.commit_wake(assistant_text="一\n代码\n三\n四", segments=segments)
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 3)
        self.assertNotIn("print(1)", rows[1]["body"])
        self.assertIn("还有 2 段", rows[2]["body"])

    def test_outbox_failure_rolls_back_turn(self):
        self.save_ready_config()
        self.make_wake_schedule()
        with patch.object(BarkNotificationStore, "enqueue_agent_wake_for_turn", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.commit_wake()
        self.assertFalse(
            self.gateway.get_conversation_turn_by_request_id(profile_id="default", request_id="wake-1")
        )

    def test_expired_lease_is_recovered_after_restart(self):
        self.save_ready_config()
        queued = self.store.enqueue_test(profile_id="default")
        started = datetime.now(timezone.utc) + timedelta(seconds=1)
        first = self.store.claim_next(owner="worker-a", lease_seconds=10, now=started)
        self.assertEqual(first["id"], queued["outbox_id"])
        restarted = BarkNotificationStore(self.db_path)
        recovered = restarted.claim_next(owner="worker-b", lease_seconds=10, now=started + timedelta(seconds=11))
        self.assertEqual(recovered["id"], first["id"])
        failed = restarted.finish_delivery(
            outbox_id=recovered["id"], owner="worker-b", sent=False, error="network", now=started + timedelta(seconds=11),
        )
        self.assertEqual(failed["status"], "retry")
        self.assertEqual(failed["attempt_count"], 1)


class BarkNotificationWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_sends_encrypted_payload_without_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "gateway_state.db")
            GatewayStateStore(db_path)
            store = BarkNotificationStore(db_path)
            store.save_config(
                profile_id="default",
                changes={
                    "enabled": True,
                    "server_url": "https://bark.example.com",
                    "device_key": "device-secret",
                    "dashboard_base_url": "https://dashboard.example.com",
                    "encryption_enabled": True,
                    "encryption_key": "1234567890abcdef",
                },
            )
            store.enqueue_test(profile_id="default")
            captured = {}

            def handler(request: httpx.Request) -> httpx.Response:
                captured.update(json.loads(request.content.decode("utf-8")))
                return httpx.Response(200, json={"code": 200})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                worker = BarkNotificationWorker(store, owner="worker", http_client=client)
                result = await worker.run_once()
            self.assertEqual(result["status"], "sent")
            self.assertEqual(captured["device_key"], "device-secret")
            self.assertIn("ciphertext", captured)
            self.assertIn("iv", captured)
            self.assertNotIn("body", captured)


if __name__ == "__main__":
    unittest.main()
