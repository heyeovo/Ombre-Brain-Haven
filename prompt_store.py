"""Persistent product-prompt overrides stored outside memory buckets."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo


class PromptStoreError(ValueError):
    """Base error for prompt configuration requests."""


class UnknownPromptError(PromptStoreError):
    pass


class EmptyPromptError(PromptStoreError):
    pass


class PromptConflictError(PromptStoreError):
    pass


class PromptStore:
    """Store only user overrides; code defaults remain the system fact source."""

    def __init__(
        self,
        config: dict | None,
        defaults: Mapping[str, str],
        *,
        db_path: str = "",
        profile_id: str = "default",
    ) -> None:
        config = config or {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(db_path or os.path.join(state_dir, "prompt_overrides.sqlite"))
        self.profile_id = str(profile_id or "default").strip() or "default"
        self.defaults = {
            str(name).strip(): str(content).strip()
            for name, content in defaults.items()
            if str(name).strip() and str(content).strip()
        }
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS prompt_overrides (
                    profile_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (profile_id, name)
                )
                """
            )
            existing = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(prompt_overrides)").fetchall()
            }
            for name, definition in {
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE prompt_overrides ADD COLUMN {name} {definition}")
            conn.commit()
        finally:
            conn.close()

    def _validate_name(self, name: object) -> str:
        normalized = str(name or "").strip()
        if normalized not in self.defaults:
            raise UnknownPromptError(f"unknown prompt: {normalized or '<empty>'}")
        return normalized

    @staticmethod
    def _validate_content(content: object) -> str:
        if not isinstance(content, str):
            raise PromptStoreError("prompt content must be a string")
        normalized = str(content or "").strip()
        if not normalized:
            raise EmptyPromptError("prompt content must not be empty")
        if len(normalized) > 50000:
            raise PromptStoreError("prompt content exceeds 50000 characters")
        return normalized

    def _row(self, name: str) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT content, revision, updated_at FROM prompt_overrides WHERE profile_id=? AND name=?",
                (self.profile_id, name),
            ).fetchone()
        finally:
            conn.close()

    def get_effective(self, name: object) -> str:
        normalized = self._validate_name(name)
        row = self._row(normalized)
        return str(row["content"]) if row else self.defaults[normalized]

    def describe(self, name: object) -> dict:
        normalized = self._validate_name(name)
        row = self._row(normalized)
        return {
            "name": normalized,
            "content": str(row["content"]) if row else self.defaults[normalized],
            "default_content": self.defaults[normalized],
            "customized": bool(row),
            "source": "user_override" if row else "system_default",
            "revision": int(row["revision"]) if row else 0,
            "updated_at": str(row["updated_at"]) if row else "",
        }

    def list_descriptions(self) -> dict[str, dict]:
        return {name: self.describe(name) for name in self.defaults}

    def save(self, name: object, content: object, *, expected_revision: int | None = None) -> dict:
        normalized = self._validate_name(name)
        text = self._validate_content(content)
        now = datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM prompt_overrides WHERE profile_id=? AND name=?",
                (self.profile_id, normalized),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise PromptConflictError(
                    f"prompt revision changed: expected {int(expected_revision)}, current {current_revision}"
                )
            next_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO prompt_overrides(profile_id, name, content, revision, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, name) DO UPDATE SET
                    content=excluded.content,
                    revision=excluded.revision,
                    updated_at=excluded.updated_at
                """,
                (self.profile_id, normalized, text, next_revision, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.describe(normalized)

    def reset(self, name: object, *, expected_revision: int | None = None) -> dict:
        normalized = self._validate_name(name)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM prompt_overrides WHERE profile_id=? AND name=?",
                (self.profile_id, normalized),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise PromptConflictError(
                    f"prompt revision changed: expected {int(expected_revision)}, current {current_revision}"
                )
            conn.execute(
                "DELETE FROM prompt_overrides WHERE profile_id=? AND name=?",
                (self.profile_id, normalized),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.describe(normalized)
