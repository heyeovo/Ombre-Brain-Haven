import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conversation_slice_store import (  # noqa: E402
    ConversationSliceConflictError,
    ConversationSliceStore,
)
from gateway_state import GatewayStateStore  # noqa: E402


class ConversationSliceContractsContractTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "gateway_state.db")
        self.gateway = GatewayStateStore(self.db_path)
        self.slices = ConversationSliceStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_turn(
        self,
        *,
        profile_id: str = "default",
        session_id: str = "session-1",
        round_id: int = 1,
        user_text: str = "今天去了海边。",
        assistant_text: str = "听起来你很开心。",
    ) -> None:
        self.gateway.record_conversation_turn(
            profile_id=profile_id,
            session_id=session_id,
            round_id=round_id,
            user_text=user_text,
            assistant_text=assistant_text,
            created_at=datetime(2026, 9, 12, 2, round_id, tzinfo=timezone.utc),
        )

    def snapshot(
        self,
        *,
        profile_id: str = "default",
        persona_id: str = "ombre",
        session_id: str = "session-1",
    ) -> dict:
        return self.slices.build_source_snapshot(
            profile_id=profile_id,
            persona_id=persona_id,
            session_id=session_id,
            chat_day="2026-09-12",
        )

    @staticmethod
    def one_slice(snapshot: dict, summary: str = "我今天去了海边，心情很开心。") -> list[dict]:
        return [{
            "source_message_ids": [item["message_id"] for item in snapshot["messages"]],
            "summary": summary,
            "boundary_reason": "day_end",
        }]

    def create_batch(
        self,
        *,
        snapshot: dict,
        slices: list[dict] | None = None,
        coverage: dict | None = None,
        revision: int = 1,
        profile_id: str = "default",
        persona_id: str = "ombre",
        session_id: str = "session-1",
    ) -> dict:
        return self.slices.create_candidate_batch(
            profile_id=profile_id,
            persona_id=persona_id,
            session_id=session_id,
            chat_day="2026-09-12",
            segmenter_version="segmenter-v1",
            slice_prompt_version="prompt-v1",
            slices=self.one_slice(snapshot) if slices is None else slices,
            coverage={} if coverage is None else coverage,
            reslice_revision=revision,
        )

    def test_schema_migration_is_additive_and_repeatable(self):
        self.add_turn()
        GatewayStateStore(self.db_path)
        ConversationSliceStore(self.db_path)
        ConversationSliceStore(self.db_path)
        conn = sqlite3.connect(self.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        turn_count = conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0]
        conn.close()
        self.assertTrue({
            "conversation_slice_revisions",
            "conversation_slice_batches",
            "conversation_slices",
            "conversation_slice_tasks",
            "conversation_slice_embeddings",
        }.issubset(tables))
        self.assertEqual(turn_count, 1)

    def test_source_snapshot_is_stable_and_profile_persona_isolated(self):
        self.add_turn()
        first = self.snapshot()
        second = self.snapshot()
        self.assertEqual(first["session_source_snapshot_hash"], second["session_source_snapshot_hash"])
        self.assertEqual(first["source_message_count"], 2)
        self.assertTrue(all(item["message_id"].startswith("msg_") for item in first["messages"]))
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE conversation_turns SET model = 'changed', raw_json = 'ignored'"
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            first["session_source_snapshot_hash"],
            self.snapshot()["session_source_snapshot_hash"],
        )
        with self.assertRaisesRegex(ValueError, "another persona"):
            self.snapshot(persona_id="other")
        with self.assertRaisesRegex(ValueError, "not found"):
            self.snapshot(profile_id="other-profile")

    def test_batch_and_task_retries_are_idempotent(self):
        self.add_turn()
        snapshot = self.snapshot()
        first = self.create_batch(snapshot=snapshot)
        replay = self.create_batch(snapshot=snapshot)
        self.assertEqual(first["batch_id"], replay["batch_id"])
        with self.assertRaisesRegex(ValueError, "reused with different output"):
            self.create_batch(
                snapshot=snapshot,
                slices=self.one_slice(snapshot, "我让同一个任务返回了不同正文。"),
            )
        task = self.slices.create_task(
            profile_id="default",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            trigger_type="slice_recovery",
            batch_idempotency_key=first["idempotency_key"],
            reslice_revision=1,
        )
        task_replay = self.slices.create_task(
            profile_id="default",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            trigger_type="slice_recovery",
            batch_idempotency_key=first["idempotency_key"],
            reslice_revision=1,
        )
        self.assertEqual(task["task_id"], task_replay["task_id"])
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_slice_batches").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_slices").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_slice_tasks").fetchone()[0], 1)
        coverage = conn.execute(
            "SELECT coverage_json FROM conversation_slice_batches"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            set(json.loads(coverage)["covered_message_ids"]),
            {item["message_id"] for item in snapshot["messages"]},
        )

    def test_manual_reslice_uses_cas_and_atomically_supersedes_active_batch(self):
        self.add_turn()
        snapshot = self.snapshot()
        first = self.create_batch(snapshot=snapshot)
        self.slices.activate_batch(first["batch_id"])
        with self.assertRaisesRegex(ValueError, "not allocated by CAS"):
            self.create_batch(snapshot=snapshot, revision=2)
        second_revision = self.slices.allocate_reslice_revision(
            profile_id="default",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            session_source_snapshot_hash=snapshot["session_source_snapshot_hash"],
            segmenter_version="segmenter-v1",
            slice_prompt_version="prompt-v1",
            expected_revision=1,
        )
        self.assertEqual(second_revision, 2)
        with self.assertRaises(ConversationSliceConflictError):
            self.slices.allocate_reslice_revision(
                profile_id="default",
                persona_id="ombre",
                session_id="session-1",
                chat_day="2026-09-12",
                session_source_snapshot_hash=snapshot["session_source_snapshot_hash"],
                segmenter_version="segmenter-v1",
                slice_prompt_version="prompt-v1",
                expected_revision=1,
            )
        second = self.create_batch(
            snapshot=snapshot,
            slices=self.one_slice(snapshot, "我去了海边，也确认自己很享受那段放松的时间。"),
            revision=second_revision,
        )
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        )[0]["batch_id"], first["batch_id"])
        self.slices.activate_batch(second["batch_id"])
        conn = sqlite3.connect(self.db_path)
        statuses = dict(conn.execute(
            "SELECT batch_id, status FROM conversation_slice_batches"
        ).fetchall())
        conn.close()
        self.assertEqual(statuses[first["batch_id"]], "superseded")
        self.assertEqual(statuses[second["batch_id"]], "active")

    def test_invalid_cross_scope_order_and_incomplete_coverage_are_rejected(self):
        self.add_turn()
        self.add_turn(session_id="session-2")
        snapshot = self.snapshot()
        other = self.snapshot(session_id="session-2")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.create_batch(snapshot=snapshot, slices=[{
                "source_message_ids": [other["messages"][0]["message_id"]],
                "summary": "错误串线。",
                "boundary_reason": "day_end",
            }])
        with self.assertRaisesRegex(ValueError, "continuous ordered"):
            self.create_batch(snapshot=snapshot, slices=[{
                "source_message_ids": list(reversed([
                    item["message_id"] for item in snapshot["messages"]
                ])),
                "summary": "错误顺序。",
                "boundary_reason": "day_end",
            }])
        with self.assertRaisesRegex(ValueError, "account for every"):
            self.create_batch(snapshot=snapshot, slices=[{
                "source_message_ids": [snapshot["messages"][0]["message_id"]],
                "summary": "我只覆盖了其中一条。",
                "boundary_reason": "day_end",
            }])
        with self.assertRaisesRegex(ValueError, "first-person"):
            self.create_batch(snapshot=snapshot, slices=[{
                "source_message_ids": [item["message_id"] for item in snapshot["messages"]],
                "summary": "用户今天去了海边。",
                "boundary_reason": "day_end",
            }])

    def test_zero_slice_batch_requires_reasoned_coverage_and_can_activate(self):
        self.add_turn(user_text="你好", assistant_text="你好呀")
        snapshot = self.snapshot()
        ids = [item["message_id"] for item in snapshot["messages"]]
        with self.assertRaisesRegex(ValueError, "requires a coverage reason"):
            self.create_batch(snapshot=snapshot, slices=[], coverage={})
        batch = self.create_batch(
            snapshot=snapshot,
            slices=[],
            coverage={"ignored_ranges": [{"message_ids": ids, "reason": "pure_greeting"}]},
        )
        active = self.slices.activate_batch(batch["batch_id"])
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["slice_count"], 0)

    def test_source_change_invalidates_slices_and_failed_candidate_keeps_old_active(self):
        self.add_turn()
        snapshot = self.snapshot()
        active = self.create_batch(snapshot=snapshot)
        self.slices.activate_batch(active["batch_id"])
        revision = self.slices.allocate_reslice_revision(
            profile_id="default",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            session_source_snapshot_hash=snapshot["session_source_snapshot_hash"],
            segmenter_version="segmenter-v1",
            slice_prompt_version="prompt-v1",
            expected_revision=1,
        )
        candidate = self.create_batch(snapshot=snapshot, revision=revision)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE conversation_turns SET user_text = ? WHERE profile_id = ? AND session_id = ?",
            ("原文发生变化", "default", "session-1"),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.slices.activate_batch(candidate["batch_id"])
        conn = sqlite3.connect(self.db_path)
        statuses = dict(conn.execute(
            "SELECT batch_id, status FROM conversation_slice_batches"
        ).fetchall())
        conn.close()
        self.assertEqual(statuses[active["batch_id"]], "active")
        self.assertEqual(statuses[candidate["batch_id"]], "failed")
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        ), [])
        self.assertFalse(self.slices.refresh_source_validity(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        ))

    def test_soft_delete_hides_restore_recovers_and_permanent_delete_cascades(self):
        self.add_turn()
        snapshot = self.snapshot()
        batch = self.create_batch(snapshot=snapshot)
        self.slices.activate_batch(batch["batch_id"])
        self.slices.create_task(
            profile_id="default",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            trigger_type="slice_recovery",
            batch_idempotency_key=batch["idempotency_key"],
            reslice_revision=1,
        )
        self.add_turn(profile_id="other-profile")
        other_snapshot = self.snapshot(profile_id="other-profile")
        other_batch = self.create_batch(
            snapshot=other_snapshot,
            profile_id="other-profile",
        )
        self.slices.activate_batch(other_batch["batch_id"])
        self.slices.create_task(
            profile_id="other-profile",
            persona_id="ombre",
            session_id="session-1",
            chat_day="2026-09-12",
            trigger_type="slice_recovery",
            batch_idempotency_key=other_batch["idempotency_key"],
            reslice_revision=1,
        )
        self.gateway.soft_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        ), [])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE conversation_sessions SET deleted_at = NULL WHERE profile_id = ? AND session_id = ?",
            ("default", "session-1"),
        )
        slice_id = conn.execute(
            "SELECT slice_id FROM conversation_slices WHERE profile_id = ?",
            ("default",),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO conversation_slice_embeddings
               (slice_id, embedding_version, summary_content_hash, embedding_json, created_at, updated_at)
               VALUES (?, 'embed-v1', 'summary-hash', '[0.1]', 'now', 'now')""",
            (slice_id,),
        )
        conn.commit()
        conn.close()
        self.assertEqual(len(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        )), 1)
        deleted = self.gateway.permanently_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(deleted["conversation_slice_batches"], 1)
        self.assertEqual(deleted["conversation_slices"], 1)
        self.assertEqual(deleted["conversation_slice_embeddings"], 1)
        self.assertEqual(self.slices.list_active_slices(
            profile_id="default", persona_id="ombre", session_id="session-1", chat_day="2026-09-12"
        ), [])
        conn = sqlite3.connect(self.db_path)
        for table in (
            "conversation_slice_revisions",
            "conversation_slice_batches",
            "conversation_slices",
            "conversation_slice_tasks",
        ):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(f"SELECT profile_id FROM {table}").fetchone()[0],
                "other-profile",
            )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM conversation_slice_embeddings").fetchone()[0],
            0,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
