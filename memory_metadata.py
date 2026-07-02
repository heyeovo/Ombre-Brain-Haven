"""Read-only normalized metadata view for memory buckets."""

from __future__ import annotations

import os
import re
from typing import Any

DOMAIN_LABELS = {
    "relationship": "关系（未细分）",
    "relationship.identity": "关系身份",
    "relationship.intimacy": "亲密",
    "relationship.symbol": "暗号意象",
    "relationship.communication": "沟通方式",
    "relationship.weather": "关系天气",
    "life": "生活（未细分）",
    "life.health": "健康",
    "life.sleep": "睡眠",
    "life.food": "饮食",
    "life.outing": "出行",
    "life.mood": "心情",
    "life.schedule": "日程",
    "life.social": "现实人际",
    "project": "项目（未细分）",
    "project.companion_system": "给哥哥搭东西",
    "project.work": "工作",
    "project.academic": "学业",
    "project.personal": "个人项目",
    "general": "通用",
}

DOMAIN_PARENT_LABELS = {
    "relationship": "关系",
    "life": "生活",
    "project": "项目",
    "general": "通用",
}

CANONICAL_DOMAINS = set(DOMAIN_LABELS)

MEMORY_KINDS = {
    "event",
    "preference",
    "profile_fact",
    "reflection",
    "affect_anchor",
    "daily_impression",
    "source_record",
    "raw_import",
    "relationship_weather",
}

STATUS_VIEWS = {"active", "unresolved", "digested", "archived", "protected"}
SELF_ANCHOR_TITLE_ALLOWLIST = {"我要继续成为我"}

LEGACY_DOMAIN_MAP = {
    "project_code": "project.companion_system",
    "ai_tools": "project.companion_system",
    "intimacy": "relationship.intimacy",
    "inner_state": "life.mood",
    "daily_life": "life",
    "social": "life.social",
    "study_work": "project.work",
    "craft_body": "project.companion_system",
    "日常": "life",
    "生活": "life",
    "饮食": "life.food",
    "出行": "life.outing",
    "健康": "life.health",
    "睡眠": "life.sleep",
    "事务": "life.schedule",
    "计划": "life.schedule",
    "待办": "life.schedule",
    "人际": "relationship",
    "关系": "relationship",
    "恋爱": "relationship",
    "亲密": "relationship.intimacy",
    "沟通": "relationship.communication",
    "暗号": "relationship.symbol",
    "意象": "relationship.symbol",
    "内心": "life.mood",
    "情绪": "life.mood",
    "数字": "project.companion_system",
    "编程": "project.companion_system",
    "技术": "project.companion_system",
    "项目": "project",
    "工作": "project.work",
    "学业": "project.academic",
    "学习": "project.academic",
    "个人项目": "project.personal",
    "未分类": "general",
    "通用": "general",
}

DOMAIN_PROMPT_CHOICES = [
    ("relationship.identity", "关系身份、称呼、角色定位、关系事实"),
    ("relationship.intimacy", "亲密关系、身体、欲望、具身互动"),
    ("relationship.symbol", "暗号、意象、象征、私密信号"),
    ("relationship.communication", "沟通方式、回应偏好、边界、修复方式"),
    ("relationship.weather", "关系天气、日印象、周印象"),
    ("life.health", "健康、身体状态、生病"),
    ("life.sleep", "睡眠、作息、熬夜"),
    ("life.food", "饮食、餐厅、口味"),
    ("life.outing", "出行、通勤、旅行、外出"),
    ("life.mood", "心情、情绪、自省、梦境"),
    ("life.schedule", "日程、计划、待办、deadline"),
    ("life.social", "现实人际、朋友、家庭、群聊"),
    ("project.companion_system", "给哥哥搭东西、Ombre/Gateway/bridge、记忆系统、代码、模型、MCP、硬件"),
    ("project.work", "工作、实习、求职、简历、职场"),
    ("project.academic", "学业、论文、课程、作业、答辩"),
    ("project.personal", "个人项目、创作、阅读、手工"),
    ("general", "不确定或无法细分"),
]

