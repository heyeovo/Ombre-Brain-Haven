# ============================================================
# Module: Embedding Maintenance (embedding_maintenance.py)
# 模块：向量维护
#
# Retries per-bucket embedding refreshes and sweeps buckets whose
# vector is missing or stale, so a failed write heals itself.
# 单桶向量刷新重试 + 缺失/过期向量巡检，写失败后能自愈。
#
# 2026-10-04: from 2026-09-01 every new bucket silently lost its vector
# (failures were fire-and-forget, no retry, no sweep). This module exists
# so that cannot happen quietly again.
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import asyncio
import logging
import threading
import time
from typing import Awaitable, Callable

logger = logging.getLogger("ombre_brain.embedding")

RETRY_DELAYS_SECONDS = (2.0, 10.0, 30.0)


async def refresh_with_retry(
    refresh: Callable[[str], Awaitable[bool]],
    bucket_id: str,
    *,
    delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Run refresh(bucket_id); on False/exception retry after each delay."""
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            if await refresh(bucket_id):
                return True
        except Exception as e:
            logger.warning("Embedding refresh error / 向量刷新异常: %s: %s", bucket_id, e)
        if attempt < len(delays):
            await sleep(delays[attempt])
    logger.warning(
        "Embedding refresh gave up after %s attempts / 向量刷新重试耗尽: %s (sweep will retry)",
        attempts,
        bucket_id,
    )
    return False


class EmbeddingSweeper:
    """Find buckets without a usable vector and refresh a bounded batch."""

    def __init__(self, embedding_engine, list_bucket_ids: Callable[[], Awaitable[list[str]]],
                 refresh: Callable[[str], Awaitable[bool]]):
        self.embedding_engine = embedding_engine
        self.list_bucket_ids = list_bucket_ids
        self.refresh = refresh
        self.last_result: dict | None = None
        # Sweeps run on the scheduler thread and from /admin/backfill on the main loop.
        self._lock = threading.Lock()

    async def run_once(
        self,
        limit: int | None = 30,
        *,
        list_bucket_ids: Callable[[], Awaitable[list[str]]] | None = None,
        refresh: Callable[[str], Awaitable[bool]] | None = None,
    ) -> dict:
        """Overrides let a scheduler thread pass its own bucket manager while sharing the lock."""
        list_bucket_ids = list_bucket_ids or self.list_bucket_ids
        refresh = refresh or self.refresh
        if not getattr(self.embedding_engine, "enabled", False):
            result = {"status": "disabled"}
            self.last_result = {**result, "finished_at": time.time()}
            return result
        if not self._lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            bucket_ids = [str(item) for item in await list_bucket_ids() if str(item or "").strip()]
            missing = self.embedding_engine.missing_bucket_ids(bucket_ids)
            batch = missing if limit is None else missing[: max(0, int(limit))]
            refreshed: list[str] = []
            failed: list[str] = []
            for bucket_id in batch:
                try:
                    ok = await refresh(bucket_id)
                except Exception as e:
                    logger.warning("Embedding sweep refresh error / 向量巡检刷新异常: %s: %s", bucket_id, e)
                    ok = False
                (refreshed if ok else failed).append(bucket_id)
                if failed and not refreshed and len(failed) >= 3:
                    # API is down; stop hammering it and let the next sweep retry.
                    break
            result = {
                "status": "ok" if not failed else "partial",
                "checked": len(bucket_ids),
                "missing": len(missing),
                "refreshed": len(refreshed),
                "failed": len(failed),
                "remaining": len(missing) - len(refreshed),
            }
            if missing:
                log = logger.warning if failed else logger.info
                log("Embedding sweep / 向量巡检: %s", result)
            self.last_result = {**result, "finished_at": time.time()}
            return result
        finally:
            self._lock.release()
