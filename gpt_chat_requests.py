"""Durable admission receipts, not a second chat history or a generation queue."""
from contextlib import contextmanager
import re
import sqlite3
import time
from datetime import datetime, timezone
import json
from agent_wake_store import admit_gpt_wake, gpt_wake_can_save, record_gpt_wake_user_activity
from gpt_context_audit import validate_snapshot, now, apply_event


class GPTChatRequests:
    # Dashboard generation has a 9 minute bound; include final persistence/drain grace.
    LEASE_SECONDS = 12 * 60

    def __init__(self, db_path):
        self.db_path = db_path
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS gpt_chat_requests (
                profile_id TEXT NOT NULL, request_id TEXT NOT NULL,
                session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                owner TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL,
                PRIMARY KEY (profile_id, request_id))""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_gpt_chat_active
                ON gpt_chat_requests(profile_id, session_id, status, expires_at)""")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(gpt_chat_requests)")}
            if "audit_json" not in existing:
                conn.execute("ALTER TABLE gpt_chat_requests ADD COLUMN audit_json TEXT NOT NULL DEFAULT ''")
            if "is_wake" not in existing:
                conn.execute("ALTER TABLE gpt_chat_requests ADD COLUMN is_wake INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def control(self, profile_id, body):
        action = body.get("action")
        request_id = body.get("request_id")
        session_id = body.get("session_id")
        if action == 'audit-list':
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 200:
                raise ValueError('invalid session_id')
            before = body.get('before_cursor', 2**63 - 1)
            if type(before) is not int or not 0 < before <= 2**63 - 1:
                raise ValueError('invalid audit cursor')
            with self._connect() as conn:
                rows = conn.execute("""SELECT rowid AS cursor, audit_json FROM gpt_chat_requests
                    WHERE profile_id=? AND session_id=? AND audit_json!='' AND rowid<?
                    ORDER BY rowid DESC LIMIT 21""", (profile_id, session_id, before)).fetchall()
                ids = body.get('request_ids', [])
                if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key) for key in ids):
                    raise ValueError('invalid audit request_ids')
                matches = conn.execute(f"""SELECT audit_json FROM gpt_chat_requests
                    WHERE profile_id=? AND session_id=? AND audit_json!='' AND request_id IN ({','.join('?' for _ in ids)})""",
                    (profile_id, session_id, *ids)).fetchall() if ids else []
            items = [{**json.loads(row['audit_json']), 'cursor': row['cursor']} for row in rows[:20]]
            return {'ok': True, 'records': items, 'matched_records': [json.loads(row['audit_json']) for row in matches],
                    'before_cursor': items[-1]['cursor'] if len(rows) > 20 else None}
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("invalid request_id")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 200:
            raise ValueError("invalid session_id")
        if action not in {"claim", "finish", "status", "prepare"}:
            raise ValueError("invalid action")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM gpt_chat_requests WHERE profile_id=? AND request_id=?",
                               (profile_id, request_id)).fetchone()
            if row and row["session_id"] != session_id:
                return {"ok": False, "code": "request_id_reused"}
            if action == "status":
                return {"ok": True, "found": bool(row), "status": self._status(row)}
            owner = body.get("owner")
            if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
                raise ValueError("invalid owner")
            if action == 'prepare':
                if not row or row['owner'] != owner or self._status(row) != 'running':
                    return {'ok': False, 'code': 'request_owner_conflict'}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_sessions'").fetchone():
                    session = conn.execute('SELECT deleted_at FROM conversation_sessions WHERE profile_id=? AND session_id=?',
                                           (profile_id, session_id)).fetchone()
                    if not session or session['deleted_at']:
                        return {'ok': False, 'code': 'conversation_deleted'}
                snapshot = validate_snapshot(body.get('snapshot'))
                if row['audit_json']:
                    old = json.loads(row['audit_json'])
                    return {'ok': old['snapshot'] == snapshot, 'code': 'audit_already_prepared'}
                record = {'request_id': request_id, 'session_id': session_id, 'snapshot': snapshot,
                          'prepared_at': now(), 'usage': None}
                conn.execute('UPDATE gpt_chat_requests SET audit_json=? WHERE profile_id=? AND request_id=?',
                             (json.dumps(record, ensure_ascii=False), profile_id, request_id))
                return {'ok': True}
            if action == "finish":
                if not row or row["owner"] != owner:
                    return {"ok": False, "code": "request_owner_conflict"}
                conn.execute("UPDATE gpt_chat_requests SET status='finished' WHERE profile_id=? AND request_id=?",
                             (profile_id, request_id))
                return {"ok": True}
            fingerprint = body.get("fingerprint")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise ValueError("invalid fingerprint")
            if row:
                if row["fingerprint"] != fingerprint:
                    return {"ok": False, "code": "request_id_reused"}
                # Even an expired receipt never authorizes repeating a paid model call.
                return {"ok": False, "code": "request_already_admitted", "status": self._status(row)}
            active = conn.execute("""SELECT 1 FROM gpt_chat_requests
                WHERE profile_id=? AND session_id=? AND status='running' AND expires_at>?""",
                                  (profile_id, session_id, time.time())).fetchone()
            if active:
                return {"ok": False, "code": "conversation_busy"}
            accepted_at = datetime.now(timezone.utc)
            if body.get("wake") is True and not admit_gpt_wake(
                conn, profile_id, session_id, request_id, accepted_at, body.get("expected_state_version")
            ):
                return {"ok": False, "code": "wake_superseded"}
            if body.get("user_activity") is True and body.get("wake") is not True:
                record_gpt_wake_user_activity(conn, profile_id, session_id, accepted_at)
            conn.execute("""
                INSERT INTO gpt_chat_requests
                    (profile_id, request_id, session_id, fingerprint, owner, status, expires_at, is_wake)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
            """, (profile_id, request_id, session_id, fingerprint, owner,
                  time.time() + self.LEASE_SECONDS, int(body.get("wake") is True)))
            return {"ok": True}

    def audit(self, profile_id, request_id):
        with self._connect() as conn:
            row = conn.execute('SELECT audit_json FROM gpt_chat_requests WHERE profile_id=? AND request_id=?',
                               (profile_id, request_id)).fetchone()
        return json.loads(row['audit_json']) if row and row['audit_json'] else None

    def audit_event(self, profile_id, request_id, event):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("""
                SELECT audit_json, session_id, is_wake FROM gpt_chat_requests WHERE profile_id=? AND request_id=?
            """, (profile_id, request_id)).fetchone()
            if not row or not row["audit_json"]:
                raise ValueError("audit record missing")
            if row["is_wake"] and event.get("type") == "request" and not gpt_wake_can_save(
                conn, profile_id, row["session_id"], request_id
            ):
                raise ValueError("GPT agent wake is no longer active")
            record = apply_event(json.loads(row['audit_json']), event)
            conn.execute('UPDATE gpt_chat_requests SET audit_json=? WHERE profile_id=? AND request_id=?',
                         (json.dumps(record, ensure_ascii=False), profile_id, request_id))

    @staticmethod
    def _status(row):
        if not row:
            return None
        return "interrupted" if row["status"] == "running" and row["expires_at"] <= time.time() else row["status"]
