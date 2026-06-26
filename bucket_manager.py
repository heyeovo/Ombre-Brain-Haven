# ============================================================
# Module: Memory Bucket Manager (bucket_manager.py)
# 模块：记忆桶管理器
#
# CRUD operations, multi-dimensional index search, activation updates
# for memory buckets.
# 记忆桶的增删改查、多维索引搜索、激活更新。
#
# Core design:
# 核心逻辑：
#   - Each bucket = one Markdown file (YAML frontmatter + body)
#     每个记忆桶 = 一个 Markdown 文件
#   - Storage by type: permanent / dynamic / archive
#     存储按类型分目录
#   - Multi-dimensional soft index: domain + valence/arousal + fuzzy text
#     多维软索引：主题域 + 情感坐标 + 文本模糊匹配
#   - Search strategy: domain pre-filter → weighted multi-dim ranking
#     搜索策略：主题域预筛 → 多维加权精排
#   - Emotion coordinates based on Russell circumplex model:
#     情感坐标基于环形情感模型（Russell circumplex）：
#       valence (0~1): 0=negative → 1=positive
#       arousal (0~1): 0=calm → 1=excited
#
# Depended on by: server.py, decay_engine.py
# 被谁依赖：server.py, decay_engine.py
# ============================================================

import os
import re
import math
import json
import atexit
import logging
import shutil
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import jieba
import frontmatter
from rapidfuzz import fuzz

from utils import generate_bucket_id, sanitize_name, safe_path, now_iso

