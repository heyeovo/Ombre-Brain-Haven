from __future__ import annotations

import os
import sqlite3
import uuid
from typing import Any

from utils import now_iso


TODO_DOMAINS = {"tech", "emotional"}


class TodoStore:
    """Standalone todos. Bucket-attached todos remain in bucket metadata."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(config.get("todo_db_path") or os.path.join(state_dir, "todos.sqlite"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    source_bucket TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_domain_done ON todos(domain, done)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def normalize_domain(domain: str) -> str:
        value = str(domain or "").strip().lower()
        if value not in TODO_DOMAINS:
            raise ValueError("domain must be tech or emotional")
        return value

    @staticmethod
    def _validate_context(source_bucket: str, context: str) -> tuple[str, str]:
        bucket_id = str(source_bucket or "").strip()
        safe_context = str(context or "").strip()
        if not bucket_id and not safe_context:
            raise ValueError("context is required when source_bucket is empty")
        return bucket_id, safe_context

    def create(
        self,
        *,
        content: str,
        domain: str,
        source_bucket: str = "",
        context: str = "",
        todo_id: str = "",
    ) -> dict:
        safe_content = str(content or "").strip()
        if not safe_content:
            raise ValueError("content is required")
        safe_domain = self.normalize_domain(domain)
        bucket_id, safe_context = self._validate_context(source_bucket, context)
        item_id = str(todo_id or "").strip() or uuid.uuid4().hex[:16]
        now = now_iso()
        values = {
            "id": item_id,
            "content": safe_content,
            "domain": safe_domain,
            "source_bucket": bucket_id,
            "context": safe_context,
            "done": 0,
            "created_at": now,
            "updated_at": now,
        }
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO todos (
                        id, content, domain, source_bucket, context, done, created_at, updated_at
                    ) VALUES (
                        :id, :content, :domain, :source_bucket, :context, :done, :created_at, :updated_at
                    )
                    """,
                    values,
                )
            return self.get(item_id) or values
        finally:
            conn.close()

    def list(self, *, domain: str = "", done: bool | None = None, limit: int = 200) -> list[dict]:
        params: list[Any] = []
        where: list[str] = []
        if str(domain or "").strip():
            where.append("domain = ?")
            params.append(self.normalize_domain(domain))
        if done is not None:
            where.append("done = ?")
            params.append(1 if done else 0)
        sql = "SELECT * FROM todos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY done ASC, created_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit or 200))))
        conn = self._connect()
        try:
            return [self._row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def get(self, todo_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM todos WHERE id = ?", (str(todo_id or ""),)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def update(
        self,
        todo_id: str,
        *,
        content: str | None = None,
        domain: str | None = None,
        source_bucket: str | None = None,
        context: str | None = None,
        done: bool | None = None,
    ) -> dict | None:
        current = self.get(todo_id)
        if not current:
            return None
        next_content = current["content"] if content is None else str(content or "").strip()
        if not next_content:
            raise ValueError("content is required")
        next_domain = current["domain"] if domain is None else self.normalize_domain(domain)
        next_bucket = current["source_bucket"] if source_bucket is None else str(source_bucket or "").strip()
        next_context = current["context"] if context is None else str(context or "").strip()
        next_bucket, next_context = self._validate_context(next_bucket, next_context)
        values = {
            "content": next_content,
            "domain": next_domain,
            "source_bucket": next_bucket,
            "context": next_context,
            "done": int(bool(current["done"])) if done is None else int(bool(done)),
            "updated_at": now_iso(),
            "id": str(todo_id or ""),
        }
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE todos
                    SET content = :content, domain = :domain, source_bucket = :source_bucket,
                        context = :context, done = :done, updated_at = :updated_at
                    WHERE id = :id
                    """,
                    values,
                )
            return self.get(todo_id)
        finally:
            conn.close()

    def set_done(self, todo_id: str, done: bool) -> dict | None:
        return self.update(todo_id, done=done)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "source": "standalone",
            "content": row["content"],
            "domain": row["domain"],
            "source_bucket": row["source_bucket"],
            "source_bucket_name": "",
            "context": row["context"],
            "done": bool(row["done"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