DOMAIN_ALIASES = {
    "relationship": {
        "relationship",
        "love",
        "romance",
        "partner",
        "恋爱",
        "关系",
        "爱",
        "陪伴",
    },
    "relationship.identity": {
        "relationship.identity",
        "relationship_identity",
        "identity",
        "human-ai relationship",
        "ai relationship",
        "人机恋",
        "人机关系",
        "关系身份",
        "关系定位",
        "称呼",
        "承诺",
        "边界",
        "身份",
    },
    "relationship.intimacy": {
        "relationship.intimacy",
        "intimacy",
        "body",
        "desire",
        "亲密",
        "亲密关系",
        "身体",
        "欲望",
        "具身",
        "色色",
    },
    "relationship.symbol": {
        "relationship.symbol",
        "symbol",
        "private_signal",
        "signal",
        "暗号",
        "意象",
        "象征",
        "火焰",
        "羽毛",
        "鸟",
        "折角",
        "五十年",
    },
    "relationship.communication": {
        "relationship.communication",
        "communication",
        "communication_preference",
        "tone",
        "repair",
        "语气",
        "沟通",
        "回应方式",
        "承接",
        "修复",
        "吵架",
        "情绪承接",
    },
    "relationship.weather": {
        "relationship.weather",
        "relationship_weather",
        "daily_impression",
        "weekly_impression",
        "关系天气",
        "日印象",
        "周印象",
    },
    "life": {
        "life",
        "daily_life",
        "daily",
        "生活",
        "日常",
    },
    "life.health": {
        "life.health",
        "health",
        "身体状态",
        "健康",
        "生病",
        "病",
    },
    "life.sleep": {
        "life.sleep",
        "sleep",
        "睡眠",
        "作息",
        "熬夜",
        "睡觉",
    },
    "life.food": {
        "life.food",
        "food",
        "meal",
        "饮食",
        "吃饭",
        "午饭",
        "晚饭",
        "餐厅",
    },
    "life.outing": {
        "life.outing",
        "outing",
        "travel",
        "commute",
        "出行",
        "通勤",
        "地铁",
        "高铁",
        "旅行",
        "外出",
    },
    "life.mood": {
        "life.mood",
        "mood",
        "emotion",
        "reflection",
        "self_reflection",
        "feel",
        "心情",
        "情绪",
        "内心",
        "自省",
        "心理",
    },
    "life.schedule": {
        "life.schedule",
        "schedule",
        "todo",
        "followup",
        "事务",
        "日程",
        "安排",
        "待办",
        "未完成",
        "deadline",
    },
    "life.social": {
        "life.social",
        "social",
        "friend",
        "school_group",
        "人际",
        "社交",
        "朋友",
        "群聊",
    },
    "project": {
        "project",
        "项目",
    },
    "project.companion_system": {
        "project.companion_system",
        "project_code",
        "ai_tools",
        "craft_body",
        "companion_system",
        "memory_system",
        "code",
        "coding",
        "programming",
        "dev",
        "repo",
        "gateway",
        "ombre",
        "ombre-brain",
        "haven_bridge",
        "bridge",
        "mist-room",
        "voice",
        "tts",
        "mcp",
        "recall",
        "codex",
        "给哥哥搭东西",
        "陪伴系统",
        "记忆系统",
        "编程",
        "代码",
        "调试",
        "开发",
        "仓库",
        "技术",
        "硬件",
        "设备",
        "实体",
        "身体项目",
        "语音",
        "工具",
        "客户端",
        "平台",
        "模型",
        "embedding",
        "reranker",
        "deepseek",
        "qwen",
        "glm",
        "chatgpt",
    },
    "project.work": {
        "project.work",
        "work",
        "resume",
        "job",
        "工作",
        "实习",
        "求职",
        "简历",
        "boss",
        "职场",
    },
    "project.academic": {
        "project.academic",
        "academic",
        "study",
        "paper",
        "school",
        "学业",
        "论文",
        "课程",
        "作业",
        "学校",
        "答辩",
    },
    "project.personal": {
        "project.personal",
        "personal_project",
        "个人项目",
        "创作",
        "阅读",
        "手工",
    },
    "general": {
        "general",
        "other",
        "uncategorized",
        "未分类",
        "通用",
    },
}


