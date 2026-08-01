import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway_state import (  # noqa: E402
    ConversationConflictError,
    ConversationPersonaConflictError,
    GatewayStateStore,
    RequestIdReuseError,
)


class GatewayStateContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_store(self) -> GatewayStateStore:
        return GatewayStateStore(str(self.root / "gateway_state.db"))

    def commit(
        self,
        store: GatewayStateStore,
        *,
        request_id: str,
        expected: int | None,
        source: str = "selfhost",
        persona_id: str = "ombre",
        assistant_text: str = "reply",
        recalled_bucket_ids: list[str] | None = None,
        created_bucket_ids: list[str] | None = None,
    ):
        return store.commit_conversation_turn(
            profile_id="default",
            session_id="session-1",
            persona_id=persona_id,
            request_id=request_id,
            expected_last_round_id=expected,
            user_text="hello",
            assistant_text=assistant_text,
            model="model-a",
            client=f"ob2-chat/{persona_id}",
            route="selfhost" if source == "selfhost" else "cc",
            source=source,
            recalled_bucket_ids=recalled_bucket_ids,
            created_bucket_ids=created_bucket_ids,
        )

    def test_old_database_migration_backfills_persona_and_cc_cursor(self):
        db_path = self.root / "gateway_state.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                user_text TEXT NOT NULL DEFAULT '',
                assistant_text TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                client TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                UNIQUE(profile_id, session_id, round_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE conversation_sessions (
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                deleted_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile_id, session_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_turns
            (profile_id, session_id, round_id, created_at, user_text, assistant_text,
             model, client, route)
            VALUES ('default', 'legacy-session', 3, '2026-01-01T00:00:00+00:00',
                    'u', 'a', 'm', 'ob2-chat/lyra', 'cc')
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_sessions
            (profile_id, session_id, title, deleted_at, updated_at)
            VALUES ('default', 'legacy-session', '', NULL, '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()
        conn.close()

        store = GatewayStateStore(str(db_path))
        state = store.get_conversation_session_state(
            profile_id="default", session_id="legacy-session"
        )
        self.assertEqual(state["persona_id"], "lyra")
        self.assertEqual(state["cc_seen_round_id"], 3)
        self.assertEqual(state["local_engine_preference"], "cc")
        restarted = GatewayStateStore(str(db_path))
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="default", session_id="legacy-session"
            )["cc_seen_round_id"],
            3,
        )

    def test_persona_defaults_and_session_state_are_persisted(self):
        store = self.make_store()
        with mock.patch.dict(
            sys.modules,
            {"utils": SimpleNamespace(now_iso=lambda: "2026-08-01T00:00:00+00:00")},
        ):
            saved = store.save_cc_persona(
                {
                    "id": "ombre",
                    "name": "Ombre",
                    "selfhost_defaults": {
                        "model": "claude-x",
                        "history_token_budget": 12000,
                    },
                }
            )
        self.assertEqual(saved["selfhost_defaults"]["model"], "claude-x")

        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            updates={
                "local_engine_preference": "selfhost",
                "selfhost_overrides": {"max_history_rounds": 20},
            },
            expected_state_version=0,
        )
        self.assertEqual(state["local_engine_preference"], "selfhost")
        self.assertEqual(state["selfhost_overrides"], {"max_history_rounds": 20})
        with self.assertRaisesRegex(ValueError, "runtime-only"):
            store.patch_conversation_session_state(
                profile_id="default",
                session_id="session-1",
                persona_id="ombre",
                updates={"effective_engine": "selfhost"},
            )

    def test_atomic_commit_tracks_idempotency_cursor_and_buckets(self):
        store = self.make_store()
        first = self.commit(
            store,
            request_id="request-1",
            expected=None,
            recalled_bucket_ids=["bucket-recalled"],
            created_bucket_ids=["bucket-created"],
        )
        replay = self.commit(
            store,
            request_id="request-1",
            expected=None,
            recalled_bucket_ids=["bucket-recalled"],
            created_bucket_ids=["bucket-created"],
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["turn"]["id"], first["turn"]["id"])
        self.assertEqual(
            store.get_session_bucket_exclusion_ids(
                profile_id="default", session_id="session-1"
            ),
            {"bucket-recalled", "bucket-created"},
        )
        with self.assertRaises(ConversationConflictError):
            self.commit(store, request_id="request-2", expected=0)
        cc_turn = self.commit(store, request_id="request-3", expected=1, source="cc")
        self.assertEqual(cc_turn["turn"]["round_id"], 2)
        state = store.get_conversation_session_state(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(state["cc_seen_round_id"], 2)
        self.commit(store, request_id="request-4", expected=2, source="selfhost")
        missing = store.list_conversation_turns_by_session(
            profile_id="default",
            session_id="session-1",
            after_round_id=state["cc_seen_round_id"],
            source="selfhost",
        )
        self.assertEqual([turn["round_id"] for turn in missing], [3])
        self.assertEqual(
            store.get_conversation_session_state(
                profile_id="default", session_id="session-1"
            )["cc_seen_round_id"],
            2,
        )

    def test_committed_turn_can_be_read_by_request_id_for_replay(self):
        store = self.make_store()
        committed = store.commit_conversation_turn(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            request_id="request-replay",
            expected_last_round_id=0,
            user_text="hello",
            assistant_text="saved reply",
            model="model-a",
            client="ob2-chat/ombre",
            route="/api/cc-chat-selfhost",
            source="selfhost",
            raw_json='{"usage":{"inputTokens":12}}',
        )

        replay = store.get_conversation_turn_by_request_id(
            profile_id="default", request_id="request-replay"
        )
        self.assertEqual(replay["id"], committed["turn"]["id"])
        self.assertEqual(replay["persona_id"], "ombre")
        self.assertEqual(replay["assistant_text"], "saved reply")
        self.assertEqual(replay["raw_json"], '{"usage":{"inputTokens":12}}')
        self.assertEqual(
            store.get_conversation_turn_by_request_id(
                profile_id="default", request_id="missing"
            ),
            {},
        )

    def test_request_id_and_persona_cannot_be_reused(self):
        store = self.make_store()
        self.commit(store, request_id="request-1", expected=0)
        with self.assertRaises(RequestIdReuseError):
            self.commit(
                store,
                request_id="request-1",
                expected=0,
                assistant_text="different reply",
            )
        with self.assertRaises(RequestIdReuseError):
            self.commit(
                store,
                request_id="request-1",
                expected=0,
                created_bucket_ids=["different-bucket"],
            )
        with self.assertRaises(ConversationPersonaConflictError):
            self.commit(
                store,
                request_id="request-2",
                expected=1,
                persona_id="another-persona",
            )

    def test_two_writers_on_same_old_head_only_commit_once(self):
        store = self.make_store()

        def attempt(request_id: str) -> str:
            try:
                self.commit(store, request_id=request_id, expected=0)
                return "ok"
            except ConversationConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["request-a", "request-b"]))
        self.assertEqual(sorted(results), ["conflict", "ok"])
        turns = store.list_conversation_turns_by_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(len(turns), 1)

    def test_session_list_filters_by_persisted_persona(self):
        store = self.make_store()
        self.commit(store, request_id="request-1", expected=0, persona_id="ombre")
        self.assertEqual(
            len(store.list_conversation_sessions(profile_id="default", persona_id="ombre")),
            1,
        )
        self.assertEqual(
            store.list_conversation_sessions(
                profile_id="default", persona_id="another-persona"
            ),
            [],
        )

    def test_permanent_delete_removes_window_but_not_unscoped_legacy_state(self):
        store = self.make_store()
        self.commit(
            store,
            request_id="request-1",
            expected=0,
            recalled_bucket_ids=["bucket-recalled"],
            created_bucket_ids=["bucket-created"],
        )
        deleted = store.permanently_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(deleted["conversation_turns"], 1)
        self.assertEqual(deleted["conversation_sessions"], 1)
        self.assertEqual(deleted["session_created_buckets"], 1)
        self.assertEqual(
            store.get_conversation_session_state(
                profile_id="default", session_id="session-1"
            ),
            {},
        )
        self.assertEqual(
            store.list_conversation_turns_by_session(
                profile_id="default", session_id="session-1"
            ),
            [],
        )
        # Legacy recall/cooldown tables have no profile_id, so they remain until
        # a profile-safe migration exists.
        self.assertEqual(
            store.get_session_bucket_exclusion_ids(
                profile_id="default", session_id="session-1"
            ),
            {"bucket-recalled"},
        )


if __name__ == "__main__":
    unittest.main()
