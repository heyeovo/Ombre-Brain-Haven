"""Persistent schedules, runs, and review candidates for background automations."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


RUN_STATUSES = {"running", "completed", "failed", "skipped"}
CANDIDATE_STATUSES = {"pending", "rejected", "applying", "completed", "conflict", "failed"}


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")


def _lease_until_iso(seconds: int) -> str:
    return (
        datetime.now(ZoneInfo("Asia/Hong_Kong")) + timedelta(seconds=max(30, int(seconds)))
    ).isoformat(timespec="seconds")


def _parse_iso(value: str, timezone: str = "Asia/Hong_Kong") -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        tz = ZoneInfo(str(timezone or "Asia/Hong_Kong"))
    except Exception:
        tz = ZoneInfo("Asia/Hong_Kong")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


class AutomationStore:
    """Small generic automation control plane stored outside memory buckets."""

    def __init__(self, config: dict | None = None, *, db_path: str = ""):
        config = config or {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(
            db_path
            or config.get("automation_db_path")
            or os.path.join(state_dir, "automations.sqlite")
        )
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    handler_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
                    next_run_at TEXT NOT NULL DEFAULT '',
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    last_run_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    execution_engine TEXT NOT NULL DEFAULT 'api',
                    execution_model TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_columns(
                conn,
                "automation_schedules",
                {
                    "task_type": "TEXT NOT NULL DEFAULT ''",
                    "handler_key": "TEXT NOT NULL DEFAULT ''",
                    "enabled": "INTEGER NOT NULL DEFAULT 0",
                    "timezone": "TEXT NOT NULL DEFAULT 'Asia/Hong_Kong'",
                    "next_run_at": "TEXT NOT NULL DEFAULT ''",
                    "policy_json": "TEXT NOT NULL DEFAULT '{}'",
                    "lease_owner": "TEXT NOT NULL DEFAULT ''",
                    "lease_until": "TEXT NOT NULL DEFAULT ''",
                    "last_run_at": "TEXT NOT NULL DEFAULT ''",
                    "last_error": "TEXT NOT NULL DEFAULT ''",
                    "execution_engine": "TEXT NOT NULL DEFAULT 'api'",
                    "execution_model": "TEXT NOT NULL DEFAULT ''",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    cycle_key TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'running',
                    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    input_hash TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_type, cycle_key, input_hash)
                )
                """
            )
            self._ensure_columns(
                conn,
                "automation_runs",
                {
                    "task_type": "TEXT NOT NULL DEFAULT ''",
                    "cycle_key": "TEXT NOT NULL DEFAULT ''",
                    "window_start": "TEXT NOT NULL DEFAULT ''",
                    "window_end": "TEXT NOT NULL DEFAULT ''",
                    "timezone": "TEXT NOT NULL DEFAULT 'Asia/Hong_Kong'",
                    "trigger": "TEXT NOT NULL DEFAULT 'manual'",
                    "status": "TEXT NOT NULL DEFAULT 'running'",
                    "input_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                    "input_hash": "TEXT NOT NULL DEFAULT ''",
                    "error": "TEXT NOT NULL DEFAULT ''",
                    "started_at": "TEXT NOT NULL DEFAULT ''",
                    "completed_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    rationale_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    draft_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL DEFAULT 1,
                    approved_payload_json TEXT NOT NULL DEFAULT '{}',
                    approved_payload_hash TEXT NOT NULL DEFAULT '',
                    operation_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id),
                    FOREIGN KEY(run_id) REFERENCES automation_runs(run_id)
                )
                """
            )
            self._ensure_columns(
                conn,
                "automation_candidates",
                {
                    "run_id": "TEXT NOT NULL DEFAULT ''",
                    "task_type": "TEXT NOT NULL DEFAULT ''",
                    "candidate_type": "TEXT NOT NULL DEFAULT ''",
                    "status": "TEXT NOT NULL DEFAULT 'pending'",
                    "rationale_json": "TEXT NOT NULL DEFAULT '[]'",
                    "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                    "preview_json": "TEXT NOT NULL DEFAULT '{}'",
                    "draft_json": "TEXT NOT NULL DEFAULT '{}'",
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                    "approved_payload_json": "TEXT NOT NULL DEFAULT '{}'",
                    "approved_payload_hash": "TEXT NOT NULL DEFAULT ''",
                    "operation_id": "TEXT NOT NULL DEFAULT ''",
                    "result_json": "TEXT NOT NULL DEFAULT '{}'",
                    "error": "TEXT NOT NULL DEFAULT ''",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                    "confirmed_at": "TEXT NOT NULL DEFAULT ''",
                    "applied_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_executions (
                    execution_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'scheduled',
                    requested_engine TEXT NOT NULL,
                    actual_engine TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    error_code TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_columns(
                conn,
                "automation_executions",
                {
                    "task_type": "TEXT NOT NULL DEFAULT ''",
                    "trigger": "TEXT NOT NULL DEFAULT 'scheduled'",
                    "requested_engine": "TEXT NOT NULL DEFAULT 'api'",
                    "actual_engine": "TEXT NOT NULL DEFAULT 'api'",
                    "model": "TEXT NOT NULL DEFAULT ''",
                    "status": "TEXT NOT NULL DEFAULT 'running'",
                    "error_code": "TEXT NOT NULL DEFAULT ''",
                    "error": "TEXT NOT NULL DEFAULT ''",
                    "started_at": "TEXT NOT NULL DEFAULT ''",
                    "completed_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_schedules_due "
                "ON automation_schedules(enabled, next_run_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_executions_task_started "
                "ON automation_executions(task_type, started_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_runs_task_started "
                "ON automation_runs(task_type, started_at DESC)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_runs_input_unique "
                "ON automation_runs(task_type, cycle_key, input_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_candidates_task_status "
                "ON automation_candidates(task_type, status, created_at DESC)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_candidates_run_unique "
                "ON automation_candidates(run_id)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _json_load(value: Any, fallback: Any) -> Any:
        try:
            parsed = json.loads(str(value or ""))
        except (TypeError, ValueError):
            return fallback
        return parsed

    @classmethod
    def _schedule_payload(cls, row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        payload = dict(row)
        payload["enabled"] = bool(payload.get("enabled"))
        payload["policy"] = cls._json_load(payload.pop("policy_json", "{}"), {})
        return payload

    @classmethod
    def _run_payload(cls, row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        payload = dict(row)
        payload["input_snapshot"] = cls._json_load(payload.pop("input_snapshot_json", "{}"), {})
        return payload

    @classmethod
    def _candidate_payload(cls, row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        payload = dict(row)
        for source, target, fallback in (
            ("rationale_json", "rationale", []),
            ("evidence_json", "evidence", []),
            ("preview_json", "preview", {}),
            ("draft_json", "draft", {}),
            ("approved_payload_json", "approved_payload", {}),
            ("result_json", "result", {}),
        ):
            payload[target] = cls._json_load(payload.pop(source, ""), fallback)
        return payload

    def ensure_schedule(
        self,
        *,
        schedule_id: str,
        task_type: str,
        handler_key: str,
        timezone: str = "Asia/Hong_Kong",
        enabled: bool = False,
        policy: dict | None = None,
    ) -> dict:
        now = _now_iso()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO automation_schedules (
                        schedule_id, task_type, handler_key, enabled, timezone,
                        policy_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(schedule_id), str(task_type), str(handler_key),
                        1 if enabled else 0, str(timezone or "Asia/Hong_Kong"),
                        self._json_dump(policy or {}), now, now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM automation_schedules WHERE schedule_id = ?",
                (str(schedule_id),),
            ).fetchone()
            return self._schedule_payload(row)
        finally:
            conn.close()

    def get_schedule(self, *, task_type: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_schedules WHERE task_type = ? ORDER BY created_at LIMIT 1",
                (str(task_type),),
            ).fetchone()
            return self._schedule_payload(row)
        finally:
            conn.close()

    def update_execution_choice(
        self, *, task_type: str, engine: str, model: str = "",
    ) -> dict:
        normalized_engine = str(engine or "").strip().lower()
        if normalized_engine not in {"api", "pro"}:
            raise ValueError("execution engine must be api or pro")
        normalized_model = str(model or "").strip()[:200]
        if normalized_engine == "pro" and not normalized_model:
            normalized_model = "claude-sonnet-4-6"
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_schedules
                    SET execution_engine = ?, execution_model = ?, updated_at = ?
                    WHERE task_type = ?
                    """,
                    (normalized_engine, normalized_model, _now_iso(), str(task_type)),
                )
            if cursor.rowcount != 1:
                raise ValueError("automation schedule not found")
            return self.get_schedule(task_type=task_type)
        finally:
            conn.close()

    def update_schedule(
        self,
        *,
        task_type: str,
        enabled: bool,
        timezone: str,
        policy: dict,
        next_run_at: str,
    ) -> dict:
        now = _now_iso()
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_schedules
                    SET enabled = ?, timezone = ?, policy_json = ?, next_run_at = ?,
                        lease_owner = '', lease_until = '', updated_at = ?
                    WHERE task_type = ?
                    """,
                    (
                        1 if enabled else 0,
                        str(timezone or "Asia/Hong_Kong"),
                        self._json_dump(policy or {}),
                        str(next_run_at or ""),
                        now,
                        str(task_type),
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("automation schedule not found")
            return self.get_schedule(task_type=task_type)
        finally:
            conn.close()

    def claim_due_schedule(
        self,
        *,
        task_type: str,
        owner: str,
        now: datetime,
        lease_seconds: int = 900,
    ) -> dict:
        safe_owner = str(owner or "").strip()
        if not safe_owner:
            raise ValueError("automation lease owner is required")
        schedule = self.get_schedule(task_type=task_type)
        if not schedule or not schedule.get("enabled"):
            return {}
        due_at = _parse_iso(schedule.get("next_run_at", ""), schedule.get("timezone", ""))
        if due_at is None or due_at > now.astimezone(due_at.tzinfo):
            return {}
        if not self.acquire_task_lease(
            task_type=task_type, owner=safe_owner, lease_seconds=lease_seconds,
        ):
            return {}
        claimed = self.get_schedule(task_type=task_type)
        claimed_due = _parse_iso(claimed.get("next_run_at", ""), claimed.get("timezone", ""))
        if not claimed.get("enabled") or claimed_due is None or claimed_due > now.astimezone(claimed_due.tzinfo):
            self.release_task_lease(task_type=task_type, owner=safe_owner)
            return {}
        return claimed

    def complete_schedule_run(
        self,
        *,
        task_type: str,
        owner: str,
        next_run_at: str,
        error: str = "",
    ) -> dict:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_schedules
                    SET next_run_at = ?, last_run_at = ?, last_error = ?, lease_owner = '', lease_until = '',
                        updated_at = ?
                    WHERE task_type = ? AND lease_owner = ?
                    """,
                    (
                        str(next_run_at), _now_iso(), str(error or "")[:1000], _now_iso(),
                        str(task_type), str(owner),
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("automation schedule lease was lost")
            return self.get_schedule(task_type=task_type)
        finally:
            conn.close()

    def acquire_task_lease(
        self, *, task_type: str, owner: str, lease_seconds: int = 120,
    ) -> bool:
        """Claim one persisted task lease; expired leases may be recovered."""
        safe_owner = str(owner or "").strip()
        if not safe_owner:
            raise ValueError("automation lease owner is required")
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE automation_schedules
                SET lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE task_type = ?
                  AND (lease_owner = '' OR lease_owner = ? OR lease_until = '' OR lease_until <= ?)
                """,
                (
                    safe_owner, _lease_until_iso(lease_seconds), now,
                    str(task_type), safe_owner, now,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_task_lease(self, *, task_type: str, owner: str) -> bool:
        safe_owner = str(owner or "").strip()
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_schedules
                    SET lease_owner = '', lease_until = '', updated_at = ?
                    WHERE task_type = ? AND lease_owner = ?
                    """,
                    (_now_iso(), str(task_type), safe_owner),
                )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def start_run(
        self,
        *,
        task_type: str,
        cycle_key: str,
        window_start: str,
        window_end: str,
        timezone: str,
        trigger: str,
        input_snapshot: dict,
        input_hash: str,
    ) -> tuple[dict, bool]:
        run_id = uuid.uuid4().hex[:24]
        now = _now_iso()
        conn = self._connect()
        try:
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO automation_runs (
                            run_id, task_type, cycle_key, window_start, window_end,
                            timezone, trigger, status, input_snapshot_json,
                            input_hash, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                        """,
                        (
                            run_id, str(task_type), str(cycle_key), str(window_start),
                            str(window_end), str(timezone), str(trigger or "manual"),
                            self._json_dump(input_snapshot), str(input_hash), now,
                        ),
                    )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                """
                SELECT * FROM automation_runs
                WHERE task_type = ? AND cycle_key = ? AND input_hash = ?
                """,
                (str(task_type), str(cycle_key), str(input_hash)),
            ).fetchone()
            return self._run_payload(row), created
        finally:
            conn.close()

    def finish_run(self, run_id: str, *, status: str, error: str = "") -> dict:
        safe_status = str(status or "").strip().lower()
        if safe_status not in RUN_STATUSES - {"running"}:
            raise ValueError("invalid automation run status")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE automation_runs
                    SET status = ?, error = ?, completed_at = ?
                    WHERE run_id = ?
                    """,
                    (safe_status, str(error or "")[:1000], _now_iso(), str(run_id)),
                )
            row = conn.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return self._run_payload(row)
        finally:
            conn.close()

    def restart_failed_run(self, run_id: str) -> dict:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE automation_runs
                    SET status = 'running', error = '', completed_at = '', started_at = ?
                    WHERE run_id = ? AND status = 'failed'
                    """,
                    (_now_iso(), str(run_id)),
                )
            row = conn.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return self._run_payload(row)
        finally:
            conn.close()

    def get_run(self, run_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return self._run_payload(row)
        finally:
            conn.close()

    def latest_run(self, *, task_type: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM automation_runs
                WHERE task_type = ? ORDER BY started_at DESC, run_id DESC LIMIT 1
                """,
                (str(task_type),),
            ).fetchone()
            return self._run_payload(row)
        finally:
            conn.close()

    def start_execution(
        self, *, task_type: str, trigger: str, engine: str, model: str = "",
    ) -> dict:
        execution_id = uuid.uuid4().hex[:24]
        now = _now_iso()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO automation_executions (
                        execution_id, task_type, trigger, requested_engine,
                        actual_engine, model, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        execution_id, str(task_type), str(trigger or "scheduled"),
                        str(engine), str(engine), str(model or ""), now,
                    ),
                )
            return self.get_execution(execution_id)
        finally:
            conn.close()

    def finish_execution(
        self, execution_id: str, *, status: str, error_code: str = "", error: str = "",
    ) -> dict:
        normalized = str(status or "").strip().lower()
        if normalized not in {"completed", "failed", "skipped"}:
            raise ValueError("invalid automation execution status")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE automation_executions
                    SET status = ?, error_code = ?, error = ?, completed_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        normalized, str(error_code or "")[:100], str(error or "")[:1000],
                        _now_iso(), str(execution_id),
                    ),
                )
            return self.get_execution(execution_id)
        finally:
            conn.close()

    def get_execution(self, execution_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_executions WHERE execution_id = ?",
                (str(execution_id),),
            ).fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()

    def latest_execution(self, *, task_type: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM automation_executions
                WHERE task_type = ? ORDER BY started_at DESC, execution_id DESC LIMIT 1
                """,
                (str(task_type),),
            ).fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()

    def create_candidate(
        self,
        *,
        run_id: str,
        task_type: str,
        candidate_type: str,
        rationale: list,
        evidence: list,
        preview: dict,
        draft: dict,
    ) -> tuple[dict, bool]:
        candidate_id = uuid.uuid4().hex[:24]
        now = _now_iso()
        conn = self._connect()
        try:
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO automation_candidates (
                            candidate_id, run_id, task_type, candidate_type, status,
                            rationale_json, evidence_json, preview_json, draft_json,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            candidate_id, str(run_id), str(task_type), str(candidate_type),
                            self._json_dump(rationale or []), self._json_dump(evidence or []),
                            self._json_dump(preview or {}), self._json_dump(draft or {}),
                            now, now,
                        ),
                    )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return self._candidate_payload(row), created
        finally:
            conn.close()

    def get_candidate(self, candidate_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            return self._candidate_payload(row)
        finally:
            conn.close()

    def get_candidate_for_run(self, run_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return self._candidate_payload(row)
        finally:
            conn.close()

    def update_candidate_draft(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        draft: dict,
    ) -> tuple[str, dict]:
        """Save one edited draft as a new revision without changing the original preview."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                return "not_found", {}
            current = self._candidate_payload(row)
            if current.get("status") != "pending":
                conn.rollback()
                return "not_pending", current
            if int(current.get("revision") or 0) != int(expected_revision):
                conn.rollback()
                return "revision_mismatch", current
            next_revision = int(expected_revision) + 1
            conn.execute(
                """
                UPDATE automation_candidates
                SET draft_json = ?, revision = ?, updated_at = ?
                WHERE candidate_id = ? AND status = 'pending' AND revision = ?
                """,
                (
                    self._json_dump(draft or {}), next_revision, _now_iso(),
                    str(candidate_id), int(expected_revision),
                ),
            )
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            conn.commit()
            return "updated", self._candidate_payload(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reject_candidate(
        self, candidate_id: str, *, expected_revision: int,
    ) -> tuple[str, dict]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                return "not_found", {}
            current = self._candidate_payload(row)
            if current.get("status") != "pending":
                conn.rollback()
                return "not_pending", current
            if int(current.get("revision") or 0) != int(expected_revision):
                conn.rollback()
                return "revision_mismatch", current
            now = _now_iso()
            conn.execute(
                """
                UPDATE automation_candidates
                SET status = 'rejected', error = '', updated_at = ?, confirmed_at = ?
                WHERE candidate_id = ? AND status = 'pending' AND revision = ?
                """,
                (now, now, str(candidate_id), int(expected_revision)),
            )
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            conn.commit()
            return "rejected", self._candidate_payload(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def freeze_candidate_approval(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        approved_payload: dict,
        approved_payload_hash: str,
        operation_id: str,
    ) -> tuple[str, dict]:
        """Atomically freeze the displayed revision before any lifecycle write."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                return "not_found", {}
            current = self._candidate_payload(row)
            if current.get("status") != "pending":
                conn.rollback()
                return "not_pending", current
            if int(current.get("revision") or 0) != int(expected_revision):
                conn.rollback()
                return "revision_mismatch", current
            now = _now_iso()
            conn.execute(
                """
                UPDATE automation_candidates
                SET status = 'applying', approved_payload_json = ?, approved_payload_hash = ?,
                    operation_id = ?, result_json = '{}', error = '', updated_at = ?, confirmed_at = ?
                WHERE candidate_id = ? AND status = 'pending' AND revision = ?
                """,
                (
                    self._json_dump(approved_payload), str(approved_payload_hash),
                    str(operation_id), now, now, str(candidate_id), int(expected_revision),
                ),
            )
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            conn.commit()
            return "frozen", self._candidate_payload(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_candidate_execution(
        self,
        candidate_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: str = "",
    ) -> dict:
        safe_status = str(status or "").strip().lower()
        if safe_status not in {"applying", "completed", "conflict", "failed"}:
            raise ValueError("invalid automation candidate execution status")
        now = _now_iso()
        applied_at = now if safe_status == "completed" else ""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE automation_candidates
                    SET status = ?, result_json = ?, error = ?, updated_at = ?, applied_at = ?
                    WHERE candidate_id = ?
                      AND status IN ('pending', 'applying', 'failed')
                    """,
                    (
                        safe_status, self._json_dump(result or {}), str(error or "")[:2000],
                        now, applied_at, str(candidate_id),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            return self._candidate_payload(row)
        finally:
            conn.close()

    def complete_candidate_and_advance_review_cursor(
        self,
        candidate_id: str,
        *,
        result: dict,
        expected_reviewed_through_date: str,
        reviewed_through_date: str,
    ) -> dict:
        """Complete one approved weekly candidate and advance its cursor atomically."""
        expected = str(expected_reviewed_through_date or "").strip()
        target = str(reviewed_through_date or "").strip()
        try:
            expected_date = datetime.fromisoformat(expected).date()
            target_date = datetime.fromisoformat(target).date()
        except ValueError as exc:
            raise ValueError("invalid weekly journey review cursor") from exc
        if target_date < expected_date:
            raise ValueError("weekly journey review cursor cannot move backward")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            candidate_row = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            if candidate_row is None or str(candidate_row["status"] or "") != "applying":
                raise ValueError("weekly journey candidate is not applying")
            schedule_row = conn.execute(
                "SELECT * FROM automation_schedules WHERE task_type = ? ORDER BY created_at LIMIT 1",
                (str(candidate_row["task_type"] or ""),),
            ).fetchone()
            if schedule_row is None:
                raise ValueError("weekly journey schedule not found")
            policy = self._json_load(schedule_row["policy_json"], {})
            current = str(policy.get("reviewed_through_date") or "").strip()
            if current != expected:
                raise ValueError("weekly journey review cursor changed")
            policy["reviewed_through_date"] = target
            now = _now_iso()
            conn.execute(
                """
                UPDATE automation_candidates
                SET status = 'completed', result_json = ?, error = '',
                    updated_at = ?, applied_at = ?
                WHERE candidate_id = ? AND status = 'applying'
                """,
                (self._json_dump(result or {}), now, now, str(candidate_id)),
            )
            conn.execute(
                """
                UPDATE automation_schedules
                SET policy_json = ?, updated_at = ?
                WHERE schedule_id = ?
                """,
                (self._json_dump(policy), now, str(schedule_row["schedule_id"])),
            )
            saved = conn.execute(
                "SELECT * FROM automation_candidates WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            conn.commit()
            return self._candidate_payload(saved)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def unresolved_candidate_for_persona(self, *, task_type: str, persona_id: str) -> dict:
        """Return one still-actionable candidate for the same persona, if any."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT c.*, r.input_snapshot_json
                FROM automation_candidates c
                JOIN automation_runs r ON r.run_id = c.run_id
                WHERE c.task_type = ? AND c.status IN ('pending', 'applying', 'failed')
                ORDER BY c.created_at DESC, c.candidate_id DESC
                """,
                (str(task_type),),
            ).fetchall()
            wanted = str(persona_id or "").strip()
            for row in rows:
                snapshot = self._json_load(row["input_snapshot_json"], {})
                persona = snapshot.get("persona") if isinstance(snapshot.get("persona"), dict) else {}
                if str(persona.get("id") or "").strip() == wanted:
                    candidate = self._candidate_payload(row)
                    candidate.pop("input_snapshot_json", None)
                    return candidate
            return {}
        finally:
            conn.close()

    def list_candidates(
        self, *, task_type: str, status: str = "pending", limit: int = 50,
    ) -> list[dict]:
        safe_status = str(status or "pending").strip().lower()
        if safe_status and safe_status != "all" and safe_status not in CANDIDATE_STATUSES:
            raise ValueError("invalid automation candidate status")
        clauses = ["task_type = ?"]
        params: list[Any] = [str(task_type)]
        if safe_status and safe_status != "all":
            clauses.append("status = ?")
            params.append(safe_status)
        params.append(max(1, min(200, int(limit or 50))))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM automation_candidates WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, candidate_id DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._candidate_payload(row) for row in rows]
        finally:
            conn.close()

    def task_status(self, *, task_type: str) -> dict:
        conn = self._connect()
        try:
            pending = conn.execute(
                """
                SELECT COUNT(*) FROM automation_candidates
                WHERE task_type = ? AND status = 'pending'
                """,
                (str(task_type),),
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "task_type": str(task_type),
            "schedule": self.get_schedule(task_type=task_type),
            "latest_run": self.latest_run(task_type=task_type),
            "latest_execution": self.latest_execution(task_type=task_type),
            "pending_candidates": int(pending or 0),
        }