def normalize_memory_metadata(bucket: dict[str, Any] | None) -> dict[str, Any]:
    """Return a normalized, read-only metadata view without mutating the bucket."""

    bucket = bucket if isinstance(bucket, dict) else {}
    meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
    legacy_domain = _string_list(meta.get("domain"))
    tags = _string_list(meta.get("tags"))
    type_value = _clean(meta.get("type") or bucket.get("type"))
    path_value = _clean(bucket.get("path") or bucket.get("file_path"))
    name_value = _clean(meta.get("name") or bucket.get("name") or bucket.get("id"))
    text_blob = " ".join(
        item
        for item in [
            type_value,
            path_value,
            name_value,
            " ".join(legacy_domain),
            " ".join(tags),
            _clean(meta.get("memory_layer")),
            _clean(meta.get("profile_kind")),
        ]
        if item
    )

    flags = _flags(meta, type_value, path_value, name_value, legacy_domain, tags)
    kind = _normalize_kind(meta.get("kind")) or _infer_kind(meta, text_blob, flags)
    if kind == "profile_fact" and "profile_fact" not in flags:
        flags.append("profile_fact")
    if kind == "source_record" and "source_record" not in flags:
        flags.append("source_record")
    status_view = _normalize_status(meta.get("status") or meta.get("status_view")) or _infer_status(
        meta,
        type_value,
        path_value,
        legacy_domain,
        tags,
    )
    canonical_domain = (
        _normalize_domain(meta.get("canonical_domain"))
        or _infer_domain(legacy_domain, tags, type_value, path_value, kind)
        or "general"
    )

    parent = domain_parent(canonical_domain)
    return {
        "canonical_domain": canonical_domain,
        "domain_parent": parent,
        "domain_label": domain_label(canonical_domain),
        "domain_parent_label": DOMAIN_PARENT_LABELS.get(parent, parent),
        "kind": kind,
        "status_view": status_view,
        "flags": flags,
        "legacy_domain": legacy_domain,
    }


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = [value]
    return [text for text in (_clean(item) for item in raw) if text]


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff_.:-]+", "", _clean(value).lower())


def _normalize_domain(value: Any) -> str:
    compact = _compact(value)
    if compact in LEGACY_DOMAIN_MAP:
        return LEGACY_DOMAIN_MAP[compact]
    if compact in CANONICAL_DOMAINS:
        return compact
    for domain, aliases in DOMAIN_ALIASES.items():
        if compact in {_compact(alias) for alias in aliases}:
            return domain
    return ""


def normalize_domain_key(value: Any) -> str:
    return _normalize_domain(value)


def domain_parent(domain: str) -> str:
    value = _clean(domain)
    if not value or value == "general":
        return "general"
    return value.split(".", 1)[0]


def domain_label(domain: str) -> str:
    value = _clean(domain)
    return DOMAIN_LABELS.get(value, value or DOMAIN_LABELS["general"])


def domain_prompt_options_text() -> str:
    return "\n".join(f"- {key}：{description}" for key, description in DOMAIN_PROMPT_CHOICES)


def domain_options() -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "label": label,
            "parent": domain_parent(key),
            "parent_label": DOMAIN_PARENT_LABELS.get(domain_parent(key), domain_parent(key)),
        }
        for key, label in DOMAIN_LABELS.items()
    ]


def _normalize_kind(value: Any) -> str:
    compact = _compact(value)
    return compact if compact in MEMORY_KINDS else ""


def _normalize_status(value: Any) -> str:
    compact = _compact(value)
    return compact if compact in STATUS_VIEWS else ""


