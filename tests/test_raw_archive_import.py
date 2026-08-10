import json
import base64
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from raw_archive_import import build_preview, write_selected_archive
from raw_events import RAW_EVENT_ARCHIVE_SCOPE, RawEventStore


class RawEventArchiveScopeTest(unittest.TestCase):
    def make_store(self, root: Path) -> RawEventStore:
        return RawEventStore(
            {
                "state_dir": str(root / "state"),
                "buckets_dir": str(root / "buckets"),
            }
        )

    def test_existing_schema_migrates_and_historical_events_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir()
            db_path = state / "raw_events.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL DEFAULT '',
                    event_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    conversation_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    client TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(source, event_hash)
                )
                """
            )
            conn.commit()
            conn.close()

            store = self.make_store(root)
            check_conn = store._connect()
            try:
                columns = {
                    row["name"] for row in check_conn.execute("PRAGMA table_info(raw_events)").fetchall()
                }
            finally:
                check_conn.close()
            self.assertTrue({"usage_scope", "canonical_hash", "import_id"}.issubset(columns))

            runtime = {
                "id": "runtime-1",
                "role": "user",
                "text": "运行时消息",
                "created_at": "2026-07-01T00:00:00+00:00",
            }
            archive = {
                "id": "archive-1",
                "role": "user",
                "text": "历史档案消息",
                "created_at": "2026-07-01T00:00:00+00:00",
                "usage_scope": RAW_EVENT_ARCHIVE_SCOPE,
            }
            store.ingest([runtime], source="runtime")
            first = store.ingest([archive], source="claude_official_export")
            second = store.ingest([archive], source="claude_official_export")
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["duplicate"], 1)

            changed = {**archive, "text": "历史档案消息已变化"}
            changed_result = store.ingest([changed], source="claude_official_export")
            self.assertEqual(changed_result["conflict"], 1)

            start = datetime(2026, 7, 1, tzinfo=timezone.utc)
            end = datetime(2026, 7, 2, tzinfo=timezone.utc)
            default_items = store.list_events_between(start_at=start, end_at=end, limit=0)
            archive_items = store.list_events_between(
                start_at=start,
                end_at=end,
                limit=0,
                usage_scope=RAW_EVENT_ARCHIVE_SCOPE,
            )
            self.assertEqual([item["text"] for item in default_items], ["运行时消息"])
            self.assertEqual([item["text"] for item in archive_items], ["历史档案消息"])
            self.assertEqual(store.search("历史档案")["count"], 0)
            self.assertEqual(
                store.search("历史档案", usage_scope=RAW_EVENT_ARCHIVE_SCOPE)["count"],
                1,
            )

    def test_canonical_matches_preserve_both_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self.make_store(Path(temp))
            common = {
                "role": "assistant",
                "text": "相同原文",
                "created_at": "2026-07-01T00:00:00+00:00",
                "usage_scope": RAW_EVENT_ARCHIVE_SCOPE,
            }
            store.ingest([{**common, "id": "a"}], source="claude_official_export")
            store.ingest([{**common, "id": "b"}], source="kelivo_export")
            canonical = RawEventStore.canonical_event_hash(
                role="assistant",
                text="相同原文",
                created_at="2026-07-01T00:00:00+00:00",
            )
            matches = store.find_canonical_matches([canonical])
            self.assertEqual({item["source"] for item in matches[canonical]}, {"claude_official_export", "kelivo_export"})

    def test_archive_chunks_are_idempotent_and_commit_only_after_hash_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self.make_store(Path(temp))
            payload = b"private selected archive"
            digest = hashlib.sha256(payload).hexdigest()
            kwargs = {
                "import_id": "history-test",
                "index": 0,
                "total_chunks": 1,
                "data_base64": base64.b64encode(payload).decode("ascii"),
                "chunk_sha256": digest,
                "source": "historical_exports",
                "source_file_sha256": "a" * 64,
                "selection_hash": "b" * 64,
                "archive_sha256": digest,
            }
            first = store.put_archive_chunk(**kwargs)
            duplicate_chunk = store.put_archive_chunk(**kwargs)
            committed = store.commit_archive("history-test")
            duplicate_commit = store.commit_archive("history-test")

            self.assertEqual(first["status"], "stored")
            self.assertEqual(duplicate_chunk["status"], "duplicate")
            self.assertEqual(committed["status"], "archived")
            self.assertEqual(duplicate_commit["status"], "duplicate")
            self.assertEqual(Path(committed["archive_path"]).read_bytes(), payload)


class RawArchiveAdapterTest(unittest.TestCase):
    def write_exports(self, root: Path) -> tuple[Path, Path]:
        claude = root / "conversations.json"
        kelivo = root / "chats.json"
        claude.write_text(
            json.dumps(
                [
                    {
                        "uuid": "keep-claude",
                        "name": "保留窗口",
                        "created_at": "2026-07-01T00:00:00Z",
                        "updated_at": "2026-07-01T00:01:00Z",
                        "chat_messages": [
                            {
                                "uuid": "c-user",
                                "sender": "human",
                                "text": "重复原文",
                                "content": [{"type": "text", "text": "重复原文"}],
                                "created_at": "2026-07-01T00:00:00Z",
                                "attachments": [],
                                "files": [],
                            },
                            {
                                "uuid": "c-assistant",
                                "sender": "assistant",
                                "text": "回答",
                                "content": [
                                    {"type": "thinking", "thinking": "保留Claude推理"},
                                    {"type": "text", "text": "回答"},
                                    {"type": "tool_use", "name": "秘密工具", "input": "秘密参数"},
                                    {"type": "tool_result", "content": "秘密工具结果"},
                                ],
                                "created_at": "2026-07-01T00:01:00Z",
                                "attachments": [],
                                "files": [{"file_name": "private.txt"}],
                            },
                        ],
                    },
                    {
                        "uuid": "drop-claude",
                        "name": "不保留窗口",
                        "chat_messages": [{"uuid": "secret", "sender": "human", "text": "不得归档"}],
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        kelivo.write_text(
            json.dumps(
                {
                    "version": 1,
                    "conversations": [
                        {
                            "id": "keep-kelivo",
                            "title": "窗口1",
                            "messageIds": ["k-user", "k-assistant"],
                            "isPinned": False,
                        },
                        {
                            "id": "drop-kelivo",
                            "title": "不保留",
                            "messageIds": ["k-secret"],
                            "isPinned": False,
                        },
                    ],
                    "messages": [
                        {
                            "id": "k-user",
                            "conversationId": "keep-kelivo",
                            "role": "user",
                            "content": "重复原文",
                            "timestamp": "2026-07-01T08:00:00",
                            "reasoningText": "",
                        },
                        {
                            "id": "k-assistant",
                            "conversationId": "keep-kelivo",
                            "role": "assistant",
                            "content": "Kelivo回答",
                            "timestamp": "2026-07-01T08:02:00",
                            "reasoningText": "隐藏推理",
                        },
                        {
                            "id": "k-secret",
                            "conversationId": "drop-kelivo",
                            "role": "user",
                            "content": "不得归档",
                            "timestamp": "2026-07-01T09:00:00",
                        },
                    ],
                    "toolEvents": {"k-assistant": [{"name": "秘密Kelivo工具"}]},
                    "geminiThoughtSigs": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return claude, kelivo

    def test_preview_uses_only_selected_windows_and_normalizes_kelivo_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            claude, kelivo = self.write_exports(Path(temp))
            preview = build_preview(
                claude_path=claude,
                kelivo_path=kelivo,
                claude_ids={"keep-claude"},
                kelivo_ids={"keep-kelivo"},
            )
            self.assertEqual(preview["claude"]["message_count"], 2)
            self.assertEqual(preview["kelivo"]["message_count"], 2)
            self.assertEqual(preview["claude"]["roles"], {"user": 1, "assistant": 1})
            self.assertEqual(preview["kelivo"]["date_range"]["earliest"], "2026-07-01T00:00:00.000+00:00")
            self.assertEqual(preview["claude"]["preserved_nonindexed"]["thinking"], 1)
            self.assertEqual(preview["claude"]["excluded_content"]["tool_use"], 1)
            self.assertEqual(preview["claude"]["excluded_content"]["tool_result"], 1)
            self.assertEqual(preview["kelivo"]["preserved_nonindexed"]["reasoningText"], 1)
            self.assertEqual(preview["kelivo"]["excluded_content"]["toolEvents"], 1)
            self.assertEqual(preview["cross_source_suspected_duplicates"]["exact_role_text_time"], 1)
            self.assertFalse(preview["import_authorized"])

    def test_selected_archive_contains_no_unselected_conversations_or_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            claude, kelivo = self.write_exports(root)
            output = root / "selected.zip"
            write_selected_archive(
                output,
                claude_path=claude,
                kelivo_path=kelivo,
                claude_ids={"keep-claude"},
                kelivo_ids={"keep-kelivo"},
            )
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertIn("claude/conversations/keep-claude.json", names)
                self.assertIn("kelivo/conversations/keep-kelivo.json", names)
                self.assertNotIn("claude/conversations/drop-claude.json", names)
                self.assertNotIn("kelivo/conversations/drop-kelivo.json", names)
                combined = b"\n".join(archive.read(name) for name in names)
                self.assertNotIn("不得归档".encode("utf-8"), combined)
                self.assertIn("保留Claude推理".encode("utf-8"), combined)
                self.assertIn("隐藏推理".encode("utf-8"), combined)
                self.assertNotIn("秘密工具".encode("utf-8"), combined)
                self.assertNotIn("秘密参数".encode("utf-8"), combined)
                self.assertNotIn("秘密工具结果".encode("utf-8"), combined)
                self.assertNotIn("秘密Kelivo工具".encode("utf-8"), combined)
                self.assertNotIn("private.txt".encode("utf-8"), combined)


if __name__ == "__main__":
    unittest.main()
