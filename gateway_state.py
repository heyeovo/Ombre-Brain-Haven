import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


class GatewayStateStore:
    """
    Tracks successful gateway rounds and which dynamic buckets were injected
    per session, so cooldown and recent-round skipping can work.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        """给已存在的表补列（幂等）。老库升级用，不重建表。"""
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_rounds (
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, round_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS injected_buckets (
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                bucket_id TEXT NOT NULL,
                injected_at TEXT NOT NULL,
                PRIMARY KEY (session_id, round_id, bucket_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_injected_lookup
            ON injected_buckets (session_id, bucket_id, injected_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS injection_debug (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_injection_debug_lookup
            ON injection_debug (session_id, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_context_injections (
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                injected_at TEXT NOT NULL,
                PRIMARY KEY (session_id, round_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recent_context_lookup
            ON recent_context_injections (session_id, injected_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                user_text TEXT NOT NULL DEFAULT '',
                assistant_text TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                client TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                UNIQUE(profile_id, session_id, round_id)
            )
            """
        )
        # 老库补列：conversation_turns 建表时没有 source / raw_json，
        # 新前端（cc 引擎）写入要靠它们区分来源、留一份原始 JSON。
        # 不重建表、不动已有行 —— 已有数据的 source 落到默认值 'gateway'。
        self._ensure_columns(
            conn,
            "conversation_turns",
            {
                "source": "TEXT NOT NULL DEFAULT 'gateway'",
                "raw_json": "TEXT NOT NULL DEFAULT ''",
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent
            ON conversation_turns (profile_id, created_at DESC, id DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_turns_session
            ON conversation_turns (profile_id, session_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS upstream_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                prompt_cache_hit_tokens INTEGER,
                prompt_cache_miss_tokens INTEGER,
                cached_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                usage_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_upstream_usage_lookup
            ON upstream_usage (session_id, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS handoff_blocks (
                session_id TEXT PRIMARY KEY,
                handoff_block TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # cc 前端的协作者配置（4.5b）。存这里而不是浏览器 localStorage，
        # 是为了避免 Polaris 那个「手机和 PC 两份数据」的坑 —— 所有入口读同一份。
        # memory_entries 存 JSON 数组；engine 取值 subscription | api | selfhost。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_personas (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                initial TEXT NOT NULL DEFAULT '',
                tint TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                memory_entries TEXT NOT NULL DEFAULT '[]',
                recall_on INTEGER NOT NULL DEFAULT 1,
                semantic_on INTEGER NOT NULL DEFAULT 1,
                engine TEXT NOT NULL DEFAULT 'api',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()
        conn.close()

    def save_handoff_block(self, session_id: str, handoff_block: str) -> None:
        from utils import now_iso
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO handoff_blocks (session_id, handoff_block, created_at) VALUES (?, ?, ?)",
            (session_id, handoff_block, now_iso()),
        )
        conn.commit()
        conn.close()

    def load_handoff_block(self, session_id: str) -> str | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT handoff_block FROM handoff_blocks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.close()
        if row:
            return str(row[0])
        return None

    # ------------------------------------------------------------------
    # cc 前端协作者配置（4.5b）
    # ------------------------------------------------------------------

    _CC_PERSONA_TEXT_FIELDS = (
        "name",
        "initial",
        "tint",
        "user_name",
        "purpose",
        "description",
        "prompt",
        "engine",
    )

    @staticmethod
    def _cc_persona_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            entries = json.loads(row["memory_entries"] or "[]")
        except (TypeError, ValueError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        return {
            "id": str(row["id"]),
            "name": row["name"] or "",
            "initial": row["initial"] or "",
            "tint": row["tint"] or "",
            "user_name": row["user_name"] or "",
            "purpose": row["purpose"] or "",
            "description": row["description"] or "",
            "prompt": row["prompt"] or "",
            "memory_entries": [str(item) for item in entries if str(item).strip()],
            "recall_on": bool(row["recall_on"]),
            "semantic_on": bool(row["semantic_on"]),
            "engine": row["engine"] or "api",
            "sort_order": int(row["sort_order"] or 0),
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
        }

    def list_cc_personas(self) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT * FROM cc_personas
            ORDER BY sort_order ASC, created_at ASC, id ASC
            """
        ).fetchall()
        conn.close()
        return [self._cc_persona_row_to_dict(row) for row in rows]

    def get_cc_persona(self, persona_id: str) -> dict[str, Any] | None:
        safe_id = str(persona_id or "").strip()
        if not safe_id:
            return None
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM cc_personas WHERE id = ?", (safe_id,)
        ).fetchone()
        conn.close()
        return self._cc_persona_row_to_dict(row) if row else None

    def save_cc_persona(self, persona: dict[str, Any]) -> dict[str, Any] | None:
        """
        upsert 一个协作者。已存在时只更新 payload 里出现的字段（PATCH 语义），
        没出现的保持原值 —— 界面按 tab 分开保存，不能因为某个 tab 没送就把别的清空。
        """
        from utils import now_iso

        safe_id = str(persona.get("id") or "").strip()
        if not safe_id:
            return None
        existing = self.get_cc_persona(safe_id)
        now = now_iso()

        merged: dict[str, Any] = dict(existing or {})
        for field in self._CC_PERSONA_TEXT_FIELDS:
            if field in persona:
                merged[field] = str(persona.get(field) or "")
        for field in ("recall_on", "semantic_on"):
            if field in persona:
                merged[field] = bool(persona.get(field))
        if "sort_order" in persona:
            try:
                merged["sort_order"] = int(persona.get("sort_order") or 0)
            except (TypeError, ValueError):
                merged["sort_order"] = 0
        if "memory_entries" in persona:
            raw_entries = persona.get("memory_entries")
            if isinstance(raw_entries, list):
                merged["memory_entries"] = [
                    str(item).strip() for item in raw_entries if str(item).strip()
                ]
            else:
                merged["memory_entries"] = []

        merged.setdefault("recall_on", True)
        merged.setdefault("semantic_on", True)
        merged.setdefault("engine", "api")
        merged.setdefault("sort_order", 0)
        merged.setdefault("memory_entries", [])
        for field in self._CC_PERSONA_TEXT_FIELDS:
            merged.setdefault(field, "")

        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO cc_personas
            (id, name, initial, tint, user_name, purpose, description, prompt,
             memory_entries, recall_on, semantic_on, engine, sort_order,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_id,
                merged["name"],
                merged["initial"],
                merged["tint"],
                merged["user_name"],
                merged["purpose"],
                merged["description"],
                merged["prompt"],
                json.dumps(merged["memory_entries"], ensure_ascii=False),
                1 if merged["recall_on"] else 0,
                1 if merged["semantic_on"] else 0,
                merged["engine"] or "api",
                int(merged["sort_order"] or 0),
                (existing or {}).get("created_at") or now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_cc_persona(safe_id)

    def delete_cc_persona(self, persona_id: str) -> bool:
        safe_id = str(persona_id or "").strip()
        if not safe_id:
            return False
        conn = self._connect()
        cursor = conn.execute("DELETE FROM cc_personas WHERE id = ?", (safe_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def record_success(
        self,
        session_id: str,
        bucket_ids: list[str],
        completed_at: datetime | None = None,
    ) -> int:
        completed_at = completed_at or datetime.now()
        completed_iso = completed_at.isoformat(timespec="seconds")
        conn = self._connect()
        row = conn.execute(
            "SELECT COALESCE(MAX(round_id), 0) AS current_round FROM request_rounds WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        next_round = int(row["current_round"]) + 1
        conn.execute(
            "INSERT INTO request_rounds (session_id, round_id, completed_at) VALUES (?, ?, ?)",
            (session_id, next_round, completed_iso),
        )
        for bucket_id in bucket_ids:
            conn.execute(
                """
                INSERT OR REPLACE INTO injected_buckets
                (session_id, round_id, bucket_id, injected_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, next_round, bucket_id, completed_iso),
            )
        conn.commit()
        conn.close()
        return next_round

    def get_current_round(self, session_id: str) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT COALESCE(MAX(round_id), 0) AS current_round FROM request_rounds WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.close()
        return int(row["current_round"]) if row else 0

    def get_last_success_at(self, session_id: str) -> datetime | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT completed_at
            FROM request_rounds
            WHERE session_id = ?
            ORDER BY round_id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["completed_at"]))
        except ValueError:
            return None

    def get_recent_bucket_ids(self, session_id: str, recent_rounds: int) -> set[str]:
        if recent_rounds <= 0:
            return set()
        conn = self._connect()
        current_round = self.get_current_round(session_id)
        if current_round <= 0:
            conn.close()
            return set()
        min_round = max(1, current_round - recent_rounds + 1)
        rows = conn.execute(
            """
            SELECT DISTINCT bucket_id
            FROM injected_buckets
            WHERE session_id = ? AND round_id >= ?
            """,
            (session_id, min_round),
        ).fetchall()
        conn.close()
        return {row["bucket_id"] for row in rows}

    def get_last_injected_at(self, session_id: str, bucket_id: str) -> datetime | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT injected_at
            FROM injected_buckets
            WHERE session_id = ? AND bucket_id = ?
            ORDER BY injected_at DESC
            LIMIT 1
            """,
            (session_id, bucket_id),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["injected_at"]))
        except ValueError:
            return None

    def record_recent_context_injection(
        self,
        session_id: str,
        round_id: int,
        injected_at: datetime | None = None,
    ) -> None:
        injected_at = injected_at or datetime.now()
        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO recent_context_injections
            (session_id, round_id, injected_at)
            VALUES (?, ?, ?)
            """,
            (session_id, int(round_id), injected_at.isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()

    def get_last_recent_context_at(self, session_id: str) -> datetime | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT injected_at
            FROM recent_context_injections
            WHERE session_id = ?
            ORDER BY injected_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["injected_at"]))
        except ValueError:
            return None

    def record_injection_debug(
        self,
        session_id: str,
        round_id: int,
        payload: dict[str, Any],
        *,
        max_entries: int = 80,
    ) -> int:
        created_at = datetime.now().isoformat(timespec="seconds")
        body = json.dumps(payload, ensure_ascii=False)
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO injection_debug (session_id, round_id, created_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, int(round_id), created_at, body),
        )
        debug_id = int(cursor.lastrowid or 0)
        conn.execute(
            """
            DELETE FROM injection_debug
            WHERE id NOT IN (
                SELECT id FROM injection_debug ORDER BY id DESC LIMIT ?
            )
            """,
            (max(1, int(max_entries)),),
        )
        conn.commit()
        conn.close()
        return debug_id

    def record_conversation_turn(
        self,
        *,
        profile_id: str,
        session_id: str,
        round_id: int,
        user_text: str,
        assistant_text: str = "",
        model: str = "",
        client: str = "",
        route: str = "",
        source: str = "gateway",
        raw_json: str = "",
        created_at: datetime | None = None,
        max_entries: int = 500,
    ) -> int:
        created_at = created_at or datetime.now(timezone.utc)
        created_iso = created_at.isoformat(timespec="seconds")
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "default").strip() or "default"
        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO conversation_turns
            (profile_id, session_id, round_id, created_at, user_text, assistant_text,
             model, client, route, source, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_profile_id,
                safe_session_id,
                int(round_id),
                created_iso,
                str(user_text or ""),
                str(assistant_text or ""),
                str(model or ""),
                str(client or ""),
                str(route or ""),
                str(source or "gateway").strip() or "gateway",
                str(raw_json or ""),
            ),
        )
        if max_entries > 0:
            conn.execute(
                """
                DELETE FROM conversation_turns
                WHERE profile_id = ?
                  AND id NOT IN (
                    SELECT id FROM conversation_turns
                    WHERE profile_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (safe_profile_id, safe_profile_id, max(1, int(max_entries))),
            )
        conn.commit()
        turn_id = int(cursor.lastrowid or 0)
        conn.close()
        return turn_id

    def list_recent_conversation_turns(
        self,
        *,
        profile_id: str,
        session_id: str | None = None,
        limit: int = 10,
        hours: float = 6.0,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(50, int(limit or 10)))
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours or 0)))
        conn = self._connect()
        where_clause = "profile_id = ? AND created_at >= ?"
        params: list[Any] = [safe_profile_id, cutoff.isoformat(timespec="seconds")]
        if safe_session_id:
            where_clause += " AND session_id = ?"
            params.append(safe_session_id)
        params.append(safe_limit)
        rows = conn.execute(
            f"""
            SELECT id, profile_id, session_id, round_id, created_at,
                   user_text, assistant_text, model, client, route, source
            FROM conversation_turns
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        conn.close()
        return [
            {
                "id": row["id"],
                "profile_id": row["profile_id"],
                "session_id": row["session_id"],
                "round_id": row["round_id"],
                "created_at": row["created_at"],
                "user_text": row["user_text"] or "",
                "assistant_text": row["assistant_text"] or "",
                "model": row["model"] or "",
                "client": row["client"] or "",
                "route": row["route"] or "",
                "source": row["source"] or "gateway",
            }
            for row in rows
        ]

    def list_conversation_turns_between(
        self,
        *,
        profile_id: str,
        start_at: datetime,
        end_at: datetime,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(80, int(limit or 12)))
        safe_profile_id = str(profile_id or "default").strip() or "default"
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT id, profile_id, session_id, round_id, created_at,
                   user_text, assistant_text, model, client, route, source
            FROM conversation_turns
            WHERE profile_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_profile_id, max(safe_limit, 500)),
        ).fetchall()
        conn.close()

        compare_tz = start_at.tzinfo or end_at.tzinfo

        def parse_local(value: Any) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(str(value or ""))
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

        filtered = []
        for row in rows:
            created = parse_local(row["created_at"])
            if created is None or not (start <= created < end):
                continue
            filtered.append(row)
            if len(filtered) >= safe_limit:
                break

        return [
            {
                "id": row["id"],
                "profile_id": row["profile_id"],
                "session_id": row["session_id"],
                "round_id": row["round_id"],
                "created_at": row["created_at"],
                "user_text": row["user_text"] or "",
                "assistant_text": row["assistant_text"] or "",
                "model": row["model"] or "",
                "client": row["client"] or "",
                "route": row["route"] or "",
                "source": row["source"] or "gateway",
            }
            for row in filtered
        ]

    def next_conversation_round_id(self, *, profile_id: str, session_id: str) -> int:
        """
        同 session 里的下一个 round_id。
        cc 引擎那条路没有 request_rounds 的注入冷却语义，所以不走 record_success，
        直接按这张表自己算 —— date_recall 拼原文时按 (session_id, round_id) 排序，
        口径跟 /v1/messages 那条链一致。
        """
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "default").strip() or "default"
        conn = self._connect()
        row = conn.execute(
            """
            SELECT COALESCE(MAX(round_id), 0) AS current_round
            FROM conversation_turns
            WHERE profile_id = ? AND session_id = ?
            """,
            (safe_profile_id, safe_session_id),
        ).fetchone()
        conn.close()
        return int(row["current_round"] or 0) + 1

    def list_conversation_sessions(
        self,
        *,
        profile_id: str,
        limit: int = 50,
        source: str = "",
    ) -> list[dict[str, Any]]:
        """会话列表：每个 session_id 一行，带轮数、时间范围和第一句用户原话做标题。"""
        safe_limit = max(1, min(200, int(limit or 50)))
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_source = str(source or "").strip()
        where_clause = "profile_id = ?"
        params: list[Any] = [safe_profile_id]
        if safe_source:
            where_clause += " AND source = ?"
            params.append(safe_source)
        params.append(safe_limit)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT session_id,
                   COUNT(*) AS turn_count,
                   MIN(created_at) AS first_at,
                   MAX(created_at) AS last_at,
                   MAX(id) AS last_id,
                   MIN(id) AS first_id
            FROM conversation_turns
            WHERE {where_clause}
            GROUP BY session_id
            ORDER BY last_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        sessions = []
        for row in rows:
            head = conn.execute(
                """
                SELECT user_text, model, client, route, source
                FROM conversation_turns
                WHERE id = ?
                """,
                (row["first_id"],),
            ).fetchone()
            title = ((head["user_text"] if head else "") or "").strip().replace("\n", " ")
            sessions.append(
                {
                    "session_id": row["session_id"],
                    "turn_count": int(row["turn_count"] or 0),
                    "first_at": row["first_at"],
                    "last_at": row["last_at"],
                    "title": title[:80],
                    "model": (head["model"] if head else "") or "",
                    "client": (head["client"] if head else "") or "",
                    "route": (head["route"] if head else "") or "",
                    "source": (head["source"] if head else "") or "gateway",
                }
            )
        conn.close()
        return sessions

    def list_conversation_turns_by_session(
        self,
        *,
        profile_id: str,
        session_id: str,
        limit: int = 200,
        before_id: int | None = None,
        include_raw: bool = False,
    ) -> list[dict[str, Any]]:
        """某个会话的消息，按时间正序返回（界面直接顺着渲染）。"""
        safe_limit = max(1, min(500, int(limit or 200)))
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return []
        where_clause = "profile_id = ? AND session_id = ?"
        params: list[Any] = [safe_profile_id, safe_session_id]
        if before_id is not None:
            where_clause += " AND id < ?"
            params.append(int(before_id))
        params.append(safe_limit)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT id, profile_id, session_id, round_id, created_at,
                   user_text, assistant_text, model, client, route, source, raw_json
            FROM conversation_turns
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        conn.close()
        turns = [
            {
                "id": row["id"],
                "profile_id": row["profile_id"],
                "session_id": row["session_id"],
                "round_id": row["round_id"],
                "created_at": row["created_at"],
                "user_text": row["user_text"] or "",
                "assistant_text": row["assistant_text"] or "",
                "model": row["model"] or "",
                "client": row["client"] or "",
                "route": row["route"] or "",
                "source": row["source"] or "gateway",
                **({"raw_json": row["raw_json"] or ""} if include_raw else {}),
            }
            for row in rows
        ]
        turns.reverse()
        return turns

    def list_injection_debug(
        self,
        *,
        session_id: str = "",
        limit: int = 20,
        include_context: bool = True,
        include_payload: bool = True,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        conn = self._connect()
        if session_id:
            rows = conn.execute(
                """
                SELECT id, session_id, round_id, created_at, payload_json
                FROM injection_debug
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, session_id, round_id, created_at, payload_json
                FROM injection_debug
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        conn.close()

        items: list[dict[str, Any]] = []
        for row in rows:
            if not include_payload:
                items.append(
                    {
                        "id": row["id"],
                        "session_id": row["session_id"],
                        "round_id": row["round_id"],
                        "created_at": row["created_at"],
                    }
                )
                continue
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload_json"]}
            if isinstance(payload, dict) and not include_context:
                payload = dict(payload)
                payload.pop("stable_context", None)
                payload.pop("dynamic_context", None)
            items.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "round_id": row["round_id"],
                    "created_at": row["created_at"],
                    "payload": payload,
                }
            )
        return items

    def record_upstream_usage(
        self,
        *,
        session_id: str,
        round_id: int,
        model: str,
        route: str,
        usage: dict[str, Any],
        max_entries: int = 200,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        safe_usage = dict(usage or {})
        prompt_tokens = safe_usage.get("prompt_tokens") or safe_usage.get("input_tokens")
        completion_tokens = safe_usage.get("completion_tokens") or safe_usage.get("output_tokens")
        prompt_details = safe_usage.get("prompt_tokens_details")
        cached_tokens = None
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens")

        conn = self._connect()
        cursor = conn.execute(
            """
            INSERT INTO upstream_usage (
                session_id, round_id, created_at, model, route,
                prompt_tokens, completion_tokens,
                prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                usage_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(session_id or "default"),
                int(round_id),
                created_at,
                str(model or ""),
                str(route or ""),
                _optional_int(prompt_tokens),
                _optional_int(completion_tokens),
                _optional_int(safe_usage.get("prompt_cache_hit_tokens")),
                _optional_int(safe_usage.get("prompt_cache_miss_tokens")),
                _optional_int(cached_tokens),
                _optional_int(safe_usage.get("cache_read_input_tokens")),
                _optional_int(safe_usage.get("cache_creation_input_tokens")),
                json.dumps(safe_usage, ensure_ascii=False),
            ),
        )
        usage_id = int(cursor.lastrowid or 0)
        if max_entries > 0:
            conn.execute(
                """
                DELETE FROM upstream_usage
                WHERE id NOT IN (
                    SELECT id FROM upstream_usage ORDER BY id DESC LIMIT ?
                )
                """,
                (max(1, int(max_entries)),),
            )
        conn.commit()
        conn.close()
        return usage_id

    def list_upstream_usage(
        self,
        *,
        session_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit or 20)))
        conn = self._connect()
        if session_id:
            rows = conn.execute(
                """
                SELECT id, session_id, round_id, created_at, model, route,
                       prompt_tokens, completion_tokens,
                       prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                       cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                       usage_json
                FROM upstream_usage
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, session_id, round_id, created_at, model, route,
                       prompt_tokens, completion_tokens,
                       prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                       cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                       usage_json
                FROM upstream_usage
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        conn.close()

        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                usage = json.loads(row["usage_json"] or "{}")
            except json.JSONDecodeError:
                usage = {}
            items.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "round_id": row["round_id"],
                    "created_at": row["created_at"],
                    "model": row["model"] or "",
                    "route": row["route"] or "",
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "prompt_cache_hit_tokens": row["prompt_cache_hit_tokens"],
                    "prompt_cache_miss_tokens": row["prompt_cache_miss_tokens"],
                    "cached_tokens": row["cached_tokens"],
                    "cache_read_input_tokens": row["cache_read_input_tokens"],
                    "cache_creation_input_tokens": row["cache_creation_input_tokens"],
                    "usage": usage,
                }
            )
        return items

    def get_cooldown_multiplier(
        self,
        session_id: str,
        bucket_id: str,
        cooldown_hours: float,
        cooldown_floor: float,
        now: datetime | None = None,
    ) -> float:
        if cooldown_hours <= 0:
            return 1.0
        now = now or datetime.now()
        last_injected = self.get_last_injected_at(session_id, bucket_id)
        if not last_injected:
            return 1.0
        elapsed_hours = max(0.0, (now - last_injected).total_seconds() / 3600)
        if elapsed_hours >= cooldown_hours:
            return 1.0
        progress = elapsed_hours / cooldown_hours
        return round(cooldown_floor + (1.0 - cooldown_floor) * progress, 4)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