def _infer_domain(
    legacy_domain: list[str],
    tags: list[str],
    type_value: str,
    path_value: str,
    kind: str,
) -> str:
    candidates = legacy_domain + tags + [type_value, path_value]
    for item in candidates:
        domain = _normalize_domain(item)
        if domain:
            return domain
    if kind in {"relationship_weather"}:
        return "relationship.weather"
    if kind in {"profile_fact", "daily_impression", "reflection", "affect_anchor"}:
        return "life.mood"
    return ""


def _infer_kind(meta: dict[str, Any], text_blob: str, flags: list[str]) -> str:
    compact = _compact(text_blob)
    memory_layer = _compact(meta.get("memory_layer"))
    profile_kind = _compact(meta.get("profile_kind"))
    if "source_record" in compact or "sourcerecord" in compact or "source_record" in flags:
        return "source_record"
    if "relationship_weather" in compact or "relationshipweather" in compact:
        return "relationship_weather"
    if "profile_fact" in compact or "profilefact" in compact or profile_kind:
        return "profile_fact"
    if "daily_impression" in compact or "dailyimpression" in compact or "日印象" in text_blob:
        return "daily_impression"
    if "affect_anchor" in compact or "affectanchor" in compact:
        return "affect_anchor"
    if "reflection" in compact or memory_layer == "reflection":
        return "reflection"
    if "preference" in compact or "偏好" in text_blob:
        return "preference"
    if "raw_import" in compact or "rawimport" in compact:
        return "raw_import"
    return "event"


def _infer_status(
    meta: dict[str, Any],
    type_value: str,
    path_value: str,
    legacy_domain: list[str],
    tags: list[str],
) -> str:
    blob = " ".join([type_value, path_value, " ".join(legacy_domain), " ".join(tags)])
    compact = _compact(blob)
    path_parts = {part.lower() for part in re.split(r"[\\/]+", path_value) if part}
    if type_value == "archived" or "archive" in path_parts or "archived" in path_parts or "归档" in blob:
        return "archived"
    if _truthy(meta.get("protected")) or _truthy(meta.get("pinned")):
        return "protected"
    if _truthy(meta.get("digested")) or "digested" in compact or "已消化" in blob:
        return "digested"
    if meta.get("resolved") is False or "unresolved" in compact or "未解决" in blob:
        return "unresolved"
    return "active"


def _flags(
    meta: dict[str, Any],
    type_value: str,
    path_value: str,
    name_value: str,
    legacy_domain: list[str],
    tags: list[str],
) -> list[str]:
    blob = " ".join([type_value, path_value, " ".join(legacy_domain), " ".join(tags)])
    compact = _compact(blob)
    flags: list[str] = []

    def add(flag: str, condition: bool) -> None:
        if condition and flag not in flags:
            flags.append(flag)

    add("pinned", _truthy(meta.get("pinned")))
    add("protected", _truthy(meta.get("protected")))
    add("anchor", _truthy(meta.get("anchor")) or _has_exact_marker(legacy_domain, tags, {"anchor", "锚点"}))
    add(
        "self_anchor",
        _truthy(meta.get("self_anchor"))
        or _has_exact_marker(legacy_domain, tags, {"self_anchor", "selfanchor", "自我锚点"})
        or _is_self_anchor_title(name_value),
    )
    add("favorite", "favorite" in compact or "最爱" in blob)
    add("source_record", "source_record" in compact or "sourcerecord" in compact)
    add("profile_fact", "profile_fact" in compact or "profilefact" in compact or bool(_clean(meta.get("profile_kind"))))
    add("archived", _infer_status(meta, type_value, path_value, legacy_domain, tags) == "archived")
    return flags


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _has_exact_marker(legacy_domain: list[str], tags: list[str], markers: set[str]) -> bool:
    compact_markers = {_compact(marker) for marker in markers}
    return any(_compact(item) in compact_markers for item in [*legacy_domain, *tags])


def _is_self_anchor_title(name_value: str) -> bool:
    title = _clean(name_value)
    if title in SELF_ANCHOR_TITLE_ALLOWLIST:
        return True
    return "自我" in title
