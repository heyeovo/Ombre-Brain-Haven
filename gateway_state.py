import os
import json
import sqlite3
import hashlib
import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any


class ConversationConflictError(Exception):
    def __init__(self, expected_round_id: int, actual_round_id: int):
        super().__init__("conversation head changed")
        self.expected_round_id = expected_round_id
        self.actual_round_id = actual_round_id


class ConversationPersonaConflictError(Exception):
    def __init__(self, expected_persona_id: str, actual_persona_id: str):
        super().__init__("conversation belongs to another persona")
        self.expected_persona_id = expected_persona_id
        self.actual_persona_id = actual_persona_id


class RequestIdReuseError(Exception):
    def __init__(self, request_id: str):
        super().__init__("request_id was reused with different content")
        self.request_id = request_id


class SessionStateConflictError(Exception):
    def __init__(self, expected_version: int, actual_version: int):
        super().__init__("conversation session state changed")
        self.expected_version = expected_version
        self.actual_version = actual_version


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
    ) -> set[str]:
        """给已存在的表补列（幂等）。老库升级用，不重建表。"""
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        added: set[str] = set()
        for name, ddl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.add(name)
        return added

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
                source TEXT NOT NULL DEFAULT 'gateway',
                raw_json TEXT NOT NULL DEFAULT '',
                request_id TEXT,
                request_fingerprint TEXT,
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
                "request_id": "TEXT",
                "request_fingerprint": "TEXT",
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_request_id
            ON conversation_turns (profile_id, request_id)
            WHERE request_id IS NOT NULL AND request_id != ''
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                persona_id TEXT NOT NULL DEFAULT 'ombre',
                title TEXT NOT NULL DEFAULT '',
                local_engine_preference TEXT NOT NULL DEFAULT 'cc',
                selfhost_overrides_json TEXT NOT NULL DEFAULT '{}',
                cc_seen_round_id INTEGER NOT NULL DEFAULT 0,
                state_version INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile_id, session_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_attachments (
                attachment_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id INTEGER,
                round_id INTEGER,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'image',
                text_content TEXT NOT NULL DEFAULT '',
                text_truncated INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                cleared_at TEXT
            )
            """
        )
        self._ensure_columns(
            conn,
            "conversation_attachments",
            {
                "kind": "TEXT NOT NULL DEFAULT 'image'",
                "text_content": "TEXT NOT NULL DEFAULT ''",
                "text_truncated": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_attachments_session
            ON conversation_attachments (profile_id, session_id, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_attachments_turn
            ON conversation_attachments (turn_id, created_at)
            """
        )
        session_columns_added = self._ensure_columns(
            conn,
            "conversation_sessions",
            {
                "persona_id": "TEXT NOT NULL DEFAULT 'ombre'",
                "local_engine_preference": "TEXT NOT NULL DEFAULT 'cc'",
                "selfhost_overrides_json": "TEXT NOT NULL DEFAULT '{}'",
                "cc_seen_round_id": "INTEGER NOT NULL DEFAULT 0",
                "state_version": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        if "persona_id" in session_columns_added:
            # 4.5b 起现有 cc 窗口把归属写在首轮 client="ob2-chat/<persona_id>"。
            # 无归属的更早历史继续按既有产品规则归给 ombre。
            conn.execute(
                """
                UPDATE conversation_sessions
                SET persona_id = COALESCE((
                    SELECT CASE
                        WHEN turns.client LIKE 'ob2-chat/%'
                        THEN SUBSTR(turns.client, LENGTH('ob2-chat/') + 1)
                        ELSE NULL
                    END
                    FROM conversation_turns turns
                    WHERE turns.profile_id = conversation_sessions.profile_id
                      AND turns.session_id = conversation_sessions.session_id
                      AND turns.client LIKE 'ob2-chat/%'
                    ORDER BY turns.round_id ASC, turns.id ASC
                    LIMIT 1
                ), 'ombre')
                """
            )
        if "cc_seen_round_id" in session_columns_added:
            # 10.1 上线前还没有 selfhost 对话；把现有最新轮次作为 cc 已知基线，
            # 避免首次切换时把整段旧历史重复补入 Claude Code。
            conn.execute(
                """
                UPDATE conversation_sessions
                SET cc_seen_round_id = COALESCE((
                    SELECT MAX(turns.round_id)
                    FROM conversation_turns turns
                    WHERE turns.profile_id = conversation_sessions.profile_id
                      AND turns.session_id = conversation_sessions.session_id
                ), 0)
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_import_archives (
                profile_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_conversation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (profile_id, source, source_conversation_id),
                UNIQUE (profile_id, session_id)
            )
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
        # memory_entries / dirs 存 JSON 数组；engine 取值 subscription | api | selfhost。
        # dirs = 这个协作者能读哪些目录（第一个当 cwd，其余作附加目录）。
        # 空数组 = 用前端那边的默认值，不是「什么都不能读」。
        # write_dirs = 能**改**哪些目录里的文件（第 5 步加）。规则跟 dirs 相反：
        # 空数组 = 一个文件都不能改。读错了只是浪费钱，写错了会把文件改坏。
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
                dirs TEXT NOT NULL DEFAULT '[]',
                write_dirs TEXT NOT NULL DEFAULT '[]',
                recall_on INTEGER NOT NULL DEFAULT 1,
                semantic_on INTEGER NOT NULL DEFAULT 1,
                engine TEXT NOT NULL DEFAULT 'api',
                selfhost_defaults_json TEXT NOT NULL DEFAULT '{}',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # cc_personas 建表时（4.5b 第一版）没有 dirs / write_dirs，已经落过库的要补上。
        self._ensure_columns(
            conn,
            "cc_personas",
            {
                "dirs": "TEXT NOT NULL DEFAULT '[]'",
                "write_dirs": "TEXT NOT NULL DEFAULT '[]'",
                "selfhost_defaults_json": "TEXT NOT NULL DEFAULT '{}'",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_created_buckets (
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                bucket_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (profile_id, session_id, bucket_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_session_created_buckets_lookup
            ON session_created_buckets (profile_id, session_id, created_at DESC)
            """
        )
        # cc 前端的「上游模型配置」（5.2）。整个配置就一个对象，所以一行 JSON，
        # 不建结构化表 —— 里面是中转站清单 + 订阅侧可选模型 + 新对话的默认值，
        # 字段还会变，拆成列每次加东西都要补列迁移。
        # ⚠️ token 明文存在这里。跟 cc_personas 一样属于「只有本人用」的私有库。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_upstream_config (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # cc 前端的 MCP 配置（第 7 步）。跟上游模型配置一样是一份会继续扩展的 JSON：
        # 服务、transport、权限、工具目录和密钥都放在 payload，避免每加字段就迁表。
        # 这份配置必须跨设备 / 跨部署保留，不能落在 Vercel 本地文件。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_mcp_config (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # cc 前端的永久工具权限。只保存明确批准过的细粒度 allow 规则，
        # 例如 Bash(npm run build:*) / WebFetch(domain:example.com)。
        # 会话级批准仍留在 dashboard 进程内，不写库。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_permission_config (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}',
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
    def _cc_persona_json_list(raw: Any) -> list[str]:
        """存成 JSON 数组的那两列（memory_entries / dirs）读回来。坏数据当空。"""
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _json_object(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}") if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def _cc_persona_row_to_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        entries = cls._cc_persona_json_list(row["memory_entries"])
        # dirs / write_dirs 是后加的列，老库补列前读到的 row 里可能没有
        dirs = cls._cc_persona_json_list(row["dirs"]) if "dirs" in keys else []
        write_dirs = (
            cls._cc_persona_json_list(row["write_dirs"]) if "write_dirs" in keys else []
        )
        selfhost_defaults = (
            cls._json_object(row["selfhost_defaults_json"])
            if "selfhost_defaults_json" in keys
            else {}
        )
        return {
            "id": str(row["id"]),
            "name": row["name"] or "",
            "initial": row["initial"] or "",
            "tint": row["tint"] or "",
            "user_name": row["user_name"] or "",
            "purpose": row["purpose"] or "",
            "description": row["description"] or "",
            "prompt": row["prompt"] or "",
            "memory_entries": entries,
            "dirs": dirs,
            "write_dirs": write_dirs,
            "recall_on": bool(row["recall_on"]),
            "semantic_on": bool(row["semantic_on"]),
            "engine": row["engine"] or "api",
            "selfhost_defaults": selfhost_defaults,
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
        for dir_field in ("dirs", "write_dirs"):
            if dir_field not in persona:
                continue
            raw_dirs = persona.get(dir_field)
            if isinstance(raw_dirs, list):
                merged[dir_field] = [
                    str(item).strip() for item in raw_dirs if str(item).strip()
                ]
            else:
                merged[dir_field] = []
        if "selfhost_defaults" in persona:
            raw_defaults = persona.get("selfhost_defaults")
            merged["selfhost_defaults"] = (
                dict(raw_defaults) if isinstance(raw_defaults, dict) else {}
            )

        merged.setdefault("recall_on", True)
        merged.setdefault("semantic_on", True)
        merged.setdefault("engine", "api")
        merged.setdefault("sort_order", 0)
        merged.setdefault("memory_entries", [])
        merged.setdefault("dirs", [])
        merged.setdefault("write_dirs", [])
        merged.setdefault("selfhost_defaults", {})
        for field in self._CC_PERSONA_TEXT_FIELDS:
            merged.setdefault(field, "")

        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO cc_personas
            (id, name, initial, tint, user_name, purpose, description, prompt,
             memory_entries, dirs, write_dirs, recall_on, semantic_on, engine,
             selfhost_defaults_json, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(merged["dirs"], ensure_ascii=False),
                json.dumps(merged["write_dirs"], ensure_ascii=False),
                1 if merged["recall_on"] else 0,
                1 if merged["semantic_on"] else 0,
                merged["engine"] or "api",
                json.dumps(merged["selfhost_defaults"], ensure_ascii=False),
                int(merged["sort_order"] or 0),
                (existing or {}).get("created_at") or now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return self.get_cc_persona(safe_id)

    # ------------------------------------------------------------------
    # cc 前端上游模型配置（5.2）
    # ------------------------------------------------------------------

    _CC_UPSTREAM_ID = "default"

    def load_cc_upstream_config(self) -> dict[str, Any]:
        """整份配置读回来。没存过 / 坏数据都返回空 dict，前端自己套默认值。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT payload, updated_at FROM cc_upstream_config WHERE id = ?",
            (self._CC_UPSTREAM_ID,),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        try:
            value = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        value["updated_at"] = row["updated_at"] or ""
        return value

    def save_cc_upstream_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        整份覆盖，不做 PATCH 合并 —— 这一份是「一个表单一次保存」，
        跟协作者那边按 tab 分开存不一样。
        """
        from utils import now_iso

        now = now_iso()
        safe = payload if isinstance(payload, dict) else {}
        safe = {k: v for k, v in safe.items() if k != "updated_at"}
        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO cc_upstream_config (id, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            (self._CC_UPSTREAM_ID, json.dumps(safe, ensure_ascii=False), now),
        )
        conn.commit()
        conn.close()
        return self.load_cc_upstream_config()

    # ------------------------------------------------------------------
    # cc 前端永久工具权限
    # ------------------------------------------------------------------

    _CC_PERMISSION_ID = "default"

    def load_cc_permission_config(self) -> dict[str, Any]:
        """读回永久工具权限；未保存或数据损坏时返回空配置。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT payload, updated_at FROM cc_permission_config WHERE id = ?",
            (self._CC_PERMISSION_ID,),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        try:
            value = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        value["updated_at"] = row["updated_at"] or ""
        return value

    def save_cc_permission_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """整份覆盖永久权限配置；updated_at 由服务端生成。"""
        from utils import now_iso

        now = now_iso()
        safe = payload if isinstance(payload, dict) else {}
        safe = {k: v for k, v in safe.items() if k != "updated_at"}
        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO cc_permission_config (id, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            (self._CC_PERMISSION_ID, json.dumps(safe, ensure_ascii=False), now),
        )
        conn.commit()
        conn.close()
        return self.load_cc_permission_config()

    # ------------------------------------------------------------------
    # cc 前端 MCP 配置（第 7 步）
    # ------------------------------------------------------------------

    _CC_MCP_ID = "default"

    def load_cc_mcp_config(self) -> dict[str, Any]:
        """整份 MCP 配置读回来；尚未保存时返回空 dict，由前端完成一次默认值初始化。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT payload, updated_at FROM cc_mcp_config WHERE id = ?",
            (self._CC_MCP_ID,),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        try:
            value = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        value["updated_at"] = row["updated_at"] or ""
        return value

    def save_cc_mcp_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """整份覆盖 MCP 配置；密钥由受网关认证保护的调用方原样保存。"""
        from utils import now_iso

        now = now_iso()
        safe = payload if isinstance(payload, dict) else {}
        safe = {k: v for k, v in safe.items() if k != "updated_at"}
        conn = self._connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO cc_mcp_config (id, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            (self._CC_MCP_ID, json.dumps(safe, ensure_ascii=False), now),
        )
        conn.commit()
        conn.close()
        return self.load_cc_mcp_config()

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
        max_entries: int = 0,
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
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_sessions
            (profile_id, session_id, title, deleted_at, updated_at)
            VALUES (?, ?, '', NULL, ?)
            """,
            (safe_profile_id, safe_session_id, created_iso),
        )
        # conversation_turns 现在是 cc / Polaris / API 共用的长期原文存储。
        # max_entries 参数为兼容旧调用保留，但不再据此删除历史；读取量由分页控制。
        _ = max_entries
        conn.commit()
        turn_id = int(cursor.lastrowid or 0)
        conn.close()
        return turn_id

    @staticmethod
    def _conversation_request_fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def conversation_attachment_dir(self) -> str:
        path = os.path.join(os.path.dirname(self.db_path), "cc-attachments")
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _attachment_mime(data: bytes) -> tuple[str, str]:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        raise ValueError("only JPEG, PNG, and WebP images are supported")

    @staticmethod
    def _document_attachment_mime(filename: str, data: bytes) -> tuple[str, str]:
        suffix = os.path.splitext(str(filename or ""))[1].lower()
        mime_by_suffix = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".txt": "text/plain",
            ".csv": "text/csv",
        }
        mime_type = mime_by_suffix.get(suffix)
        if not mime_type:
            raise ValueError("only PDF, DOCX, MD, TXT, and CSV files are supported")
        if suffix == ".pdf" and not data.startswith(b"%PDF-"):
            raise ValueError("invalid PDF file")
        if suffix == ".docx":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if "word/document.xml" not in archive.namelist():
                        raise ValueError("invalid DOCX file")
            except (zipfile.BadZipFile, OSError) as exc:
                raise ValueError("invalid DOCX file") from exc
        return mime_type, suffix

    @staticmethod
    def _attachment_row_payload(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        kind = str(row["kind"] or "image") if "kind" in keys else "image"
        text_content = str(row["text_content"] or "") if "text_content" in keys else ""
        return {
            "id": str(row["attachment_id"]),
            "session_id": str(row["session_id"]),
            "turn_id": int(row["turn_id"]) if row["turn_id"] is not None else None,
            "round_id": int(row["round_id"]) if row["round_id"] is not None else None,
            "filename": str(row["filename"] or "image"),
            "mime_type": str(row["mime_type"]),
            "byte_size": int(row["byte_size"] or 0),
            "sha256": str(row["sha256"]),
            "kind": kind,
            "text_chars": len(text_content),
            "text_truncated": bool(row["text_truncated"]) if "text_truncated" in keys else False,
            "created_at": str(row["created_at"]),
            "cleared": bool(row["cleared_at"]),
            "cleared_at": str(row["cleared_at"] or ""),
        }

    def create_conversation_attachment(
        self,
        *,
        profile_id: str,
        session_id: str,
        filename: str,
        data: bytes,
        kind: str = "image",
        text_content: str = "",
        text_truncated: bool = False,
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")
        safe_kind = "file" if str(kind or "").strip().lower() == "file" else "image"
        if not data:
            raise ValueError("attachment is empty")
        if safe_kind == "image":
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("compressed image must not exceed 2 MB")
            mime_type, suffix = self._attachment_mime(data)
            safe_text_content = ""
        else:
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("file must not exceed 4 MB")
            mime_type, suffix = self._document_attachment_mime(filename, data)
            safe_text_content = str(text_content or "").replace("\x00", "").strip()
            if not safe_text_content:
                raise ValueError("file has no readable text")
            if len(safe_text_content) > 121_000:
                raise ValueError("parsed file text must not exceed 121000 characters")
        attachment_id = uuid.uuid4().hex
        storage_name = f"{attachment_id}{suffix}"
        fallback_name = "image" if safe_kind == "image" else "file"
        safe_filename = os.path.basename(str(filename or fallback_name)).strip()[:200] or f"{fallback_name}{suffix}"
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sha256 = hashlib.sha256(data).hexdigest()
        final_path = os.path.join(self.conversation_attachment_dir, storage_name)
        temp_path = f"{final_path}.tmp"
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO conversation_attachments
                (attachment_id, profile_id, session_id, turn_id, round_id, filename,
                 mime_type, byte_size, sha256, storage_name, kind, text_content,
                 text_truncated, created_at, cleared_at)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    attachment_id,
                    safe_profile_id,
                    safe_session_id,
                    safe_filename,
                    mime_type,
                    len(data),
                    sha256,
                    storage_name,
                    safe_kind,
                    safe_text_content,
                    1 if text_truncated else 0,
                    created_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                os.remove(final_path)
            except OSError:
                pass
            raise
        finally:
            conn.close()
        return {
            "id": attachment_id,
            "session_id": safe_session_id,
            "turn_id": None,
            "round_id": None,
            "filename": safe_filename,
            "mime_type": mime_type,
            "byte_size": len(data),
            "sha256": sha256,
            "kind": safe_kind,
            "text_chars": len(safe_text_content),
            "text_truncated": bool(text_truncated),
            "created_at": created_at,
            "cleared": False,
            "cleared_at": "",
        }

    def get_conversation_attachment(
        self,
        *,
        profile_id: str,
        attachment_id: str,
    ) -> tuple[dict[str, Any], str]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_id = str(attachment_id or "").strip()
        conn = self._connect()
        row = conn.execute(
            """
            SELECT * FROM conversation_attachments
            WHERE profile_id = ? AND attachment_id = ?
            """,
            (safe_profile_id, safe_id),
        ).fetchone()
        conn.close()
        if row is None:
            return {}, ""
        payload = self._attachment_row_payload(row)
        if payload["cleared"]:
            return payload, ""
        return payload, os.path.join(self.conversation_attachment_dir, str(row["storage_name"]))

    def get_conversation_attachment_text(
        self,
        *,
        profile_id: str,
        attachment_id: str,
    ) -> tuple[dict[str, Any], str]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_id = str(attachment_id or "").strip()
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM conversation_attachments WHERE profile_id = ? AND attachment_id = ?",
            (safe_profile_id, safe_id),
        ).fetchone()
        conn.close()
        if row is None:
            return {}, ""
        payload = self._attachment_row_payload(row)
        if payload["cleared"] or payload["kind"] != "file":
            return payload, ""
        return payload, str(row["text_content"] or "")

    def list_conversation_attachments(
        self,
        *,
        profile_id: str,
        session_id: str = "",
        turn_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        where = "profile_id = ?"
        params: list[Any] = [safe_profile_id]
        safe_session_id = str(session_id or "").strip()
        if safe_session_id:
            where += " AND session_id = ?"
            params.append(safe_session_id)
        safe_turn_ids = [int(value) for value in (turn_ids or []) if int(value) > 0]
        if safe_turn_ids:
            placeholders = ",".join("?" for _ in safe_turn_ids)
            where += f" AND turn_id IN ({placeholders})"
            params.extend(safe_turn_ids)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM conversation_attachments WHERE {where} ORDER BY created_at, attachment_id",
            params,
        ).fetchall()
        conn.close()
        return [self._attachment_row_payload(row) for row in rows]

    def clear_conversation_attachment(
        self,
        *,
        profile_id: str,
        attachment_id: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_id = str(attachment_id or "").strip()
        safe_session_id = str(session_id or "").strip()
        conn = self._connect()
        where = "profile_id = ? AND attachment_id = ?"
        params: list[Any] = [safe_profile_id, safe_id]
        if safe_session_id:
            where += " AND session_id = ?"
            params.append(safe_session_id)
        row = conn.execute(
            f"SELECT * FROM conversation_attachments WHERE {where}", params
        ).fetchone()
        if row is None:
            conn.close()
            return {}
        cleared_at = str(row["cleared_at"] or "") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = os.path.join(self.conversation_attachment_dir, str(row["storage_name"]))
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        conn.execute(
            """
            UPDATE conversation_attachments
            SET cleared_at = ?, text_content = ''
            WHERE attachment_id = ?
            """,
            (cleared_at, safe_id),
        )
        conn.commit()
        conn.close()
        return {**self._attachment_row_payload(row), "cleared": True, "cleared_at": cleared_at}

    def clear_session_conversation_attachments(
        self,
        *,
        profile_id: str,
        session_id: str,
        kind: str = "",
    ) -> int:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        conn = self._connect()
        safe_kind = str(kind or "").strip().lower()
        if safe_kind in {"image", "file"}:
            rows = conn.execute(
                """
                SELECT attachment_id FROM conversation_attachments
                WHERE profile_id = ? AND session_id = ? AND kind = ? AND cleared_at IS NULL
                """,
                (safe_profile_id, safe_session_id, safe_kind),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT attachment_id FROM conversation_attachments
                WHERE profile_id = ? AND session_id = ? AND cleared_at IS NULL
                """,
                (safe_profile_id, safe_session_id),
            ).fetchall()
        conn.close()
        count = 0
        for row in rows:
            if self.clear_conversation_attachment(
                profile_id=safe_profile_id,
                attachment_id=str(row["attachment_id"]),
                session_id=safe_session_id,
            ):
                count += 1
        return count

    def cleanup_staged_conversation_attachments(self, *, older_than_hours: int = 24) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, older_than_hours))).isoformat(timespec="seconds")
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT profile_id, attachment_id, session_id
            FROM conversation_attachments
            WHERE turn_id IS NULL AND cleared_at IS NULL AND created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        conn.close()
        count = 0
        for row in rows:
            if self.clear_conversation_attachment(
                profile_id=str(row["profile_id"]),
                attachment_id=str(row["attachment_id"]),
                session_id=str(row["session_id"]),
            ):
                count += 1
        return count

    @staticmethod
    def _conversation_turn_row_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "profile_id": str(row["profile_id"]),
            "session_id": str(row["session_id"]),
            "round_id": int(row["round_id"]),
            "created_at": str(row["created_at"] or ""),
            "user_text": str(row["user_text"] or ""),
            "assistant_text": str(row["assistant_text"] or ""),
            "model": str(row["model"] or ""),
            "client": str(row["client"] or ""),
            "route": str(row["route"] or ""),
            "source": str(row["source"] or "gateway"),
            "request_id": str(row["request_id"] or ""),
        }

    def get_conversation_turn_by_request_id(
        self,
        *,
        profile_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Read a committed turn for durable selfhost idempotent replay."""
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_request_id = str(request_id or "").strip()
        if not safe_request_id:
            return {}
        conn = self._connect()
        row = conn.execute(
            """
            SELECT turns.id, turns.profile_id, turns.session_id, turns.round_id,
                   turns.created_at, turns.user_text, turns.assistant_text,
                   turns.model, turns.client, turns.route, turns.source,
                   turns.raw_json, turns.request_id,
                   COALESCE(sessions.persona_id, 'ombre') AS persona_id
            FROM conversation_turns AS turns
            LEFT JOIN conversation_sessions AS sessions
              ON sessions.profile_id = turns.profile_id
             AND sessions.session_id = turns.session_id
            WHERE turns.profile_id = ? AND turns.request_id = ?
            """,
            (safe_profile_id, safe_request_id),
        ).fetchone()
        conn.close()
        if row is None:
            return {}
        return {
            **self._conversation_turn_row_payload(row),
            "persona_id": str(row["persona_id"] or "ombre"),
            "raw_json": str(row["raw_json"] or ""),
            "attachments": self.list_conversation_attachments(
                profile_id=safe_profile_id,
                turn_ids=[int(row["id"])],
            ),
        }

    def commit_conversation_turn(
        self,
        *,
        profile_id: str,
        session_id: str,
        persona_id: str,
        request_id: str,
        expected_last_round_id: int | None,
        user_text: str,
        assistant_text: str = "",
        model: str = "",
        client: str = "",
        route: str = "",
        source: str = "gateway",
        raw_json: str = "",
        attachment_ids: list[str] | None = None,
        recalled_bucket_ids: list[str] | None = None,
        created_bucket_ids: list[str] | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Strict idempotent compare-and-append for cc/selfhost conversation turns."""
        created_at = created_at or datetime.now(timezone.utc)
        created_iso = created_at.isoformat(timespec="seconds")
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        safe_persona_id = str(persona_id or "").strip()
        safe_request_id = str(request_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")
        if not safe_persona_id:
            raise ValueError("persona_id is required")
        if not safe_request_id:
            raise ValueError("request_id is required")
        if len(safe_request_id) > 128:
            raise ValueError("request_id is too long")

        try:
            expected_round = int(expected_last_round_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_last_round_id must be an integer or null") from exc
        if expected_round < 0:
            raise ValueError("expected_last_round_id must be zero or greater")

        recalled_ids = list(dict.fromkeys(
            str(item or "").strip() for item in (recalled_bucket_ids or []) if str(item or "").strip()
        ))
        created_ids = list(dict.fromkeys(
            str(item or "").strip() for item in (created_bucket_ids or []) if str(item or "").strip()
        ))
        requested_attachment_ids = list(dict.fromkeys(
            str(item or "").strip() for item in (attachment_ids or []) if str(item or "").strip()
        ))
        if len(requested_attachment_ids) > 4:
            raise ValueError("no more than 4 attachments are allowed per turn")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attachment_rows: list[sqlite3.Row] = []
            if requested_attachment_ids:
                placeholders = ",".join("?" for _ in requested_attachment_ids)
                rows = conn.execute(
                    f"""
                    SELECT * FROM conversation_attachments
                    WHERE profile_id = ? AND attachment_id IN ({placeholders})
                    """,
                    [safe_profile_id, *requested_attachment_ids],
                ).fetchall()
                by_id = {str(row["attachment_id"]): row for row in rows}
                if any(item not in by_id for item in requested_attachment_ids):
                    raise ValueError("attachment not found")
                attachment_rows = [by_id[item] for item in requested_attachment_ids]
            fingerprint = self._conversation_request_fingerprint(
                {
                    "session_id": safe_session_id,
                    "persona_id": safe_persona_id,
                    "expected_last_round_id": expected_round,
                    "user_text": str(user_text or ""),
                    "assistant_text": str(assistant_text or ""),
                    "model": str(model or ""),
                    "client": str(client or ""),
                    "route": str(route or ""),
                    "source": str(source or "gateway").strip() or "gateway",
                    "attachments": [
                        {"id": str(row["attachment_id"]), "sha256": str(row["sha256"])}
                        for row in attachment_rows
                    ],
                    "recalled_bucket_ids": sorted(recalled_ids),
                    "created_bucket_ids": sorted(created_ids),
                }
            )
            existing = conn.execute(
                """
                SELECT id, profile_id, session_id, round_id, created_at,
                       user_text, assistant_text, model, client, route, source,
                       request_id, request_fingerprint
                FROM conversation_turns
                WHERE profile_id = ? AND request_id = ?
                """,
                (safe_profile_id, safe_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["session_id"]) != safe_session_id
                    or str(existing["request_fingerprint"] or "") != fingerprint
                ):
                    raise RequestIdReuseError(safe_request_id)
                conn.rollback()
                return {
                    "turn": self._conversation_turn_row_payload(existing),
                    "idempotent_replay": True,
                }

            for row in attachment_rows:
                if str(row["session_id"]) != safe_session_id:
                    raise ValueError("attachment belongs to another session")
                if row["turn_id"] is not None:
                    raise ValueError("attachment is already bound to another turn")

            session = conn.execute(
                """
                SELECT persona_id, cc_seen_round_id, state_version
                FROM conversation_sessions
                WHERE profile_id = ? AND session_id = ?
                """,
                (safe_profile_id, safe_session_id),
            ).fetchone()
            if session is not None:
                actual_persona_id = str(session["persona_id"] or "ombre")
                if actual_persona_id != safe_persona_id:
                    raise ConversationPersonaConflictError(safe_persona_id, actual_persona_id)

            head = conn.execute(
                """
                SELECT COALESCE(MAX(round_id), 0) AS current_round
                FROM conversation_turns
                WHERE profile_id = ? AND session_id = ?
                """,
                (safe_profile_id, safe_session_id),
            ).fetchone()
            actual_round = int(head["current_round"] or 0)
            if actual_round != expected_round:
                raise ConversationConflictError(expected_round, actual_round)
            next_round = actual_round + 1

            cursor = conn.execute(
                """
                INSERT INTO conversation_turns
                (profile_id, session_id, round_id, created_at, user_text, assistant_text,
                 model, client, route, source, raw_json, request_id, request_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_profile_id,
                    safe_session_id,
                    next_round,
                    created_iso,
                    str(user_text or ""),
                    str(assistant_text or ""),
                    str(model or ""),
                    str(client or ""),
                    str(route or ""),
                    str(source or "gateway").strip() or "gateway",
                    str(raw_json or ""),
                    safe_request_id,
                    fingerprint,
                ),
            )

            if session is None:
                cc_seen_round_id = next_round if str(source or "").strip() == "cc" else 0
                conn.execute(
                    """
                    INSERT INTO conversation_sessions
                    (profile_id, session_id, persona_id, title, local_engine_preference,
                     selfhost_overrides_json, cc_seen_round_id, state_version,
                     deleted_at, updated_at)
                    VALUES (?, ?, ?, '', 'cc', '{}', ?, ?, NULL, ?)
                    """,
                    (
                        safe_profile_id,
                        safe_session_id,
                        safe_persona_id,
                        cc_seen_round_id,
                        1 if cc_seen_round_id else 0,
                        created_iso,
                    ),
                )
            else:
                advances_cc_cursor = str(source or "").strip() == "cc"
                conn.execute(
                    """
                    UPDATE conversation_sessions
                    SET cc_seen_round_id = CASE
                            WHEN ? = 1 THEN MAX(cc_seen_round_id, ?)
                            ELSE cc_seen_round_id
                        END,
                        state_version = state_version + CASE WHEN ? = 1 THEN 1 ELSE 0 END,
                        deleted_at = NULL,
                        updated_at = ?
                    WHERE profile_id = ? AND session_id = ?
                    """,
                    (
                        1 if advances_cc_cursor else 0,
                        next_round,
                        1 if advances_cc_cursor else 0,
                        created_iso,
                        safe_profile_id,
                        safe_session_id,
                    ),
                )

            if recalled_ids:
                recall_head = conn.execute(
                    "SELECT COALESCE(MAX(round_id), 0) AS current_round FROM request_rounds WHERE session_id = ?",
                    (safe_session_id,),
                ).fetchone()
                recall_round = int(recall_head["current_round"] or 0) + 1
                conn.execute(
                    "INSERT INTO request_rounds (session_id, round_id, completed_at) VALUES (?, ?, ?)",
                    (safe_session_id, recall_round, created_iso),
                )
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO injected_buckets
                    (session_id, round_id, bucket_id, injected_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (safe_session_id, recall_round, bucket_id, created_iso)
                        for bucket_id in recalled_ids
                    ],
                )

            if created_ids:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO session_created_buckets
                    (profile_id, session_id, bucket_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (safe_profile_id, safe_session_id, bucket_id, created_iso)
                        for bucket_id in created_ids
                    ],
                )

            turn_id = int(cursor.lastrowid or 0)
            if requested_attachment_ids:
                placeholders = ",".join("?" for _ in requested_attachment_ids)
                conn.execute(
                    f"""
                    UPDATE conversation_attachments
                    SET turn_id = ?, round_id = ?
                    WHERE profile_id = ? AND session_id = ?
                      AND attachment_id IN ({placeholders})
                    """,
                    [turn_id, next_round, safe_profile_id, safe_session_id, *requested_attachment_ids],
                )
            conn.commit()
            row = conn.execute(
                """
                SELECT id, profile_id, session_id, round_id, created_at,
                       user_text, assistant_text, model, client, route, source,
                       request_id
                FROM conversation_turns WHERE id = ?
                """,
                (turn_id,),
            ).fetchone()
            return {
                "turn": self._conversation_turn_row_payload(row),
                "idempotent_replay": False,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_session_bucket_exclusion_ids(
        self,
        *,
        profile_id: str,
        session_id: str,
    ) -> set[str]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return set()
        conn = self._connect()
        recalled = conn.execute(
            "SELECT DISTINCT bucket_id FROM injected_buckets WHERE session_id = ?",
            (safe_session_id,),
        ).fetchall()
        created = conn.execute(
            """
            SELECT bucket_id FROM session_created_buckets
            WHERE profile_id = ? AND session_id = ?
            """,
            (safe_profile_id, safe_session_id),
        ).fetchall()
        conn.close()
        return {
            str(row["bucket_id"])
            for row in [*recalled, *created]
            if str(row["bucket_id"] or "").strip()
        }

    def import_conversation_archive(
        self,
        *,
        profile_id: str,
        source: str,
        source_conversation_id: str,
        session_id: str,
        title: str,
        raw_json: str,
        turns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """幂等导入一段历史对话；固定 session/round，不触发运行态历史裁剪。"""
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_source = str(source or "").strip()
        safe_source_id = str(source_conversation_id or "").strip()
        safe_session_id = str(session_id or "").strip()
        if not safe_source or not safe_source_id or not safe_session_id:
            raise ValueError("source/source_conversation_id/session_id is required")

        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            foreign_turn = conn.execute(
                """
                SELECT 1 FROM conversation_turns
                WHERE profile_id = ? AND session_id = ? AND source != ?
                LIMIT 1
                """,
                (safe_profile_id, safe_session_id, safe_source),
            ).fetchone()
            if foreign_turn is not None:
                raise ValueError("session already contains non-imported turns")

            existed = conn.execute(
                """
                SELECT 1 FROM conversation_import_archives
                WHERE profile_id = ? AND source = ? AND source_conversation_id = ?
                """,
                (safe_profile_id, safe_source, safe_source_id),
            ).fetchone() is not None

            conn.execute(
                """
                INSERT INTO conversation_import_archives
                (profile_id, source, source_conversation_id, session_id, imported_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, source, source_conversation_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    imported_at = excluded.imported_at,
                    raw_json = excluded.raw_json
                """,
                (
                    safe_profile_id,
                    safe_source,
                    safe_source_id,
                    safe_session_id,
                    imported_at,
                    str(raw_json or ""),
                ),
            )
            conn.execute(
                """
                INSERT INTO conversation_sessions
                (profile_id, session_id, title, deleted_at, updated_at)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(profile_id, session_id) DO UPDATE SET
                    title = CASE
                        WHEN conversation_sessions.title = '' THEN excluded.title
                        ELSE conversation_sessions.title
                    END,
                    updated_at = excluded.updated_at
                """,
                (safe_profile_id, safe_session_id, str(title or "")[:120], imported_at),
            )

            for turn in turns:
                conn.execute(
                    """
                    INSERT INTO conversation_turns
                    (profile_id, session_id, round_id, created_at, user_text, assistant_text,
                     model, client, route, source, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, session_id, round_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        user_text = excluded.user_text,
                        assistant_text = excluded.assistant_text,
                        model = excluded.model,
                        client = excluded.client,
                        route = excluded.route,
                        source = excluded.source,
                        raw_json = excluded.raw_json
                    """,
                    (
                        safe_profile_id,
                        safe_session_id,
                        int(turn["round_id"]),
                        str(turn["created_at"]),
                        str(turn.get("user_text") or ""),
                        str(turn.get("assistant_text") or ""),
                        str(turn.get("model") or ""),
                        "polaris-import",
                        "archive",
                        safe_source,
                        str(turn.get("raw_json") or ""),
                    ),
                )
            conn.execute(
                """
                DELETE FROM conversation_turns
                WHERE profile_id = ? AND session_id = ? AND source = ? AND round_id > ?
                """,
                (safe_profile_id, safe_session_id, safe_source, len(turns)),
            )
            conn.commit()
            return {
                "session_id": safe_session_id,
                "turn_count": len(turns),
                "reimported": existed,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
        persona_id: str = "",
        deleted_only: bool = False,
    ) -> list[dict[str, Any]]:
        """会话列表：每个 session_id 一行，带轮数、时间范围和第一句用户原话做标题。"""
        safe_limit = max(1, min(200, int(limit or 50)))
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_source = str(source or "").strip()
        safe_persona_id = str(persona_id or "").strip()
        deleted_predicate = "EXISTS" if deleted_only else "NOT EXISTS"
        where_clause = (
            f"turns.profile_id = ? AND {deleted_predicate} ("
            "SELECT 1 FROM conversation_sessions meta "
            "WHERE meta.profile_id = turns.profile_id "
            "AND meta.session_id = turns.session_id "
            "AND COALESCE(meta.deleted_at, '') <> ''"
            ")"
        )
        params: list[Any] = [safe_profile_id]
        if safe_source:
            where_clause += " AND turns.source = ?"
            params.append(safe_source)
        if safe_persona_id:
            where_clause += (
                " AND EXISTS (SELECT 1 FROM conversation_sessions owner "
                "WHERE owner.profile_id = turns.profile_id "
                "AND owner.session_id = turns.session_id "
                "AND owner.persona_id = ?)"
            )
            params.append(safe_persona_id)
        params.append(safe_limit)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT turns.session_id,
                   COUNT(*) AS turn_count,
                   MIN(created_at) AS first_at,
                   MAX(created_at) AS last_at,
                   MAX(id) AS last_id,
                   MIN(id) AS first_id
            FROM conversation_turns turns
            WHERE {where_clause}
            GROUP BY turns.session_id
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
            meta = conn.execute(
                """
                SELECT persona_id, title, deleted_at FROM conversation_sessions
                WHERE profile_id = ? AND session_id = ?
                """,
                (safe_profile_id, row["session_id"]),
            ).fetchone()
            custom_title = ((meta["title"] if meta else "") or "").strip()
            automatic_title = ((head["user_text"] if head else "") or "").strip().replace("\n", " ")
            title = custom_title or automatic_title
            sessions.append(
                {
                    "session_id": row["session_id"],
                    "persona_id": (meta["persona_id"] if meta else "ombre") or "ombre",
                    "turn_count": int(row["turn_count"] or 0),
                    "first_at": row["first_at"],
                    "last_at": row["last_at"],
                    "title": title[:80],
                    "model": (head["model"] if head else "") or "",
                    "client": (head["client"] if head else "") or "",
                    "route": (head["route"] if head else "") or "",
                    "source": (head["source"] if head else "") or "gateway",
                    "deleted_at": meta["deleted_at"] if meta else None,
                }
            )
        conn.close()
        return sessions

    def set_conversation_session_title(
        self,
        *,
        profile_id: str,
        session_id: str,
        title: str,
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        safe_title = " ".join(str(title or "").strip().split())[:120]
        if not safe_session_id or not safe_title:
            return {}
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO conversation_sessions
            (profile_id, session_id, title, deleted_at, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(profile_id, session_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (safe_profile_id, safe_session_id, safe_title, updated_at),
        )
        conn.commit()
        conn.close()
        return {
            "profile_id": safe_profile_id,
            "session_id": safe_session_id,
            "title": safe_title,
            "deleted_at": None,
            "updated_at": updated_at,
        }

    def soft_delete_conversation_session(
        self,
        *,
        profile_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return {}
        deleted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO conversation_sessions
            (profile_id, session_id, title, deleted_at, updated_at)
            VALUES (?, ?, '', ?, ?)
            ON CONFLICT(profile_id, session_id) DO UPDATE SET
                deleted_at = excluded.deleted_at,
                updated_at = excluded.updated_at
            """,
            (safe_profile_id, safe_session_id, deleted_at, deleted_at),
        )
        conn.commit()
        conn.close()
        return {
            "profile_id": safe_profile_id,
            "session_id": safe_session_id,
            "deleted_at": deleted_at,
            "updated_at": deleted_at,
        }

    def get_conversation_session_state(
        self,
        *,
        profile_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return {}
        conn = self._connect()
        row = conn.execute(
            """
            SELECT profile_id, session_id, persona_id, title,
                   local_engine_preference, selfhost_overrides_json,
                   cc_seen_round_id, state_version, deleted_at, updated_at
            FROM conversation_sessions
            WHERE profile_id = ? AND session_id = ?
            """,
            (safe_profile_id, safe_session_id),
        ).fetchone()
        conn.close()
        if row is None:
            return {}
        preference = str(row["local_engine_preference"] or "cc")
        if preference not in {"cc", "selfhost"}:
            preference = "cc"
        return {
            "profile_id": str(row["profile_id"]),
            "session_id": str(row["session_id"]),
            "persona_id": str(row["persona_id"] or "ombre"),
            "title": str(row["title"] or ""),
            "local_engine_preference": preference,
            "selfhost_overrides": self._json_object(row["selfhost_overrides_json"]),
            "cc_seen_round_id": int(row["cc_seen_round_id"] or 0),
            "state_version": int(row["state_version"] or 0),
            "deleted_at": row["deleted_at"],
            "updated_at": str(row["updated_at"] or ""),
        }

    def patch_conversation_session_state(
        self,
        *,
        profile_id: str,
        session_id: str,
        persona_id: str,
        updates: dict[str, Any],
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        safe_persona_id = str(persona_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")
        if not safe_persona_id:
            raise ValueError("persona_id is required")
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")
        if "effective_engine" in updates:
            raise ValueError("effective_engine is runtime-only and cannot be persisted")
        allowed = {"local_engine_preference", "selfhost_overrides"}
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ValueError(f"unsupported session state fields: {', '.join(unknown)}")

        preference: str | None = None
        if "local_engine_preference" in updates:
            preference = str(updates.get("local_engine_preference") or "").strip()
            if preference not in {"cc", "selfhost"}:
                raise ValueError("local_engine_preference must be cc or selfhost")
        overrides: dict[str, Any] | None = None
        if "selfhost_overrides" in updates:
            raw_overrides = updates.get("selfhost_overrides")
            if not isinstance(raw_overrides, dict):
                raise ValueError("selfhost_overrides must be an object")
            overrides = dict(raw_overrides)

        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT persona_id, local_engine_preference, selfhost_overrides_json,
                       state_version
                FROM conversation_sessions
                WHERE profile_id = ? AND session_id = ?
                """,
                (safe_profile_id, safe_session_id),
            ).fetchone()
            if row is not None:
                actual_persona_id = str(row["persona_id"] or "ombre")
                if actual_persona_id != safe_persona_id:
                    raise ConversationPersonaConflictError(safe_persona_id, actual_persona_id)
                current_version = int(row["state_version"] or 0)
                if (
                    expected_state_version is not None
                    and int(expected_state_version) != current_version
                ):
                    raise SessionStateConflictError(int(expected_state_version), current_version)
                current_preference = str(row["local_engine_preference"] or "cc")
                current_overrides = self._json_object(row["selfhost_overrides_json"])
            else:
                current_version = 0
                if expected_state_version not in (None, 0):
                    raise SessionStateConflictError(int(expected_state_version), 0)
                current_preference = "cc"
                current_overrides = {}

            next_preference = preference or current_preference
            next_overrides = overrides if overrides is not None else current_overrides
            next_version = current_version + 1
            conn.execute(
                """
                INSERT INTO conversation_sessions
                (profile_id, session_id, persona_id, title, local_engine_preference,
                 selfhost_overrides_json, cc_seen_round_id, state_version,
                 deleted_at, updated_at)
                VALUES (?, ?, ?, '', ?, ?, 0, ?, NULL, ?)
                ON CONFLICT(profile_id, session_id) DO UPDATE SET
                    local_engine_preference = excluded.local_engine_preference,
                    selfhost_overrides_json = excluded.selfhost_overrides_json,
                    state_version = excluded.state_version,
                    updated_at = excluded.updated_at
                """,
                (
                    safe_profile_id,
                    safe_session_id,
                    safe_persona_id,
                    next_preference,
                    json.dumps(next_overrides, ensure_ascii=False),
                    next_version,
                    updated_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_conversation_session_state(
            profile_id=safe_profile_id,
            session_id=safe_session_id,
        )

    def permanently_delete_conversation_session(
        self,
        *,
        profile_id: str,
        session_id: str,
    ) -> dict[str, int]:
        """Delete one chat window's profile-scoped records, never memory buckets."""
        safe_profile_id = str(profile_id or "default").strip() or "default"
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")
        conn = self._connect()
        counts: dict[str, int] = {}
        attachment_storage_names: list[str] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                """
                SELECT profile_id FROM conversation_sessions
                WHERE profile_id = ? AND session_id = ?
                """,
                (safe_profile_id, safe_session_id),
            ).fetchone()
            if owner is None:
                raise ValueError("session ownership could not be verified")
            attachment_storage_names = [
                str(row["storage_name"])
                for row in conn.execute(
                    """
                    SELECT storage_name FROM conversation_attachments
                    WHERE profile_id = ? AND session_id = ?
                    """,
                    (safe_profile_id, safe_session_id),
                ).fetchall()
            ]
            for storage_name in attachment_storage_names:
                try:
                    os.remove(os.path.join(self.conversation_attachment_dir, storage_name))
                except FileNotFoundError:
                    pass
            for table in (
                "conversation_attachments",
                "conversation_turns",
                "conversation_sessions",
                "conversation_import_archives",
                "session_created_buckets",
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE profile_id = ? AND session_id = ?",
                    (safe_profile_id, safe_session_id),
                )
                counts[table] = max(0, int(cursor.rowcount or 0))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return counts

    def list_conversation_session_metadata(
        self,
        *,
        profile_id: str,
    ) -> list[dict[str, Any]]:
        safe_profile_id = str(profile_id or "default").strip() or "default"
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT profile_id, session_id, persona_id, title,
                   local_engine_preference, cc_seen_round_id, state_version,
                   deleted_at, updated_at
            FROM conversation_sessions
            WHERE profile_id = ?
            """,
            (safe_profile_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "profile_id": row["profile_id"],
                "session_id": row["session_id"],
                "persona_id": row["persona_id"] or "ombre",
                "title": row["title"] or "",
                "local_engine_preference": row["local_engine_preference"] or "cc",
                "cc_seen_round_id": int(row["cc_seen_round_id"] or 0),
                "state_version": int(row["state_version"] or 0),
                "deleted_at": row["deleted_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_conversation_turns_by_session(
        self,
        *,
        profile_id: str,
        session_id: str,
        limit: int = 200,
        before_id: int | None = None,
        after_round_id: int | None = None,
        source: str = "",
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
        if after_round_id is not None:
            where_clause += " AND round_id > ?"
            params.append(int(after_round_id))
        safe_source = str(source or "").strip()
        if safe_source:
            where_clause += " AND source = ?"
            params.append(safe_source)
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
        turn_ids = [int(row["id"]) for row in rows]
        attachment_rows: list[sqlite3.Row] = []
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            attachment_rows = conn.execute(
                f"""
                SELECT * FROM conversation_attachments
                WHERE profile_id = ? AND turn_id IN ({placeholders})
                ORDER BY created_at, attachment_id
                """,
                [safe_profile_id, *turn_ids],
            ).fetchall()
        conn.close()
        attachments_by_turn: dict[int, list[dict[str, Any]]] = {}
        for attachment_row in attachment_rows:
            turn_id = int(attachment_row["turn_id"] or 0)
            attachments_by_turn.setdefault(turn_id, []).append(
                self._attachment_row_payload(attachment_row)
            )
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
                "attachments": attachments_by_turn.get(int(row["id"]), []),
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
        now = now or datetime.now(timezone.utc)
        last_injected = self.get_last_injected_at(session_id, bucket_id)
        if not last_injected:
            return 1.0
        # 新轮次统一写 UTC-aware ISO；旧 injected_buckets 仍可能是无时区 ISO。
        # 冷却只关心经过时长，旧值按 UTC 解释后再相减，避免 naive/aware 混算 500。
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        if last_injected.tzinfo is None or last_injected.utcoffset() is None:
            last_injected = last_injected.replace(tzinfo=timezone.utc)
        else:
            last_injected = last_injected.astimezone(timezone.utc)
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
