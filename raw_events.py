from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger("ombre_brain.raw_events")

ALLOWED_RAW_ROLES = {"user", "assistant"}
RAW_EVENT_DEFAULT_SOURCE = "raw"
RAW_EVENT_RUNTIME_SCOPE = "runtime"
RAW_EVENT_ARCHIVE_SCOPE = "historical_archive"
ALLOWED_RAW_USAGE_SCOPES = {RAW_EVENT_RUNTIME_SCOPE, RAW_EVENT_ARCHIVE_SCOPE}

INJECTION_SECTION_RE = re.compile(
    r"(?im)^\s*(?:"
    r"Core Memory|Recalled Memory|Recent Context|Just Now Chat Context|"
    r"Related Memory|Dream Context|Additional private memory detail|"
    r"Long-term State Summary"
    r")\s*:?\s*$"
)
CLIENT_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*>[\s\S]*?</attachment>", re.IGNORECASE)
SELF_CLOSING_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*/>", re.IGNORECASE)
WORKSPACE_ATTACHMENT_RE = re.compile(
    r"<workspace_attachment>[\s\S]*?</workspace_attachment>",
    re.IGNORECASE,
)
CLIENT_CONTEXT_BLOCK_TITLES = {
    "当前时间",
    "当前电量",
    "当前天气",
    "当前位置",
    "当前屏幕应用",
    "应用使用时长",
    "最近通知",
    "相关记忆",
    "屏幕文本",
}


