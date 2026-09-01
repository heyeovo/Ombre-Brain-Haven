import sqlite3
import io
import json
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
    SessionStateConflictError,
)
from agent_wake_store import AgentWakeStore  # noqa: E402


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
        attachment_ids: list[str] | None = None,
        raw_json: str = "",
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
            raw_json=raw_json,
            recalled_bucket_ids=recalled_bucket_ids,
            created_bucket_ids=created_bucket_ids,
            attachment_ids=attachment_ids,
        )

    def test_attachment_is_bound_to_strict_turn_and_clear_keeps_placeholder(self):
        store = self.make_store()
        attachment = store.create_conversation_attachment(
            profile_id="default",
            session_id="session-1",
            filename="截图.png",
            data=b"\x89PNG\r\n\x1a\n" + b"image-data",
        )
        _, stored_path = store.get_conversation_attachment(
            profile_id="default", attachment_id=attachment["id"]
        )
        self.assertTrue(Path(stored_path).exists())

        committed = self.commit(
            store,
            request_id="request-image",
            expected=0,
            attachment_ids=[attachment["id"]],
        )
        turns = store.list_conversation_turns_by_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(turns[0]["attachments"][0]["id"], attachment["id"])
        self.assertEqual(turns[0]["attachments"][0]["turn_id"], committed["turn"]["id"])

        cleared = store.clear_conversation_attachment(
            profile_id="default",
            session_id="session-1",
            attachment_id=attachment["id"],
        )
        self.assertTrue(cleared["cleared"])
        self.assertFalse(Path(stored_path).exists())
        turns = store.list_conversation_turns_by_session(
            profile_id="default", session_id="session-1"
        )
        self.assertTrue(turns[0]["attachments"][0]["cleared"])

    def test_document_attachment_keeps_parsed_text_until_manual_clear(self):
        store = self.make_store()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document />")
        attachment = store.create_conversation_attachment(
            profile_id="default",
            session_id="session-1",
            filename="说明.docx",
            data=buffer.getvalue(),
            kind="file",
            text_content="第一段\n\n第二段",
            text_truncated=True,
        )
        self.assertEqual(attachment["kind"], "file")
        self.assertEqual(attachment["text_chars"], len("第一段\n\n第二段"))
        self.assertTrue(attachment["text_truncated"])

        item, text = store.get_conversation_attachment_text(
            profile_id="default", attachment_id=attachment["id"]
        )
        self.assertEqual(item["kind"], "file")
        self.assertEqual(text, "第一段\n\n第二段")

        image = store.create_conversation_attachment(
            profile_id="default",
            session_id="session-1",
            filename="photo.webp",
            data=b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"image-data",
        )
        cleared = store.clear_session_conversation_attachments(
            profile_id="default", session_id="session-1", kind="file"
        )
        self.assertEqual(cleared, 1)
        cleared_item, cleared_text = store.get_conversation_attachment_text(
            profile_id="default", attachment_id=attachment["id"]
        )
        self.assertTrue(cleared_item["cleared"])
        self.assertEqual(cleared_text, "")
        image_item, image_path = store.get_conversation_attachment(
            profile_id="default", attachment_id=image["id"]
        )
        self.assertFalse(image_item["cleared"])
        self.assertTrue(Path(image_path).exists())

    def test_old_attachment_table_migrates_document_columns_idempotently(self):
        db_path = self.root / "gateway_state.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE conversation_attachments (
                attachment_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id INTEGER,
                round_id INTEGER,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cleared_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        GatewayStateStore(str(db_path))
        GatewayStateStore(str(db_path))
        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_attachments)")}
        conn.close()
        self.assertTrue({"kind", "text_content", "text_truncated"}.issubset(columns))

    def test_permanent_delete_removes_attachment_records_and_files(self):
        store = self.make_store()
        attachment = store.create_conversation_attachment(
            profile_id="default",
            session_id="session-1",
            filename="photo.webp",
            data=b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"image-data",
        )
        _, stored_path = store.get_conversation_attachment(
            profile_id="default", attachment_id=attachment["id"]
        )
        self.commit(
            store,
            request_id="request-image-delete",
            expected=0,
            attachment_ids=[attachment["id"]],
        )
        deleted = store.permanently_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(deleted["conversation_attachments"], 1)
        self.assertFalse(Path(stored_path).exists())
        item, path = store.get_conversation_attachment(
            profile_id="default", attachment_id=attachment["id"]
        )
        self.assertEqual((item, path), ({}, ""))

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
        self.assertEqual(state["handoff_snapshot"], {})
        self.assertEqual(state["frozen_persona_append"], "")
        self.assertFalse(state["frozen_persona_append_initialized"])
        turns = store.list_conversation_turns_by_session(
            profile_id="default", session_id="legacy-session"
        )
        self.assertEqual(turns[0]["turn_kind"], "user")
        conn = sqlite3.connect(db_path)
        turn_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_turns)")
        }
        conn.close()
        self.assertIn("turn_kind", turn_columns)
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
                    "base_prompt": "自定义基础提示词",
                    "prompt": "旧整块提示词",
                    "selfhost_defaults": {
                        "model": "claude-x",
                        "history_token_budget": 12000,
                    },
                }
            )
        self.assertEqual(saved["selfhost_defaults"]["model"], "claude-x")
        self.assertEqual(saved["base_prompt"], "自定义基础提示词")
        self.assertEqual(
            saved["prompt_modules"],
            [
                {
                    "id": "legacy-prompt",
                    "name": "协作者提示词",
                    "content": "旧整块提示词",
                    "enabled_by_default": True,
                }
            ],
        )
        with mock.patch.dict(
            sys.modules,
            {"utils": SimpleNamespace(now_iso=lambda: "2026-08-01T00:01:00+00:00")},
        ):
            cleared = store.save_cc_persona({"id": "ombre", "base_prompt": ""})
        self.assertEqual(cleared["base_prompt"], "")
        self.assertEqual(self.make_store().get_cc_persona("ombre")["base_prompt"], "")

        legacy_selfhost_prompt = "\n".join(
            (
                "你正在 Ombre Brain 的自建聊天链路中回复用户。",
                "你只能使用本轮明确提供的远程 MCP 工具；没有提供的文件、命令或工具能力一律不可声称已经执行。",
                "优先遵循用户当前消息，并给出直接、诚实的回答。",
            )
        )
        with mock.patch.dict(
            sys.modules,
            {"utils": SimpleNamespace(now_iso=lambda: "2026-08-01T00:02:00+00:00")},
        ):
            migrated = store.save_cc_persona(
                {"id": "ombre", "base_prompt": legacy_selfhost_prompt}
            )
        self.assertTrue(migrated["base_prompt"].startswith('你是这间房里持续存在的"你"。'))

        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            updates={
                "local_engine_preference": "selfhost",
                "selfhost_overrides": {"max_history_rounds": 20},
                "prompt_module_overrides": {"legacy-prompt": False},
            },
            expected_state_version=0,
        )
        self.assertEqual(state["local_engine_preference"], "selfhost")
        self.assertEqual(state["selfhost_overrides"], {"max_history_rounds": 20})
        self.assertEqual(state["prompt_module_overrides"], {"legacy-prompt": False})
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

    def test_cc_lanes_keep_independent_resume_points_and_cursors(self):
        store = self.make_store()
        api_raw = json.dumps(
            {
                "cred_mode": "api",
                "provider_id": "provider-a",
                "model": "api-model",
                "cc_session_id": "api-native-session",
                "settings": {"effort": "high", "thinking_on": True},
            }
        )
        self.commit(
            store, request_id="api-1", expected=0, source="cc", raw_json=api_raw
        )
        self.commit(store, request_id="selfhost-1", expected=1, source="selfhost")
        state = store.get_conversation_session_state(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(state["cc_lanes"]["api:provider-a"]["seen_round_id"], 1)
        self.assertEqual(
            state["cc_lanes"]["api:provider-a"]["cc_session_id"],
            "api-native-session",
        )

        pro_raw = json.dumps(
            {
                "cred_mode": "subscription",
                "model": "claude-sonnet-5",
                "cc_session_id": "pro-native-session",
                "thinking": "must not become lane state",
                "attachments": [{"filename": "must-not-sync.png"}],
                "settings": {"effort": "high", "thinking_on": True},
            }
        )
        self.commit(
            store, request_id="pro-1", expected=2, source="cc", raw_json=pro_raw
        )
        state = store.get_conversation_session_state(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(state["cc_lanes"]["api:provider-a"]["seen_round_id"], 1)
        self.assertEqual(state["cc_lanes"]["subscription"]["seen_round_id"], 3)
        self.assertEqual(
            state["cc_lanes"]["subscription"]["cc_session_id"],
            "pro-native-session",
        )
        self.assertNotIn("thinking", state["cc_lanes"]["subscription"])
        self.assertNotIn("attachments", state["cc_lanes"]["subscription"])
        self.assertEqual(state["cc_overrides"]["active_cred"], "subscription")

        restarted = GatewayStateStore(str(self.root / "gateway_state.db"))
        restarted_state = restarted.get_conversation_session_state(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(restarted_state["cc_lanes"], state["cc_lanes"])
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="another-profile", session_id="session-1"
            ),
            {},
        )

    def test_context_gc_preferences_and_lane_switch_are_profile_scoped_and_atomic(self):
        store = self.make_store()
        self.commit(
            store,
            request_id="gc-source",
            expected=0,
            source="cc",
            raw_json=json.dumps(
                {
                    "cred_mode": "subscription",
                    "model": "claude-sonnet-5",
                    "cc_session_id": "native-before",
                }
            ),
        )
        state = store.get_conversation_session_state(
            profile_id="default", session_id="session-1"
        )
        saved = store.patch_conversation_context_gc(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            expected_state_version=state["state_version"],
            preferences={
                "auto_enabled": False,
                "protected_keys": ["ob:bucket-1", "search:abc"],
            },
        )
        self.assertFalse(saved["context_gc"]["auto_enabled"])
        self.assertEqual(saved["context_gc"]["schedule_time"], "05:30")
        self.assertEqual(saved["context_gc"]["protected_keys"], ["ob:bucket-1", "search:abc"])

        switched = store.patch_conversation_context_gc(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            expected_state_version=saved["state_version"],
            commit={
                "lane_id": "subscription",
                "expected_cc_session_id": "native-before",
                "next_cc_session_id": "native-after",
                "released_tokens": 4321,
                "candidate_count": 2,
                "counts": {"ob_recall": 1, "search_chat": 1},
                "mode": "manual",
            },
        )
        self.assertEqual(switched["cc_lanes"]["subscription"]["cc_session_id"], "native-after")
        self.assertEqual(switched["context_gc"]["history"][-1]["released_tokens"], 4321)
        self.assertEqual(
            store.get_conversation_session_state(
                profile_id="another-profile", session_id="session-1"
            ),
            {},
        )
        with self.assertRaises(SessionStateConflictError):
            store.patch_conversation_context_gc(
                profile_id="default",
                session_id="session-1",
                persona_id="ombre",
                expected_state_version=saved["state_version"],
                preferences={"auto_enabled": True},
            )
        with self.assertRaises(ValueError):
            store.patch_conversation_context_gc(
                profile_id="default",
                session_id="session-1",
                persona_id="ombre",
                expected_state_version=switched["state_version"],
                commit={
                    "lane_id": "subscription",
                    "expected_cc_session_id": "native-before",
                    "next_cc_session_id": "must-not-win",
                },
            )

    def test_daily_review_snapshot_is_recent_fixed_and_optional(self):
        store = self.make_store()
        today = datetime.now(timezone(timedelta(hours=8))).date()
        dates = [(today - timedelta(days=offset)).isoformat() for offset in (1, 2, 3, 4)]
        for index, review_date in enumerate(dates):
            store.upsert_daily_review(
                profile_id="default",
                persona_id="ombre",
                review_date=review_date,
                content=f"review-{index}",
                edited_by_user=False,
            )

        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="daily-on",
            persona_id="ombre",
            updates={
                "mode": "work",
                "daily_review_enabled": True,
                "initialize_daily_review_snapshot": True,
            },
        )
        self.assertEqual(state["mode"], "work")
        self.assertEqual(
            [item["review_date"] for item in state["daily_review_snapshot"]],
            sorted(dates[:3]),
        )

        store.upsert_daily_review(
            profile_id="default",
            persona_id="ombre",
            review_date=dates[0],
            content="changed later",
            edited_by_user=True,
        )
        frozen = store.patch_conversation_session_state(
            profile_id="default",
            session_id="daily-on",
            persona_id="ombre",
            updates={"initialize_daily_review_snapshot": True},
        )
        self.assertNotIn("changed later", [item["content"] for item in frozen["daily_review_snapshot"]])

        disabled = store.patch_conversation_session_state(
            profile_id="default",
            session_id="daily-off",
            persona_id="ombre",
            updates={
                "daily_review_enabled": False,
                "initialize_daily_review_snapshot": True,
            },
        )
        self.assertTrue(disabled["daily_review_snapshot_initialized"])
        self.assertEqual(disabled["daily_review_snapshot"], [])

    def test_handoff_snapshot_is_fixed_persistent_and_profile_isolated(self):
        store = self.make_store()
        original = {
            "version": 1,
            "content": "<window_handoff_snapshot>固定背景</window_handoff_snapshot>",
            "stats": {"estimated_tokens": 42},
        }
        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="handoff-session",
            persona_id="ombre",
            updates={"handoff_snapshot": original},
        )
        self.assertEqual(state["handoff_snapshot"], original)

        frozen = store.patch_conversation_session_state(
            profile_id="default",
            session_id="handoff-session",
            persona_id="ombre",
            updates={"handoff_snapshot": {"version": 1, "content": "changed"}},
        )
        self.assertEqual(frozen["handoff_snapshot"], original)

        restarted = GatewayStateStore(str(self.root / "gateway_state.db"))
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="default", session_id="handoff-session"
            )["handoff_snapshot"],
            original,
        )
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="another-profile", session_id="handoff-session"
            ),
            {},
        )

    def test_frozen_persona_append_is_first_write_wins_across_restart(self):
        store = self.make_store()
        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="frozen-session",
            persona_id="ombre",
            updates={"frozen_persona_append": "固定系统前缀"},
        )
        self.assertTrue(state["frozen_persona_append_initialized"])
        self.assertEqual(state["frozen_persona_append"], "固定系统前缀")

        repeated = store.patch_conversation_session_state(
            profile_id="default",
            session_id="frozen-session",
            persona_id="ombre",
            updates={"frozen_persona_append": "不得覆盖"},
            expected_state_version=state["state_version"],
        )
        self.assertEqual(repeated["frozen_persona_append"], "固定系统前缀")

        restarted = GatewayStateStore(str(self.root / "gateway_state.db"))
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="default", session_id="frozen-session"
            )["frozen_persona_append"],
            "固定系统前缀",
        )
        self.assertEqual(
            restarted.get_conversation_session_state(
                profile_id="another-profile", session_id="frozen-session"
            ),
            {},
        )

    def test_empty_frozen_persona_append_is_still_initialized(self):
        store = self.make_store()
        state = store.patch_conversation_session_state(
            profile_id="default",
            session_id="empty-frozen-session",
            persona_id="ombre",
            updates={"frozen_persona_append": ""},
        )
        self.assertTrue(state["frozen_persona_append_initialized"])
        repeated = store.patch_conversation_session_state(
            profile_id="default",
            session_id="empty-frozen-session",
            persona_id="ombre",
            updates={"frozen_persona_append": "不得补写"},
            expected_state_version=state["state_version"],
        )
        self.assertEqual(repeated["frozen_persona_append"], "")

    def test_pro_usage_snapshot_overwrites_single_profile_row_and_is_isolated(self):
        store = self.make_store()
        first = {
            "available": True,
            "stale": False,
            "experimental": True,
            "updatedAt": "2026-08-30T01:00:00.000Z",
            "fiveHour": {"utilization": 20, "resetsAt": None},
            "sevenDay": None,
        }
        second = {**first, "updatedAt": "2026-08-30T02:00:00.000Z"}
        store.save_cc_pro_usage_snapshot(profile_id="default", payload=first)
        saved = store.save_cc_pro_usage_snapshot(profile_id="default", payload=second)
        self.assertEqual(saved["updatedAt"], second["updatedAt"])
        self.assertTrue(saved["persisted_at"])
        self.assertEqual(store.load_cc_pro_usage_snapshot(profile_id="other"), {})

        conn = sqlite3.connect(self.root / "gateway_state.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM cc_pro_usage_snapshot WHERE profile_id = 'default'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        restarted = GatewayStateStore(str(self.root / "gateway_state.db"))
        self.assertEqual(
            restarted.load_cc_pro_usage_snapshot(profile_id="default")["updatedAt"],
            second["updatedAt"],
        )

    def test_manual_daily_review_is_protected_from_automatic_overwrite(self):
        store = self.make_store()
        review_date = "2026-08-08"
        store.upsert_daily_review(
            profile_id="default", persona_id="ombre", review_date=review_date,
            content="manual", edited_by_user=True,
        )
        protected = store.upsert_daily_review(
            profile_id="default", persona_id="ombre", review_date=review_date,
            content="automatic", edited_by_user=False, preserve_user_edit=True,
        )
        self.assertEqual(protected["content"], "manual")
        self.assertTrue(protected["edited_by_user"])

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

    def test_deleted_session_list_is_explicit_and_disappears_after_permanent_delete(self):
        store = self.make_store()
        self.commit(store, request_id="request-1", expected=0, persona_id="ombre")
        store.soft_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(store.list_conversation_sessions(profile_id="default"), [])
        deleted = store.list_conversation_sessions(
            profile_id="default", persona_id="ombre", deleted_only=True
        )
        self.assertEqual([item["session_id"] for item in deleted], ["session-1"])
        self.assertTrue(deleted[0]["deleted_at"])
        store.permanently_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(
            store.list_conversation_sessions(profile_id="default", deleted_only=True),
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

    def test_permanent_delete_removes_only_target_wake_records(self):
        store = self.make_store()
        self.commit(store, request_id="request-1", expected=0)
        wake_store = AgentWakeStore(str(self.root / "gateway_state.db"))
        target, _ = wake_store.create_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        claimed = wake_store.claim_due_schedule(
            owner="owner-a", now=datetime.now(timezone.utc)
        )
        self.assertEqual(claimed["schedule"]["schedule_version"], target["schedule_version"])
        wake_store.create_schedule(
            profile_id="default", session_id="session-2", lane_id="subscription"
        )
        wake_store.create_schedule(
            profile_id="another-profile", session_id="session-1", lane_id="subscription"
        )

        deleted = store.permanently_delete_conversation_session(
            profile_id="default", session_id="session-1"
        )
        self.assertEqual(deleted["agent_wake_schedules"], 1)
        self.assertEqual(deleted["agent_wake_runs"], 1)
        self.assertEqual(
            wake_store.get_schedule(
                profile_id="default", session_id="session-1", lane_id="subscription"
            ),
            {},
        )
        self.assertTrue(
            wake_store.get_schedule(
                profile_id="default", session_id="session-2", lane_id="subscription"
            )
        )
        self.assertTrue(
            wake_store.get_schedule(
                profile_id="another-profile",
                session_id="session-1",
                lane_id="subscription",
            )
        )

    def test_user_turn_atomically_samples_silence_and_updates_cache_clock_once(self):
        store = self.make_store()
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        schedule = store.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={
                "keepalive_enabled": True,
                "agent_wake_enabled": True,
                "conversation_silence_enabled": True,
            },
        )
        started = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        payload = dict(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            request_id="user-wake-state-1",
            expected_last_round_id=0,
            user_text="hello",
            assistant_text="reply",
            source="cc",
            turn_kind="user",
            lane_id="subscription",
            raw_json=json.dumps({"display_segments": {"version": 1, "segments": [{"markdown": "reply"}]}}),
            agent_wake_update={
                "user_activity_at": started.isoformat(),
                "model_activity_at": started.isoformat(),
                "cache_refresh_at": started.isoformat(),
                "sample_silence": True,
                "silence_policy_version": "conversation-silence-v1",
                "wake_decision": {
                    "action": "schedule",
                    "at": (started + timedelta(minutes=30)).isoformat(),
                    "reason": "看看结果",
                },
            },
            created_at=started + timedelta(seconds=4),
        )
        committed = store.commit_conversation_turn(**payload)
        persisted = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        silence_at = datetime.fromisoformat(persisted["conversation_silence_check_at"])
        self.assertGreaterEqual(silence_at, started + timedelta(minutes=8))
        self.assertLessEqual(silence_at, started + timedelta(seconds=4, minutes=25))
        self.assertEqual(persisted["silence_source_turn_id"], committed["turn"]["id"])
        self.assertEqual(persisted["cache_keepalive_deadline"], (started + timedelta(minutes=55)).isoformat(timespec="seconds"))
        self.assertEqual(persisted["next_agent_wake_at"], (started + timedelta(minutes=30)).isoformat(timespec="seconds"))
        version = persisted["schedule_version"]

        replay = store.commit_conversation_turn(**payload)
        replayed = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replayed["schedule_version"], version)
        self.assertEqual(replayed["conversation_silence_check_at"], persisted["conversation_silence_check_at"])

    def test_user_turn_does_not_sample_silence_while_switch_is_off(self):
        store = self.make_store()
        started = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        store.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="silence-disabled", expected_last_round_id=0,
            user_text="hello", assistant_text="reply", source="cc", turn_kind="user",
            lane_id="subscription",
            agent_wake_update={"model_activity_at": started.isoformat(), "sample_silence": True},
            created_at=started,
        )
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        self.assertFalse(schedule["conversation_silence_enabled"])
        self.assertEqual(schedule["conversation_silence_check_at"], "")
        self.assertEqual(schedule["silence_source_turn_id"], 0)

        enabled = store.patch_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={"conversation_silence_enabled": True},
        )
        self.assertEqual(enabled["conversation_silence_check_at"], "")
        store.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="silence-enabled-next-turn", expected_last_round_id=1,
            user_text="again", assistant_text="reply", source="cc", turn_kind="user",
            lane_id="subscription",
            agent_wake_update={"model_activity_at": started.isoformat(), "sample_silence": True},
            created_at=started + timedelta(minutes=1),
        )
        sampled = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        self.assertTrue(sampled["conversation_silence_check_at"])
        self.assertGreater(sampled["silence_source_turn_id"], 0)

    def test_user_arrival_cancels_only_untriggered_silence_timer(self):
        store = self.make_store()
        now = datetime.now(timezone.utc)
        store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        current = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        store.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=current["schedule_version"],
            changes={"keepalive_paused_until_user": True},
        )
        conn = sqlite3.connect(self.root / "gateway_state.db")
        conn.execute(
            """UPDATE agent_wake_schedules
               SET conversation_silence_check_at = ?, silence_source_turn_id = 7,
                   silence_policy_version = 'conversation-silence-v1'
               WHERE profile_id = 'default' AND session_id = 'session-1' AND lane_id = 'subscription'""",
            ((now + timedelta(minutes=10)).isoformat(timespec="seconds"),),
        )
        conn.commit()
        conn.close()
        updated = store.accept_user_activity_and_cancel_silence(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            user_activity_at=now,
        )
        self.assertEqual(updated["conversation_silence_check_at"], "")
        self.assertEqual(updated["silence_source_turn_id"], 0)
        self.assertFalse(updated["keepalive_paused_until_user"])

    def test_stop_all_patch_clears_silence_timer(self):
        store = self.make_store()
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        armed = store.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={
                "keepalive_enabled": True,
                "agent_wake_enabled": True,
                "conversation_silence_enabled": True,
                "conversation_silence_check_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(timespec="seconds"),
                "silence_source_turn_id": 7,
                "silence_policy_version": "conversation-silence-v1",
            },
        )
        stopped = store.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=armed["schedule_version"],
            changes={
                "keepalive_enabled": False,
                "agent_wake_enabled": False,
                "conversation_silence_enabled": False,
                "next_agent_wake_at": "",
                "wake_reason": "",
                "conversation_silence_check_at": "",
                "silence_source_turn_id": 0,
                "silence_policy_version": "",
            },
        )
        self.assertFalse(stopped["keepalive_enabled"])
        self.assertFalse(stopped["agent_wake_enabled"])
        self.assertFalse(stopped["conversation_silence_enabled"])
        self.assertEqual(stopped["conversation_silence_check_at"], "")
        self.assertEqual(stopped["silence_source_turn_id"], 0)
        self.assertEqual(stopped["due_at"], "")

    def test_empty_agent_wake_turn_and_next_wake_commit_together(self):
        store = self.make_store()
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        store.patch_agent_wake_schedule(
            profile_id="default",
            session_id="session-1",
            lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={"agent_wake_enabled": True},
        )
        at = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
        result = store.commit_conversation_turn(
            profile_id="default",
            session_id="session-1",
            persona_id="ombre",
            request_id="wake-1",
            expected_last_round_id=0,
            user_text="",
            assistant_text="",
            source="cc",
            turn_kind="agent_wake",
            lane_id="subscription",
            agent_wake_update={
                "model_activity_at": at.isoformat(),
                "wake_cause": "cache_keepalive",
                "agent_wake": {"wake_id": "wake-1", "cause": "cache_keepalive", "at": at.isoformat()},
                "wake_decision": {"action": "schedule", "at": (at + timedelta(minutes=20)).isoformat(), "reason": "稍后再看"},
            },
            created_at=at + timedelta(seconds=3),
        )
        self.assertEqual(result["turn"]["turn_kind"], "agent_wake")
        restored = store.get_conversation_turn_by_request_id(profile_id="default", request_id="wake-1")
        raw = json.loads(restored["raw_json"])
        self.assertEqual(raw["agent_wake"]["outcome"], "noop")
        self.assertEqual(raw["next_wake"]["reason"], "稍后再看")

    def test_agent_schedule_is_consumed_once_without_overwriting_a_new_decision(self):
        store = self.make_store()
        at = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        store.patch_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={"agent_wake_enabled": True, "next_agent_wake_at": at.isoformat(), "wake_reason": "旧计划"},
        )
        store.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="agent-schedule-consume", expected_last_round_id=0,
            user_text="", assistant_text="done", source="cc", turn_kind="agent_wake",
            lane_id="subscription",
            agent_wake_update={
                "model_activity_at": at.isoformat(), "wake_cause": "agent_schedule",
                "agent_wake": {"wake_id": "agent-schedule-consume", "cause": "agent_schedule", "at": at.isoformat()},
                "wake_decision": {"action": "schedule", "at": (at + timedelta(minutes=20)).isoformat(), "reason": "新计划"},
            },
            created_at=at + timedelta(seconds=2),
        )
        updated = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        self.assertEqual(updated["next_agent_wake_at"], (at + timedelta(minutes=20)).isoformat(timespec="seconds"))
        self.assertEqual(updated["wake_reason"], "新计划")

    def test_silence_wake_never_samples_a_followup_silence_timer(self):
        store = self.make_store()
        started = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription", create=True
        )
        store.patch_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription",
            expected_version=schedule["schedule_version"],
            changes={"conversation_silence_enabled": True},
        )
        user = store.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="silence-source", expected_last_round_id=0,
            user_text="hello", assistant_text="reply", source="cc", turn_kind="user",
            lane_id="subscription",
            agent_wake_update={"model_activity_at": started.isoformat(), "sample_silence": True},
            created_at=started,
        )
        schedule = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        silence_at = schedule["conversation_silence_check_at"]
        store.commit_conversation_turn(
            profile_id="default", session_id="session-1", persona_id="ombre",
            request_id="silence-wake", expected_last_round_id=1,
            user_text="", assistant_text="要继续吗？", source="cc", turn_kind="agent_wake",
            lane_id="subscription",
            agent_wake_update={
                "model_activity_at": silence_at, "wake_cause": "conversation_silence",
                "agent_wake": {"wake_id": "silence-wake", "cause": "conversation_silence", "at": silence_at},
            },
            created_at=datetime.fromisoformat(silence_at) + timedelta(seconds=1),
        )
        updated = store.get_agent_wake_schedule(
            profile_id="default", session_id="session-1", lane_id="subscription"
        )
        self.assertGreater(user["turn"]["id"], 0)
        self.assertEqual(updated["conversation_silence_check_at"], "")
        self.assertEqual(updated["silence_source_turn_id"], 0)

    def test_cooldown_normalizes_new_aware_timestamp_with_naive_now(self):
        store = self.make_store()
        store.record_success(
            "session-1",
            ["bucket-1"],
            completed_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        )
        multiplier = store.get_cooldown_multiplier(
            "session-1",
            "bucket-1",
            cooldown_hours=2,
            cooldown_floor=0.5,
            now=datetime(2026, 8, 2, 13, 0),
        )
        self.assertEqual(multiplier, 0.75)

    def test_cooldown_normalizes_legacy_naive_timestamp_with_aware_now(self):
        store = self.make_store()
        store.record_success(
            "session-1",
            ["bucket-1"],
            completed_at=datetime(2026, 8, 2, 12, 0),
        )
        multiplier = store.get_cooldown_multiplier(
            "session-1",
            "bucket-1",
            cooldown_hours=2,
            cooldown_floor=0.5,
            now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(multiplier, 0.75)


if __name__ == "__main__":
    unittest.main()
