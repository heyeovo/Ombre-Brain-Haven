"""Persistent contracts for versioned CC conversation slices.

This module intentionally contains no model calls, retrieval, or Context injection.
It owns the phase-one SQLite contract, source hashing, validation, idempotency,
CAS reslice revisions, and atomic batch activation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


CHAT_DAY_RULE_VERSION = "persisted-fixed-boundary-v1"
SLICE_SCHEMA_VERSION = "conversation-slices-v1"
MAX_SLICE_SUMMARY_CHARS = 1200


class ConversationSliceConflictError(Exception):
    def __init__(self, expected_revision: int, actual_revision: int):
        super().__init__("conversation slice revision changed")
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize_conversation_slice_schema(conn: sqlite3.Connection) -> None:
    """Add the slice table group without rebuilding or deleting existing data."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_slice_revisions (
            profile_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chat_day TEXT NOT NULL,
            session_source_snapshot_hash TEXT NOT NULL,
            segmenter_version TEXT NOT NULL,
            slice_prompt_version TEXT NOT NULL,
            slice_schema_version TEXT NOT NULL,
            current_revision INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                profile_id, persona_id, session_id, chat_day,
                session_source_snapshot_hash, segmenter_version,
                slice_prompt_version, slice_schema_version
            )
        );

        CREATE TABLE IF NOT EXISTS conversation_slice_batches (
            batch_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chat_day TEXT NOT NULL,
            chat_day_rule_version TEXT NOT NULL,
            daily_source_snapshot_hash TEXT,
            session_source_snapshot_hash TEXT NOT NULL,
            source_message_count INTEGER NOT NULL,
            segmenter_version TEXT NOT NULL,
            slice_prompt_version TEXT NOT NULL,
            slice_schema_version TEXT NOT NULL,
            generator_provider TEXT NOT NULL DEFAULT '',
            generator_model TEXT NOT NULL DEFAULT '',
            reslice_revision INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'candidate',
            supersedes_batch_id TEXT,
            coverage_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            activated_at TEXT,
            superseded_at TEXT,
            error_code TEXT,
            error_detail TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_slice_batches_identity
        ON conversation_slice_batches (
            profile_id, persona_id, session_id, chat_day,
            session_source_snapshot_hash, segmenter_version,
            slice_prompt_version, slice_schema_version, reslice_revision
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_slice_batches_scope
        ON conversation_slice_batches (
            profile_id, persona_id, session_id, chat_day, status, created_at
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_slice_one_active_batch
        ON conversation_slice_batches (profile_id, persona_id, session_id, chat_day)
        WHERE status = 'active';

        CREATE TABLE IF NOT EXISTS conversation_slices (
            slice_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chat_day TEXT NOT NULL,
            source_start_message_id TEXT NOT NULL,
            source_end_message_id TEXT NOT NULL,
            source_message_ids_json TEXT NOT NULL,
            source_content_hash TEXT NOT NULL,
            source_time_start TEXT NOT NULL,
            source_time_end TEXT NOT NULL,
            event_time_start TEXT,
            event_time_end TEXT,
            summary TEXT NOT NULL,
            summary_content_hash TEXT NOT NULL,
            boundary_reason TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            review_reason TEXT,
            review_note TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (batch_id, sequence_no),
            UNIQUE (
                batch_id, session_id, chat_day,
                source_start_message_id, source_end_message_id,
                source_content_hash
            )
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_slices_scope
        ON conversation_slices (
            profile_id, persona_id, session_id, chat_day, lifecycle_status, sequence_no
        );

        CREATE TABLE IF NOT EXISTS conversation_slice_tasks (
            task_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chat_day TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            reslice_revision INTEGER NOT NULL,
            batch_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_slice_tasks_scope
        ON conversation_slice_tasks (
            profile_id, persona_id, session_id, chat_day, status, created_at
        );

        CREATE TABLE IF NOT EXISTS conversation_slice_embeddings (
            slice_id TEXT NOT NULL,
            embedding_version TEXT NOT NULL,
            summary_content_hash TEXT NOT NULL,
            embedding_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slice_id, embedding_version)
        );
        """
    )
    task_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_slice_tasks)").fetchall()
    }
    for name, ddl in {
        "message_count": "INTEGER NOT NULL DEFAULT 0",
        "estimated_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "estimated_call_count": "INTEGER NOT NULL DEFAULT 1",
        "started_at": "TEXT",
        "paused_at": "TEXT",
        "completed_at": "TEXT",
        "failed_at": "TEXT",
    }.items():
        if name not in task_columns:
            conn.execute(f"ALTER TABLE conversation_slice_tasks ADD COLUMN {name} {ddl}")


def delete_conversation_slice_session_records(
    conn: sqlite3.Connection,
    *,
    profile_id: str,
    session_id: str,
) -> dict[str, int]:
    """Delete only slice records belonging to one verified profile/session."""
    params = (profile_id, session_id)
    counts: dict[str, int] = {}
    slice_ids = [
        str(row[0])
        for row in conn.execute(
            """SELECT slice_id FROM conversation_slices
               WHERE profile_id = ? AND session_id = ?""",
            params,
        ).fetchall()
    ]
    if slice_ids:
        placeholders = ",".join("?" for _ in slice_ids)
        cursor = conn.execute(
            f"DELETE FROM conversation_slice_embeddings WHERE slice_id IN ({placeholders})",
            slice_ids,
        )
        counts["conversation_slice_embeddings"] = max(0, int(cursor.rowcount or 0))
    else:
        counts["conversation_slice_embeddings"] = 0
    for table in (
        "conversation_slices",
        "conversation_slice_batches",
        "conversation_slice_tasks",
        "conversation_slice_revisions",
    ):
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE profile_id = ? AND session_id = ?",
            params,
        )
        counts[table] = max(0, int(cursor.rowcount or 0))
    return counts


class ConversationSliceStore:
    _BOUNDARY_REASONS = {
        "topic_shift",
        "event_complete",
        "time_gap",
        "day_end",
        "length_limit",
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = self._connect()
        initialize_conversation_slice_schema(conn)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def batch_idempotency_key(
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        session_source_snapshot_hash: str,
        segmenter_version: str,
        slice_prompt_version: str,
        slice_schema_version: str,
        reslice_revision: int,
    ) -> str:
        return _sha256_json([
            profile_id,
            persona_id,
            session_id,
            chat_day,
            session_source_snapshot_hash,
            segmenter_version,
            slice_prompt_version,
            slice_schema_version,
            int(reslice_revision),
        ])

    @staticmethod
    def task_idempotency_key(
        *,
        trigger_type: str,
        batch_idempotency_key: str,
    ) -> str:
        return _sha256_json([trigger_type, batch_idempotency_key])

    @staticmethod
    def _validate_scope(
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
    ) -> tuple[str, str, str, str]:
        values = tuple(str(value or "").strip() for value in (
            profile_id,
            persona_id,
            session_id,
            chat_day,
        ))
        if not all(values):
            raise ValueError("profile_id, persona_id, session_id and chat_day are required")
        return values  # type: ignore[return-value]

    def _source_snapshot_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
    ) -> dict[str, Any]:
        profile_id, persona_id, session_id, chat_day = self._validate_scope(
            profile_id, persona_id, session_id, chat_day
        )
        owner = conn.execute(
            """SELECT persona_id, deleted_at, rolling_context_json
               FROM conversation_sessions
               WHERE profile_id = ? AND session_id = ?""",
            (profile_id, session_id),
        ).fetchone()
        if owner is None:
            raise ValueError("conversation session not found")
        actual_persona_id = str(owner["persona_id"] or "ombre")
        if actual_persona_id != persona_id:
            raise ValueError("conversation session belongs to another persona")
        try:
            rolling_context = json.loads(str(owner["rolling_context_json"] or "{}"))
        except (TypeError, ValueError):
            rolling_context = {}
        if not isinstance(rolling_context, dict):
            rolling_context = {}
        timezone_name = str(rolling_context.get("timezone") or "Asia/Shanghai")
        day_start_hour = max(0, min(23, int(rolling_context.get("day_start_hour") or 4)))
        chat_day_rule_version = (
            f"{CHAT_DAY_RULE_VERSION};timezone={timezone_name};start_hour={day_start_hour}"
        )
        turns = conn.execute(
            """SELECT id, round_id, user_message_id, assistant_message_id,
                      user_text, assistant_text, created_at
               FROM conversation_turns
               WHERE profile_id = ? AND session_id = ? AND chat_day = ?
               ORDER BY id ASC""",
            (profile_id, session_id, chat_day),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for turn in turns:
            attachments = [
                {
                    "attachment_id": str(row["attachment_id"]),
                    "content_hash": str(row["sha256"]),
                    "filename": str(row["filename"] or ""),
                    "text_content": str(row["text_content"] or ""),
                }
                for row in conn.execute(
                    """SELECT attachment_id, sha256, filename, text_content
                       FROM conversation_attachments
                       WHERE profile_id = ? AND session_id = ? AND turn_id = ?
                         AND cleared_at IS NULL
                       ORDER BY created_at ASC, attachment_id ASC""",
                    (profile_id, session_id, int(turn["id"])),
                ).fetchall()
            ]
            user_text = str(turn["user_text"] or "")
            if user_text.strip() or attachments:
                messages.append({
                    "message_id": str(turn["user_message_id"]),
                    "role": "user",
                    "content": user_text,
                    "attachments": attachments,
                    "created_at": str(turn["created_at"]),
                })
            assistant_text = str(turn["assistant_text"] or "")
            if assistant_text.strip():
                messages.append({
                    "message_id": str(turn["assistant_message_id"]),
                    "role": "assistant",
                    "content": assistant_text,
                    "attachments": [],
                    "created_at": str(turn["created_at"]),
                })
        if any(not item["message_id"] for item in messages):
            raise ValueError("source message is missing a permanent message id")
        normalized = [{
            "message_id": item["message_id"],
            "role": item["role"],
            "content": item["content"],
            "attachments": [
                {
                    "attachment_id": attachment["attachment_id"],
                    "content_hash": attachment["content_hash"],
                }
                for attachment in item["attachments"]
            ],
        } for item in messages]
        return {
            "profile_id": profile_id,
            "persona_id": persona_id,
            "session_id": session_id,
            "chat_day": chat_day,
            "chat_day_rule_version": chat_day_rule_version,
            "messages": messages,
            "source_message_count": len(messages),
            "session_source_snapshot_hash": _sha256_json(normalized),
        }

    def build_source_snapshot(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._source_snapshot_with_conn(
                conn,
                profile_id=profile_id,
                persona_id=persona_id,
                session_id=session_id,
                chat_day=chat_day,
            )
        finally:
            conn.close()

    @staticmethod
    def daily_source_snapshot_hash(session_snapshots: list[dict[str, Any]]) -> str:
        return _sha256_json([
            {
                "session_id": str(snapshot.get("session_id") or ""),
                "session_source_snapshot_hash": str(
                    snapshot.get("session_source_snapshot_hash") or ""
                ),
            }
            for snapshot in sorted(
                session_snapshots,
                key=lambda item: str(item.get("session_id") or ""),
            )
        ])

    def has_active_batch(
        self, *, profile_id: str, persona_id: str, session_id: str, chat_day: str,
    ) -> bool:
        conn = self._connect()
        row = conn.execute(
            """SELECT session_source_snapshot_hash FROM conversation_slice_batches
               WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                 AND status = 'active' LIMIT 1""",
            (profile_id, persona_id, session_id, chat_day),
        ).fetchone()
        if row is None:
            conn.close()
            return False
        snapshot = self._source_snapshot_with_conn(
            conn,
            profile_id=profile_id,
            persona_id=persona_id,
            session_id=session_id,
            chat_day=chat_day,
        )
        conn.close()
        return str(row["session_source_snapshot_hash"]) == str(snapshot["session_source_snapshot_hash"])

    def get_reslice_revision(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        session_source_snapshot_hash: str,
        segmenter_version: str,
        slice_prompt_version: str,
        slice_schema_version: str = SLICE_SCHEMA_VERSION,
    ) -> int:
        conn = self._connect()
        row = conn.execute(
            """SELECT current_revision FROM conversation_slice_revisions
               WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                 AND session_source_snapshot_hash = ? AND segmenter_version = ?
                 AND slice_prompt_version = ? AND slice_schema_version = ?""",
            (
                profile_id, persona_id, session_id, chat_day,
                session_source_snapshot_hash, segmenter_version,
                slice_prompt_version, slice_schema_version,
            ),
        ).fetchone()
        conn.close()
        return int(row["current_revision"] if row else 1)

    def allocate_reslice_revision(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        session_source_snapshot_hash: str,
        segmenter_version: str,
        slice_prompt_version: str,
        expected_revision: int,
        slice_schema_version: str = SLICE_SCHEMA_VERSION,
    ) -> int:
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            params = (
                profile_id, persona_id, session_id, chat_day,
                session_source_snapshot_hash, segmenter_version,
                slice_prompt_version, slice_schema_version,
            )
            row = conn.execute(
                """SELECT current_revision FROM conversation_slice_revisions
                   WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                     AND session_source_snapshot_hash = ? AND segmenter_version = ?
                     AND slice_prompt_version = ? AND slice_schema_version = ?""",
                params,
            ).fetchone()
            actual = int(row["current_revision"] if row else 1)
            if int(expected_revision) != actual:
                raise ConversationSliceConflictError(int(expected_revision), actual)
            next_revision = actual + 1
            conn.execute(
                """INSERT INTO conversation_slice_revisions
                   (profile_id, persona_id, session_id, chat_day,
                    session_source_snapshot_hash, segmenter_version,
                    slice_prompt_version, slice_schema_version,
                    current_revision, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile_id, persona_id, session_id, chat_day,
                               session_source_snapshot_hash, segmenter_version,
                               slice_prompt_version, slice_schema_version)
                   DO UPDATE SET current_revision = excluded.current_revision,
                                 updated_at = excluded.updated_at""",
                (*params, next_revision, now),
            )
            conn.commit()
            return next_revision
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _ignored_message_ids(coverage: dict[str, Any]) -> list[str]:
        ignored: list[str] = []
        for item in coverage.get("ignored_ranges") or []:
            if not isinstance(item, dict) or not str(item.get("reason") or "").strip():
                raise ValueError("every ignored source range requires a reason")
            ids = [str(value or "").strip() for value in item.get("message_ids") or []]
            if not ids or any(not value for value in ids):
                raise ValueError("ignored source range requires message_ids")
            ignored.extend(ids)
        return ignored

    def _validate_slices(
        self,
        *,
        snapshot: dict[str, Any],
        slices: list[dict[str, Any]],
        coverage: dict[str, Any],
    ) -> list[dict[str, Any]]:
        messages = snapshot["messages"]
        source_ids = [str(item["message_id"]) for item in messages]
        source_index = {message_id: index for index, message_id in enumerate(source_ids)}
        ignored_ids = self._ignored_message_ids(coverage)
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        previous_end = -1
        for sequence_no, item in enumerate(slices, start=1):
            ids = [str(value or "").strip() for value in item.get("source_message_ids") or []]
            if not ids or any(value not in source_index for value in ids):
                raise ValueError("slice source message does not belong to this session/day")
            positions = [source_index[value] for value in ids]
            if positions != list(range(positions[0], positions[-1] + 1)):
                raise ValueError("slice source messages must be one continuous ordered range")
            if positions[0] <= previous_end or any(value in seen for value in ids):
                raise ValueError("slice source ranges must be ordered and non-overlapping")
            previous_end = positions[-1]
            seen.update(ids)
            summary = str(item.get("summary") or "").strip()
            if not summary:
                raise ValueError("slice summary is required")
            if "\n" in summary or len(summary) > MAX_SLICE_SUMMARY_CHARS:
                raise ValueError("slice summary must be one paragraph within the length limit")
            if "我" not in summary:
                raise ValueError("slice summary must use the profile user's first-person perspective")
            boundary_reason = str(item.get("boundary_reason") or "").strip()
            if boundary_reason not in self._BOUNDARY_REASONS:
                raise ValueError("invalid slice boundary_reason")
            event_time_start = item.get("event_time_start")
            event_time_end = item.get("event_time_end")
            parsed_event_times: list[datetime | None] = []
            for value in (event_time_start, event_time_end):
                if value in (None, ""):
                    parsed_event_times.append(None)
                    continue
                try:
                    parsed_event_times.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
                except ValueError as exc:
                    raise ValueError("slice event time must be ISO-8601") from exc
            if all(parsed_event_times):
                try:
                    if parsed_event_times[0] > parsed_event_times[1]:
                        raise ValueError("slice event time range is reversed")
                except TypeError as exc:
                    raise ValueError("slice event time timezone forms must match") from exc
            selected_messages = messages[positions[0]:positions[-1] + 1]
            source_normalized = [{
                "message_id": message["message_id"],
                "role": message["role"],
                "content": message["content"],
                "attachments": [
                    {
                        "attachment_id": attachment["attachment_id"],
                        "content_hash": attachment["content_hash"],
                    }
                    for attachment in message["attachments"]
                ],
            } for message in selected_messages]
            normalized.append({
                "sequence_no": sequence_no,
                "source_message_ids": ids,
                "source_content_hash": _sha256_json(source_normalized),
                "source_time_start": str(selected_messages[0]["created_at"]),
                "source_time_end": str(selected_messages[-1]["created_at"]),
                "event_time_start": event_time_start,
                "event_time_end": event_time_end,
                "summary": summary,
                "summary_content_hash": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
                "boundary_reason": boundary_reason,
            })
        if len(ignored_ids) != len(set(ignored_ids)) or any(value in seen for value in ignored_ids):
            raise ValueError("source coverage overlaps or repeats message ids")
        if set(ignored_ids) - set(source_ids):
            raise ValueError("ignored source message does not belong to this session/day")
        if not slices and not ignored_ids and not str(coverage.get("zero_slice_reason") or "").strip():
            raise ValueError("zero-slice batch requires a coverage reason")
        if seen | set(ignored_ids) != set(source_ids):
            raise ValueError("coverage must account for every source message")
        return normalized

    def create_candidate_batch(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        segmenter_version: str,
        slice_prompt_version: str,
        slices: list[dict[str, Any]],
        coverage: dict[str, Any],
        reslice_revision: int = 1,
        slice_schema_version: str = SLICE_SCHEMA_VERSION,
        daily_source_snapshot_hash: str | None = None,
        generator_provider: str = "",
        generator_model: str = "",
    ) -> dict[str, Any]:
        profile_id, persona_id, session_id, chat_day = self._validate_scope(
            profile_id, persona_id, session_id, chat_day
        )
        if int(reslice_revision) < 1:
            raise ValueError("reslice_revision must be positive")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = self._source_snapshot_with_conn(
                conn,
                profile_id=profile_id,
                persona_id=persona_id,
                session_id=session_id,
                chat_day=chat_day,
            )
            normalized_slices = self._validate_slices(
                snapshot=snapshot,
                slices=list(slices or []),
                coverage=dict(coverage or {}),
            )
            canonical_coverage = {
                "covered_message_ids": [
                    message_id
                    for item in normalized_slices
                    for message_id in item["source_message_ids"]
                ],
                "ignored_ranges": list((coverage or {}).get("ignored_ranges") or []),
            }
            zero_slice_reason = str((coverage or {}).get("zero_slice_reason") or "").strip()
            if zero_slice_reason:
                canonical_coverage["zero_slice_reason"] = zero_slice_reason
            source_hash = str(snapshot["session_source_snapshot_hash"])
            if int(reslice_revision) > 1:
                allocated = conn.execute(
                    """SELECT current_revision FROM conversation_slice_revisions
                       WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                         AND session_source_snapshot_hash = ? AND segmenter_version = ?
                         AND slice_prompt_version = ? AND slice_schema_version = ?""",
                    (
                        profile_id, persona_id, session_id, chat_day, source_hash,
                        segmenter_version, slice_prompt_version, slice_schema_version,
                    ),
                ).fetchone()
                if allocated is None or int(allocated["current_revision"]) < int(reslice_revision):
                    raise ValueError("reslice_revision was not allocated by CAS")
            idempotency_key = self.batch_idempotency_key(
                profile_id=profile_id,
                persona_id=persona_id,
                session_id=session_id,
                chat_day=chat_day,
                session_source_snapshot_hash=source_hash,
                segmenter_version=segmenter_version,
                slice_prompt_version=slice_prompt_version,
                slice_schema_version=slice_schema_version,
                reslice_revision=reslice_revision,
            )
            existing = conn.execute(
                "SELECT * FROM conversation_slice_batches WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_slices = conn.execute(
                    """SELECT source_message_ids_json, summary, boundary_reason,
                              event_time_start, event_time_end
                       FROM conversation_slices WHERE batch_id = ? ORDER BY sequence_no""",
                    (str(existing["batch_id"]),),
                ).fetchall()
                existing_payload = [{
                    "source_message_ids": json.loads(row["source_message_ids_json"]),
                    "summary": str(row["summary"]),
                    "boundary_reason": str(row["boundary_reason"]),
                    "event_time_start": row["event_time_start"],
                    "event_time_end": row["event_time_end"],
                } for row in existing_slices]
                requested_payload = [{
                    "source_message_ids": item["source_message_ids"],
                    "summary": item["summary"],
                    "boundary_reason": item["boundary_reason"],
                    "event_time_start": item["event_time_start"],
                    "event_time_end": item["event_time_end"],
                } for item in normalized_slices]
                if (
                    existing_payload != requested_payload
                    or str(existing["coverage_json"]) != _canonical_json(canonical_coverage)
                    or (existing["daily_source_snapshot_hash"] or None) != (daily_source_snapshot_hash or None)
                    or str(existing["generator_provider"] or "") != str(generator_provider or "")
                    or str(existing["generator_model"] or "") != str(generator_model or "")
                ):
                    raise ValueError("batch idempotency key was reused with different output")
                conn.commit()
                return self._batch_payload(existing, len(existing_slices))
            batch_id = "csb_" + uuid.uuid4().hex
            now = _now_iso()
            conn.execute(
                """INSERT INTO conversation_slice_batches
                   (batch_id, profile_id, persona_id, session_id, chat_day,
                    chat_day_rule_version, daily_source_snapshot_hash,
                    session_source_snapshot_hash, source_message_count,
                    segmenter_version, slice_prompt_version, slice_schema_version,
                    generator_provider, generator_model, reslice_revision,
                    idempotency_key, status, coverage_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""",
                (
                    batch_id, profile_id, persona_id, session_id, chat_day,
                    str(snapshot["chat_day_rule_version"]), daily_source_snapshot_hash,
                    source_hash, int(snapshot["source_message_count"]),
                    segmenter_version, slice_prompt_version, slice_schema_version,
                    generator_provider, generator_model, int(reslice_revision),
                    idempotency_key, _canonical_json(canonical_coverage), now,
                ),
            )
            revision_params = (
                profile_id, persona_id, session_id, chat_day, source_hash,
                segmenter_version, slice_prompt_version, slice_schema_version,
            )
            conn.execute(
                """INSERT INTO conversation_slice_revisions
                   (profile_id, persona_id, session_id, chat_day,
                    session_source_snapshot_hash, segmenter_version,
                    slice_prompt_version, slice_schema_version,
                    current_revision, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile_id, persona_id, session_id, chat_day,
                               session_source_snapshot_hash, segmenter_version,
                               slice_prompt_version, slice_schema_version)
                   DO UPDATE SET current_revision = MAX(current_revision, excluded.current_revision),
                                 updated_at = excluded.updated_at""",
                (*revision_params, int(reslice_revision), now),
            )
            for item in normalized_slices:
                ids = item["source_message_ids"]
                identity_hash = _sha256_json([
                    profile_id, persona_id, session_id, chat_day,
                    ids[0], ids[-1], item["source_content_hash"],
                    segmenter_version, slice_prompt_version, slice_schema_version,
                    int(reslice_revision),
                ])
                conn.execute(
                    """INSERT INTO conversation_slices
                       (slice_id, batch_id, sequence_no, profile_id, persona_id,
                        session_id, chat_day, source_start_message_id,
                        source_end_message_id, source_message_ids_json,
                        source_content_hash, source_time_start, source_time_end,
                        event_time_start, event_time_end, summary,
                        summary_content_hash, boundary_reason, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "csl_" + identity_hash, batch_id, item["sequence_no"],
                        profile_id, persona_id, session_id, chat_day,
                        ids[0], ids[-1], _canonical_json(ids),
                        item["source_content_hash"], item["source_time_start"],
                        item["source_time_end"], item["event_time_start"],
                        item["event_time_end"], item["summary"],
                        item["summary_content_hash"], item["boundary_reason"], now, now,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM conversation_slice_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return self._batch_payload(row, len(normalized_slices))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _batch_payload(row: sqlite3.Row, slice_count: int) -> dict[str, Any]:
        return {
            "batch_id": str(row["batch_id"]),
            "profile_id": str(row["profile_id"]),
            "persona_id": str(row["persona_id"]),
            "session_id": str(row["session_id"]),
            "chat_day": str(row["chat_day"]),
            "session_source_snapshot_hash": str(row["session_source_snapshot_hash"]),
            "source_message_count": int(row["source_message_count"]),
            "reslice_revision": int(row["reslice_revision"]),
            "idempotency_key": str(row["idempotency_key"]),
            "status": str(row["status"]),
            "slice_count": int(slice_count),
        }

    def activate_batch(self, batch_id: str) -> dict[str, Any]:
        safe_batch_id = str(batch_id or "").strip()
        if not safe_batch_id:
            raise ValueError("batch_id is required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                "SELECT * FROM conversation_slice_batches WHERE batch_id = ?",
                (safe_batch_id,),
            ).fetchone()
            if batch is None:
                raise ValueError("conversation slice batch not found")
            if str(batch["status"]) == "active":
                count = conn.execute(
                    "SELECT COUNT(*) FROM conversation_slices WHERE batch_id = ?",
                    (safe_batch_id,),
                ).fetchone()[0]
                conn.commit()
                return self._batch_payload(batch, int(count))
            if str(batch["status"]) != "candidate":
                raise ValueError("only candidate batch can be activated")
            snapshot = self._source_snapshot_with_conn(
                conn,
                profile_id=str(batch["profile_id"]),
                persona_id=str(batch["persona_id"]),
                session_id=str(batch["session_id"]),
                chat_day=str(batch["chat_day"]),
            )
            if str(snapshot["session_source_snapshot_hash"]) != str(batch["session_source_snapshot_hash"]):
                conn.execute(
                    """UPDATE conversation_slice_batches
                       SET status = 'failed', error_code = 'source_changed',
                           error_detail = 'source changed before activation'
                       WHERE batch_id = ?""",
                    (safe_batch_id,),
                )
                conn.execute(
                    """UPDATE conversation_slices SET lifecycle_status = 'source_changed', updated_at = ?
                       WHERE batch_id = ?""",
                    (_now_iso(), safe_batch_id),
                )
                conn.commit()
                raise ValueError("source changed before batch activation")
            scope = (
                str(batch["profile_id"]), str(batch["persona_id"]),
                str(batch["session_id"]), str(batch["chat_day"]),
            )
            previous = conn.execute(
                """SELECT batch_id FROM conversation_slice_batches
                   WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                     AND status = 'active'""",
                scope,
            ).fetchone()
            now = _now_iso()
            previous_id = str(previous["batch_id"]) if previous else None
            if previous_id:
                conn.execute(
                    """UPDATE conversation_slice_batches
                       SET status = 'superseded', superseded_at = ? WHERE batch_id = ?""",
                    (now, previous_id),
                )
                conn.execute(
                    """UPDATE conversation_slices
                       SET lifecycle_status = 'superseded', updated_at = ? WHERE batch_id = ?""",
                    (now, previous_id),
                )
            conn.execute(
                """UPDATE conversation_slice_batches
                   SET status = 'active', supersedes_batch_id = ?, activated_at = ?,
                       error_code = NULL, error_detail = NULL
                   WHERE batch_id = ?""",
                (previous_id, now, safe_batch_id),
            )
            conn.execute(
                """UPDATE conversation_slices
                   SET lifecycle_status = 'active', updated_at = ? WHERE batch_id = ?""",
                (now, safe_batch_id),
            )
            conn.commit()
            active = conn.execute(
                "SELECT * FROM conversation_slice_batches WHERE batch_id = ?",
                (safe_batch_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM conversation_slices WHERE batch_id = ?",
                (safe_batch_id,),
            ).fetchone()[0]
            return self._batch_payload(active, int(count))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def refresh_source_validity(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
    ) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = self._source_snapshot_with_conn(
                conn,
                profile_id=profile_id,
                persona_id=persona_id,
                session_id=session_id,
                chat_day=chat_day,
            )
            current_hash = str(snapshot["session_source_snapshot_hash"])
            active = conn.execute(
                """SELECT batch_id, session_source_snapshot_hash
                   FROM conversation_slice_batches
                   WHERE profile_id = ? AND persona_id = ? AND session_id = ? AND chat_day = ?
                     AND status = 'active'""",
                (profile_id, persona_id, session_id, chat_day),
            ).fetchone()
            valid = active is None or str(active["session_source_snapshot_hash"]) == current_hash
            if active is not None and not valid:
                conn.execute(
                    """UPDATE conversation_slices
                       SET lifecycle_status = 'source_changed', updated_at = ? WHERE batch_id = ?""",
                    (_now_iso(), str(active["batch_id"])),
                )
            conn.commit()
            return valid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_active_slices(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
    ) -> list[dict[str, Any]]:
        session_conn = self._connect()
        session_exists = session_conn.execute(
            """SELECT 1 FROM conversation_sessions
               WHERE profile_id = ? AND session_id = ?""",
            (profile_id, session_id),
        ).fetchone() is not None
        session_conn.close()
        if not session_exists:
            return []
        self.refresh_source_validity(
            profile_id=profile_id,
            persona_id=persona_id,
            session_id=session_id,
            chat_day=chat_day,
        )
        conn = self._connect()
        rows = conn.execute(
            """SELECT slices.*, sessions.deleted_at
               FROM conversation_slices slices
               JOIN conversation_slice_batches batches ON batches.batch_id = slices.batch_id
               JOIN conversation_sessions sessions
                 ON sessions.profile_id = slices.profile_id AND sessions.session_id = slices.session_id
               WHERE slices.profile_id = ? AND slices.persona_id = ?
                 AND slices.session_id = ? AND slices.chat_day = ?
                 AND batches.status = 'active'
                 AND slices.lifecycle_status = 'active'
                 AND slices.review_status != 'rejected'
                 AND sessions.deleted_at IS NULL
               ORDER BY slices.sequence_no ASC""",
            (profile_id, persona_id, session_id, chat_day),
        ).fetchall()
        conn.close()
        return [{
            "slice_id": str(row["slice_id"]),
            "batch_id": str(row["batch_id"]),
            "sequence_no": int(row["sequence_no"]),
            "summary": str(row["summary"]),
            "source_message_ids": json.loads(row["source_message_ids_json"]),
            "review_status": str(row["review_status"]),
            "lifecycle_status": str(row["lifecycle_status"]),
        } for row in rows]

    def create_task(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        trigger_type: str,
        batch_idempotency_key: str,
        reslice_revision: int,
        message_count: int = 0,
        estimated_input_tokens: int = 0,
        estimated_call_count: int = 1,
    ) -> dict[str, Any]:
        if not str(trigger_type or "").strip() or not str(batch_idempotency_key or "").strip():
            raise ValueError("trigger_type and batch_idempotency_key are required")
        task_key = self.task_idempotency_key(
            trigger_type=str(trigger_type or "").strip(),
            batch_idempotency_key=str(batch_idempotency_key or "").strip(),
        )
        now = _now_iso()
        task_id = "cst_" + task_key
        conn = self._connect()
        conn.execute(
            """INSERT OR IGNORE INTO conversation_slice_tasks
               (task_id, profile_id, persona_id, session_id, chat_day,
                trigger_type, idempotency_key, status, reslice_revision,
                message_count, estimated_input_tokens, estimated_call_count,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (
                task_id, profile_id, persona_id, session_id, chat_day,
                trigger_type, task_key, int(reslice_revision),
                max(0, int(message_count)), max(0, int(estimated_input_tokens)),
                max(1, int(estimated_call_count)), now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM conversation_slice_tasks WHERE idempotency_key = ?",
            (task_key,),
        ).fetchone()
        conn.close()
        return self._task_payload(row)

    @staticmethod
    def _task_payload(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "task_id": str(row["task_id"]),
            "profile_id": str(row["profile_id"]),
            "persona_id": str(row["persona_id"]),
            "session_id": str(row["session_id"]),
            "chat_day": str(row["chat_day"]),
            "trigger_type": str(row["trigger_type"]),
            "idempotency_key": str(row["idempotency_key"]),
            "status": str(row["status"]),
            "reslice_revision": int(row["reslice_revision"]),
            "batch_id": str(row["batch_id"] or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "message_count": int(row["message_count"] or 0) if "message_count" in keys else 0,
            "estimated_input_tokens": (
                int(row["estimated_input_tokens"] or 0)
                if "estimated_input_tokens" in keys else 0
            ),
            "estimated_call_count": (
                int(row["estimated_call_count"] or 1)
                if "estimated_call_count" in keys else 1
            ),
            "error_code": str(row["error_code"] or ""),
            "error_detail": str(row["error_detail"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def list_tasks(
        self,
        *,
        profile_id: str,
        persona_id: str = "",
        session_id: str = "",
        chat_day: str = "",
        statuses: tuple[str, ...] = (),
        trigger_types: tuple[str, ...] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["profile_id = ?"]
        params: list[Any] = [str(profile_id or "default")]
        for column, value in (("persona_id", persona_id), ("session_id", session_id), ("chat_day", chat_day)):
            if str(value or "").strip():
                clauses.append(f"{column} = ?")
                params.append(str(value).strip())
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if trigger_types:
            clauses.append("trigger_type IN (" + ",".join("?" for _ in trigger_types) + ")")
            params.extend(trigger_types)
        params.append(max(1, min(1000, int(limit or 200))))
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM conversation_slice_tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
        return [self._task_payload(row) for row in rows]

    def get_task(self, *, profile_id: str, task_id: str) -> dict[str, Any]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM conversation_slice_tasks WHERE profile_id = ? AND task_id = ?",
            (str(profile_id or "default"), str(task_id or "")),
        ).fetchone()
        conn.close()
        return self._task_payload(row) if row is not None else {}

    def set_task_status(
        self,
        task_id: str,
        *,
        profile_id: str = "",
        status: str,
        batch_id: str = "",
        error_code: str = "",
        error_detail: str = "",
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        safe_status = str(status or "").strip()
        allowed = {"queued", "running", "paused", "completed", "failed"}
        if safe_status not in allowed:
            raise ValueError("invalid conversation slice task status")
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            raise ValueError("task_id is required")
        now = _now_iso()
        timestamp_column = {
            "running": "started_at",
            "paused": "paused_at",
            "completed": "completed_at",
            "failed": "failed_at",
        }.get(safe_status)
        assignments = ["status = ?", "updated_at = ?", "error_code = ?", "error_detail = ?"]
        params: list[Any] = [safe_status, now, str(error_code or ""), str(error_detail or "")[:2000]]
        if batch_id:
            assignments.append("batch_id = ?")
            params.append(str(batch_id))
        if increment_attempt:
            assignments.append("attempt_count = attempt_count + 1")
        if timestamp_column:
            assignments.append(f"{timestamp_column} = ?")
            params.append(now)
        where = "task_id = ?"
        params.append(safe_task_id)
        if str(profile_id or "").strip():
            where += " AND profile_id = ?"
            params.append(str(profile_id).strip())
        conn = self._connect()
        cursor = conn.execute(
            f"UPDATE conversation_slice_tasks SET {', '.join(assignments)} WHERE {where}",
            params,
        )
        if int(cursor.rowcount or 0) != 1:
            conn.rollback()
            conn.close()
            raise ValueError("conversation slice task not found")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM conversation_slice_tasks WHERE task_id = ?", (safe_task_id,)
        ).fetchone()
        conn.close()
        return self._task_payload(row)

    def claim_task(self, *, profile_id: str, task_id: str) -> dict[str, Any]:
        """Atomically move one queued task to running; concurrent callers get no claim."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _now_iso()
            cursor = conn.execute(
                """UPDATE conversation_slice_tasks
                   SET status = 'running', attempt_count = attempt_count + 1,
                       started_at = ?, updated_at = ?, error_code = '', error_detail = ''
                   WHERE profile_id = ? AND task_id = ? AND status = 'queued'""",
                (now, now, str(profile_id or "default"), str(task_id or "")),
            )
            row = conn.execute(
                "SELECT * FROM conversation_slice_tasks WHERE profile_id = ? AND task_id = ?",
                (str(profile_id or "default"), str(task_id or "")),
            ).fetchone()
            conn.commit()
            if row is None:
                raise ValueError("conversation slice task not found")
            return self._task_payload(row) if int(cursor.rowcount or 0) == 1 else {}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_inspection_days(
        self, *, profile_id: str, persona_id: str, limit: int = 366,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        day_rows = conn.execute(
            """SELECT chat_day FROM (
                    SELECT chat_day FROM conversation_turns turns
                    JOIN conversation_sessions sessions
                      ON sessions.profile_id = turns.profile_id AND sessions.session_id = turns.session_id
                    WHERE turns.profile_id = ? AND sessions.persona_id = ? AND turns.chat_day != ''
                    UNION
                    SELECT chat_day FROM conversation_slice_batches WHERE profile_id = ? AND persona_id = ?
                    UNION
                    SELECT chat_day FROM conversation_slice_tasks WHERE profile_id = ? AND persona_id = ?
               ) days ORDER BY chat_day DESC LIMIT ?""",
            (
                profile_id, persona_id, profile_id, persona_id, profile_id, persona_id,
                max(1, min(3660, int(limit or 366))),
            ),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for day_row in day_rows:
            chat_day = str(day_row["chat_day"])
            slice_stats = conn.execute(
                """SELECT COUNT(*) AS slice_count,
                          SUM(CASE WHEN slices.review_status = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed_count,
                          GROUP_CONCAT(DISTINCT batches.slice_prompt_version) AS prompt_versions
                   FROM conversation_slice_batches batches
                   JOIN conversation_slices slices ON slices.batch_id = batches.batch_id
                   WHERE batches.profile_id = ? AND batches.persona_id = ? AND batches.chat_day = ?
                     AND batches.status = 'active' AND slices.lifecycle_status = 'active'""",
                (profile_id, persona_id, chat_day),
            ).fetchone()
            task_stats = conn.execute(
                """SELECT SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                          GROUP_CONCAT(DISTINCT status) AS task_statuses
                   FROM conversation_slice_tasks
                   WHERE profile_id = ? AND persona_id = ? AND chat_day = ?""",
                (profile_id, persona_id, chat_day),
            ).fetchone()
            invalid_count = conn.execute(
                """SELECT COUNT(*) FROM conversation_slices
                   WHERE profile_id = ? AND persona_id = ? AND chat_day = ?
                     AND lifecycle_status = 'source_changed'""",
                (profile_id, persona_id, chat_day),
            ).fetchone()[0]
            output.append({
                "chat_day": chat_day,
                "slice_count": int(slice_stats["slice_count"] or 0),
                "unreviewed_count": int(slice_stats["unreviewed_count"] or 0),
                "issue_count": int(task_stats["failed_count"] or 0) + int(invalid_count or 0),
                "prompt_versions": [value for value in str(slice_stats["prompt_versions"] or "").split(",") if value],
                "task_statuses": [value for value in str(task_stats["task_statuses"] or "").split(",") if value],
            })
        conn.close()
        return output

    def get_inspection_day(
        self, *, profile_id: str, persona_id: str, chat_day: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        batch_rows = conn.execute(
            """SELECT batches.*, COALESCE(sessions.title, '') AS session_title
               FROM conversation_slice_batches batches
               LEFT JOIN conversation_sessions sessions
                 ON sessions.profile_id = batches.profile_id AND sessions.session_id = batches.session_id
               WHERE batches.profile_id = ? AND batches.persona_id = ? AND batches.chat_day = ?
                 AND batches.status = 'active'
               ORDER BY batches.session_id""",
            (profile_id, persona_id, chat_day),
        ).fetchall()
        sessions: list[dict[str, Any]] = []
        for batch in batch_rows:
            slice_rows = conn.execute(
                "SELECT * FROM conversation_slices WHERE batch_id = ? ORDER BY sequence_no",
                (str(batch["batch_id"]),),
            ).fetchall()
            sessions.append({
                "session_id": str(batch["session_id"]),
                "session_title": str(batch["session_title"] or ""),
                "batch_id": str(batch["batch_id"]),
                "batch_status": str(batch["status"]),
                "source_message_count": int(batch["source_message_count"] or 0),
                "segmenter_version": str(batch["segmenter_version"]),
                "slice_prompt_version": str(batch["slice_prompt_version"]),
                "slice_schema_version": str(batch["slice_schema_version"]),
                "generator_provider": str(batch["generator_provider"] or ""),
                "generator_model": str(batch["generator_model"] or ""),
                "reslice_revision": int(batch["reslice_revision"] or 1),
                "coverage": json.loads(str(batch["coverage_json"] or "{}")),
                "slices": [{
                    "slice_id": str(row["slice_id"]),
                    "sequence_no": int(row["sequence_no"]),
                    "summary": str(row["summary"]),
                    "source_message_ids": json.loads(str(row["source_message_ids_json"])),
                    "source_time_start": str(row["source_time_start"] or ""),
                    "source_time_end": str(row["source_time_end"] or ""),
                    "event_time_start": row["event_time_start"],
                    "event_time_end": row["event_time_end"],
                    "boundary_reason": str(row["boundary_reason"]),
                    "lifecycle_status": str(row["lifecycle_status"]),
                    "review_status": str(row["review_status"]),
                    "review_reason": str(row["review_reason"] or ""),
                    "review_note": str(row["review_note"] or ""),
                    "reviewed_at": str(row["reviewed_at"] or ""),
                } for row in slice_rows],
            })
        tasks = conn.execute(
            """SELECT * FROM conversation_slice_tasks
               WHERE profile_id = ? AND persona_id = ? AND chat_day = ?
               ORDER BY created_at DESC""",
            (profile_id, persona_id, chat_day),
        ).fetchall()
        conn.close()
        return {
            "chat_day": chat_day,
            "sessions": sessions,
            "tasks": [self._task_payload(row) for row in tasks],
        }

    def get_slice_source(
        self, *, profile_id: str, slice_id: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        row = conn.execute(
            """SELECT slices.*, batches.persona_id
               FROM conversation_slices slices
               JOIN conversation_slice_batches batches ON batches.batch_id = slices.batch_id
               WHERE slices.profile_id = ? AND slices.slice_id = ?""",
            (profile_id, slice_id),
        ).fetchone()
        if row is None:
            conn.close()
            raise ValueError("conversation slice not found")
        wanted = json.loads(str(row["source_message_ids_json"] or "[]"))
        snapshot = self._source_snapshot_with_conn(
            conn,
            profile_id=profile_id,
            persona_id=str(row["persona_id"]),
            session_id=str(row["session_id"]),
            chat_day=str(row["chat_day"]),
        )
        by_id = {str(item["message_id"]): item for item in snapshot["messages"]}
        messages = [by_id[message_id] for message_id in wanted if message_id in by_id]
        conn.close()
        return {
            "slice_id": slice_id,
            "session_id": str(row["session_id"]),
            "chat_day": str(row["chat_day"]),
            "messages": messages,
        }

    def review_slice(
        self,
        *,
        profile_id: str,
        slice_id: str,
        review_status: str,
        review_reason: str = "",
        review_note: str = "",
    ) -> dict[str, Any]:
        status = str(review_status or "").strip()
        if status not in {"approved", "rejected"}:
            raise ValueError("review_status must be approved or rejected")
        reason = str(review_reason or "").strip()
        allowed_reasons = {"fabricated", "stiff", "emotion", "missing", "boundary", "duplicate", "other"}
        if status == "rejected" and reason not in allowed_reasons:
            raise ValueError("a valid review_reason is required for rejected slices")
        now = _now_iso()
        conn = self._connect()
        cursor = conn.execute(
            """UPDATE conversation_slices
               SET review_status = ?, review_reason = ?, review_note = ?, reviewed_at = ?, updated_at = ?
               WHERE profile_id = ? AND slice_id = ?""",
            (status, reason if status == "rejected" else "", str(review_note or "")[:1000], now, now, profile_id, slice_id),
        )
        if int(cursor.rowcount or 0) != 1:
            conn.rollback()
            conn.close()
            raise ValueError("conversation slice not found")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM conversation_slices WHERE profile_id = ? AND slice_id = ?",
            (profile_id, slice_id),
        ).fetchone()
        conn.close()
        return {
            "slice_id": str(row["slice_id"]),
            "review_status": str(row["review_status"]),
            "review_reason": str(row["review_reason"] or ""),
            "review_note": str(row["review_note"] or ""),
            "reviewed_at": str(row["reviewed_at"] or ""),
        }
