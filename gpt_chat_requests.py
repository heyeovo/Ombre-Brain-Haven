"""Durable admission receipts, not a second chat history or a generation queue."""
from contextlib import contextmanager
import re
import sqlite3
import time


class GPTChatRequests:
    # Dashboard generation has a 9 minute bound; include final persistence/drain grace.
    LEASE_SECONDS = 12 * 60

    def __init__(self, db_path):
        self.db_path = db_path
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS gpt_chat_requests (
                profile_id TEXT NOT NULL, request_id TEXT NOT NULL,
                session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                owner TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL,
                PRIMARY KEY (profile_id, request_id))""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_gpt_chat_active
                ON gpt_chat_requests(profile_id, session_id, status, expires_at)""")

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
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("invalid request_id")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 200:
            raise ValueError("invalid session_id")
        if action not in {"claim", "finish", "status"}:
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
            conn.execute("INSERT INTO gpt_chat_requests VALUES (?, ?, ?, ?, ?, 'running', ?)",
                         (profile_id, request_id, session_id, fingerprint, owner, time.time() + self.LEASE_SECONDS))
            return {"ok": True}

    @staticmethod
    def _status(row):
        if not row:
            return None
        return "interrupted" if row["status"] == "running" and row["expires_at"] <= time.time() else row["status"]