def strip_raw_client_context(text: str) -> str:
    cleaned = WORKSPACE_ATTACHMENT_RE.sub("", str(text or ""))
    cleaned = CLIENT_ATTACHMENT_RE.sub("", cleaned)
    cleaned = SELF_CLOSING_ATTACHMENT_RE.sub("", cleaned)
    cleaned = _strip_client_context_blocks(cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_client_context_blocks(text: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        title = ""
        if stripped.startswith("【") and "】" in stripped:
            title = stripped[1 : stripped.index("】")].strip()
        if title:
            skipping = title in CLIENT_CONTEXT_BLOCK_TITLES
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def raw_event_text_looks_injected(text: str, raw: dict[str, Any] | None = None) -> bool:
    raw = raw or {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    flags = {
        str(raw.get("kind") or "").lower(),
        str(raw.get("source_type") or "").lower(),
        str(metadata.get("kind") or "").lower(),
        str(metadata.get("source_type") or "").lower(),
    }
    if flags & {"injection", "memory_injection", "tool", "tool_result", "system", "developer"}:
        return True
    stripped = str(text or "").strip()
    if stripped.startswith("Live private context for the current turn"):
        return True
    if INJECTION_SECTION_RE.search(stripped):
        return True
    return "[bucket_id:" in stripped and any(
        marker in stripped
        for marker in (
            "Recalled Memory",
            "Related Memory",
            "Recent Context",
            "Core Memory",
        )
    )


class RawEventStore:
    """Append-only-ish raw dialogue archive with optional FTS search."""

    def __init__(self, config: dict):
        config = config or {}
        raw_cfg = config.get("raw_events", {}) if isinstance(config.get("raw_events", {}), dict) else {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(raw_cfg.get("db_path") or os.path.join(state_dir, "raw_events.sqlite"))
        self.archive_dir = str(raw_cfg.get("archive_dir") or os.path.join(state_dir, "raw-archives"))
        self.max_ingest_batch = max(1, min(5000, int(raw_cfg.get("max_ingest_batch", 1000))))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)
        self.fts_enabled = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
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
                usage_scope TEXT NOT NULL DEFAULT 'runtime',
                canonical_hash TEXT NOT NULL DEFAULT '',
                import_id TEXT NOT NULL DEFAULT '',
                UNIQUE(source, event_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_imports (
                import_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT '',
                source_file_sha256 TEXT NOT NULL DEFAULT '',
                selection_hash TEXT NOT NULL DEFAULT '',
                archive_sha256 TEXT NOT NULL DEFAULT '',
                archive_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'staging',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_imports_file_selection "
            "ON raw_imports(source, source_file_sha256, selection_hash) "
            "WHERE source_file_sha256 != '' AND selection_hash != ''"
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(raw_events)").fetchall()}
        if "usage_scope" not in columns:
            conn.execute(
                "ALTER TABLE raw_events ADD COLUMN usage_scope TEXT NOT NULL DEFAULT 'runtime'"
            )
        if "canonical_hash" not in columns:
            conn.execute(
                "ALTER TABLE raw_events ADD COLUMN canonical_hash TEXT NOT NULL DEFAULT ''"
            )
        if "import_id" not in columns:
            conn.execute(
                "ALTER TABLE raw_events ADD COLUMN import_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_created ON raw_events(created_at DESC, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_role ON raw_events(role, created_at DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_scope_created "
            "ON raw_events(usage_scope, created_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_scope_conversation "
            "ON raw_events(usage_scope, source, conversation_id, created_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_canonical "
            "ON raw_events(canonical_hash) WHERE canonical_hash != ''"
        )
        missing_canonical = conn.execute(
            "SELECT id, role, text, created_at FROM raw_events WHERE canonical_hash = ''"
        ).fetchall()
        if missing_canonical:
            conn.executemany(
                "UPDATE raw_events SET canonical_hash = ? WHERE id = ?",
                [
                    (
                        self.canonical_event_hash(
                            role=str(row["role"] or ""),
                            text=str(row["text"] or ""),
                            created_at=str(row["created_at"] or ""),
                        ),
                        int(row["id"]),
                    )
                    for row in missing_canonical
                ],
            )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_events_source_event_id
            ON raw_events(source, source_event_id)
            WHERE source_event_id != ''
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS raw_events_fts
                USING fts5(text, source, conversation_id, session_id, content='raw_events', content_rowid='id')
                """
            )
            self.fts_enabled = True
        except sqlite3.OperationalError as exc:
            self.fts_enabled = False
            logger.warning("raw_events FTS5 disabled: %s", exc)
        conn.commit()
        conn.close()

    def find_canonical_matches(self, hashes: list[str], *, limit: int = 5000) -> dict[str, list[dict[str, Any]]]:
        cleaned = list(dict.fromkeys(str(value or "").strip() for value in hashes if str(value or "").strip()))
        cleaned = cleaned[: max(1, min(int(limit or 5000), 5000))]
        if not cleaned:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        conn = self._connect()
        try:
            for start in range(0, len(cleaned), 500):
                batch = cleaned[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id, source, source_event_id, conversation_id, created_at, canonical_hash, usage_scope "
                    f"FROM raw_events WHERE canonical_hash IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    result.setdefault(str(row["canonical_hash"]), []).append(
                        {
                            "id": int(row["id"]),
                            "source": row["source"],
                            "source_event_id": row["source_event_id"],
                            "conversation_id": row["conversation_id"],
                            "created_at": row["created_at"],
                            "usage_scope": row["usage_scope"],
                        }
                    )
        finally:
            conn.close()
        return result

    @staticmethod
    def _clean_import_id(value: Any) -> str:
        import_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())[:120]
        if not import_id:
            raise ValueError("missing import_id")
        return import_id

    def put_archive_chunk(
        self,
        *,
        import_id: str,
        index: int,
        total_chunks: int,
        data_base64: str,
        chunk_sha256: str,
        source: str,
        source_file_sha256: str,
        selection_hash: str,
        archive_sha256: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_id = self._clean_import_id(import_id)
        safe_index = int(index)
        safe_total = int(total_chunks)
        if safe_index < 0 or safe_total < 1 or safe_index >= safe_total:
            raise ValueError("invalid archive chunk position")
        try:
            payload = base64.b64decode(str(data_base64 or ""), validate=True)
        except Exception as exc:
            raise ValueError("invalid archive chunk encoding") from exc
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("archive chunk exceeds 2 MiB")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != str(chunk_sha256 or "").strip().lower():
            raise ValueError("archive chunk hash mismatch")
        staging_dir = os.path.abspath(os.path.join(self.archive_dir, ".staging", safe_id))
        archive_root = os.path.abspath(self.archive_dir)
        if os.path.commonpath([archive_root, staging_dir]) != archive_root:
            raise ValueError("invalid archive staging path")
        os.makedirs(staging_dir, exist_ok=True)
        chunk_path = os.path.join(staging_dir, f"{safe_index:08d}.part")
        if os.path.exists(chunk_path):
            with open(chunk_path, "rb") as existing:
                existing_hash = hashlib.sha256(existing.read()).hexdigest()
            if existing_hash != digest:
                raise ValueError("archive chunk conflict")
            status = "duplicate"
        else:
            with open(chunk_path, "wb") as stream:
                stream.write(payload)
            status = "stored"
        now = self._now_iso()
        manifest = {
            **(metadata or {}),
            "total_chunks": safe_total,
            "archive_sha256": str(archive_sha256 or "").strip().lower(),
        }
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO raw_imports
                (import_id, source, source_file_sha256, selection_hash, archive_sha256,
                 status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'staging', ?, ?, ?)
                ON CONFLICT(import_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    safe_id,
                    self._clean_source(source),
                    str(source_file_sha256 or "").strip().lower(),
                    str(selection_hash or "").strip().lower(),
                    str(archive_sha256 or "").strip().lower(),
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "status": status, "import_id": safe_id, "index": safe_index}

    def commit_archive(self, import_id: str) -> dict[str, Any]:
        safe_id = self._clean_import_id(import_id)
        conn = self._connect()
        row = conn.execute("SELECT * FROM raw_imports WHERE import_id = ?", (safe_id,)).fetchone()
        conn.close()
        if not row:
            raise ValueError("archive import not found")
        if row["status"] == "archived" and os.path.isfile(row["archive_path"]):
            return {"ok": True, "status": "duplicate", "import_id": safe_id, "archive_path": row["archive_path"]}
        metadata = json.loads(row["metadata_json"] or "{}")
        total_chunks = int(metadata.get("total_chunks") or 0)
        expected_hash = str(row["archive_sha256"] or "").strip().lower()
        staging_dir = os.path.join(self.archive_dir, ".staging", safe_id)
        chunk_paths = [os.path.join(staging_dir, f"{index:08d}.part") for index in range(total_chunks)]
        if total_chunks < 1 or not all(os.path.isfile(path) for path in chunk_paths):
            raise ValueError("archive chunks incomplete")
        final_path = os.path.abspath(os.path.join(self.archive_dir, f"{safe_id}.zip"))
        temp_path = final_path + ".tmp"
        digest = hashlib.sha256()
        with open(temp_path, "wb") as output:
            for chunk_path in chunk_paths:
                with open(chunk_path, "rb") as source_stream:
                    while True:
                        chunk = source_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        output.write(chunk)
        if digest.hexdigest() != expected_hash:
            os.remove(temp_path)
            raise ValueError("archive hash mismatch")
        os.replace(temp_path, final_path)
        shutil.rmtree(staging_dir)
        now = self._now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE raw_imports SET status = 'archived', archive_path = ?, updated_at = ? WHERE import_id = ?",
                (final_path, now, safe_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "status": "archived", "import_id": safe_id, "archive_path": final_path}

    def ingest(self, events: list[dict[str, Any]], *, source: str = "") -> dict[str, Any]:
        safe_source = self._clean_source(source)
        now = self._now_iso()
        items = []
        inserted = 0
        duplicate = 0
        conflict = 0
        rejected = 0
        for raw in list(events or [])[: self.max_ingest_batch]:
            normalized, reason = self._normalize_event(raw, default_source=safe_source, ingested_at=now)
            if reason:
                rejected += 1
                items.append(
                    {
                        "status": "rejected",
                        "reason": reason,
                        "source_event_id": str((raw or {}).get("source_event_id") or (raw or {}).get("id") or ""),
                    }
                )
                continue
            status, row_id = self._insert_event(normalized)
            if status == "inserted":
                inserted += 1
            elif status == "conflict":
                conflict += 1
            else:
                duplicate += 1
            items.append(
                {
                    "status": status,
                    "id": row_id,
                    "source": normalized["source"],
                    "source_event_id": normalized["source_event_id"],
                    "role": normalized["role"],
                }
            )
        return {
            "ok": True,
            "inserted": inserted,
            "duplicate": duplicate,
            "conflict": conflict,
            "rejected": rejected,
            "items": items,
        }

    def search(
        self,
        query: str = "",
        *,
        limit: int = 10,
        source: str = "",
        role: str = "",
        conversation_id: str = "",
        session_id: str = "",
        since: str = "",
        until: str = "",
        usage_scope: str = RAW_EVENT_RUNTIME_SCOPE,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(100, int(limit or 10)))
        cleaned_query = str(query or "").strip()
        filters, params = self._search_filters(
            source=source,
            role=role,
            conversation_id=conversation_id,
            session_id=session_id,
            since=since,
            until=until,
            usage_scope=usage_scope,
        )
        rows = self._search_fts(cleaned_query, filters, params, safe_limit) if cleaned_query else []
        if len(rows) < safe_limit:
            rows = self._merge_rows(
                rows,
                self._search_like(cleaned_query, filters, params, safe_limit) if cleaned_query else self._search_recent(filters, params, safe_limit),
                safe_limit,
            )
        return {
            "ok": True,
            "query": cleaned_query,
            "count": len(rows),
            "items": [self._row_to_event(row) for row in rows],
        }

    def list_archive_conversations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        source: str = "",
    ) -> dict[str, Any]:
        """List imported historical windows without exposing runtime raw events."""
        safe_limit = max(1, min(500, int(limit or 100)))
        safe_offset = max(0, int(offset or 0))
        clauses = ["usage_scope = ?", "conversation_id != ''"]
        params: list[Any] = [RAW_EVENT_ARCHIVE_SCOPE]
        if source:
            clauses.append("source = ?")
            params.append(self._clean_source(source))
        where = " AND ".join(clauses)
        conn = self._connect()
        try:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT 1 FROM raw_events WHERE {where} GROUP BY source, conversation_id)",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT source, conversation_id, MAX(client) AS client,
                       COUNT(*) AS message_count,
                       MIN(created_at) AS first_at, MAX(created_at) AS last_at
                FROM raw_events
                WHERE {where}
                GROUP BY source, conversation_id
                ORDER BY last_at DESC, conversation_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
            items = []
            for row in rows:
                metadata_row = conn.execute(
                    """
                    SELECT metadata_json FROM raw_events
                    WHERE usage_scope = ? AND source = ? AND conversation_id = ?
                    ORDER BY created_at ASC, id ASC LIMIT 1
                    """,
                    [RAW_EVENT_ARCHIVE_SCOPE, row["source"], row["conversation_id"]],
                ).fetchone()
                metadata = {}
                try:
                    metadata = json.loads((metadata_row["metadata_json"] if metadata_row else "") or "{}")
                except Exception:
                    metadata = {}
                items.append(
                    {
                        "source": row["source"],
                        "client": row["client"],
                        "conversation_id": row["conversation_id"],
                        "title": str(metadata.get("conversation_title") or ""),
                        "message_count": int(row["message_count"] or 0),
                        "first_at": row["first_at"],
                        "last_at": row["last_at"],
                    }
                )
        finally:
            conn.close()
        return {
            "ok": True,
            "count": len(items),
            "total": total,
            "offset": safe_offset,
            "has_more": safe_offset + len(items) < total,
            "items": items,
        }

    def list_archive_conversation_events(
        self,
        *,
        conversation_id: str,
        source: str = "",
        limit: int = 50,
        offset: int = 0,
        query: str = "",
    ) -> dict[str, Any]:
        """Read one imported window from oldest to newest with stable, bounded pages."""
        safe_conversation_id = str(conversation_id or "").strip()
        if not safe_conversation_id:
            raise ValueError("missing conversation_id")
        safe_limit = max(1, min(100, int(limit or 50)))
        safe_offset = max(0, int(offset or 0))
        cleaned_query = str(query or "").strip()
        clauses = ["usage_scope = ?", "conversation_id = ?"]
        params: list[Any] = [RAW_EVENT_ARCHIVE_SCOPE, safe_conversation_id]
        if source:
            clauses.append("source = ?")
            params.append(self._clean_source(source))
        if cleaned_query:
            clauses.append("text LIKE ?")
            params.append(f"%{cleaned_query}%")
        where = " AND ".join(clauses)
        conn = self._connect()
        try:
            total = int(conn.execute(f"SELECT COUNT(*) FROM raw_events WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM raw_events
                WHERE {where}
                ORDER BY created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
        finally:
            conn.close()
        items = [self._row_to_event(row) for row in rows]
        return {
            "ok": True,
            "query": cleaned_query,
            "count": len(items),
            "total": total,
            "offset": safe_offset,
            "has_more": safe_offset + len(items) < total,
            "items": items,
        }

    def list_events_between(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        limit: int = 40,
        source: str = "",
        conversation_id: str = "",
        session_id: str = "",
        usage_scope: str = RAW_EVENT_RUNTIME_SCOPE,
    ) -> list[dict[str, Any]]:
        try:
            raw_limit = int(limit)
        except (TypeError, ValueError):
            raw_limit = 40
        safe_limit = max(0, min(10000, raw_limit))
        filters, params = self._search_filters(
            source=source,
            conversation_id=conversation_id,
            session_id=session_id,
            usage_scope=usage_scope,
        )
        conn = self._connect()
        if safe_limit > 0:
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.id DESC
                LIMIT ?
                """,
                [*params, max(safe_limit, 500)],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.id DESC
                """,
                params,
            ).fetchall()
        conn.close()

        compare_tz = start_at.tzinfo or end_at.tzinfo

        def parse_local(value: Any) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                return None
            if compare_tz is None:
                return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=compare_tz)
            return parsed.astimezone(compare_tz)

        start = start_at
        end = end_at
        if compare_tz is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=compare_tz)
            else:
                start = start.astimezone(compare_tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=compare_tz)
            else:
                end = end.astimezone(compare_tz)
        elif start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        elif end.tzinfo is not None:
            end = end.replace(tzinfo=None)

        selected: list[dict[str, Any]] = []
        for row in rows:
            created = parse_local(row["created_at"])
            if created is None or not (start <= created < end):
                continue
            selected.append(self._row_to_event(row))
            if safe_limit > 0 and len(selected) >= safe_limit:
                break
        return selected

    def _insert_event(self, event: dict[str, Any]) -> tuple[str, int | None]:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO raw_events
                (source, source_event_id, event_hash, role, text, created_at, ingested_at,
                 conversation_id, session_id, client, metadata_json, usage_scope,
                 canonical_hash, import_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["source"],
                    event["source_event_id"],
                    event["event_hash"],
                    event["role"],
                    event["text"],
                    event["created_at"],
                    event["ingested_at"],
                    event["conversation_id"],
                    event["session_id"],
                    event["client"],
                    event["metadata_json"],
                    event["usage_scope"],
                    event["canonical_hash"],
                    event["import_id"],
                ),
            )
            if cursor.rowcount:
                row_id = int(cursor.lastrowid or 0)
                if self.fts_enabled:
                    try:
                        conn.execute(
                            """
                            INSERT INTO raw_events_fts(rowid, text, source, conversation_id, session_id)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                row_id,
                                event["text"],
                                event["source"],
                                event["conversation_id"],
                                event["session_id"],
                            ),
                        )
                    except sqlite3.OperationalError as exc:
                        logger.warning("raw_events FTS insert failed: %s", exc)
                conn.commit()
                return "inserted", row_id
            conn.commit()
            if event.get("source_event_id"):
                existing = conn.execute(
                    "SELECT id, event_hash FROM raw_events "
                    "WHERE source = ? AND source_event_id = ? LIMIT 1",
                    (event["source"], event["source_event_id"]),
                ).fetchone()
                if existing:
                    status = "duplicate" if existing["event_hash"] == event["event_hash"] else "conflict"
                    return status, int(existing["id"])
            row_id = self._find_existing_id(conn, event)
            return "duplicate", row_id
        finally:
            conn.close()

    def _find_existing_id(self, conn: sqlite3.Connection, event: dict[str, Any]) -> int | None:
        if event.get("source_event_id"):
            row = conn.execute(
                "SELECT id FROM raw_events WHERE source = ? AND source_event_id = ? LIMIT 1",
                (event["source"], event["source_event_id"]),
            ).fetchone()
            if row:
                return int(row["id"])
        row = conn.execute(
            "SELECT id FROM raw_events WHERE source = ? AND event_hash = ? LIMIT 1",
            (event["source"], event["event_hash"]),
        ).fetchone()
        return int(row["id"]) if row else None

    def _normalize_event(
        self,
        raw: dict[str, Any] | None,
        *,
        default_source: str,
        ingested_at: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(raw, dict):
            return None, "invalid_event"
        role = str(raw.get("role") or "").strip().lower()
        if role not in ALLOWED_RAW_ROLES:
            return None, "invalid_role"
        text = strip_raw_client_context(self._coerce_text(raw.get("text", raw.get("content", ""))))
        if not text:
            return None, "empty_text"
        if self._looks_injected(text, raw):
            return None, "injected_context"

        source = self._clean_source(raw.get("source") or default_source)
        source_event_id = str(raw.get("source_event_id") or raw.get("event_id") or raw.get("id") or "").strip()
        conversation_id = str(raw.get("conversation_id") or raw.get("thread_id") or "").strip()
        session_id = str(raw.get("session_id") or "").strip()
        client = str(raw.get("client") or "").strip()
        usage_scope = self._clean_usage_scope(raw.get("usage_scope"))
        raw_time = raw.get("created_at") or raw.get("timestamp") or raw.get("time")
        created_at = self._clean_time(
            raw_time if raw_time is not None else ("" if usage_scope == RAW_EVENT_ARCHIVE_SCOPE else ingested_at)
        )
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        event_hash = self._event_hash(
            source=source,
            source_event_id=source_event_id,
            role=role,
            text=text,
            created_at=created_at,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        canonical_hash = self.canonical_event_hash(
            role=role,
            text=text,
            created_at=created_at,
        )
        return {
            "source": source,
            "source_event_id": source_event_id,
            "event_hash": event_hash,
            "role": role,
            "text": text,
            "created_at": created_at,
            "ingested_at": ingested_at,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "client": client,
            "metadata_json": metadata_json,
            "usage_scope": usage_scope,
            "canonical_hash": canonical_hash,
            "import_id": str(raw.get("import_id") or "").strip()[:160],
        }, ""

    @staticmethod
    def _clean_source(value: Any) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or RAW_EVENT_DEFAULT_SOURCE).strip())
        return text[:80] or RAW_EVENT_DEFAULT_SOURCE

    @staticmethod
    def _clean_time(value: Any) -> str:
        text = str(value or "").strip()
        return text[:80]

    @staticmethod
    def _clean_usage_scope(value: Any) -> str:
        scope = str(value or RAW_EVENT_RUNTIME_SCOPE).strip().lower()
        return scope if scope in ALLOWED_RAW_USAGE_SCOPES else RAW_EVENT_RUNTIME_SCOPE

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    item_type = str(item.get("type") or "").lower()
                    if item_type in {"tool_result", "tool_use", "function_call", "function_result"}:
                        continue
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(part for part in parts if part)
        return str(value or "")

    @staticmethod
    def _looks_injected(text: str, raw: dict[str, Any]) -> bool:
        return raw_event_text_looks_injected(text, raw)

    @staticmethod
    def _event_hash(**parts: str) -> str:
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def canonical_event_hash(*, role: str, text: str, created_at: str = "") -> str:
        normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
        payload = json.dumps(
            {
                "role": str(role or "").strip().lower(),
                "text": normalized_text,
                "created_at": str(created_at or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        metadata = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return {
            "id": row["id"],
            "source": row["source"],
            "source_event_id": row["source_event_id"],
            "role": row["role"],
            "text": row["text"],
            "created_at": row["created_at"],
            "ingested_at": row["ingested_at"],
            "conversation_id": row["conversation_id"],
            "session_id": row["session_id"],
            "client": row["client"],
            "usage_scope": row["usage_scope"],
            "canonical_hash": row["canonical_hash"],
            "import_id": row["import_id"],
            "metadata": metadata,
        }

    def _search_filters(
        self,
        *,
        source: str = "",
        role: str = "",
        conversation_id: str = "",
        session_id: str = "",
        since: str = "",
        until: str = "",
        usage_scope: str = RAW_EVENT_RUNTIME_SCOPE,
    ) -> tuple[str, list[Any]]:
        clauses = []
        params: list[Any] = []
        if source:
            clauses.append("e.source = ?")
            params.append(self._clean_source(source))
        role = str(role or "").strip().lower()
        if role in ALLOWED_RAW_ROLES:
            clauses.append("e.role = ?")
            params.append(role)
        if conversation_id:
            clauses.append("e.conversation_id = ?")
            params.append(str(conversation_id))
        if session_id:
            clauses.append("e.session_id = ?")
            params.append(str(session_id))
        if since:
            clauses.append("e.created_at >= ?")
            params.append(str(since))
        if until:
            clauses.append("e.created_at <= ?")
            params.append(str(until))
        scope = str(usage_scope or "").strip().lower()
        if scope != "all":
            clauses.append("e.usage_scope = ?")
            params.append(self._clean_usage_scope(scope))
        return (" AND " + " AND ".join(clauses)) if clauses else "", params

    def _search_fts(self, query: str, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        if not self.fts_enabled or not query:
            return []
        match = '"' + query.replace('"', '""') + '"'
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events_fts f
                JOIN raw_events e ON e.id = f.rowid
                WHERE raw_events_fts MATCH ? {filters}
                ORDER BY bm25(raw_events_fts), e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [match, *params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def _search_like(self, query: str, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE e.text LIKE ? {filters}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [f"%{query}%", *params, limit],
            ).fetchall()
        finally:
            conn.close()

    def _search_recent(self, filters: str, params: list[Any], limit: int) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                f"""
                SELECT e.*
                FROM raw_events e
                WHERE 1 = 1 {filters}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _merge_rows(first: list[sqlite3.Row], second: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
        rows = []
        seen = set()
        for row in [*(first or []), *(second or [])]:
            row_id = int(row["id"])
            if row_id in seen:
                continue
            seen.add(row_id)
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows
