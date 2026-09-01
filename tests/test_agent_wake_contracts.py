import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_wake_store import (  # noqa: E402
    AgentWakeConflictError,
    AgentWakeStore,
)


class AgentWakeStoreContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "gateway_state.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_store(self) -> AgentWakeStore:
        return AgentWakeStore(str(self.db_path))

    def add_turn_table(self, rows=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversation_turns (
                   id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL,
                   session_id TEXT NOT NULL, turn_kind TEXT NOT NULL
               )"""
        )
        conn.executemany(
            "INSERT INTO conversation_turns (id, profile_id, session_id, turn_kind) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

    @staticmethod
    def past(minutes: int = 1) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def test_partial_tables_upgrade_and_reinitialize_idempotently(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE agent_wake_schedules (legacy_id TEXT)")
        conn.execute("CREATE TABLE agent_wake_runs (legacy_id TEXT)")
        conn.commit()
        conn.close()

        AgentWakeStore(str(self.db_path))
        AgentWakeStore(str(self.db_path))

        conn = sqlite3.connect(self.db_path)
        schedule_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_wake_schedules)")
        }
        run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_wake_runs)")
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(agent_wake_schedules)")
        }
        conn.close()
        self.assertTrue(
            {
                "profile_id", "session_id", "lane_id", "due_at",
                "schedule_version", "lease_until", "background_turn_limit",
                "conversation_silence_enabled",
                "conversation_silence_check_at", "silence_source_turn_id",
                "silence_policy_version", "agent_wake_min_minutes",
                "silence_min_minutes", "silence_max_minutes",
                "retry_at",
            }.issubset(schedule_columns)
        )
        self.assertTrue(
            {"wake_id", "schedule_version", "status", "turn_id"}.issubset(run_columns)
        )
        self.assertIn("idx_agent_wake_schedule_scope", indexes)

    def test_silence_timer_participates_in_due_at_and_is_claimed_once(self):
        store = self.make_store()
        silence_due = self.past(3)
        cache_due = self.past(1)
        schedule, _ = store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            conversation_silence_enabled=True,
            cache_keepalive_deadline=cache_due,
            conversation_silence_check_at=silence_due,
            silence_source_turn_id=9,
            silence_policy_version="conversation-silence-v1",
        )
        self.assertEqual(schedule["due_at"], silence_due.isoformat(timespec="seconds"))
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        self.assertEqual(claimed["run"]["cause"], "conversation_silence")
        self.assertEqual(claimed["schedule"]["silence_source_turn_id"], 9)

    def test_disabling_silence_clears_timer_and_invalidates_old_schedule_version(self):
        store = self.make_store()
        due = self.past()
        schedule, _ = store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            conversation_silence_enabled=True,
            conversation_silence_check_at=due, silence_source_turn_id=9,
            silence_policy_version="conversation-silence-v1",
        )
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        disabled = store.update_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            expected_version=schedule["schedule_version"],
            conversation_silence_enabled=False,
        )
        self.assertFalse(disabled["conversation_silence_enabled"])
        self.assertEqual(disabled["conversation_silence_check_at"], "")
        self.assertEqual(disabled["silence_source_turn_id"], 0)
        result = store.begin_run(wake_id=claimed["run"]["wake_id"], owner="owner-a")
        self.assertEqual(result["status"], "superseded")

    def test_schema_upgrade_cancels_a_legacy_timer_defaulted_to_off(self):
        store = self.make_store()
        due = self.past()
        schedule, _ = store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            conversation_silence_enabled=True,
            conversation_silence_check_at=due, silence_source_turn_id=9,
            silence_policy_version="conversation-silence-v1",
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """UPDATE agent_wake_schedules
               SET conversation_silence_enabled = 0
               WHERE profile_id = 'profile-a' AND session_id = 'session-a'
                 AND lane_id = 'subscription'"""
        )
        conn.commit()
        conn.close()

        AgentWakeStore(str(self.db_path))
        upgraded = store.get_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription"
        )
        self.assertFalse(upgraded["conversation_silence_enabled"])
        self.assertEqual(upgraded["conversation_silence_check_at"], "")
        self.assertEqual(upgraded["silence_source_turn_id"], 0)
        self.assertEqual(upgraded["schedule_version"], schedule["schedule_version"] + 1)

    def test_schedule_crud_derives_due_at_and_uses_version_cas(self):
        store = self.make_store()
        cache_due = self.past(1)
        agent_due = self.past(2)
        schedule, created = store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            agent_wake_enabled=True,
            cache_keepalive_deadline=cache_due,
            next_agent_wake_at=agent_due,
            wake_reason="看看结果",
        )
        self.assertTrue(created)
        self.assertEqual(schedule["schedule_version"], 1)
        self.assertEqual(
            schedule["due_at"], agent_due.isoformat(timespec="seconds")
        )

        updated = store.update_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            expected_version=1,
            agent_wake_enabled=False,
        )
        self.assertEqual(updated["schedule_version"], 2)
        self.assertEqual(
            updated["due_at"], cache_due.isoformat(timespec="seconds")
        )
        with self.assertRaises(AgentWakeConflictError):
            store.update_schedule(
                profile_id="profile-a",
                session_id="session-a",
                lane_id="subscription",
                expected_version=1,
                wake_reason="过期写入",
            )
        with self.assertRaises(AgentWakeConflictError):
            store.delete_schedule(
                profile_id="profile-a",
                session_id="session-a",
                lane_id="subscription",
                expected_version=1,
            )
        self.assertTrue(
            store.delete_schedule(
                profile_id="profile-a",
                session_id="session-a",
                lane_id="subscription",
                expected_version=2,
            )
        )
        self.assertEqual(
            store.get_schedule(
                profile_id="profile-a",
                session_id="session-a",
                lane_id="subscription",
            ),
            {},
        )

    def test_profile_session_and_lane_are_isolated(self):
        store = self.make_store()
        scopes = [
            ("profile-a", "session-a", "subscription"),
            ("profile-a", "session-a", "api:one"),
            ("profile-a", "session-b", "subscription"),
            ("profile-b", "session-a", "subscription"),
        ]
        for profile_id, session_id, lane_id in scopes:
            store.create_schedule(
                profile_id=profile_id, session_id=session_id, lane_id=lane_id
            )
        self.assertEqual(
            [item["lane_id"] for item in store.list_schedules(
                profile_id="profile-a", session_id="session-a"
            )],
            ["api:one", "subscription"],
        )
        updated = store.update_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            expected_version=1,
            wake_reason="only this lane",
        )
        self.assertEqual(updated["wake_reason"], "only this lane")
        for profile_id, session_id, lane_id in scopes[1:]:
            untouched = store.get_schedule(
                profile_id=profile_id, session_id=session_id, lane_id=lane_id
            )
            self.assertEqual(untouched["wake_reason"], "")
            self.assertEqual(untouched["schedule_version"], 1)

    def test_only_one_owner_claims_the_same_due_schedule(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=self.past(),
        )
        now = datetime.now(timezone.utc)

        def claim(owner: str) -> dict:
            return self.make_store().claim_due_schedule(owner=owner, now=now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, ["owner-a", "owner-b"]))
        winners = [claim for claim in claims if claim]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["run"]["status"], "claimed")
        self.assertEqual(
            len(store.list_runs(profile_id="profile-a", session_id="session-a")), 1
        )

    def test_expired_lease_recovers_the_same_wake_id(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=self.past(),
        )
        first = store.claim_due_schedule(
            owner="owner-a", now=datetime.now(timezone.utc), lease_seconds=30
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            UPDATE agent_wake_schedules SET lease_until = ?
            WHERE profile_id = 'profile-a' AND session_id = 'session-a'
              AND lane_id = 'subscription'
            """,
            (self.past().isoformat(timespec="seconds"),),
        )
        conn.commit()
        conn.close()

        recovered = store.claim_due_schedule(
            owner="owner-b", now=datetime.now(timezone.utc), lease_seconds=30
        )
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["run"]["wake_id"], first["run"]["wake_id"])
        self.assertEqual(recovered["run"]["lease_owner"], "owner-b")
        self.assertEqual(
            len(store.list_runs(profile_id="profile-a", session_id="session-a")), 1
        )

    def test_schedule_version_change_supersedes_old_run(self):
        store = self.make_store()
        schedule, _ = store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=self.past(),
        )
        claimed = store.claim_due_schedule(
            owner="owner-a", now=datetime.now(timezone.utc)
        )
        store.update_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            expected_version=schedule["schedule_version"],
            keepalive_enabled=False,
        )
        run = store.mark_run_running(
            wake_id=claimed["run"]["wake_id"], owner="owner-a"
        )
        self.assertEqual(run["status"], "superseded")

    def test_finish_run_is_idempotent_and_releases_matching_lease(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a",
            session_id="session-a",
            lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=self.past(),
        )
        claimed = store.claim_due_schedule(
            owner="owner-a", now=datetime.now(timezone.utc)
        )
        wake_id = claimed["run"]["wake_id"]
        self.assertEqual(
            store.mark_run_running(wake_id=wake_id, owner="owner-a")["status"],
            "running",
        )
        first = store.finish_run(
            wake_id=wake_id, owner="owner-a", status="completed", turn_id=17
        )
        replay = store.finish_run(
            wake_id=wake_id, owner="owner-a", status="failed", error="ignored"
        )
        self.assertEqual(first, replay)
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(replay["turn_id"], 17)
        schedule = store.get_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription"
        )
        self.assertEqual(schedule["lease_owner"], "")

    def test_begin_run_rejects_duplicate_callback_and_enforces_rolling_limit(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            keepalive_enabled=True, cache_keepalive_deadline=self.past(), background_turn_limit=1,
        )
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        started = store.begin_run(wake_id=claimed["run"]["wake_id"], owner="owner-a")
        duplicate = store.begin_run(wake_id=claimed["run"]["wake_id"], owner="owner-a")
        self.assertEqual(started["status"], "started")
        self.assertEqual(duplicate["status"], "duplicate")
        store.finish_run(wake_id=claimed["run"]["wake_id"], owner="owner-a", status="completed")

        second = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        limited = store.begin_run(wake_id=second["run"]["wake_id"], owner="owner-a")
        self.assertEqual(limited["status"], "limit_reached")
        schedule = store.get_schedule(profile_id="profile-a", session_id="session-a", lane_id="subscription")
        self.assertIn("background_turn_limit_reached", schedule["last_error"])
        self.assertTrue(schedule["retry_at"])

    def test_background_limit_counts_all_lanes_in_the_window(self):
        store = self.make_store()
        due = self.past()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="api:one",
            keepalive_enabled=True, cache_keepalive_deadline=due, background_turn_limit=1,
        )
        first = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        store.begin_run(wake_id=first["run"]["wake_id"], owner="owner-a")
        store.finish_run(wake_id=first["run"]["wake_id"], owner="owner-a", status="completed")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE agent_wake_schedules SET due_at = '' WHERE lane_id = 'api:one'")
        conn.commit()
        conn.close()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            keepalive_enabled=True, cache_keepalive_deadline=due, background_turn_limit=1,
        )
        second = store.claim_due_schedule(owner="owner-b", now=datetime.now(timezone.utc))
        limited = store.begin_run(wake_id=second["run"]["wake_id"], owner="owner-b")
        self.assertEqual(limited["status"], "limit_reached")

    def test_inactive_lane_supersede_makes_schedule_dormant_until_next_turn(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="api:old",
            keepalive_enabled=True, cache_keepalive_deadline=self.past(),
        )
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        store.finish_run(
            wake_id=claimed["run"]["wake_id"], owner="owner-a", status="superseded",
            error="claimed_lane_is_not_active",
        )
        schedule = store.get_schedule(profile_id="profile-a", session_id="session-a", lane_id="api:old")
        self.assertEqual(schedule["due_at"], "")
        self.assertEqual(store.claim_due_schedule(owner="owner-b", now=datetime.now(timezone.utc)), {})

    def test_begin_scope_mismatch_does_not_consume_the_valid_claim(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            keepalive_enabled=True, cache_keepalive_deadline=self.past(),
        )
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        mismatch = store.begin_run(
            wake_id=claimed["run"]["wake_id"], owner="owner-a",
            expected_profile_id="profile-b", expected_session_id="session-a",
            expected_lane_id="subscription", expected_schedule_version=1,
        )
        self.assertEqual(mismatch["status"], "scope_mismatch")
        self.assertEqual(store.get_run(claimed["run"]["wake_id"])["status"], "claimed")

    def test_silence_source_is_rechecked_atomically_before_model_start(self):
        store = self.make_store()
        self.add_turn_table([
            (9, "profile-a", "session-a", "user"),
            (10, "profile-a", "session-a", "user"),
        ])
        due = self.past()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            conversation_silence_enabled=True,
            conversation_silence_check_at=due, silence_source_turn_id=9,
            silence_policy_version="conversation-silence-v1",
        )
        claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
        result = store.begin_run(wake_id=claimed["run"]["wake_id"], owner="owner-a")
        self.assertEqual(result["status"], "superseded")
        schedule = store.get_schedule(profile_id="profile-a", session_id="session-a", lane_id="subscription")
        self.assertEqual(schedule["conversation_silence_check_at"], "")

    def test_failed_runs_persist_backoff_and_pause_after_threshold(self):
        store = self.make_store()
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            keepalive_enabled=True, cache_keepalive_deadline=self.past(), background_turn_limit=48,
        )
        for attempt in range(5):
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE agent_wake_schedules SET retry_at = ''")
            conn.commit()
            conn.close()
            claimed = store.claim_due_schedule(owner="owner-a", now=datetime.now(timezone.utc))
            self.assertTrue(claimed, attempt)
            store.begin_run(wake_id=claimed["run"]["wake_id"], owner="owner-a")
            store.finish_run(
                wake_id=claimed["run"]["wake_id"], owner="owner-a",
                status="failed", error=f"failure-{attempt + 1}",
            )
        schedule = store.get_schedule(profile_id="profile-a", session_id="session-a", lane_id="subscription")
        self.assertEqual(schedule["consecutive_failures"], 5)
        self.assertEqual(store.claim_due_schedule(owner="owner-b", now=datetime.now(timezone.utc)), {})

    def test_24_hour_user_inactivity_pauses_only_keepalive(self):
        store = self.make_store()
        now = datetime.now(timezone.utc)
        future_agent = now + timedelta(hours=2)
        store.create_schedule(
            profile_id="profile-a", session_id="session-a", lane_id="subscription",
            keepalive_enabled=True, agent_wake_enabled=True,
            last_user_activity_at=now - timedelta(hours=25),
            cache_keepalive_deadline=now - timedelta(minutes=1),
            next_agent_wake_at=future_agent,
        )
        self.assertEqual(store.claim_due_schedule(owner="owner-a", now=now), {})
        schedule = store.get_schedule(profile_id="profile-a", session_id="session-a", lane_id="subscription")
        self.assertTrue(schedule["keepalive_enabled"])
        self.assertTrue(schedule["keepalive_paused_until_user"])
        self.assertEqual(schedule["cache_state"], "cooling")
        self.assertEqual(schedule["next_agent_wake_at"], future_agent.isoformat(timespec="seconds"))
        self.assertEqual(schedule["due_at"], future_agent.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()
