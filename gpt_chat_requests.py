"""Durable admission receipts, not a second chat history or a generation queue."""
from contextlib import contextmanager
import re
import sqlite3
import time
import json
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
            if 'audit_json' not in {row[1] for row in conn.execute('PRAGMA table_info(gpt_chat_requests)')}:
                conn.execute("ALTER TABLE gpt_chat_requests ADD COLUMN audit_json TEXT NOT NULL DEFAULT ''")

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
        if action not in {"claim", "finish", "status", "prepare", "prepare-tool-step"}:
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
            if action in {'prepare', 'prepare-tool-step'}:
                if not row or row['owner'] != owner or self._status(row) != 'running':
                    return {'ok': False, 'code': 'request_owner_conflict'}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_sessions'").fetchone():
                    session = conn.execute('SELECT deleted_at FROM conversation_sessions WHERE profile_id=? AND session_id=?',
                                           (profile_id, session_id)).fetchone()
                    if not session or session['deleted_at']:
                        return {'ok': False, 'code': 'conversation_deleted'}
            if action == 'prepare-tool-step':
                step = body.get('step')
                digest = body.get('body_hash')
                if type(step) is not int or not 1 <= step <= 4 or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('invalid tool step')
                if not row['audit_json']:
                    return {'ok': False, 'code': 'audit_missing'}
                record = json.loads(row['audit_json'])
                steps = record.setdefault('tool_steps', [])
                previous = steps[-1] if steps else record
                if step != len(steps) + 1 or not previous.get('tool_continuation') or previous.get('outcome') != 'completed' or not record.get('connection_id'):
                    return {'ok': False, 'code': 'tool_step_not_ready'}
                steps.append({'snapshot': {'body_hash': digest}, 'prepared_at': now()})
                conn.execute('UPDATE gpt_chat_requests SET audit_json=? WHERE profile_id=? AND request_id=?',
                             (json.dumps(record, ensure_ascii=False), profile_id, request_id))
                return {'ok': True}
            if action == 'prepare':
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
            conn.execute("INSERT INTO gpt_chat_requests (profile_id, request_id, session_id, fingerprint, owner, status, expires_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
                         (profile_id, request_id, session_id, fingerprint, owner, time.time() + self.LEASE_SECONDS))
            return {"ok": True}

    def audit(self, profile_id, request_id, step=0):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM gpt_chat_requests WHERE profile_id=? AND request_id=?',
                               (profile_id, request_id)).fetchone()
        if not row or not row['audit_json']:
            return None
        record = json.loads(row['audit_json'])
        if not step:
            return record
        steps = record.get('tool_steps', [])
        if self._status(row) != 'running' or step != len(steps):
            return None
        return {**steps[step - 1], 'connection_id': record.get('connection_id')}

    def audit_event(self, profile_id, request_id, event, step=0):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT audit_json FROM gpt_chat_requests WHERE profile_id=? AND request_id=?',
                               (profile_id, request_id)).fetchone()
            if not row or not row['audit_json']:
                raise ValueError('audit record missing')
            record = json.loads(row['audit_json'])
            target = record['tool_steps'][step - 1] if step else record
            apply_event(target, event)
            conn.execute('UPDATE gpt_chat_requests SET audit_json=? WHERE profile_id=? AND request_id=?',
                         (json.dumps(record, ensure_ascii=False), profile_id, request_id))

    @staticmethod
    def _status(row):
        if not row:
            return None
        return "interrupted" if row["status"] == "running" and row["expires_at"] <= time.time() else row["status"]
