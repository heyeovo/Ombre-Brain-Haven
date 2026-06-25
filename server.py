# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import re
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import datetime
import json as _json_lib
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- In-memory caches for hot endpoints / 内存级缓存 ---
# 日记/桶列表每次都是 walk + frontmatter.load 全量文件, 100+ 条就很慢。
# 加 TTL 缓存: 命中的时候不用读盘, 写操作主动 invalidate。
_JOURNAL_CACHE = {"ts": 0.0, "payload": None}
_BUCKETS_CACHE = {"ts": 0.0, "payload": None}
_CACHE_TTL = 60.0  # 秒

def _invalidate_cache(key: str):
    globals()[f"_{key}_CACHE"]["ts"] = 0.0
    globals()[f"_{key}_CACHE"]["payload"] = None

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器

# --- Load runtime scoring overrides from runtime_config.json ---
# --- 从 runtime_config.json 加载运行时评分覆盖 ---
_runtime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_config.json")
if os.path.exists(_runtime_path):
    try:
        with open(_runtime_path, "r", encoding="utf-8") as _f:
            _rt = json.loads(_f.read()) or {}
        _scoring_ov = _rt.get("scoring", {})
        if _scoring_ov:
            bucket_mgr.apply_runtime_scoring_overrides(_scoring_ov)
            logger.info(f"Loaded runtime scoring overrides: {list(_scoring_ov.keys())}")
    except Exception:
        pass

dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, 
                        samesite="none", secure=True, max_age=86400 * 7)
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"📌 [核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 20 surfacing buckets in hook
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    wish: bool = False,
    todo: str = "",
    todo_done: bool = False,
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).

    When config "auto_merge" is False (controlled by OMBRE_AUTO_MERGE env var
    or config.yaml), skips merge entirely and always creates new buckets.
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    auto_merge=False 时跳过合并，始终新建。
    """
    # --- Respect auto_merge config / 尊重 auto_merge 配置 ---
    if not config.get("auto_merge", True):
        bucket_id = await bucket_mgr.create(
            content=content, tags=tags, importance=importance,
            domain=domain, valence=valence, arousal=arousal,
            name=name or None, wish=wish, todo=todo, todo_done=todo_done,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return bucket_id, False

    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                update_kwargs = dict(
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                )
                if wish:
                    update_kwargs["wish"] = True
                if todo:
                    update_kwargs["todo"] = todo
                    update_kwargs["todo_done"] = todo_done
                await bucket_mgr.update(bucket["id"], **update_kwargs)
                # --- Update embedding after merge ---
                try:
                    await embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception:
                    pass
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
        wish=wish,
        todo=todo,
        todo_done=todo_done,
    )
    # --- Generate embedding for new bucket ---
    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return bucket_id, False


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 20,
    importance_min: int = -1,
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认10000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认20,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。domain="journal"读取独立日记通道(上锁的桶只显示标题和提示)。"""
    await decay_engine.ensure_started()
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- importance_min mode: bulk fetch by importance threshold ---
    # --- 重要度批量拉取模式：跳过语义搜索，按 importance 降序返回 ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    # --- 无参数或空query：浮现模式（权重池主动推送）---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        pinned_results = []
        for b in pinned_buckets:
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                pinned_results.append(f"📌 [核心准则] [bucket_id:{b['id']}] {summary}")
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                continue

        # --- Unresolved buckets: surface top N by weight ---
        # --- 未解决桶：按权重浮现前 N 条 ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and not b["metadata"].get("digested", False)   # 加这行
            and b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]

        logger.info(
            f"Breath surfacing: {len(all_buckets)} total, "
            f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
        )

        scored = sorted(
            unresolved,
            key=lambda b: decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )

        if scored:
            top_scores = [(b["metadata"].get("name", b["id"]), decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
            logger.info(f"Top unresolved scores: {top_scores}")

        # --- Cold-start detection: never-seen important buckets surface first ---
        # --- 冷启动检测：从未被访问过且重要度>=8的桶优先插入最前面（最多2个）---
        cold_start = [
            b for b in unresolved
            if int(b["metadata"].get("activation_count", 0)) == 0
            and int(b["metadata"].get("importance", 0)) >= 8
        ][:2]
        cold_start_ids = {b["id"] for b in cold_start}
        # Merge: cold_start first, then scored (excluding duplicates)
        scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
        scored_with_cold = cold_start + scored_deduped

        # --- Token-budgeted surfacing with diversity + hard cap ---
        # --- 按 token 预算浮现，带多样性 + 硬上限 ---
        # Top-1 always surfaces; rest sampled from top-20 for diversity
        token_budget = max_tokens
        for r in pinned_results:
            token_budget -= count_tokens_approx(r)

        candidates = list(scored_with_cold)
        if len(candidates) > 1:
            # Cold-start buckets stay at front; shuffle rest from top-20
            n_cold = len(cold_start)
            non_cold = candidates[n_cold:]
            if len(non_cold) > 1:
                top1 = [non_cold[0]]
                pool = non_cold[1:min(20, len(non_cold))]
                random.shuffle(pool)
                non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
            candidates = cold_start + non_cold
        # Hard cap: never surface more than max_results buckets
        candidates = candidates[:max_results]

        dynamic_results = []
        for b in candidates:
            if token_budget <= 0:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                summary_tokens = count_tokens_approx(summary)
                if summary_tokens > token_budget:
                    break
                # NOTE: no touch() here — surfacing should NOT reset decay timer
                score = decay_engine.calculate_score(b["metadata"])
                dynamic_results.append(f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}")
                token_budget -= summary_tokens
                # dream() 去重用：标记"刚被 breath 浮现过"，不影响衰减打分
                await bucket_mgr.mark_surfaced(b["id"])
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        if not pinned_results and not dynamic_results:
            return "权重池平静，没有需要处理的记忆。"

        # --- wish 低概率随机浮现：长期悬念，不受 max_results 常规限制 ---
        # --- 不是每次都出，约15%概率从已浮现内容之外随机挑一个 wish 桶 ---
        wish_result = None
        WISH_SURFACE_PROBABILITY = 0.15
        if random.random() < WISH_SURFACE_PROBABILITY:
            surfaced_ids = {b["id"] for b in candidates} | {b["id"] for b in pinned_buckets}
            wish_candidates = [
                b for b in all_buckets
                if b["metadata"].get("wish")
                and b["id"] not in surfaced_ids
                and not b["metadata"].get("resolved", False)
                and not b["metadata"].get("digested", False)
            ]
            if wish_candidates:
                b = random.choice(wish_candidates)
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    wish_result = f"🌙 [bucket_id:{b['id']}] {summary}"
                except Exception as e:
                    logger.warning(f"Failed to dehydrate wish bucket / wish桶脱水失败: {e}")

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        if wish_result:
            parts.append("=== 还记得这个吗 ===\n" + wish_result)
        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            feels = feels[:20]  # 只取最近20条
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- Journal retrieval: domain="journal" is a fully independent channel ---
    # --- 日记检索：domain="journal" 完全独立通道，不与普通breath/search混 ---
    if domain.strip().lower() == "journal":
        try:
            journal_entries = await bucket_mgr.list_journal()
            journal_entries.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            journal_entries = journal_entries[:20]
            if not journal_entries:
                return "日记本是空的。"
            results = []
            for j in journal_entries:
                meta = j["metadata"]
                name = meta.get("name", j["id"])
                author = meta.get("author", "共同")
                created = meta.get("created", "")
                if meta.get("locked"):
                    hint = meta.get("unlock_hint", "")
                    # --- 日期形式的 hint：到点自动解锁；非日期形式视为密码，保持锁定 ---
                    auto_unlocked = False
                    try:
                        unlock_date = datetime.fromisoformat(str(hint))
                        auto_unlocked = datetime.now() >= unlock_date
                    except (ValueError, TypeError):
                        auto_unlocked = False
                    if not auto_unlocked:
                        entry = (
                            f"🔒 [{name}] [作者:{author}] [bucket_id:{j['id']}]\n"
                            f"（已上锁，提示：{hint or '无'}）"
                        )
                        results.append(entry)
                        if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                            break
                        continue
                entry = (
                    f"[{created}] [{name}] [作者:{author}] [bucket_id:{j['id']}]\n"
                    f"{strip_wikilinks(j['content'])}"
                )
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 日记本 ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Journal retrieval failed: {e}")
            return "读取日记失败。"

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max_results,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # --- Vector similarity channel: hybrid retrieval, threshold-triggered ---
    # --- 向量相似度通道：混合检索，按关键词最高分置信度决定是否启用向量 ---
    # --- (而非 len(matches)==0 一刀切) ---
    top_score = max((b.get("score", 0) for b in matches), default=0)
    VECTOR_TRIGGER_THRESHOLD = 50  # 阈值可调
    if top_score < VECTOR_TRIGGER_THRESHOLD:
        matched_ids = {b["id"] for b in matches}
        try:
            vector_results = await embedding_engine.search_similar(
                query, top_k=max_results
            )
            for bucket_id, sim_score in vector_results:
                if bucket_id not in matched_ids and sim_score > 0.7:
                    bucket = await bucket_mgr.get(bucket_id)
                    if bucket:
                        meta = bucket.get("metadata", {})
                        if (meta.get("type") == "archived" 
                                or meta.get("digested", False)):
                            continue
                        # 向量结果降权，不允许排到高质量关键词结果前面
                        bucket["score"] = round(sim_score * 100 * 0.4, 2)
                        bucket["vector_match"] = True
                        matches.append(bucket)
                        matched_ids.add(bucket_id)
        except Exception as e:
            logger.warning(f"Vector search failed / 向量搜索失败: {e}")

        # 关键词+向量合并后重新排序，统一受 max_results 管控
        matches.sort(key=lambda b: b.get("score", 0), reverse=True)
        matches = matches[:max_results]

    results = []
    token_used = 0

    # --- Pinned/protected buckets participate in normal scoring (importance=10
    # --- gives them enough boost); no forced top-insertion here anymore —
    # --- that was the source of the duplicate-bucket bug (forced-top once +
    # --- matches loop once). 钉选桶现在走正常评分(importance=10已经能让
    # --- 相关的排前面)，不再强制置顶——那是重复桶问题的根源。
    for bucket in matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await bucket_mgr.soft_touch(bucket["id"])
            if bucket.get("vector_match"):
                summary = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                summary = f"[bucket_id:{bucket['id']}] {summary}"
            results.append(summary)
            token_used += summary_tokens

            # --- 关系边：命中桶若有 related 字段，附带关联桶摘要 ---
            # --- 不占主结果名额(max_results只数主结果)，但仍受 max_tokens 约束 ---
            related_ids = clean_meta.get("related") or []
            for rel_id in related_ids[:2]:  # 每条最多带2个关联，避免一条带出一长串
                if token_used >= max_tokens:
                    break
                try:
                    rel_bucket = await bucket_mgr.get(rel_id)
                    if not rel_bucket:
                        continue
                    rel_meta = {k: v for k, v in rel_bucket["metadata"].items() if k != "tags"}
                    rel_summary = await dehydrator.dehydrate(strip_wikilinks(rel_bucket["content"]), rel_meta)
                    rel_tokens = count_tokens_approx(rel_summary)
                    if token_used + rel_tokens > max_tokens:
                        break
                    results.append(f"  ↳ [关联] [bucket_id:{rel_id}] {rel_summary}")
                    token_used += rel_tokens
                except Exception as e:
                    logger.warning(f"Failed to attach related bucket {rel_id}: {e}")
                    continue
        except Exception as e:
            logger.error(
                f"Failed to dehydrate search result / 检索结果脱水失败 "
                f"(bucket_id={bucket.get('id', '?')}): {e}",
                exc_info=True,
            )
            continue

    # --- 时间涟漪已移除：它在 max_results 之外额外补充桶，是返回数量
    # --- 失控的来源之一。有意义的前后关联改用关系边（related 字段）替代。

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    final_text = "\n---\n".join(results)
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",    valence: float = -1,
    arousal: float = -1,
    wish: bool = False,
    todo: str = "",
    todo_done: bool = False,
    journal: bool = False,
    author: str = "",
    locked: bool = False,
    unlock_hint: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。wish=True打上长期悬念标签。todo=附着待办内容,todo_done=是否已完成。journal=True存进独立日记通道(只能breath(domain="journal")读取),author=言之/小羊/共同,locked=True上锁(配合unlock_hint:日期或密码)。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Journal mode: independent channel, bypasses merge entirely ---
    # --- 日记模式：独立通道，完全不走合并 ---
    if journal:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=extra_tags,
            importance=importance,
            domain=[],
            valence=valence if 0 <= valence <= 1 else 0.5,
            arousal=arousal if 0 <= arousal <= 1 else 0.3,
            name=None,
            bucket_type="journal",
            author=author or "共同",
            locked=locked,
            unlock_hint=unlock_hint,
        )
        return f"📔日记→{bucket_id} [作者:{author or '共同'}]" + ("[已上锁]" if locked else "")

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            wish=wish,
            todo=todo,
            todo_done=todo_done,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        wish=wish,
        todo=todo,
        todo_done=todo_done,
    )

    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
    touch: bool = False,      # 新增：轻触激活
    ripple: bool = False,     # 新增：完整激活+涟漪（仅 touch=True 时有效）
    wish: int = -1,           # 1=打上wish标签(长期悬念),0=取消
    todo: str = "",           # 附着在桶上的待办内容
    todo_done: int = -1,      # 1=待办已完成,0=未完成
    author: str = "",         # 日记作者:言之/小羊/共同
    locked: int = -1,         # 日记上锁:1=锁,0=解锁
    unlock_hint: str = "",    # 解锁提示(日期或密码)
    related: str = "",        # 关联桶id,逗号分隔
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文,delete=True删除。只传需改的,-1或空=不改。touch=True轻触激活，ripple=True完整激活+时间涟漪。wish=1/0打标/取消长期悬念；todo/todo_done附着待办；author/locked/unlock_hint用于日记桶；related=逗号分隔的关联桶id列表。"""


    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Touch / activate ---
    if touch:
        if ripple:
            await bucket_mgr.touch(bucket_id)
            return f"已完整激活记忆桶 {bucket_id}（含涟漪）"
        else:
            await bucket_mgr.soft_touch(bucket_id)
            return f"已轻触激活记忆桶 {bucket_id}"
        
    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        updates["content"] = content
    if wish in (0, 1):
        updates["wish"] = bool(wish)
    if todo:
        updates["todo"] = todo
    if todo_done in (0, 1):
        updates["todo_done"] = bool(todo_done)
    if author:
        updates["author"] = author
    if locked in (0, 1):
        updates["locked"] = bool(locked)
    if unlock_hint:
        updates["unlock_hint"] = unlock_hint
    if related:
        updates["related"] = [r.strip() for r in related.split(",") if r.strip()]

    # --- Touch / activate ---
    if touch:
        if ripple:
            await bucket_mgr.touch(bucket_id)
            return f"已完整激活记忆桶 {bucket_id}（含涟漪）"
        else:
            await bucket_mgr.soft_touch(bucket_id)
            return f"已轻触激活记忆桶 {bucket_id}"
        
    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed
    if "content" in updates:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream() -> str:
    """做梦——读取最近新增的记忆桶,供你自省。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。"""
    await decay_engine.ensure_started()

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    # --- dream 去重：breath() 浮现窗口刚出现过的桶跳过，不重复返回全文 ---
    # --- (10分钟窗口，覆盖"开窗三件套" breath→dream→breath(feel) 连续调用场景) ---
    DREAM_DEDUPE_WINDOW_MINUTES = 10
    now = datetime.now()

    def _recently_surfaced(meta: dict) -> bool:
        ts = meta.get("last_breath_surfaced")
        if not ts:
            return False
        try:
            surfaced_at = datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            return False
        return (now - surfaced_at).total_seconds() / 60 < DREAM_DEDUPE_WINDOW_MINUTES

    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("resolved", False)   
        and not b["metadata"].get("digested", False)   
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not _recently_surfaced(b["metadata"])
    ]

    # --- Sort by creation time desc, take top 10 ---
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:10]

    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        created = meta.get("created", "")
        todo_line = ""
        if meta.get("todo"):
            done_tag = "✅" if meta.get("todo_done") else "⬜"
            todo_line = f"{done_tag} 待办: {meta['todo']}\n"
        parts.append(
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} V{val:.1f}/A{aro:.1f} "
            f"创建:{created}\n"
            f"ID: {b['id']}\n"
            f"{todo_line}"
            f"{strip_wikilinks(b['content'][:2000])}"
        )

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata. ?full=1 returns full content."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        full = request.query_params.get("full", "").lower() in ("1", "true")
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            raw_content = strip_wikilinks(b.get("content", ""))
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": raw_content[:200] if not full else raw_content,
                "wish": meta.get("wish", False),
                "todo": meta.get("todo", ""),
                "todo_done": meta.get("todo_done", False),
                "related": meta.get("related", []),
                # Noise = resolved + importance==1 (user-marked soft-delete)
                # 噪声 = 已解决 + importance为1（用户标记的软删除）
                "noise": bool(meta.get("resolved", False) and meta.get("importance") == 1),
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
        "noise": bool(meta.get("resolved", False) and meta.get("importance") == 1),
    })

@mcp.custom_route("/api/archive/{bucket_id}", methods=["POST"])
async def api_archive_bucket(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.archive(bucket_id)
    if not success:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})

@mcp.custom_route("/api/unarchive/{bucket_id}", methods=["POST"])
async def api_unarchive_bucket(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.unarchive(bucket_id)
    if not success:
        return JSONResponse({"error": "not found or not archived"}, status_code=404)
    return JSONResponse({"ok": True})

@mcp.custom_route("/api/touch/{bucket_id}", methods=["POST"])
async def api_touch_bucket(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    ripple = request.query_params.get("ripple", "").lower() in ("1", "true")
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    if ripple:
        await bucket_mgr.touch(bucket_id)
    else:
        await bucket_mgr.soft_touch(bucket_id)
    return JSONResponse({"ok": True, "ripple": ripple})

@mcp.custom_route("/api/bucket/{bucket_id}", methods=["PATCH", "POST"])
async def api_update_bucket(request):
    """
    通用桶元数据更新端点——前端编辑面板(wish开关/todo/related连线/日记锁)
    都走这个接口。只更新body里实际传的字段，其余不动。
    Generic metadata update endpoint for the dashboard. Accepts any subset
    of: name, domain(list), valence, arousal, importance, tags(list),
    resolved, pinned, digested, content, wish, todo, todo_done, author,
    locked, unlock_hint, related(list of bucket ids).
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)

    allowed_fields = {
        "name", "domain", "valence", "arousal", "importance", "tags",
        "resolved", "pinned", "digested", "content",
        "wish", "todo", "todo_done", "author", "locked", "unlock_hint", "related",
        "model_valence",
    }
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        return JSONResponse({"error": "no recognized fields in body"}, status_code=400)

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return JSONResponse({"error": "update failed"}, status_code=500)

    if "content" in updates:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    return JSONResponse({"ok": True, "updated": list(updates.keys())})

@mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
async def api_delete_bucket(request):
    """软删除桶——移入回收站。前端编辑面板的"抹除此记忆"按钮用。可恢复。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.delete(bucket_id)
    if not success:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})

@mcp.custom_route("/api/bucket/{bucket_id}/to-journal", methods=["POST"])
async def api_convert_to_journal(request):
    """
    把一个已有桶(动态/永久/feel)转为日记桶——前端编辑面板"设为日记"按钮用。
    物理移动文件到journal目录，转换后脱离常规update/delete查找范围，
    只能通过 /api/journal 系列接口编辑。不可逆，前端应有二次确认。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    success = await bucket_mgr.convert_to_journal(
        bucket_id,
        author=body.get("author", "共同"),
        locked=bool(body.get("locked", False)),
        unlock_hint=body.get("unlock_hint", ""),
    )
    if not success:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})

@mcp.custom_route("/api/bucket", methods=["POST"])
async def api_create_bucket(request):
    """
    通用桶创建端点(前端"新建记忆"用)。取代 add-bucket route.ts 里的 MCP hold 调用。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    content = body.get("content", "")
    if not content or not content.strip():
        return JSONResponse({"error": "content required"}, status_code=400)
    raw_tags = body.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    else:
        tags = raw_tags
    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=int(body.get("importance", 5)),
        domain=body.get("domain", None),
        valence=float(body.get("valence", 0.5)),
        arousal=float(body.get("arousal", 0.3)),
        name=body.get("name") or None,
        pinned=bool(body.get("pinned", False)),
    )
    return JSONResponse({"ok": True, "id": bucket_id})


# --- Trash / Soft Delete / 回收站 ---
@mcp.custom_route("/api/bucket/{bucket_id}/restore", methods=["POST"])
async def api_restore_bucket(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.restore(bucket_id)
    if not success:
        return JSONResponse({"error": "not found in trash"}, status_code=404)
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/bucket/{bucket_id}/purge", methods=["POST"])
async def api_purge_bucket(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    success = await bucket_mgr.purge(bucket_id)
    if not success:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/trash", methods=["GET"])
async def api_list_trash(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        items = await bucket_mgr.list_trash()
        result = []
        for b in items:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "domain": meta.get("domain", []),
                "type": meta.get("original_type", meta.get("type", "dynamic")),
                "trashed_at": meta.get("trashed_at", ""),
                "importance": meta.get("importance", 5),
                "content_preview": b.get("content", "")[:150],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/trash/empty", methods=["POST"])
async def api_empty_trash(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    count = await bucket_mgr.empty_trash()
    return JSONResponse({"ok": True, "count": count})


# --- Merge Preview / 合并预览 ---
@mcp.custom_route("/api/bucket/{bucket_id}/similar", methods=["GET"])
async def api_similar_buckets(request):
    """Find similar buckets via embedding engine."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        n = int(request.query_params.get("n", "5"))
        content = bucket.get("content", "")
        if embedding_engine and embedding_engine.enabled:
            results = await embedding_engine.search_similar(content, top_k=n + 1)
            # Exclude self
            similar = [(bid, sim) for bid, sim in results if bid != bucket_id][:n]
            emb_enabled = True
            emb_count = len(results)
        else:
            similar = []
            emb_enabled = False
            emb_count = 0
        result = []
        for bid, sim in similar:
            b = await bucket_mgr.get(bid)
            if b:
                meta = b.get("metadata", {})
                result.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "similarity": round(sim, 4),
                    "content_preview": b.get("content", "")[:150],
                    "domain": meta.get("domain", []),
                    "importance": meta.get("importance", 5),
                })
        return JSONResponse({
            "items": result,
            "embedding_enabled": emb_enabled,
            "total_scanned": emb_count,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/merge-preview", methods=["POST"])
async def api_merge_preview(request):
    """Generate LLM merge preview between two buckets. ?into={target_id}"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    into_id = request.query_params.get("into", "")
    if not into_id:
        return JSONResponse({"error": "missing `into` query param"}, status_code=400)
    if bucket_id == into_id:
        return JSONResponse({"error": "cannot merge a bucket into itself"}, status_code=400)

    bucket_a = await bucket_mgr.get(bucket_id)
    bucket_b = await bucket_mgr.get(into_id)
    if not bucket_a or not bucket_b:
        return JSONResponse({"error": "one or both buckets not found"}, status_code=404)

    # Check preconditions
    b_meta = bucket_b.get("metadata", {})
    if b_meta.get("pinned") or b_meta.get("protected"):
        return JSONResponse({
            "error": "目标桶已钉选/保护，拒绝合并",
            "hint": "请先取消目标桶的保护标记",
        }, status_code=409)

    try:
        merged_content = await dehydrator.merge(bucket_b["content"], bucket_a["content"])

        # Cost estimate
        cost = {}
        usage = getattr(dehydrator.__class__, "_last_merge_usage", None)
        if usage:
            from utils import estimate_llm_cost
            cost = estimate_llm_cost(
                usage.get("model", dehydrator.model),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

        meta_a = bucket_a["metadata"]
        meta_b = bucket_b["metadata"]
        a_content = bucket_a.get("content", "") or ""
        b_content = bucket_b.get("content", "") or ""

        return JSONResponse({
            "ok": True,
            "preview": True,
            "a": {"id": bucket_id, "name": meta_a.get("name", bucket_id)},
            "b": {"id": into_id, "name": meta_b.get("name", into_id)},
            "merged_content": merged_content,
            "a_content": a_content,
            "b_content": b_content,
            # Word counts
            "a_chars": len(a_content),
            "b_chars": len(b_content),
            "merged_chars": len(merged_content),
            # Merged metadata
            "importance": max(meta_a.get("importance", 5), meta_b.get("importance", 5)),
            "tags": list(set(meta_a.get("tags", []) + meta_b.get("tags", []))),
            "domain": list(set(meta_a.get("domain", []) + meta_b.get("domain", []))),
            "valence": round((meta_a.get("valence", 0.5) + meta_b.get("valence", 0.5)) / 2, 2),
            "arousal": round((meta_a.get("arousal", 0.3) + meta_b.get("arousal", 0.3)) / 2, 2),
            "cost": cost,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/merge-commit", methods=["POST"])
async def api_merge_commit(request):
    """Apply confirmed merge: update target, delete source."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    into_id = request.query_params.get("into", "")
    if bucket_id == into_id:
        return JSONResponse({"error": "cannot merge into itself"}, status_code=400)

    try:
        body = await request.json()
        merged_content = body.get("merged_content", "")
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)

    if not merged_content:
        return JSONResponse({"error": "missing merged_content"}, status_code=400)

    bucket_b = await bucket_mgr.get(into_id)
    if not bucket_b:
        return JSONResponse({"error": "target bucket not found"}, status_code=404)

    try:
        meta_a = (await bucket_mgr.get(bucket_id) or {}).get("metadata", {})
        meta_b = bucket_b["metadata"]
        merged_tags = list(set(meta_a.get("tags", []) + meta_b.get("tags", [])))
        merged_domain = list(set(meta_a.get("domain", []) + meta_b.get("domain", [])))
        merged_imp = max(meta_a.get("importance", 5), meta_b.get("importance", 5))
        merged_v = round((meta_a.get("valence", 0.5) + meta_b.get("valence", 0.5)) / 2, 2)
        merged_a = round((meta_a.get("arousal", 0.3) + meta_b.get("arousal", 0.3)) / 2, 2)

        await bucket_mgr.update(into_id, content=merged_content,
                                tags=merged_tags, domain=merged_domain,
                                importance=merged_imp, valence=merged_v, arousal=merged_a)
        # Update embedding
        try:
            await embedding_engine.generate_and_store(into_id, merged_content)
        except Exception:
            pass

        # Delete source bucket (hard delete, bypass trash)
        src_path = bucket_mgr._find_bucket_file(bucket_id)
        if src_path:
            try:
                os.remove(src_path)
            except OSError:
                pass
        logger.info(f"Merge committed: {bucket_id} → {into_id}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/journal", methods=["GET"])
async def api_list_journal(request):
    """
    日记列表(前端日记页用)。锁着的条目只返回标题+hint,不返回正文。
    60s 内存缓存，写日记主动 invalidate。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    # Cache hit
    if _JOURNAL_CACHE["payload"] is not None and time.time() - _JOURNAL_CACHE["ts"] < _CACHE_TTL:
        return JSONResponse(_JOURNAL_CACHE["payload"])
    try:
        entries = await bucket_mgr.list_journal()
        entries.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        result = []
        for j in entries:
            meta = j["metadata"]
            item = {
                "id": j["id"],
                "name": meta.get("name", j["id"]),
                "author": meta.get("author", "共同"),
                "created": meta.get("created", ""),
                "locked": bool(meta.get("locked", False)),
            }
            if meta.get("locked"):
                hint = meta.get("unlock_hint", "")
                auto_unlocked = False
                try:
                    unlock_date = datetime.fromisoformat(str(hint))
                    auto_unlocked = datetime.now() >= unlock_date
                except (ValueError, TypeError):
                    auto_unlocked = False
                if not auto_unlocked:
                    item["unlock_hint"] = hint
                    item["content"] = None
                    result.append(item)
                    continue
            item["content"] = strip_wikilinks(j.get("content", ""))
            result.append(item)
        _JOURNAL_CACHE["payload"] = result
        _JOURNAL_CACHE["ts"] = time.time()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@mcp.custom_route("/api/journal", methods=["POST"])
async def api_create_journal(request):
    """新建日记条目(前端日记页的写入按钮用)。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    content = body.get("content", "")
    if not content or not content.strip():
        return JSONResponse({"error": "content required"}, status_code=400)
    bucket_id = await bucket_mgr.create(
        content=content,
        tags=body.get("tags", []),
        importance=int(body.get("importance", 5)),
        domain=[],
        valence=float(body.get("valence", 0.5)),
        arousal=float(body.get("arousal", 0.3)),
        name=body.get("name") or None,
        bucket_type="journal",
        author=body.get("author", "共同"),
        locked=bool(body.get("locked", False)),
        unlock_hint=body.get("unlock_hint", ""),
    )
    _invalidate_cache("JOURNAL")
    return JSONResponse({"ok": True, "id": bucket_id})

@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    
    # 1. 在这里解析参数
    include_archive = request.query_params.get("include_archive", "false").lower() in ("1", "true")
    
    try:
        # 2. 将 include_archive 传入 search 方法
        limit = int(request.query_params.get("limit", bucket_mgr.max_results))
        show_all = request.query_params.get("show_all", "false").lower() in ("1", "true")
        include_noise = request.query_params.get("include_noise", "false").lower() in ("1", "true")
        simulate = request.query_params.get("simulate", "false").lower() in ("1", "true")
        include_vector = request.query_params.get("include_vector", "false").lower() in ("1", "true")
        matches = await bucket_mgr.search(query, limit=limit, include_archive=include_archive,
                                          show_all=show_all, include_noise=include_noise,
                                          record_stats=not simulate)  # 即时模拟不记统计

        # --- Simulate mode: enrich with vector similarity ---
        vector_map = {}
        if simulate and include_vector:
            try:
                if embedding_engine and embedding_engine.enabled:
                    vr = await embedding_engine.search_similar(query, top_k=200)
                    vector_map = {bid: round(score, 4) for bid, score in vr}
            except Exception:
                pass

        result = []
        seen_ids = set()
        for b in matches:
            meta = b.get("metadata", {})
            seen_ids.add(b["id"])
            vec_sim = vector_map.get(b["id"], 0)
            item = {
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            }
            if simulate:
                match_in = b.get("matched_in", [])
                field_scores = b.get("field_scores", {})
                item["matched_fields"] = {
                    "name": round(field_scores.get("name", 0), 1),
                    "domain": round(field_scores.get("domain", 0), 1),
                    "tags": round(field_scores.get("tags", 0), 1),
                    "content": round(field_scores.get("content", 0), 1),
                    "matched_in": match_in,
                }
                if include_vector and vec_sim > 0:
                    item["vector_similarity"] = vec_sim
            result.append(item)

        # --- Add vector-only matches (vec >= 0.5, not in keyword results) ---
        vector_only = []
        VEC_MIN_SCORE = 0.5
        if simulate and include_vector and vector_map:
            keyword_ids = seen_ids
            for bid, sim in vector_map.items():
                if sim < VEC_MIN_SCORE:
                    continue
                if bid not in keyword_ids:
                    b = await bucket_mgr.get(bid)
                    if b:
                        meta = b.get("metadata", {})
                        vector_only.append({
                            "id": bid,
                            "name": meta.get("name", bid),
                            "score": 0,
                            "domain": meta.get("domain", []),
                            "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                            "vector_similarity": round(sim, 4),
                            "matched_fields": None,
                            "_vector_only": True,
                        })
            vector_only.sort(key=lambda x: x["vector_similarity"], reverse=True)
            vector_only = vector_only[:limit]

        if simulate:
            return JSONResponse({"items": result, "vector_only": vector_only})
        else:
            # Strip extra fields for backward compat
            return JSONResponse([{
                "id": it["id"], "name": it["name"], "score": it["score"],
                "domain": it.get("domain", []), "valence": it.get("valence", 0.5),
                "arousal": it.get("arousal", 0.3), "content_preview": it.get("content_preview", ""),
            } for it in result])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get embedding similarity network for visualization."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
            if embedding_engine and embedding_engine.enabled:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        # Build edges from embeddings (similarity > 0.5)
        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    edges.append({"source": id_a, "target": id_b, "similarity": round(sim, 3)})

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None
    threshold_param = request.query_params.get("threshold")
    threshold = int(threshold_param) if threshold_param else bucket_mgr.fuzzy_threshold

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

 # 在 for bucket in all_buckets 循环之前加
        vector_map = {}
        if query:
            try:
                vr = await embedding_engine.search_similar(query, top_k=200)
                vector_map = {bid: round(score, 4) for bid, score in vr}
            except Exception:
                pass
            
        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic_match = bucket_mgr._calc_topic_match(query, bucket) if query else {"score": 0.0, "field_scores": {}, "matched_in": []}
                topic = topic_match["score"]
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= threshold,
                    "vector_score": vector_map.get(bid, 0.0),
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]

        # --- Record hit stats for search tracing ---
        if query:
            # Build scored items with metadata for record_hit
            hit_items = []
            for r in results[:20]:
                meta_item = {"id": r["id"], "metadata": {"name": r["name"], "domain": r["domain"], "type": r.get("type")}}
                hit_items.append({**meta_item, "score": r["normalized"]})
            bucket_mgr.record_hit(query, hit_items)

        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": threshold,     
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Hit stats & search tracing / 命中统计 & 检索追溯 ---
@mcp.custom_route("/api/hit-stats", methods=["GET"])
async def api_hit_stats(request):
    """Return per-bucket hit counts and search statistics."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", 50))
        include_zero = request.query_params.get("include_zero", "false").lower() in ("1", "true")
        order = request.query_params.get("order", "desc")
        exclude_gated = request.query_params.get("exclude_gated", "true").lower() not in ("0", "false")
        stats = bucket_mgr.get_hit_stats(limit=limit, include_zero=include_zero,
                                         order=order, exclude_gated=exclude_gated)
        return JSONResponse(stats)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/hit-stats/reset", methods=["POST"])
async def api_reset_hit_stats(request):
    """Clear all hit stats."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_mgr.reset_hit_stats()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/recent-searches", methods=["GET"])
