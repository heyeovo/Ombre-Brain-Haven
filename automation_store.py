"""Persistent schedules, runs, and review candidates for background automations."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


RUN_STATUSES = {"running", "completed", "failed", "skipped"}
CANDIDATE_STATUSES = {"pending", "rejected", "applying", "completed", "conflict", "failed"}


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")


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
                "CREATE INDEX IF NOT EXISTS idx_automation_schedules_due "
                "ON automation_schedules(enabled, next_run_at)"
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
            "pending_candidates": int(pending or 0),
        }