logger = logging.getLogger("ombre_brain.bucket")


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(self, config: dict, embedding_engine=None):
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.journal_dir = os.path.join(self.base_dir, "journal")
        self.trash_dir = os.path.join(self.base_dir, "trash")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.content_weight = config.get("matching", {}).get("content_weight", 1)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec
        # Runtime-tunable knobs
        # 运行时旋钮
        self.title_hit_bonus = float(scoring.get("title_hit_bonus", 0.0))
        self.keyword_first_sort = bool(scoring.get("keyword_first_sort", False))
        self.precise_match_mode = bool(scoring.get("precise_match_mode", False))
        self.keyword_bypass = bool(scoring.get("keyword_bypass", False))
        self.token_exact_match = bool(scoring.get("token_exact_match", True))  # default ON
        _env_warmth = os.environ.get("OMBRE_SCORING_WARMTH_BOOST")
        self.w_warmth = float(_env_warmth) if _env_warmth is not None else float(scoring.get("warmth_boost", 0.0))

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine

        # --- Hit stats & search tracing / 命中统计 & 检索追溯 ---
        self._hit_stats_path = os.path.join(self.base_dir, "hit_stats.json")
        self._hit_stats: dict = {}      # {bucket_id: {count, last_hit_iso, last_query, surface_count}}
        self._total_searches = 0
        self._hit_dirty = 0
        self._recent_searches = deque(maxlen=20)
        self._load_hit_stats()
        atexit.register(self._flush_hit_stats, True)

    # Runtime-tunable scoring keys whitelist (for /api/scoring-config)
    SCORING_OVERRIDE_DEFAULTS = {
        "content_weight": 1.0,
        "title_hit_bonus": 0.0,
        "keyword_first_sort": False,
        "keyword_bypass": False,
        "token_exact_match": True,
        "dryrun_log": False,
        "precise_match_mode": False,
        "warmth_boost": 0.0,
    }

    def apply_runtime_scoring_overrides(self, overrides: dict) -> None:
        """Apply runtime scoring overrides to this instance (in-place).
        启动 + POST /api/scoring-config 后调，立刻生效。"""
        if not isinstance(overrides, dict):
            return
        for key in self.SCORING_OVERRIDE_DEFAULTS:
            if key not in overrides:
                continue
            val = overrides[key]
            try:
                if key in ("content_weight", "title_hit_bonus", "warmth_boost"):
                    setattr(self, key if key != "warmth_boost" else "w_warmth", max(0.0, float(val)))
                elif key in ("keyword_first_sort", "keyword_bypass", "token_exact_match", "dryrun_log", "precise_match_mode"):
                    setattr(self, key, bool(val))
            except (TypeError, ValueError):
                pass
        logger.info(
            f"[scoring] overrides: cw={self.content_weight} bonus={self.title_hit_bonus} "
            f"kw1={self.keyword_first_sort} precise={self.precise_match_mode} warm={self.w_warmth}"
        )

    def current_scoring_overrides(self) -> dict:
        """Return current scoring knob values (for GET /api/scoring-config)."""
        return {
            "content_weight": self.content_weight,
            "title_hit_bonus": self.title_hit_bonus,
            "keyword_first_sort": self.keyword_first_sort,
            "keyword_bypass": self.keyword_bypass,
            "token_exact_match": self.token_exact_match,
            "dryrun_log": self.dryrun_log,
            "precise_match_mode": self.precise_match_mode,
            "warmth_boost": self.w_warmth,
        }

    # ---------------------------------------------------------
    # Jieba tokenizer — Chinese-friendly query splitting
    # 中文分词 — 解决长句无空格拆分问题
    # ---------------------------------------------------------
    _TOKEN_SPLIT_RE = None   # lazy compile

    _BUILTIN_STOPWORDS = frozenset([
        "什么", "怎么", "为什么", "怎样", "如何",
        "可以", "应该", "想要", "需要",
        "一下", "一点", "一些", "已经", "还有",
        "你的", "我的", "他的", "她的", "我们", "你们",
        "你还", "还记", "记得吗",
        "现在", "当前", "测试", "调用",
    ])

    @classmethod
    def _split_query_tokens(cls, query: str) -> list:
        """Split Chinese query into keyword tokens using jieba."""
        import re
        if cls._TOKEN_SPLIT_RE is None:
            cls._TOKEN_SPLIT_RE = re.compile(
                r'[\s,。!?:;、《》「」\"\'“”‘’()()【】\[\]<>\.\!\?\:;,/\\\|·~`@#$%^&*+=_-]+'
            )
        raw = cls._TOKEN_SPLIT_RE.split(query or "")
        tokens = set()
        for t in raw:
            if not t:
                continue
            if len(t) <= 3:
                if 2 <= len(t) <= 12:
                    tokens.add(t)
                continue
            # Long token: use jieba to split
            for w in jieba.lcut(t):
                if 2 <= len(w) <= 12:
                    tokens.add(w)
        # Filter stopwords, keep order
        out = [t for t in raw if t and 2 <= len(t) <= 12 and t not in cls._BUILTIN_STOPWORDS]
        # Also add jieba tokens
        for tok in sorted(tokens, key=len, reverse=True):
            if tok not in cls._BUILTIN_STOPWORDS and tok not in out:
                if 2 <= len(tok) <= 12:
                    out.append(tok)
        return out

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: list[str] = None,
        importance: int = 5,
        domain: list[str] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: str = None,
        pinned: bool = False,
        protected: bool = False,
        wish: bool = False,
        todo: str = "",
        todo_done: bool = False,
        author: str = "",
        locked: bool = False,
        unlock_hint: str = "",
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。

        bucket_type="journal": 完全独立通道，不进入 list_all()/breath()/search()，
        只能通过 list_journal() 读取。author 区分作者(言之/小羊/共同)，
        locked+unlock_hint 支持上锁(日期或密码)。
        wish=True: 长期悬念标签，不受 max_results 常规限制，低概率随机浮现。
        todo/todo_done: 附着在桶上的待办，不单独成桶。
        """
        bucket_id = generate_bucket_id()
        bucket_name = sanitize_name(name) if name else bucket_id
        # feel/journal buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type in ("feel", "journal"):
            domain = domain if domain is not None else []
        else:
            domain = domain or ["未分类"]
        tags = tags or []
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = 10

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": max(0.0, min(1.0, valence)),
            "arousal": max(0.0, min(1.0, arousal)),
            "importance": max(1, min(10, importance)),
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "activation_count": 0,
        }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if wish:
            metadata["wish"] = True
        if todo:
            metadata["todo"] = todo
            metadata["todo_done"] = bool(todo_done)
        if bucket_type == "journal":
            if author:
                metadata["author"] = author
            if locked:
                metadata["locked"] = True
                metadata["unlock_hint"] = unlock_hint

        # --- Assemble Markdown file (frontmatter + body) ---
        # --- 组装 Markdown 文件 ---
        post = frontmatter.Post(linked_content, **metadata)

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
            if pinned and bucket_type != "permanent":
                metadata["type"] = "permanent"
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        elif bucket_type == "journal":
            type_dir = self.journal_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        elif bucket_type == "journal":
            primary_domain = author or "共同"  # journal subfolder: 按作者分
        else:
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        if bucket_name and bucket_name != bucket_id:
            filename = f"{bucket_name}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
            raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: list[str] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return new_path

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        # --- Noise marking: save/restore importance_before_noise ---
        # --- 噪声标记：保存/恢复 importance_before_noise ---
        was_noise = bool(post.get("resolved", False) and post.get("importance") == 1)
        new_resolved = kwargs.get("resolved")
        new_importance = kwargs.get("importance")

        # Detect noise state transition
        # 检测噪声态变化
        marking_noise = (new_resolved is True and new_importance == 1
                         and not was_noise)
        unmarking_noise = ((new_resolved == 0 or new_resolved is False)
                           and was_noise
                           and kwargs.get("importance") is None)

        if marking_noise:
            # Save current importance before marking noise
            # 保存当前 importance 值
            current_imp = post.get("importance", 5)
            if "importance_before_noise" not in post:
                post["importance_before_noise"] = current_imp

        if unmarking_noise:
            # Restore importance from backup when un-noising
            # 取消噪声时恢复原始 importance
            saved_imp = post.get("importance_before_noise")
            if saved_imp is not None:
                kwargs["importance"] = int(saved_imp)
            if "importance_before_noise" in post:
                del post["importance_before_noise"]

        # --- Pinned/protected buckets: lock importance to 10, ignore importance changes ---
        # --- 钉选/保护桶：importance 不可修改，强制保持 10 ---
        is_pinned = post.get("pinned", False) or post.get("protected", False)
        if is_pinned:
            kwargs.pop("importance", None)  # silently ignore importance update

        # --- Update only fields that were passed in / 只改传入的字段 ---
        if "content" in kwargs:
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "importance" in kwargs:
            post["importance"] = max(1, min(10, int(kwargs["importance"])))
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = max(0.0, min(1.0, float(kwargs["valence"])))
        if "arousal" in kwargs:
            post["arousal"] = max(0.0, min(1.0, float(kwargs["arousal"])))
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = bool(kwargs["resolved"])
        if "pinned" in kwargs:
            post["pinned"] = bool(kwargs["pinned"])
            if kwargs["pinned"]:
                post["importance"] = 10  # pinned → lock importance to 10
        if "digested" in kwargs:
            post["digested"] = bool(kwargs["digested"])
        if "model_valence" in kwargs:
            post["model_valence"] = max(0.0, min(1.0, float(kwargs["model_valence"])))
        if "wish" in kwargs:
            post["wish"] = bool(kwargs["wish"])
        if "todo" in kwargs:
            post["todo"] = kwargs["todo"]
        if "todo_done" in kwargs:
            post["todo_done"] = bool(kwargs["todo_done"])
        if "author" in kwargs:
            post["author"] = kwargs["author"]
        if "locked" in kwargs:
            post["locked"] = bool(kwargs["locked"])
        if "unlock_hint" in kwargs:
            post["unlock_hint"] = kwargs["unlock_hint"]
        if "related" in kwargs:
            post["related"] = kwargs["related"]

        # --- Auto-refresh activation time / 自动刷新激活时间 ---
        # 修改：内容变更也不会刷新激活时间
        # if "content" in kwargs:
        #    post["last_active"] = now_iso()

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
            return False

        # --- Auto-move: pinned → permanent/ ---
        # --- 自动移动：钉选 → permanent/ ---
        # NOTE: resolved buckets are NOT auto-archived here.
        # They stay in dynamic/ and decay naturally until score < threshold.
        # 注意：resolved 桶不在此自动归档，留在 dynamic/ 随衰减引擎自然归档。
        domain = post.get("domain", ["未分类"])
        if kwargs.get("pinned") and post.get("type") != "permanent":
            post["type"] = "permanent"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            self._move_bucket(file_path, self.permanent_dir, domain)

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")
        return True

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Soft-delete a memory bucket: move to trash_dir.
        软删除指定记忆桶：移到回收站目录。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            post = frontmatter.load(file_path)
        except Exception:
            return False

        current_type = post.get("type", "dynamic")
        domain = post.get("domain", [])

        # Save original type and timestamp
        post["original_type"] = current_type
        post["trashed_at"] = now_iso()

        # Determine trash subdir
        trash_sub = os.path.join(self.trash_dir, domain[0]) if domain else self.trash_dir
        os.makedirs(trash_sub, exist_ok=True)

        dest = os.path.join(trash_sub, os.path.basename(file_path))
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            shutil.move(file_path, dest)
        except OSError as e:
            logger.error(f"Failed to soft-delete bucket / 软删除失败: {file_path}: {e}")
            return False

        # Clean up empty original directory
        orig_dir = os.path.dirname(file_path)
        try:
            if os.path.isdir(orig_dir) and not os.listdir(orig_dir):
                os.rmdir(orig_dir)
        except Exception:
            pass

        logger.info(f"Soft-deleted bucket / 软删除: {bucket_id} → trash")
        return True

    async def restore(self, bucket_id: str) -> bool:
        """Restore from trash to original type directory."""
        # Search trash dir
        file_path = None
        for root, _, files in os.walk(self.trash_dir):
            for f in files:
                if f == f"{bucket_id}.md" or f.startswith(f"{bucket_id}."):
                    file_path = os.path.join(root, f)
                    break
            if file_path:
                break

        if not file_path:
            return False

        try:
            post = frontmatter.load(file_path)
        except Exception:
            return False

        original_type = post.get("original_type", "dynamic")
        domain = post.get("domain", [])
        post.pop("original_type", None)
        post.pop("trashed_at", None)

        # Determine target dir
        type_dir_map = {
            "permanent": self.permanent_dir,
            "dynamic": self.dynamic_dir,
            "archive": self.archive_dir,
            "feel": self.feel_dir,
        }
        target_base = type_dir_map.get(original_type, self.dynamic_dir)
        target_dir = os.path.join(target_base, domain[0]) if domain else target_base
        os.makedirs(target_dir, exist_ok=True)

        dest = os.path.join(target_dir, os.path.basename(file_path))
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            shutil.move(file_path, dest)
        except OSError as e:
            logger.error(f"Failed to restore / 恢复失败: {e}")
            return False

        logger.info(f"Restored bucket / 恢复: {bucket_id}")
        return True

    async def purge(self, bucket_id: str) -> bool:
        """Permanently delete from trash (physical remove)."""
        for root, _, files in os.walk(self.trash_dir):
            for f in files:
                if f == f"{bucket_id}.md" or f.startswith(f"{bucket_id}."):
                    file_path = os.path.join(root, f)
                    try:
                        os.remove(file_path)
                    except OSError as e:
                        logger.error(f"Purge failed / 彻底删除失败: {e}")
                        return False
                    logger.info(f"Purged bucket / 彻底删除: {bucket_id}")
                    return True
        return False

    async def empty_trash(self) -> int:
        """Delete all files in trash. Returns count."""
        count = 0
        try:
            for root, _, files in os.walk(self.trash_dir):
                for f in files:
                    if f.endswith(".md"):
                        try:
                            os.remove(os.path.join(root, f))
                            count += 1
                        except OSError:
                            pass
        except Exception:
            pass
        logger.info(f"Emptied trash / 清空回收站: {count} files")
        return count

    async def list_trash(self) -> list[dict]:
        """List all trashed buckets."""
        result = []
        try:
            for root, _, files in os.walk(self.trash_dir):
                for f in files:
                    if f.endswith(".md"):
                        file_path = os.path.join(root, f)
                        try:
                            post = frontmatter.load(file_path)
                            meta = dict(post.metadata)
                            result.append({
                                "id": meta.get("id", f.rsplit(".", 1)[0]),
                                "metadata": meta,
                                "content": post.content,
                                "path": file_path,
                            })
                        except Exception:
                            pass
        except Exception:
            pass
        result.sort(key=lambda b: b["metadata"].get("trashed_at", ""), reverse=True)
        return result

    # ---------------------------------------------------------
    # Convert an existing bucket into a journal entry
    # 把已有桶(dynamic/permanent/feel/archive)转成日记桶
    # journal 是独立通道，不进 list_all()/search()，转换后这个桶就脱离
    # update()/delete() 等常规接口的查找范围(_find_bucket_file 不搜 journal_dir)，
    # 只能通过 journal 专属接口编辑——这是有意为之，跟 create() 里 journal 的隔离设计一致。
    # ---------------------------------------------------------
    async def convert_to_journal(self, bucket_id: str, author: str = "共同", locked: bool = False, unlock_hint: str = "") -> bool:
        """把一个已有桶转为日记桶：物理移动文件到 journal_dir/<author>/ 下。"""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for journal conversion / 加载桶失败: {file_path}: {e}")
            return False

        post["type"] = "journal"
        post["author"] = author or "共同"
        post["domain"] = []
        post["locked"] = bool(locked)
        post["unlock_hint"] = unlock_hint if locked else ""

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write journal conversion / 写入转换失败: {file_path}: {e}")
            return False

        self._move_bucket(file_path, self.journal_dir, [author or "共同"])
        logger.info(f"Converted bucket to journal / 桶转为日记: {bucket_id}")
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str) -> None:
        """
        Update a bucket's last activation time and count.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = post.get("activation_count", 0) + 1

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # --- Time ripple: boost nearby memories within ±48h ---
            # --- 时间涟漪：±48小时内的记忆轻微唤醒 ---
            current_time = datetime.fromisoformat(str(post.get("created", post.get("last_active", ""))))
            await self._time_ripple(bucket_id, current_time)
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")

    async def soft_touch(self, bucket_id: str) -> None:
        """Search hit: refresh last_active + small activation bump, no ripple."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return
        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()                          # 更新时间
            current_count = post.get("activation_count", 1)
            post["activation_count"] = round(current_count + 0.3, 1)  # 轻量 +0.3
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except Exception as e:
            logger.warning(f"soft_touch failed: {bucket_id}: {e}")

    async def mark_surfaced(self, bucket_id: str) -> None:
        """
        Stamp a bucket as just surfaced via breath() weight-pool surfacing.
        Used by dream() to skip buckets already shown in the same open-window
        sequence, without touching last_active/activation_count (so decay
        scoring is unaffected — breath surfacing is deliberately not a "touch").
        标记一个桶刚被 breath() 浮现过。用于 dream() 去重——同一次开窗序列里
        breath 已经浮现的桶，dream 不再重复返回全文。不动 last_active/
        activation_count，不影响衰减打分（浮现本身不算"触碰"）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return
        try:
            post = frontmatter.load(file_path)
            post["last_breath_surfaced"] = now_iso()
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
        except Exception as e:
            logger.warning(f"mark_surfaced failed: {bucket_id}: {e}")
    
    async def _time_ripple(self, source_id: str, reference_time: datetime, hours: float = 48.0) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        try:
            all_buckets = await self.list_all(include_archive=False)
        except Exception:
            return

        rippled = 0
        max_ripple = 5
        for bucket in all_buckets:
            if rippled >= max_ripple:
                break
            if bucket["id"] == source_id:
                continue
            meta = bucket.get("metadata", {})
            # Skip pinned/permanent/feel
            if meta.get("pinned") or meta.get("protected") or meta.get("type") in ("permanent", "feel"):
                continue

            created_str = meta.get("created", meta.get("last_active", ""))
            try:
                created = datetime.fromisoformat(str(created_str))
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue

            if delta_hours <= hours:
                # Boost activation_count by 0.3 (fractional), don't change last_active
                file_path = self._find_bucket_file(bucket["id"])
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                    current_count = post.get("activation_count", 1)
                    # Store as float for fractional increments; calculate_score handles it
                    post["activation_count"] = round(current_count + 0.3, 1)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                    rippled += 1
                except Exception:
                    continue

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: int = None,
        domain_filter: list[str] = None,
        query_valence: float = None,
        query_arousal: float = None,
        include_archive: bool = False,
        show_all: bool = False,   # 新增：为True时不过滤/惩罚resolved桶
        include_noise: bool = False,  # 新增：为True时才包含噪声桶
        record_stats: bool = True,    # 新增：是否记录命中统计
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

    # --- Bucket ID direct lookup / 桶ID直接读取，跳过语义搜索 ---
        if re.fullmatch(r"[0-9a-f]{12}", query.strip()):
            bucket = await self.get(query.strip())
            if bucket:
                bucket["score"] = 100.0
                return [bucket]
            return []

        limit = limit or self.max_results
        all_buckets = await self.list_all(include_archive=include_archive)

        if not all_buckets:
            return []

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        # --- Layer 1.5: embedding pre-filter (optional, reduces multi-dim ranking set) ---
        # --- 第1.5层：embedding 预筛（可选，缩小精排候选集）---
        if self.embedding_engine and self.embedding_engine.enabled:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=150)
                if vector_results:
                    vector_ids = {bid for bid, _ in vector_results}
                    emb_candidates = [b for b in candidates if b["id"] in vector_ids]
                    if emb_candidates:
                        if include_archive:
                            no_emb = [b for b in candidates if b["id"] not in vector_ids]
                            candidates = emb_candidates + no_emb
                        else:
                            candidates = emb_candidates
            except Exception as e:
                logger.warning(f"Embedding pre-filter failed: {e}")

        # --- Layer 1.5: noise filtering (exclude noise buckets by default) ---
        # --- 第1.5层：噪声过滤（默认排除已标记噪声的桶）---
        if not include_noise:
            candidates = [
                b for b in candidates
                if not (b.get("metadata", {}).get("resolved", False)
                        and b.get("metadata", {}).get("importance") == 1)
            ]

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            # keyword bypass: token hits on name/domain skip threshold check
            # 关键词命中优先：命中了名字/域就强制通过（仅 keyword_bypass 开启时）
            keyword_matched = False
            title_hit = False
            if query and self.keyword_bypass:
                tokens = self._split_query_tokens(query)
                name_lower = meta.get("name", "").lower()
                domain_text = " ".join(meta.get("domain", [])).lower()
                for t in tokens:
                    if t.lower() in name_lower or t.lower() in domain_text:
                        keyword_matched = True
                        break
                # Check title (name) hit for bonus/sort
                if self.title_hit_bonus > 0 or self.keyword_first_sort:
                    for t in tokens:
                        if self.precise_match_mode:
                            if t.lower() in name_lower:
                                title_hit = True
                                break
                        elif fuzz.partial_ratio(t, name_lower) >= 50:
                            title_hit = True
                            break

            try:
                # Always compute fuzzy match for matched_in/field_scores display
                match = self._calc_topic_match(query, bucket)

                if self.precise_match_mode and query:
                    # ===============================================
                    # precise_match_mode: token exact only, cut 3D, bypass threshold
                    # ===============================================
                    tokens = self._split_query_tokens(query)
                    name_lower = meta.get("name", "").lower()
                    domain_text = " ".join(meta.get("domain", [])).lower()
                    tags_lower = " ".join(meta.get("tags", [])).lower()
                    content_lower = bucket.get("content", "").lower()
                    total_weight = 3 + 2.5 + 2 + self.content_weight
                    name_hits = sum(1 for t in tokens if t.lower() in name_lower)
                    domain_hits = sum(1 for t in tokens if t.lower() in domain_text)
                    tags_hits = sum(1 for t in tokens if t.lower() in tags_lower)
                    content_hits = sum(1 for t in tokens if t.lower() in content_lower)
                    raw_name = name_hits * 3
                    raw_domain = domain_hits * 2.5
                    raw_tags = tags_hits * 2
                    raw_content = content_hits * self.content_weight
                    topic_score = (raw_name + raw_domain + raw_tags + raw_content) / total_weight
                    normalized = topic_score * 100
                    if self.w_warmth > 0:
                        b_valence = float(meta.get("valence", 0.5))
                        warmth = max(0.0, b_valence - 0.5) * self.w_warmth
                        normalized += warmth * 10
                    emotion_score = 0; time_score = 0; importance_score = 0
                    passed_threshold = True  # precise mode: any hit passes
                    normalized = max(normalized, 1.0)
                    # Override matched_in/field_scores with token-exact results
                    match["field_scores"] = {
                        "name": 100.0 if name_hits > 0 else 0.0,
                        "domain": 100.0 if domain_hits > 0 else 0.0,
                        "tags": 100.0 if tags_hits > 0 else 0.0,
                        "content": 100.0 if content_hits > 0 else 0.0,
                    }
                    match["matched_in"] = [f for f, v in match["field_scores"].items() if v >= 50]

                else:
                    # ===============================================
                    # Normal 4D weighted scoring
                    # ===============================================
                    if self.token_exact_match and query:
                        # Use token exact substring matching for topic_score
                        tokens = self._split_query_tokens(query)
                        name_lower = meta.get("name", "").lower()
                        domain_text = " ".join(meta.get("domain", [])).lower()
                        tags_lower = " ".join(meta.get("tags", [])).lower()
                        content_lower = bucket.get("content", "").lower()
                        total_weight = 3 + 2.5 + 2 + self.content_weight
                        name_hits = sum(1 for t in tokens if t.lower() in name_lower)
                        domain_hits = sum(1 for t in tokens if t.lower() in domain_text)
                        tags_hits = sum(1 for t in tokens if t.lower() in tags_lower)
                        content_hits = sum(1 for t in tokens if t.lower() in content_lower)
                        raw_name = name_hits * 3
                        raw_domain = domain_hits * 2.5
                        raw_tags = tags_hits * 2
                        raw_content = content_hits * self.content_weight
                        topic_score = (raw_name + raw_domain + raw_tags + raw_content) / total_weight
                        # Override matched_in/field_scores with token-exact results
                        match["field_scores"] = {
                            "name": 100.0 if name_hits > 0 else 0.0,
                            "domain": 100.0 if domain_hits > 0 else 0.0,
                            "tags": 100.0 if tags_hits > 0 else 0.0,
                            "content": 100.0 if content_hits > 0 else 0.0,
                        }
                        match["matched_in"] = [f for f, v in match["field_scores"].items() if v >= 50]
                    else:
                        # Default: fuzzy partial_ratio
                        topic_score = match["score"]

                    emotion_score = self._calc_emotion_score(query_valence, query_arousal, meta)
                    time_score = self._calc_time_score(meta)
                    importance_score = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                    total = (
                        topic_score * self.w_topic
                        + emotion_score * self.w_emotion
                        + time_score * self.w_time
                        + importance_score * self.w_importance
                    )
                    if self.w_warmth > 0:
                        b_valence = float(meta.get("valence", 0.5))
                        total += max(0.0, b_valence - 0.5) * self.w_warmth

                    weight_sum = self.w_topic + self.w_emotion + self.w_time + self.w_importance
                    normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                    # Title hit bonus
                    if title_hit and self.title_hit_bonus > 0:
                        normalized += self.title_hit_bonus

                    # Threshold
                    passed_threshold = normalized >= self.fuzzy_threshold
                    if keyword_matched and not passed_threshold:
                        passed_threshold = True
                        normalized = max(normalized, self.fuzzy_threshold * 0.7)

                if not show_all and not passed_threshold:
                    continue
                # show_all mode: at least one field genuinely matched (>=50% fuzzy)
                if show_all:
                    if len(match.get("matched_in", [])) == 0:
                        continue
                if passed_threshold or show_all:
                    bucket["score"] = round(normalized, 2)
                    bucket["matched_in"] = match.get("matched_in", [])
                    bucket["field_scores"] = match.get("field_scores", {})
                    if title_hit:
                        bucket["_title_hit"] = True
                    scored.append(bucket)

            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        # Keyword-first sort: title hits always sort above non-title hits
        if self.keyword_first_sort or (self.title_hit_bonus > 0):
            scored.sort(key=lambda x: (
                x.get("_title_hit", False),
                x["score"]
            ), reverse=True)
        else:
            scored.sort(key=lambda x: x["score"], reverse=True)
        result = scored[:limit]

        # --- Record hit stats for search tracing / 记录命中统计 ---
        if record_stats:
            self.record_hit(query, result)

        return result

    # ---------------------------------------------------------
    # Hit stats & search tracing / 命中统计 & 检索追溯
    # ---------------------------------------------------------
    def _load_hit_stats(self):
        """Load hit stats from disk, if file exists."""
        try:
            if os.path.exists(self._hit_stats_path):
                with open(self._hit_stats_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._hit_stats = data.get("buckets", {})
                self._total_searches = data.get("total_searches", 0)
        except Exception:
            pass

    def _flush_hit_stats(self, force=False):
        """Persist hit stats to disk. Debounced: writes every 10 dirty or forced."""
        if not force:
            self._hit_dirty += 1
            if self._hit_dirty < 10:
                return
        try:
            data = {"total_searches": self._total_searches, "buckets": self._hit_stats}
            tmp = self._hit_stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._hit_stats_path)
            self._hit_dirty = 0
        except Exception:
            pass

    def record_hit(self, query, scored_buckets):
        """Record keyword search hit stats for each result bucket."""
        from utils import now_iso as _now_iso
        self._total_searches += 1
        ts = _now_iso()
        # Record per-bucket hits (exclude feel type for privacy)
        hit_entries = []
        for b in scored_buckets[:20]:
            meta = b.get("metadata", {})
            if meta.get("type") == "feel":
                continue
            bid = b["id"]
            entry = self._hit_stats.setdefault(bid, {"count": 0, "surface_count": 0, "name": meta.get("name", bid)})
            entry["count"] += 1
            entry["last_hit_iso"] = ts
            entry["last_query"] = query[:200]
            entry["name"] = meta.get("name", bid)
            hit_entries.append({
                "id": bid, "name": meta.get("name", bid),
                "score": b.get("score", 0), "domain": meta.get("domain", []),
            })
        # Record search trace
        self._recent_searches.appendleft({
            "kind": "search", "query": query[:200], "time_iso": ts,
            "count": len(scored_buckets), "top": hit_entries[:10],
        })
        self._flush_hit_stats()

    def record_surface_trace(self, items):
        """Record breath surface (no-query) event trace."""
        from utils import now_iso as _now_iso
        ts = _now_iso()
        surface_entries = []
        for b in items[:10]:
            meta = b.get("metadata", {})
            bid = b["id"]
            entry = self._hit_stats.setdefault(bid, {"count": 0, "surface_count": 0})
            entry["surface_count"] += 1
            surface_entries.append({
                "id": bid, "name": meta.get("name", bid),
                "importance": meta.get("importance"), "pinned": meta.get("pinned"),
            })
        self._recent_searches.appendleft({
            "kind": "surface", "query": None, "time_iso": ts,
            "count": len(items), "top": surface_entries,
        })
        self._flush_hit_stats()

    def get_hit_stats(self, limit=50, include_zero=False, order="desc", exclude_gated=True):
        """Return hit statistics for all tracked buckets.
        exclude_gated: exclude feel-type and noise buckets.
        """
        from utils import now_iso as _now_iso
        items = []
        for bid, entry in self._hit_stats.items():
            cc = entry.get("count", 0)
            if not include_zero and cc == 0:
                continue
            items.append({
                "id": bid,
                "name": entry.get("name", bid),  # include name for display
                "count": cc,
                "surface_count": entry.get("surface_count", 0),
                "last_hit_iso": entry.get("last_hit_iso", ""),
                "last_query": entry.get("last_query", ""),
            })
        reverse = order != "asc"
        items.sort(key=lambda x: x["count"], reverse=reverse)
        return {
            "total_searches": self._total_searches,
            "tracked_buckets": len(self._hit_stats),
            "items": items[:limit],
        }

    def reset_hit_stats(self):
        """Clear all hit stats in memory and on disk."""
        self._hit_stats = {}
        self._total_searches = 0
        self._recent_searches.clear()
        self._hit_dirty = 0
        try:
            if os.path.exists(self._hit_stats_path):
                os.remove(self._hit_stats_path)
        except Exception:
            pass

    def get_recent_searches(self, limit=20):
        """Return recent search/surface traces (newest first)."""
        return list(self._recent_searches)[:limit]

    # ---------------------------------------------------------
    # Topic relevance sub-score:
    # name(×3) + domain(×2.5) + tags(×2) + body(×1)
    # 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
    # ---------------------------------------------------------
    def _multi_word_ratio(self, words: list[str], text: str) -> float:
        """
        Multi-keyword fuzzy match: split query into words via jieba,
        score each word against text independently. Uses partial_ratio
        (substring match) by default; switches to exact substring check
        in precise_match_mode.
        """
        if not text:
            return 0.0
        # Use jieba tokens when available (more than whitespace split)
        tokens = self._split_query_tokens(" ".join(words))
        if not tokens:
            return 0.0
        text_lower = text.lower()
        if self.precise_match_mode:
            # Exact substring check: token must appear as-is in text
            hits = sum(1 for w in tokens if w.lower() in text_lower)
            return (hits / len(tokens)) * 100.0
        else:
            return sum(fuzz.partial_ratio(w, text) for w in tokens) / len(tokens)

    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        """
        Calculate text dimension relevance score (0~1).
        Delegates to _calc_topic_match for the weighted sum.
        计算文本维度的相关性得分。委托 _calc_topic_match。
        """
        return self._calc_topic_match(query, bucket)["score"]

    def _calc_topic_match(self, query: str, bucket: dict,
                           match_threshold: float = 50.0) -> dict:
        """
        Calculate per-field fuzzy match details.
        Returns {score, field_scores, matched_in}.
        score: normalized weighted sum (identical to _calc_topic_score).
        field_scores: raw rapidfuzz partial_ratio per field (0-100).
        matched_in: list of field names whose raw ratio >= match_threshold.
        计算逐字段匹配详情。返回综合分 + 各字段原始 fuzzy 分 + 命中字段。
        """
        meta = bucket.get("metadata", {})
        words = [w for w in query.split() if w] or [query]
        weight_sum = 3 + 2.5 + 2 + self.content_weight

        raw_name = self._multi_word_ratio(words, meta.get("name", ""))
        raw_domain = max(
            (self._multi_word_ratio(words, d) for d in meta.get("domain", [])),
            default=0,
        )
        raw_tags = max(
            (self._multi_word_ratio(words, tag) for tag in meta.get("tags", [])),
            default=0,
        )
        raw_content = self._multi_word_ratio(words, bucket.get("content", "")[:3000])

        weighted = (
            raw_name * 3 + raw_domain * 2.5
            + raw_tags * 2 + raw_content * self.content_weight
        )
        score = weighted / (100 * weight_sum)

        field_scores = {
            "name": round(raw_name, 1),
            "domain": round(raw_domain, 1),
            "tags": round(raw_tags, 1),
            "content": round(raw_content, 1),
        }
        matched_in = [
            k for k, v in field_scores.items() if v >= match_threshold
        ]

        return {
            "score": score,
            "field_scores": field_scores,
            "matched_in": matched_in,
        }

    # ---------------------------------------------------------
    # Emotion resonance sub-score:
    # Based on Russell circumplex Euclidean distance
    # 情感共鸣子分：基于环形情感模型的欧氏距离
    # No emotion in query → neutral 0.5 (doesn't affect ranking)
    # ---------------------------------------------------------
    def _calc_emotion_score(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score (0~1, closer = higher).
        计算情感共鸣度（0~1，越近越高）。
        """
        if q_valence is None or q_arousal is None:
            return 0.5  # No emotion coordinates → neutral / 无情感坐标时给中性分

        try:
            b_valence = float(meta.get("valence", 0.5))
            b_arousal = float(meta.get("arousal", 0.3))
        except (ValueError, TypeError):
            return 0.5

        # Euclidean distance, max sqrt(2) ≈ 1.414
        dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
        return max(0.0, 1.0 - dist / 1.414)

    # ---------------------------------------------------------
    # Time proximity sub-score:
    # More recent activation → higher score
    # 时间亲近子分：距上次激活越近分越高
    # ---------------------------------------------------------
    def _calc_time_score(self, meta: dict) -> float:
        """
        Calculate time proximity score (0~1, more recent = higher).
        计算时间亲近度。
        """
        last_active_str = meta.get("last_active", meta.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            days = max(0.0, (datetime.now() - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        buckets = []

        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            dirs.append(self.archive_dir)

        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)

        return buckets

    async def list_journal(self) -> list[dict]:
        """
        Read journal entries only. 完全独立通道——journal_dir 不在 list_all()
        的扫描目录里，所以日记不会泄漏进普通 breath/search/dream。
        只有 breath(domain="journal") 走这个方法。
        """
        buckets = []
        if not os.path.exists(self.journal_dir):
            return buckets
        for root, _, files in os.walk(self.journal_dir):
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                file_path = os.path.join(root, filename)
                bucket = self._load_bucket(file_path)
                if bucket:
                    buckets.append(bucket)
        return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type / 一次性读取
            post = frontmatter.load(file_path)
            domain = post.get("domain", ["未分类"])
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))

            # Update type marker then move file / 更新类型标记后移动文件
            post["type"] = "archived"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # Use shutil.move for cross-filesystem safety
            # 使用 shutil.move 保证跨文件系统安全
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        return True

    async def unarchive(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path or self.archive_dir not in str(file_path):
            return False
        try:
            post = frontmatter.load(file_path)
            domain = post.get("domain", ["未分类"])
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
            dynamic_subdir = os.path.join(self.dynamic_dir, primary_domain)
            os.makedirs(dynamic_subdir, exist_ok=True)
            dest = safe_path(dynamic_subdir, os.path.basename(file_path))
            post["type"] = "dynamic"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(f"Failed to unarchive bucket: {bucket_id}: {e}")
            return False
        return True

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    # Match by exact ID segment in filename
                    # 通过文件名中的 ID 片段精确匹配
                    name_part = fname[:-3]  # remove .md
                    if name_part == bucket_id or name_part.endswith(f"_{bucket_id}"):
                        return os.path.join(root, fname)
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": dict(post.metadata),
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None