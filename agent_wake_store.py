"""Persistent control plane for CC agent wake schedules and runs."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


CACHE_STATES = {"unarmed", "warm", "cooling", "cold"}
RUN_STATUSES = {"claimed", "running", "completed", "deferred", "failed", "superseded"}
TERMINAL_RUN_STATUSES = {"completed", "deferred", "failed", "superseded"}
MAX_CONSECUTIVE_FAILURES = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None, *, allow_empty: bool = True) -> str:
    if value is None or value == "":
        if allow_empty:
            return ""
        raise ValueError("timestamp is required")
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            if allow_empty:
                return ""
            raise ValueError("timestamp is required")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be RFC 3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def initialize_agent_wake_schema(conn: sqlite3.Connection) -> None:
    """Create or idempotently upgrade the agent wake tables in gateway_state.db."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_wake_schedules (
            profile_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            keepalive_enabled INTEGER NOT NULL DEFAULT 0,
            keepalive_paused_until_user INTEGER NOT NULL DEFAULT 0,
            agent_wake_enabled INTEGER NOT NULL DEFAULT 0,
            conversation_silence_enabled INTEGER NOT NULL DEFAULT 0,
            last_user_activity_at TEXT NOT NULL DEFAULT '',
            last_model_activity_at TEXT NOT NULL DEFAULT '',
            last_cache_refresh_at TEXT NOT NULL DEFAULT '',
            last_heartbeat_at TEXT NOT NULL DEFAULT '',
            next_agent_wake_at TEXT NOT NULL DEFAULT '',
            wake_reason TEXT NOT NULL DEFAULT '',
            conversation_silence_check_at TEXT NOT NULL DEFAULT '',
            silence_source_turn_id INTEGER NOT NULL DEFAULT 0,
            silence_policy_version TEXT NOT NULL DEFAULT '',
            cache_keepalive_deadline TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL DEFAULT '',
            cache_state TEXT NOT NULL DEFAULT 'unarmed',
            schedule_version INTEGER NOT NULL DEFAULT 1,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            retry_at TEXT NOT NULL DEFAULT '',
            background_turn_limit INTEGER NOT NULL DEFAULT 48,
            agent_wake_min_minutes INTEGER NOT NULL DEFAULT 10,
            silence_min_minutes INTEGER NOT NULL DEFAULT 8,
            silence_max_minutes INTEGER NOT NULL DEFAULT 25,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            gc_eligible_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, session_id, lane_id)
        )
        """
    )
    _ensure_columns(
        conn,
        "agent_wake_schedules",
        {
            "profile_id": "TEXT NOT NULL DEFAULT ''",
            "session_id": "TEXT NOT NULL DEFAULT ''",
            "lane_id": "TEXT NOT NULL DEFAULT ''",
            "keepalive_enabled": "INTEGER NOT NULL DEFAULT 0",
            "keepalive_paused_until_user": "INTEGER NOT NULL DEFAULT 0",
            "agent_wake_enabled": "INTEGER NOT NULL DEFAULT 0",
            "conversation_silence_enabled": "INTEGER NOT NULL DEFAULT 0",
            "last_user_activity_at": "TEXT NOT NULL DEFAULT ''",
            "last_model_activity_at": "TEXT NOT NULL DEFAULT ''",
            "last_cache_refresh_at": "TEXT NOT NULL DEFAULT ''",
            "last_heartbeat_at": "TEXT NOT NULL DEFAULT ''",
            "next_agent_wake_at": "TEXT NOT NULL DEFAULT ''",
            "wake_reason": "TEXT NOT NULL DEFAULT ''",
            "conversation_silence_check_at": "TEXT NOT NULL DEFAULT ''",
            "silence_source_turn_id": "INTEGER NOT NULL DEFAULT 0",
            "silence_policy_version": "TEXT NOT NULL DEFAULT ''",
            "cache_keepalive_deadline": "TEXT NOT NULL DEFAULT ''",
            "due_at": "TEXT NOT NULL DEFAULT ''",
            "cache_state": "TEXT NOT NULL DEFAULT 'unarmed'",
            "schedule_version": "INTEGER NOT NULL DEFAULT 1",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "lease_until": "TEXT NOT NULL DEFAULT ''",
            "retry_at": "TEXT NOT NULL DEFAULT ''",
            "background_turn_limit": "INTEGER NOT NULL DEFAULT 48",
            "agent_wake_min_minutes": "INTEGER NOT NULL DEFAULT 10",
            "silence_min_minutes": "INTEGER NOT NULL DEFAULT 8",
            "silence_max_minutes": "INTEGER NOT NULL DEFAULT 25",
            "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "gc_eligible_at": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        },
    )
    # The switch was introduced after silence timers already existed. Defaulting the
    # new column off must also invalidate those persisted callbacks immediately.
    stale_silence_rows = conn.execute(
        """SELECT * FROM agent_wake_schedules
           WHERE conversation_silence_enabled = 0
             AND conversation_silence_check_at != ''"""
    ).fetchall()
    for row in stale_silence_rows:
        values = dict(row)
        values["conversation_silence_check_at"] = ""
        candidates: list[str] = []
        if (
            bool(values.get("keepalive_enabled"))
            and not bool(values.get("keepalive_paused_until_user"))
            and str(values.get("cache_keepalive_deadline") or "")
        ):
            candidates.append(str(values["cache_keepalive_deadline"]))
        if bool(values.get("agent_wake_enabled")) and str(values.get("next_agent_wake_at") or ""):
            candidates.append(str(values["next_agent_wake_at"]))
        conn.execute(
            """UPDATE agent_wake_schedules
               SET conversation_silence_check_at = '', silence_source_turn_id = 0,
                   silence_policy_version = '', due_at = ?,
                   schedule_version = schedule_version + 1,
                   lease_owner = '', lease_until = '', updated_at = ?
               WHERE profile_id = ? AND session_id = ? AND lane_id = ?""",
            (
                min(candidates) if candidates else "",
                _iso(_utc_now(), allow_empty=False),
                row["profile_id"], row["session_id"], row["lane_id"],
            ),
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_wake_schedule_scope
        ON agent_wake_schedules (profile_id, session_id, lane_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_wake_schedules_due
        ON agent_wake_schedules (due_at, lease_until)
        WHERE due_at != ''
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_wake_runs (
            wake_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            schedule_version INTEGER NOT NULL,
            cause TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed',
            lease_owner TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            turn_id INTEGER,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "agent_wake_runs",
        {
            "wake_id": "TEXT NOT NULL DEFAULT ''",
            "profile_id": "TEXT NOT NULL DEFAULT ''",
            "session_id": "TEXT NOT NULL DEFAULT ''",
            "lane_id": "TEXT NOT NULL DEFAULT ''",
            "schedule_version": "INTEGER NOT NULL DEFAULT 0",
            "cause": "TEXT NOT NULL DEFAULT ''",
            "due_at": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'claimed'",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "completed_at": "TEXT NOT NULL DEFAULT ''",
            "turn_id": "INTEGER",
            "error": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        },
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_wake_runs_wake_id
        ON agent_wake_runs (wake_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_wake_runs_scope
        ON agent_wake_runs (profile_id, session_id, lane_id, created_at DESC)
        """
    )