async def api_recent_searches(request):
    """Return recent search and surface traces."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", 20))
        result = bucket_mgr.get_recent_searches(limit=limit)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Scoring knobs / 检索评分旋钮 ---
@mcp.custom_route("/api/scoring-config", methods=["GET"])
async def api_get_scoring_config(request):
    """Return current scoring knob values + defaults schema."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse({
        "current": bucket_mgr.current_scoring_overrides(),
        "defaults": dict(bucket_mgr.SCORING_OVERRIDE_DEFAULTS),
    })


@mcp.custom_route("/api/scoring-config", methods=["POST"])
async def api_set_scoring_config(request):
    """Update scoring knobs. Only whitelisted keys accepted."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    # Persist to runtime_config.json
    import json as _json
    import os as _os
    rt_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "runtime_config.json")
    rt = {}
    try:
        if _os.path.exists(rt_path):
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = _json.load(f) or {}
    except Exception:
        pass
    rt["scoring"] = dict(rt.get("scoring", {}))
    for k, v in body.items():
        if k in bucket_mgr.SCORING_OVERRIDE_DEFAULTS:
            rt["scoring"][k] = v
    try:
        with open(rt_path, "w", encoding="utf-8") as f:
            _json.dump(rt, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    bucket_mgr.apply_runtime_scoring_overrides(body)
    return JSONResponse({"ok": True, "current": bucket_mgr.current_scoring_overrides()})


@mcp.custom_route("/api/scoring-config/reset", methods=["POST"])
async def api_reset_scoring_config(request):
    """Reset scoring knobs to defaults."""
    from starlette.responses import JSONResponse
    import json as _json
    import os as _os
    err = _require_auth(request)
    if err: return err
    rt_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "runtime_config.json")
    try:
        if _os.path.exists(rt_path):
            rt = {}
            try:
                with open(rt_path, "r", encoding="utf-8") as f:
                    rt = _json.load(f) or {}
            except Exception:
                pass
            rt.pop("scoring", None)
            with open(rt_path, "w", encoding="utf-8") as f:
                _json.dump(rt, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    bucket_mgr.apply_runtime_scoring_overrides(dict(bucket_mgr.SCORING_OVERRIDE_DEFAULTS))
    return JSONResponse({"ok": True, "current": bucket_mgr.current_scoring_overrides()})


@mcp.custom_route("/api/config", methods=["GET"])
async def api_get_config(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse({
        "fuzzy_threshold": bucket_mgr.fuzzy_threshold,
        "max_results": bucket_mgr.max_results,
    })

@mcp.custom_route("/api/config", methods=["POST"])
async def api_update_config(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        if "fuzzy_threshold" in body:
            val = int(body["fuzzy_threshold"])
            if 0 <= val <= 100:
                bucket_mgr.fuzzy_threshold = val
        return JSONResponse({"ok": True, "fuzzy_threshold": bucket_mgr.fuzzy_threshold})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@mcp.custom_route("/api/prompts", methods=["GET"])
async def api_get_prompts(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse({
        "dehydrate": dehydrator.dehydrate_prompt,
        "analyze": dehydrator.analyze_prompt,
    })

@mcp.custom_route("/api/prompts", methods=["POST"])
async def api_update_prompts(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        name = body.get("name")
        content = body.get("content", "").strip()
        if not name or not content:
            return JSONResponse({"error": "missing fields"}, status_code=400)
        if name == "dehydrate":
            dehydrator.dehydrate_prompt = content
        elif name == "analyze":
            dehydrator.analyze_prompt = content
        else:
            return JSONResponse({"error": "unknown prompt"}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    
@mcp.custom_route("/api/prompts/test", methods=["POST"])
async def api_test_prompt(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        name = body.get("name")
        content = body.get("content", "")
        prompt_override = body.get("prompt_override", "").strip()

        if name == "dehydrate":
            original = dehydrator.dehydrate_prompt
            if prompt_override:
                dehydrator.dehydrate_prompt = prompt_override
            try:
                result = await dehydrator._api_dehydrate(content)
            finally:
                dehydrator.dehydrate_prompt = original
        elif name == "analyze":
            original = dehydrator.analyze_prompt
            if prompt_override:
                dehydrator.analyze_prompt = prompt_override
            try:
                result = await dehydrator._api_analyze(content)
            finally:
                dehydrator.analyze_prompt = original
        else:
            return JSONResponse({"error": "unknown"}, status_code=400)

        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")
        max_chunks = int(request.query_params.get("max_chunks", "0") or "0")
        mode = request.query_params.get("mode", "large")  # "large" or "small"

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume,
                                      max_chunks=max_chunks, mode=mode)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@mcp.custom_route("/admin/backfill", methods=["POST"])
async def admin_backfill(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        import asyncio
        from backfill_embeddings import backfill
        asyncio.create_task(backfill(batch_size=20))
        return JSONResponse({"status": "started"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "https://ob-dashboard2.vercel.app",
                "http://localhost:3000",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
