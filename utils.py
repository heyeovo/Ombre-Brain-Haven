# ============================================================
# Module: Common Utilities (utils.py)
# 模块：通用工具函数
#
# Provides config loading, logging init, path safety, ID generation, etc.
# 提供配置加载、日志初始化、路径安全校验、ID 生成等基础能力
#
# Depended on by: server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# 被谁依赖：server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# ============================================================

import os
import re
import uuid
import yaml
import logging
from pathlib import Path
from datetime import datetime


def load_config(config_path: str = None) -> dict:
    """
    Load configuration file.
    加载配置文件。

    Priority: environment variables > config.yaml > built-in defaults.
    优先级：环境变量 > config.yaml > 内置默认值。
    """
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- 内置默认配置（兜底，保证即使没有 config.yaml 也能跑）---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "buckets_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "auto_merge": False,
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
    }

    # --- Load user config from YAML file ---
    # --- 从 YAML 文件加载用户自定义配置 ---
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # --- Environment variable overrides (highest priority) ---
    # --- 环境变量覆盖敏感/运行时配置（优先级最高）---
    env_api_key = os.environ.get("OMBRE_API_KEY", "")
    if env_api_key:
        config.setdefault("dehydration", {})["api_key"] = env_api_key

    env_base_url = os.environ.get("OMBRE_BASE_URL", "")
    if env_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_base_url

    env_transport = os.environ.get("OMBRE_TRANSPORT", "")
    if env_transport:
        config["transport"] = env_transport

    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")
    if env_buckets_dir:
        config["buckets_dir"] = env_buckets_dir

    # OMBRE_DEHYDRATION_MODEL (with OMBRE_MODEL alias) overrides dehydration.model
    env_dehy_model = os.environ.get("OMBRE_DEHYDRATION_MODEL", "") or os.environ.get("OMBRE_MODEL", "")
    if env_dehy_model:
        config.setdefault("dehydration", {})["model"] = env_dehy_model

    # OMBRE_DEHYDRATION_BASE_URL overrides dehydration.base_url
    env_dehy_base_url = os.environ.get("OMBRE_DEHYDRATION_BASE_URL", "")
    if env_dehy_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_dehy_base_url

    # OMBRE_EMBEDDING_MODEL overrides embedding.model
    env_embed_model = os.environ.get("OMBRE_EMBEDDING_MODEL", "")
    if env_embed_model:
        config.setdefault("embedding", {})["model"] = env_embed_model

    # OMBRE_EMBEDDING_BASE_URL overrides embedding.base_url
    env_embed_base_url = os.environ.get("OMBRE_EMBEDDING_BASE_URL", "")
    if env_embed_base_url:
        config.setdefault("embedding", {})["base_url"] = env_embed_base_url

    # OMBRE_AUTO_MERGE overrides auto_merge (true/false)
    env_auto_merge = os.environ.get("OMBRE_AUTO_MERGE", "")
    if env_auto_merge.lower() in ("0", "false", "no"):
        config["auto_merge"] = False
    elif env_auto_merge.lower() in ("1", "true", "yes"):
        config["auto_merge"] = True

    # --- Ensure bucket storage directories exist ---
    # --- 确保记忆桶存储目录存在 ---
    buckets_dir = config["buckets_dir"]
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    深度合并两个字典，override 的值覆盖 base。
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def setup_logging(level: str = "INFO") -> None:
    """
    Initialize logging system.
    初始化日志系统。

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    注意：MCP stdio 模式下 stdout 被协议占用，日志只能走 stderr。
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler()],  # StreamHandler defaults to stderr
    )


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    生成唯一的记忆桶 ID（12 位短 UUID，方便人类阅读）。
    """
    return uuid.uuid4().hex[:12]


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] → word
    去除 Obsidian 双链括号
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    清洗桶名称，只保留安全字符。防止路径遍历攻击。
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    构造安全的文件路径，确保最终路径始终在 base_dir 内部。
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    粗略估算 token 数。

    Chinese ≈ 1 char = 1.5 tokens, English ≈ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    中文 ≈ 1字=1.5token，英文 ≈ 1词=1.3token。
    用于判断是否需要脱水压缩，不追求精确。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(chinese_chars * 1.5 + english_words * 1.3 + len(text) * 0.05)


def now_iso() -> str:
    """
    Return current time as ISO format string.
    返回当前时间的 ISO 格式字符串。
    """
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# LLM pricing — per 1M tokens (input, output) in USD
# LLM 定价 — 每百万 token (input, output) 美元
# Model matching: exact match first, then longest prefix match
# 模型匹配: 先精确, 失败后最长前缀匹配
# ============================================================
LLM_PRICING = {
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-sonnet-4":    (3.00, 15.00),
    "claude-sonnet":      (3.00, 15.00),
    "claude-haiku-4-5":   (1.00,  5.00),
    "claude-haiku":       (1.00,  5.00),
    "claude-opus":        (15.00, 75.00),
    "gemini-2.5-flash":   (0.075,  0.30),
    "gemini-2.0-flash":   (0.075,  0.30),
    "gemini-2.5-pro":     (1.25,  10.00),
    "gemini-1.5-flash":   (0.075,  0.30),
    "gemini-1.5-pro":     (1.25,  10.00),
    "deepseek-chat":      (0.14,   0.28),
    "deepseek-reasoner":  (0.55,   2.19),
    "qwen-max":           (0.40,   1.60),
    "qwen-plus":          (0.10,   0.30),
    "gpt-4.1":            (2.00,   8.00),
    "gpt-4o-mini":        (0.15,   0.60),
    "gpt-4o":             (2.50,  10.00),
}


def estimate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """Estimate LLM API cost in USD and CNY.
    估算 LLM API 调用费用（美元 + 人民币）。

    Returns {usd, cny, in_tokens, out_tokens, model_matched, known}
    known=False means model not in pricing table, cost will be 0.
    """
    if not model:
        return {"usd": 0.0, "cny": 0.0, "in_tokens": prompt_tokens or 0,
                "out_tokens": completion_tokens or 0, "model_matched": "", "known": False}
    m = model.lower()
    # Exact match first, then longest prefix match
    matched_key = None
    if m in LLM_PRICING:
        matched_key = m
    else:
        for k in sorted(LLM_PRICING.keys(), key=len, reverse=True):
            if m.startswith(k) or k in m:
                matched_key = k
                break
    if not matched_key:
        return {"usd": 0.0, "cny": 0.0, "in_tokens": prompt_tokens or 0,
                "out_tokens": completion_tokens or 0, "model_matched": "", "known": False}
    p_in, p_out = LLM_PRICING[matched_key]
    p_tok = max(0, int(prompt_tokens or 0))
    c_tok = max(0, int(completion_tokens or 0))
    usd = (p_tok / 1_000_000) * p_in + (c_tok / 1_000_000) * p_out
    return {
        "usd": round(usd, 6),
        "cny": round(usd * 7.2, 4),
        "in_tokens": p_tok,
        "out_tokens": c_tok,
        "model_matched": matched_key,
        "known": True,
    }