class AgentWakeConflictError(RuntimeError):
    def __init__(self, expected_version: int, actual_version: int):
        super().__init__(
            f"agent wake schedule version conflict: expected {expected_version}, "
            f"actual {actual_version}"
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class AgentWakeStore:
    """Session/lane-scoped schedule and idempotent run persistence."""

    _BOOLEAN_FIELDS = {
        "keepalive_enabled",
        "keepalive_paused_until_user",
        "agent_wake_enabled",
        "conversation_silence_enabled",
    }
    _TIMESTAMP_FIELDS = {
        "last_user_activity_at",
        "last_model_activity_at",
        "last_cache_refresh_at",
        "last_heartbeat_at",
        "next_agent_wake_at",
        "conversation_silence_check_at",
        "cache_keepalive_deadline",
        "retry_at",
        "gc_eligible_at",
    }
    _WRITABLE_FIELDS = _BOOLEAN_FIELDS | _TIMESTAMP_FIELDS | {
        "wake_reason",
        "silence_source_turn_id",
        "silence_policy_version",
        "cache_state",
        "background_turn_limit",
        "agent_wake_min_minutes",
        "silence_min_minutes",
        "silence_max_minutes",
        "consecutive_failures",
        "last_error",
    }

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            initialize_agent_wake_schema(conn)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _scope(profile_id: str, session_id: str, lane_id: str) -> tuple[str, str, str]:
        profile = str(profile_id or "default").strip() or "default"
        session = str(session_id or "").strip()
        lane = str(lane_id or "").strip()
        if not session or not lane:
            raise ValueError("session_id and lane_id are required")
        return profile, session, lane

    @staticmethod
    def _computed_due_at(values: dict[str, Any]) -> str:
        candidates: list[str] = []
        if (
            bool(values.get("keepalive_enabled"))
            and not bool(values.get("keepalive_paused_until_user"))
            and str(values.get("cache_keepalive_deadline") or "")
        ):
            candidates.append(str(values["cache_keepalive_deadline"]))
        if bool(values.get("agent_wake_enabled")) and str(values.get("next_agent_wake_at") or ""):
            candidates.append(str(values["next_agent_wake_at"]))
        if (
            bool(values.get("conversation_silence_enabled"))
            and str(values.get("conversation_silence_check_at") or "")
        ):
            candidates.append(str(values["conversation_silence_check_at"]))
        return min(candidates) if candidates else ""

    @staticmethod
    def _schedule_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        for key in AgentWakeStore._BOOLEAN_FIELDS:
            payload[key] = bool(payload.get(key))
        payload["schedule_version"] = int(payload.get("schedule_version") or 0)
        payload["background_turn_limit"] = int(payload.get("background_turn_limit") or 0)
        payload["agent_wake_min_minutes"] = int(payload.get("agent_wake_min_minutes") or 10)
        payload["silence_min_minutes"] = int(payload.get("silence_min_minutes") or 8)
        payload["silence_max_minutes"] = int(payload.get("silence_max_minutes") or 25)
        payload["silence_source_turn_id"] = int(payload.get("silence_source_turn_id") or 0)
        payload["consecutive_failures"] = int(payload.get("consecutive_failures") or 0)
        return payload

    @staticmethod
    def _run_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        payload["schedule_version"] = int(payload.get("schedule_version") or 0)
        payload["turn_id"] = int(payload["turn_id"]) if payload.get("turn_id") is not None else None
        return payload

    def create_schedule(
        self,
        *,
        profile_id: str,
        session_id: str,
        lane_id: str,
        **initial: Any,
    ) -> tuple[dict[str, Any], bool]:
        profile, session, lane = self._scope(profile_id, session_id, lane_id)
        unknown = set(initial) - self._WRITABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported schedule fields: {', '.join(sorted(unknown))}")
        now = _iso(_utc_now(), allow_empty=False)
        defaults: dict[str, Any] = {
            "keepalive_enabled": False,
            "keepalive_paused_until_user": False,
            "agent_wake_enabled": False,
            "conversation_silence_enabled": False,
            "last_user_activity_at": "",
            "last_model_activity_at": "",
            "last_cache_refresh_at": "",
            "last_heartbeat_at": "",
            "next_agent_wake_at": "",
            "wake_reason": "",
            "conversation_silence_check_at": "",
            "silence_source_turn_id": 0,
            "silence_policy_version": "",
            "cache_keepalive_deadline": "",
            "cache_state": "unarmed",
            "background_turn_limit": 48,
            "agent_wake_min_minutes": 10,
            "silence_min_minutes": 8,
            "silence_max_minutes": 25,
            "consecutive_failures": 0,
            "last_error": "",
            "retry_at": "",
            "gc_eligible_at": "",
        }
        defaults.update(self._normalized_changes(initial))
        if not bool(defaults.get("conversation_silence_enabled")):
            defaults["conversation_silence_check_at"] = ""
            defaults["silence_source_turn_id"] = 0
            defaults["silence_policy_version"] = ""
        due_at = self._computed_due_at(defaults)
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_wake_schedules
                    (profile_id, session_id, lane_id, keepalive_enabled,
                     keepalive_paused_until_user, agent_wake_enabled, conversation_silence_enabled,
                     last_user_activity_at, last_model_activity_at,
                     last_cache_refresh_at, last_heartbeat_at, next_agent_wake_at,
                     wake_reason, conversation_silence_check_at,
                     silence_source_turn_id, silence_policy_version,
                     cache_keepalive_deadline, due_at, cache_state,
                     schedule_version, lease_owner, lease_until, retry_at,
                     background_turn_limit, agent_wake_min_minutes,
                     silence_min_minutes, silence_max_minutes,
                     consecutive_failures, last_error,
                     gc_eligible_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile, session, lane,
                        int(defaults["keepalive_enabled"]),
                        int(defaults["keepalive_paused_until_user"]),
                        int(defaults["agent_wake_enabled"]),
                        int(defaults["conversation_silence_enabled"]),
                        defaults["last_user_activity_at"],
                        defaults["last_model_activity_at"],
                        defaults["last_cache_refresh_at"],
                        defaults["last_heartbeat_at"],
                        defaults["next_agent_wake_at"], defaults["wake_reason"],
                        defaults["conversation_silence_check_at"],
                        defaults["silence_source_turn_id"],
                        defaults["silence_policy_version"],
                        defaults["cache_keepalive_deadline"], due_at,
                        defaults["cache_state"], defaults["retry_at"], defaults["background_turn_limit"],
                        defaults["agent_wake_min_minutes"],
                        defaults["silence_min_minutes"], defaults["silence_max_minutes"],
                        defaults["consecutive_failures"], defaults["last_error"],
                        defaults["gc_eligible_at"], now, now,
                    ),
                )
            return self.get_schedule(
                profile_id=profile, session_id=session, lane_id=lane
            ), cursor.rowcount == 1
        finally:
            conn.close()

    def get_schedule(self, *, profile_id: str, session_id: str, lane_id: str) -> dict[str, Any]:
        profile, session, lane = self._scope(profile_id, session_id, lane_id)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                """,
                (profile, session, lane),
            ).fetchone()
            return self._schedule_payload(row)
        finally:
            conn.close()

    def list_schedules(
        self, *, profile_id: str, session_id: str = ""
    ) -> list[dict[str, Any]]:
        profile = str(profile_id or "default").strip() or "default"
        session = str(session_id or "").strip()
        conn = self._connect()
        try:
            if session:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_wake_schedules
                    WHERE profile_id = ? AND session_id = ?
                    ORDER BY lane_id
                    """,
                    (profile, session),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_wake_schedules
                    WHERE profile_id = ?
                    ORDER BY session_id, lane_id
                    """,
                    (profile,),
                ).fetchall()
            return [self._schedule_payload(row) for row in rows]
        finally:
            conn.close()

    def _normalized_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key in self._BOOLEAN_FIELDS:
                normalized[key] = bool(value)
            elif key in self._TIMESTAMP_FIELDS:
                normalized[key] = _iso(value)
            elif key == "cache_state":
                state = str(value or "").strip()
                if state not in CACHE_STATES:
                    raise ValueError("invalid cache_state")
                normalized[key] = state
            elif key == "wake_reason":
                reason = str(value or "").strip()
                if len(reason.encode("utf-8")) > 90:
                    raise ValueError("wake_reason exceeds 30 Chinese characters or equivalent")
                normalized[key] = reason
            elif key in {
                "background_turn_limit", "consecutive_failures", "silence_source_turn_id",
                "agent_wake_min_minutes", "silence_min_minutes", "silence_max_minutes",
            }:
                number = int(value)
                if number < 0:
                    raise ValueError(f"{key} cannot be negative")
                if key == "agent_wake_min_minutes" and not 1 <= number <= 7 * 24 * 60:
                    raise ValueError("agent_wake_min_minutes must be between 1 and 10080")
                if key in {"silence_min_minutes", "silence_max_minutes"} and not 1 <= number <= 24 * 60:
                    raise ValueError(f"{key} must be between 1 and 1440")
                normalized[key] = number
            elif key == "silence_policy_version":
                normalized[key] = str(value or "").strip()[:80]
            elif key == "last_error":
                normalized[key] = str(value or "")[:1000]
        return normalized

    def update_schedule(
        self,
        *,
        profile_id: str,
        session_id: str,
        lane_id: str,
        expected_version: int,
        **changes: Any,
    ) -> dict[str, Any]:
        profile, session, lane = self._scope(profile_id, session_id, lane_id)
        unknown = set(changes) - self._WRITABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported schedule fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_schedule(profile_id=profile, session_id=session, lane_id=lane)
        normalized = self._normalized_changes(changes)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                """,
                (profile, session, lane),
            ).fetchone()
            if row is None:
                raise KeyError("agent wake schedule not found")
            actual_version = int(row["schedule_version"] or 0)
            if actual_version != int(expected_version):
                raise AgentWakeConflictError(int(expected_version), actual_version)
            values = dict(row)
            values.update(normalized)
            if not bool(values.get("conversation_silence_enabled")):
                values["conversation_silence_check_at"] = ""
                values["silence_source_turn_id"] = 0
                values["silence_policy_version"] = ""
                normalized.update({
                    "conversation_silence_check_at": "",
                    "silence_source_turn_id": 0,
                    "silence_policy_version": "",
                })
            values["due_at"] = self._computed_due_at(values)
            assignments = [f"{key} = ?" for key in normalized]
            params = [int(value) if key in self._BOOLEAN_FIELDS else value for key, value in normalized.items()]
            assignments.extend(
                [
                    "due_at = ?",
                    "schedule_version = schedule_version + 1",
                    "lease_owner = ''",
                    "lease_until = ''",
                    "updated_at = ?",
                ]
            )
            params.extend([values["due_at"], _iso(_utc_now(), allow_empty=False)])
            params.extend([profile, session, lane, actual_version])
            cursor = conn.execute(
                f"""
                UPDATE agent_wake_schedules SET {', '.join(assignments)}
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                  AND schedule_version = ?
                """,
                params,
            )
            if cursor.rowcount != 1:
                latest = conn.execute(
                    """
                    SELECT schedule_version FROM agent_wake_schedules
                    WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                    """,
                    (profile, session, lane),
                ).fetchone()
                raise AgentWakeConflictError(
                    int(expected_version), int(latest["schedule_version"] if latest else 0)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_schedule(profile_id=profile, session_id=session, lane_id=lane)

    def delete_schedule(
        self,
        *,
        profile_id: str,
        session_id: str,
        lane_id: str,
        expected_version: int,
    ) -> bool:
        profile, session, lane = self._scope(profile_id, session_id, lane_id)
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM agent_wake_schedules
                    WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                      AND schedule_version = ?
                    """,
                    (profile, session, lane, int(expected_version)),
                )
            if cursor.rowcount == 1:
                return True
            row = conn.execute(
                """
                SELECT schedule_version FROM agent_wake_schedules
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                """,
                (profile, session, lane),
            ).fetchone()
            if row is None:
                return False
            raise AgentWakeConflictError(int(expected_version), int(row["schedule_version"]))
        finally:
            conn.close()

    @staticmethod
    def _cause(row: sqlite3.Row) -> str:
        due_at = str(row["due_at"] or "")
        if str(row["conversation_silence_check_at"] or "") == due_at:
            return "conversation_silence"
        if str(row["next_agent_wake_at"] or "") == due_at:
            return "agent_schedule"
        return "cache_keepalive"

    def claim_due_schedule(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int = 120,
    ) -> dict[str, Any]:
        safe_owner = str(owner or "").strip()
        if not safe_owner:
            raise ValueError("lease owner is required")
        now_iso = _iso(now, allow_empty=False)
        lease_until = _iso(
            now.astimezone(timezone.utc) + timedelta(seconds=max(30, int(lease_seconds))),
            allow_empty=False,
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inactive_before = _iso(
                now.astimezone(timezone.utc) - timedelta(hours=24), allow_empty=False
            )
            inactive_rows = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE keepalive_enabled = 1 AND keepalive_paused_until_user = 0
                  AND last_user_activity_at != '' AND last_user_activity_at <= ?
                """,
                (inactive_before,),
            ).fetchall()
            for inactive in inactive_rows:
                values = {**dict(inactive), "keepalive_paused_until_user": 1}
                conn.execute(
                    """
                    UPDATE agent_wake_schedules
                    SET keepalive_paused_until_user = 1, cache_state = 'cooling', due_at = ?,
                        schedule_version = schedule_version + 1,
                        lease_owner = '', lease_until = '', updated_at = ?
                    WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                      AND schedule_version = ?
                    """,
                    (
                        self._computed_due_at(values), now_iso,
                        inactive["profile_id"], inactive["session_id"], inactive["lane_id"],
                        inactive["schedule_version"],
                    ),
                )
            row = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE due_at != '' AND due_at <= ?
                  AND consecutive_failures < ?
                  AND (retry_at = '' OR retry_at <= ?)
                  AND (lease_owner = '' OR lease_until = '' OR lease_until <= ?)
                ORDER BY due_at, profile_id, session_id, lane_id
                LIMIT 1
                """,
                (now_iso, MAX_CONSECUTIVE_FAILURES, now_iso, now_iso),
            ).fetchone()
            if row is None:
                conn.commit()
                return {}
            cursor = conn.execute(
                """
                UPDATE agent_wake_schedules
                SET lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                  AND schedule_version = ?
                  AND (lease_owner = '' OR lease_until = '' OR lease_until <= ?)
                """,
                (
                    safe_owner, lease_until, now_iso,
                    row["profile_id"], row["session_id"], row["lane_id"],
                    row["schedule_version"], now_iso,
                ),
            )
            if cursor.rowcount != 1:
                conn.commit()
                return {}
            run = conn.execute(
                """
                SELECT * FROM agent_wake_runs
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                  AND schedule_version = ? AND due_at = ?
                  AND status IN ('claimed', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    row["profile_id"], row["session_id"], row["lane_id"],
                    row["schedule_version"], row["due_at"],
                ),
            ).fetchone()
            recovered = run is not None
            if run is None:
                wake_id = f"wake_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO agent_wake_runs
                    (wake_id, profile_id, session_id, lane_id, schedule_version,
                     cause, due_at, status, lease_owner, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)
                    """,
                    (
                        wake_id, row["profile_id"], row["session_id"], row["lane_id"],
                        row["schedule_version"], self._cause(row), row["due_at"],
                        safe_owner, now_iso, now_iso,
                    ),
                )
                run = conn.execute(
                    "SELECT * FROM agent_wake_runs WHERE wake_id = ?", (wake_id,)
                ).fetchone()
            else:
                conn.execute(
                    """
                    UPDATE agent_wake_runs
                    SET status = 'claimed', lease_owner = ?, completed_at = '', error = '', updated_at = ?
                    WHERE wake_id = ?
                    """,
                    (safe_owner, now_iso, run["wake_id"]),
                )
                run = conn.execute(
                    "SELECT * FROM agent_wake_runs WHERE wake_id = ?", (run["wake_id"],)
                ).fetchone()
            conn.commit()
            claimed_schedule = dict(row)
            claimed_schedule["lease_owner"] = safe_owner
            claimed_schedule["lease_until"] = lease_until
            return {
                "schedule": self._schedule_payload(claimed_schedule),
                "run": self._run_payload(run),
                "recovered": recovered,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_run(self, wake_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_wake_runs WHERE wake_id = ?", (str(wake_id),)
            ).fetchone()
            return self._run_payload(row)
        finally:
            conn.close()

    def begin_run(
        self,
        *,
        wake_id: str,
        owner: str,
        expected_profile_id: str = "",
        expected_session_id: str = "",
        expected_lane_id: str = "",
        expected_schedule_version: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        safe_owner = str(owner or "").strip()
        now_dt = (now or _utc_now()).astimezone(timezone.utc)
        now_iso = _iso(now_dt, allow_empty=False)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM agent_wake_runs WHERE wake_id = ?", (str(wake_id),)
            ).fetchone()
            if run is None:
                raise KeyError("agent wake run not found")
            scope_matches = (
                (not expected_profile_id or str(run["profile_id"]) == str(expected_profile_id))
                and (not expected_session_id or str(run["session_id"]) == str(expected_session_id))
                and (not expected_lane_id or str(run["lane_id"]) == str(expected_lane_id))
                and (
                    expected_schedule_version is None
                    or int(run["schedule_version"] or 0) == int(expected_schedule_version)
                )
            )
            if not scope_matches:
                conn.commit()
                return {"status": "scope_mismatch", "run": self._run_payload(run)}
            schedule = conn.execute(
                """
                SELECT * FROM agent_wake_schedules
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                """,
                (run["profile_id"], run["session_id"], run["lane_id"]),
            ).fetchone()
            current = (
                schedule is not None
                and int(schedule["schedule_version"] or 0) == int(run["schedule_version"] or 0)
                and str(schedule["lease_owner"] or "") == safe_owner
                and str(run["lease_owner"] or "") == safe_owner
            )
            if not current:
                conn.execute(
                    """
                    UPDATE agent_wake_runs
                    SET status = 'superseded', completed_at = ?, updated_at = ?
                    WHERE wake_id = ? AND status IN ('claimed', 'running')
                    """,
                    (now_iso, now_iso, str(wake_id)),
                )
                conn.commit()
                return {"status": "superseded", "run": self.get_run(str(wake_id))}
            if str(run["status"]) == "running":
                conn.commit()
                return {"status": "duplicate", "run": self._run_payload(run)}
            if str(run["status"]) != "claimed":
                conn.commit()
                return {"status": str(run["status"]), "run": self._run_payload(run)}

            if str(run["cause"]) == "conversation_silence":
                source_turn_id = int(schedule["silence_source_turn_id"] or 0)
                source = conn.execute(
                    """
                    SELECT id FROM conversation_turns
                    WHERE id = ? AND profile_id = ? AND session_id = ? AND turn_kind = 'user'
                    """,
                    (source_turn_id, run["profile_id"], run["session_id"]),
                ).fetchone()
                later_user = conn.execute(
                    """
                    SELECT 1 FROM conversation_turns
                    WHERE profile_id = ? AND session_id = ? AND turn_kind = 'user' AND id > ?
                    LIMIT 1
                    """,
                    (run["profile_id"], run["session_id"], source_turn_id),
                ).fetchone()
                valid_silence = (
                    source is not None
                    and later_user is None
                    and source_turn_id > 0
                    and bool(schedule["conversation_silence_enabled"])
                    and str(schedule["conversation_silence_check_at"] or "") == str(run["due_at"])
                )
                if not valid_silence:
                    conn.execute(
                        """UPDATE agent_wake_runs
                           SET status = 'superseded', completed_at = ?, error = ?, updated_at = ?
                           WHERE wake_id = ?""",
                        (now_iso, "conversation_silence_source_invalid", now_iso, str(wake_id)),
                    )
                    conn.execute(
                        """UPDATE agent_wake_schedules
                           SET conversation_silence_check_at = '', silence_source_turn_id = 0,
                               silence_policy_version = '', due_at = ?, lease_owner = '', lease_until = '', updated_at = ?
                           WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                             AND schedule_version = ? AND lease_owner = ?""",
                        (
                            self._computed_due_at({**dict(schedule), "conversation_silence_check_at": ""}),
                            now_iso, run["profile_id"], run["session_id"], run["lane_id"],
                            run["schedule_version"], safe_owner,
                        ),
                    )
                    conn.commit()
                    return {"status": "superseded", "run": self.get_run(str(wake_id))}

            limit = int(schedule["background_turn_limit"] or 0)
            window_start = _iso(now_dt - timedelta(hours=24), allow_empty=False)
            used = int(conn.execute(
                """
                SELECT COUNT(*) FROM agent_wake_runs
                WHERE profile_id = ? AND session_id = ?
                  AND wake_id != ? AND started_at >= ?
                  AND status IN ('running', 'completed', 'failed')
                """,
                (run["profile_id"], run["session_id"], str(wake_id), window_start),
            ).fetchone()[0])
            if limit <= 0 or used >= limit:
                oldest = conn.execute(
                    """
                    SELECT started_at FROM agent_wake_runs
                    WHERE profile_id = ? AND session_id = ?
                      AND started_at >= ? AND status IN ('running', 'completed', 'failed')
                    ORDER BY started_at LIMIT 1
                    """,
                    (run["profile_id"], run["session_id"], window_start),
                ).fetchone()
                retry_at = _iso(
                    datetime.fromisoformat(str(oldest["started_at"])) + timedelta(hours=24)
                    if oldest and str(oldest["started_at"] or "") else now_dt + timedelta(hours=24),
                    allow_empty=False,
                )
                error = f"background_turn_limit_reached:{used}/{limit}"
                conn.execute(
                    """UPDATE agent_wake_runs
                       SET status = 'deferred', completed_at = ?, error = ?, updated_at = ?
                       WHERE wake_id = ?""",
                    (now_iso, error, now_iso, str(wake_id)),
                )
                conn.execute(
                    """UPDATE agent_wake_schedules
                       SET retry_at = ?, last_error = ?, lease_owner = '', lease_until = '', updated_at = ?
                       WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                         AND schedule_version = ? AND lease_owner = ?""",
                    (retry_at, error, now_iso, run["profile_id"], run["session_id"], run["lane_id"], run["schedule_version"], safe_owner),
                )
                conn.commit()
                return {"status": "limit_reached", "run": self.get_run(str(wake_id)), "retry_at": retry_at}

            cursor = conn.execute(
                    """
                    UPDATE agent_wake_runs
                    SET status = 'running', started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                        updated_at = ?
                    WHERE wake_id = ? AND status = 'claimed'
                    """,
                    (now_iso, now_iso, str(wake_id)),
                )
            if cursor.rowcount != 1:
                raise ValueError("agent wake run is not claimable")
            conn.commit()
            return {"status": "started", "run": self.get_run(str(wake_id)), "used": used + 1, "limit": limit}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_run_running(self, *, wake_id: str, owner: str) -> dict[str, Any]:
        return self.begin_run(wake_id=wake_id, owner=owner)["run"]

    def finish_run(
        self,
        *,
        wake_id: str,
        owner: str,
        status: str,
        turn_id: int | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        safe_status = str(status or "").strip()
        if safe_status not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish status must be terminal")
        safe_owner = str(owner or "").strip()
        now = _iso(_utc_now(), allow_empty=False)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM agent_wake_runs WHERE wake_id = ?", (str(wake_id),)
            ).fetchone()
            if run is None:
                raise KeyError("agent wake run not found")
            if str(run["status"]) in TERMINAL_RUN_STATUSES:
                conn.commit()
                return self._run_payload(run)
            if str(run["lease_owner"] or "") != safe_owner:
                raise ValueError("agent wake run lease was lost")
            conn.execute(
                """
                UPDATE agent_wake_runs
                SET status = ?, completed_at = ?, turn_id = ?, error = ?, updated_at = ?
                WHERE wake_id = ?
                """,
                (safe_status, now, turn_id, str(error or "")[:1000], now, str(wake_id)),
            )
            schedule = conn.execute(
                """SELECT * FROM agent_wake_schedules
                   WHERE profile_id = ? AND session_id = ? AND lane_id = ?""",
                (run["profile_id"], run["session_id"], run["lane_id"]),
            ).fetchone()
            if schedule is not None and int(schedule["schedule_version"] or 0) == int(run["schedule_version"] or 0):
                failures = int(schedule["consecutive_failures"] or 0)
                retry_at = str(schedule["retry_at"] or "")
                last_error = str(schedule["last_error"] or "")
                if safe_status == "deferred":
                    retry_at = _iso(_utc_now() + timedelta(seconds=30), allow_empty=False)
                elif safe_status == "failed":
                    failures += 1
                    last_error = str(error or "agent wake failed")[:1000]
                    retry_at = _iso(
                        _utc_now() + timedelta(seconds=min(900, 30 * (2 ** max(0, failures - 1)))),
                        allow_empty=False,
                    )
                elif safe_status == "completed":
                    failures = 0
                    retry_at = ""
                    last_error = ""
                elif safe_status == "superseded" and str(error or "") == "claimed_lane_is_not_active":
                    retry_at = ""
                conn.execute(
                    """UPDATE agent_wake_schedules
                       SET retry_at = ?, consecutive_failures = ?, last_error = ?,
                           due_at = CASE WHEN ? THEN '' ELSE due_at END, updated_at = ?
                       WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                         AND schedule_version = ?""",
                    (
                        retry_at, failures, last_error,
                        int(safe_status == "superseded" and str(error or "") == "claimed_lane_is_not_active"),
                        now, run["profile_id"], run["session_id"], run["lane_id"], run["schedule_version"],
                    ),
                )
            conn.execute(
                """
                UPDATE agent_wake_schedules
                SET lease_owner = '', lease_until = '', updated_at = ?
                WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                  AND schedule_version = ? AND lease_owner = ?
                """,
                (
                    now, run["profile_id"], run["session_id"], run["lane_id"],
                    run["schedule_version"], safe_owner,
                ),
            )
            conn.commit()
            return self.get_run(str(wake_id))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_runs(
        self, *, profile_id: str, session_id: str, lane_id: str = ""
    ) -> list[dict[str, Any]]:
        profile = str(profile_id or "default").strip() or "default"
        session = str(session_id or "").strip()
        lane = str(lane_id or "").strip()
        if not session:
            raise ValueError("session_id is required")
        conn = self._connect()
        try:
            if lane:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_wake_runs
                    WHERE profile_id = ? AND session_id = ? AND lane_id = ?
                    ORDER BY created_at, wake_id
                    """,
                    (profile, session, lane),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_wake_runs
                    WHERE profile_id = ? AND session_id = ?
                    ORDER BY created_at, wake_id
                    """,
                    (profile, session),
                ).fetchall()
            return [self._run_payload(row) for row in rows]
        finally:
            conn.close()


def delete_agent_wake_session_records(
    conn: sqlite3.Connection, *, profile_id: str, session_id: str
) -> dict[str, int]:
    """Delete only one verified profile/session's wake control-plane records."""
    counts: dict[str, int] = {}
    for table in ("agent_wake_runs", "agent_wake_schedules"):
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE profile_id = ? AND session_id = ?",
            (str(profile_id), str(session_id)),
        )
        counts[table] = max(0, int(cursor.rowcount or 0))
    return counts
