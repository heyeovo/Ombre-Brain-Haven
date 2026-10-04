# ============================================================
# Module: Embedding Engine (embedding_engine.py)
# 模块：向量化引擎
#
# Generates embeddings via Gemini API (OpenAI-compatible),
# stores them in SQLite, and provides cosine similarity search.
# 通过 Gemini API（OpenAI 兼容）生成 embedding，
# 存储在 SQLite 中，提供余弦相似度搜索。
#
# Depended on by: server.py, bucket_manager.py
# 被谁依赖：server.py, bucket_manager.py
# ============================================================

import os
import json
import math
import time
import asyncio
import sqlite3
import logging
import weakref

from openai import AsyncOpenAI

logger = logging.getLogger("ombre_brain.embedding")


class EmbeddingEngine:
    """
    Embedding generation + SQLite vector storage + cosine search.
    向量生成 + SQLite 向量存储 + 余弦搜索。
    """

    def __init__(self, config: dict):
        dehy_cfg = config.get("dehydration", {})
        embed_cfg = config.get("embedding", {})

        self.api_key = embed_cfg.get("api_key") or dehy_cfg.get("api_key", "")
        self.base_url = (
            embed_cfg.get("base_url")
            or dehy_cfg.get("base_url")
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.model = embed_cfg.get("model", "gemini-embedding-001")
        self.enabled = bool(self.api_key) and embed_cfg.get("enabled", True)
        self.max_chars = self._int_between(embed_cfg.get("max_chars", 6000), 6000, 500, 32000)
        self.query_instruction = str(
            embed_cfg.get("query_instruction")
            or "Given a memory search query, retrieve relevant long-term memory passages."
        ).strip()
        self.document_instruction = str(embed_cfg.get("document_instruction") or "").strip()

        # --- SQLite path: buckets_dir/embeddings.db ---
        db_path = os.path.join(config["buckets_dir"], "embeddings.db")
        self.db_path = db_path

        # --- Health snapshot (runtime only; lost on restart) ---
        # --- 健康快照（仅运行态，重启丢失）---
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None
        self.last_error = ""
        self.consecutive_failures = 0

        # AsyncOpenAI keeps an httpx pool bound to the event loop that first used it.
        # Brain runs schedulers on their own loops, so each extra loop gets its own client.
        # AsyncOpenAI 的连接池绑定首次使用它的事件循环；Brain 的调度线程各有循环，各自建客户端。
        self._base_client = None
        self._base_loop = None
        self._loop_clients = weakref.WeakKeyDictionary()

        # --- Initialize client ---
        if self.enabled:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )
        else:
            self.client = None

        # --- Initialize SQLite ---
        self._init_db()

    def _init_db(self):
        """Create embeddings table if not exists."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                model TEXT,
                dimension INTEGER,
                updated_at TEXT NOT NULL
            )
        """)
        self._ensure_column(conn, "embeddings", "model", "TEXT")
        self._ensure_column(conn, "embeddings", "dimension", "INTEGER")
        conn.commit()
        conn.close()

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """
        Generate embedding for content and store in SQLite.
        为内容生成 embedding 并存入 SQLite。
        Returns True on success, False on failure.
        """
        if not self.enabled or not content or not content.strip():
            return False

        try:
            embedding = await self._generate_embedding(content, kind="document")
            if not embedding:
                return False
            self._store_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            reason = self._describe_error(e)
            self._record_failure(reason)
            logger.warning("Embedding store failed / 向量写入失败 for %s: %s", bucket_id, reason)
            return False

    async def _generate_embedding(self, text: str, *, kind: str = "document") -> list[float]:
        """Call API to generate embedding vector."""
        # Truncate to avoid token limits
        prepared = self._prepare_embedding_input(text, kind=kind)
        truncated = prepared[: self.max_chars]
        try:
            client = self._client_for_running_loop()
            if client is None:
                self._record_failure("client_unavailable")
                return []
            response = await client.embeddings.create(
                model=self.model,
                input=truncated,
            )
            if response.data and len(response.data) > 0:
                self._record_success()
                return response.data[0].embedding
            self._record_failure("empty_response")
            return []
        except Exception as e:
            reason = self._describe_error(e)
            self._record_failure(reason)
            logger.warning("Embedding API call failed / 向量接口调用失败 (%s): %s", kind, reason)
            return []

    def _client_for_running_loop(self):
        base = self.client
        if base is None or not isinstance(base, AsyncOpenAI):
            return base
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return base
        if self._base_client is not base:
            # First use, or hot reload swapped the client: bind it to this loop.
            self._base_client = base
            self._base_loop = loop
            self._loop_clients = weakref.WeakKeyDictionary()
            return base
        if loop is self._base_loop:
            return base
        client = self._loop_clients.get(loop)
        if client is None:
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30.0)
            self._loop_clients[loop] = client
        return client

    def _describe_error(self, exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        message = str(exc).replace("\n", " ").strip()
        if self.api_key:
            message = message.replace(self.api_key, "[REDACTED]")
        prefix = type(exc).__name__ + (f" {status}" if status else "")
        return f"{prefix}: {message[:160]}" if message else prefix

    def _record_success(self) -> None:
        self.last_success_at = time.time()
        self.consecutive_failures = 0

    def _record_failure(self, reason: str) -> None:
        self.last_failure_at = time.time()
        self.last_error = str(reason or "unknown")[:200]
        self.consecutive_failures += 1

    def health_snapshot(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "model": self.model,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    async def self_check(self) -> bool:
        """Embed a short probe query; result lands in the health snapshot."""
        if not self.enabled:
            return False
        return bool(await self._generate_embedding("embedding self check", kind="query"))

    def _store_embedding(self, bucket_id: str, embedding: list[float]):
        """Store embedding in SQLite."""
        from utils import now_iso
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO embeddings (bucket_id, embedding, model, dimension, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bucket_id, json.dumps(embedding), self.model, len(embedding), now_iso()),
        )
        conn.commit()
        conn.close()

    def delete_embedding(self, bucket_id: str):
        """Remove embedding when bucket is deleted."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
        conn.commit()
        conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        """Retrieve stored embedding for a bucket. Returns None if not found."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT embedding, model, dimension FROM embeddings WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        conn.close()
        if row:
            try:
                embedding = json.loads(row[0])
                if not self._row_matches_current_model(row[1], row[2], embedding):
                    return None
                return embedding
            except json.JSONDecodeError:
                return None
        return None

    async def get_embeddings(self, bucket_ids: list[str]) -> dict[str, list[float]]:
        """Retrieve stored embeddings for several buckets with one SQLite read."""
        unique_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in bucket_ids
                if str(item or "").strip()
            )
        )
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT bucket_id, embedding, model, dimension FROM embeddings WHERE bucket_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        finally:
            conn.close()

        output: dict[str, list[float]] = {}
        for bucket_id, payload, model, dimension in rows:
            try:
                embedding = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            if self._row_matches_current_model(model, dimension, embedding):
                output[str(bucket_id)] = embedding
        return output

    def get_embedding_statuses(self, bucket_ids: list[str]) -> dict[str, str]:
        """Report whether requested buckets have a usable vector for the active model."""
        unique_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in bucket_ids
                if str(item or "").strip()
            )
        )
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT bucket_id, embedding, model, dimension FROM embeddings WHERE bucket_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        finally:
            conn.close()

        by_id = {str(row[0]): row for row in rows}
        statuses: dict[str, str] = {}
        for bucket_id in unique_ids:
            row = by_id.get(bucket_id)
            if row is None:
                statuses[bucket_id] = "embedding_missing"
                continue
            try:
                embedding = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                statuses[bucket_id] = "embedding_invalid"
                continue
            statuses[bucket_id] = (
                "indexed"
                if self._row_matches_current_model(row[2], row[3], embedding)
                else "embedding_stale_model_or_dimension"
            )
        return statuses

    def missing_bucket_ids(self, bucket_ids: list[str]) -> list[str]:
        """Buckets without a usable vector for the active model (missing, invalid or stale)."""
        statuses = self.get_embedding_statuses(bucket_ids)
        return [bucket_id for bucket_id, status in statuses.items() if status != "indexed"]

    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        Search for buckets similar to query text.
        Returns list of (bucket_id, similarity_score) sorted by score desc.
        搜索与查询文本相似的桶。返回 (bucket_id, 相似度分数) 列表。
        """
        results, _status = await self.search_similar_with_status(query, top_k=top_k)
        return results

    async def search_similar_with_status(
        self,
        query: str,
        top_k: int = 10,
    ) -> tuple[list[tuple[str, float]], str]:
        """Search with a diagnostic status so an absent score is not reported as zero."""
        if not self.enabled:
            return [], "engine_disabled"

        try:
            query_embedding = await self._generate_embedding(query, kind="query")
            if not query_embedding:
                return [], "query_embedding_unavailable"
        except Exception as e:
            logger.warning(f"Query embedding failed: {e}")
            return [], "query_embedding_failed"

        # Load all embeddings from SQLite
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT bucket_id, embedding, model, dimension FROM embeddings").fetchall()
        conn.close()

        if not rows:
            return [], "index_empty"

        # Calculate cosine similarity
        results = []
        for bucket_id, emb_json, model, dimension in rows:
            try:
                stored_embedding = json.loads(emb_json)
                if not self._row_matches_current_model(model, dimension, stored_embedding):
                    continue
                sim = self._cosine_similarity(query_embedding, stored_embedding)
                results.append((bucket_id, sim))
            except (json.JSONDecodeError, Exception):
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        if not results:
            return [], "no_current_model_embeddings"
        return results[:top_k], "completed"

    def _prepare_embedding_input(self, text: str, *, kind: str) -> str:
        raw = str(text or "")
        if kind == "query" and self.query_instruction:
            return f"Instruct: {self.query_instruction}\nQuery: {raw}"
        if kind == "document" and self.document_instruction:
            return f"Instruct: {self.document_instruction}\nDocument: {raw}"
        return raw

    def _row_matches_current_model(self, model: str | None, dimension: int | None, embedding: list[float]) -> bool:
        if not embedding:
            return False
        if model != self.model:
            return False
        try:
            stored_dimension = int(dimension)
        except (TypeError, ValueError):
            return False
        return stored_dimension == len(embedding)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row[1] == column for row in rows):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    @staticmethod
    def _int_between(value, default: int, min_value: int, max_value: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(min_value, min(max_value, number))

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
