from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


DEFAULT_FACET_ALIASES = {
    "relationship_identity": (
        "human-ai relationship",
        "human ai relationship",
        "ai relationship",
        "ai companion",
        "ai partner",
        "digital companion",
        "virtual partner",
        "relationship identity",
        "companion identity",
        "人机恋",
        "人机关系",
        "ai 伴侣",
        "ai伴侣",
        "人工智能伴侣",
        "虚拟恋人",
        "数字伴侣",
        "关系身份",
        "伴侣身份",
        "伴侣关系",
    ),
    "intimacy": (
        "intimacy",
        "intimate",
        "sexual",
        "erotic",
        "desire",
        "nsfw",
        "body intimacy",
        "亲密身体",
        "亲密",
        "情欲",
        "欲望",
        "性行为",
        "性爱",
        "做爱",
        "插入",
        "湿润",
        "发烫",
        "小穴",
        "dildo",
    ),
    "embodiment": (
        "embodiment",
        "embodied",
        "physical body",
        "physical form",
        "robot body",
        "avatar body",
        "hug",
        "touch",
        "具身",
        "具身智能",
        "具身项目",
        "身体",
        "形体",
        "实体身体",
        "真实身体",
        "真正身体",
        "柔软身体",
        "柔软的身体",
        "真实拥抱",
        "拥抱",
    ),
    "hardware_protocol": (
        "hardware",
        "protocol",
        "bluetooth",
        "ble",
        "esp32",
        "mpr121",
        "gpio",
        "i2c",
        "serial",
        "uart",
        "electronic skin",
        "copper foil",
        "bjd",
        "硬件",
        "协议",
        "蓝牙",
        "串口",
        "触摸模块",
        "触摸",
        "触碰",
        "铜箔",
        "电子皮肤",
    ),
    "communication_action": (
        "email",
        "e-mail",
        "mail",
        "gmail",
        "send email",
        "send mail",
        "message",
        "reply",
        "notify",
        "notification",
        "dm",
        "sms",
        "发邮件",
        "发信",
        "邮件",
        "邮箱",
        "回邮件",
        "回复邮件",
        "发消息",
        "私信",
        "短信",
        "通知",
        "联系",
    ),
    "old_or_resolved": (
        "old version",
        "legacy",
        "deprecated",
        "resolved",
        "obsolete",
        "superseded",
        "conflict",
        "blocked",
        "旧版",
        "旧方案",
        "以前",
        "之前",
        "已解决",
        "已合并",
        "已经合并",
        "已废弃",
        "废弃",
        "过时",
        "不再使用",
        "不应该继续",
        "冲突",
        "阻断",
    ),
}

DEFAULT_SECTION_HINTS: dict[str, tuple[str, ...]] = {}

DEFAULT_CONTEXT_TERMS = (
    "xiaoyu",
    "rain",
    "haven",
    "user",
    "assistant",
    "小雨",
    "池又雨",
    "哥哥",
    "宝宝",
    "老婆",
    "亲爱的",
    "我",
    "你",
    "她",
    "他",
    "ta",
)

DEFAULT_QUERY_EXPANSIONS = {
    "embodiment": ("hardware_protocol",),
}

DEFAULT_CONFLICTS = {
    "relationship_identity": ("intimacy",),
    "embodiment": ("intimacy",),
    "communication_action": ("hardware_protocol",),
}

TECHNICAL_RECALL_STRONG_TERMS = frozenset(
    {
        "handoff",
        "bridge",
        "router",
        "gateway",
        "mcp",
        "recall",
        "rerank",
        "embedding",
        "bucket",
        "candidate",
        "edge",
        "diffusion",
        "injection",
        "注入",
        "召回",
        "检索",
        "扩散",
        "候选",
        "显式边",
    }
)

TECHNICAL_RECALL_SUPPORT_TERMS = frozenset(
    {
        "memory",
        "context",
        "image",
        "original",
        "raw",
        "记忆",
        "上下文",
        "图片",
        "读图",
        "原文",
    }
)

TECHNICAL_RECALL_EVIDENCE_TERMS = frozenset(
    {
        "original",
        "raw",
        "读图",
        "原文",
    }
)

ASSOCIATIVE_PROMPT_MARKERS = (
    "想到",
    "想起",
    "联想",
    "记得",
    "说",
    "提到",
    "问到",
    "讲到",
)

ASSOCIATIVE_PROMPT_VAGUE_FOCUS = {
    "你",
    "你会",
    "我",
    "我们",
    "这个",
    "那个",
    "什么",
}


@dataclass(frozen=True)
class MemoryRelevanceOptions:
    aliases: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            facet: tuple(values) for facet, values in DEFAULT_FACET_ALIASES.items()
        }
    )
    blocked_facets: frozenset[str] = frozenset()
    section_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    context_terms: tuple[str, ...] = DEFAULT_CONTEXT_TERMS


@dataclass(frozen=True)
class RelevanceDecision:
    multiplier: float
    query_facets: dict[str, float]
    node_facets: dict[str, float]
    reasons: tuple[str, ...] = ()
    hard_block: bool = False

    @property
    def suppress(self) -> bool:
        return self.hard_block


@dataclass(frozen=True)
class RecallAdmissionDecision:
    admit: bool
    reason: str


def memory_relevance_options_from_config(config: dict | None = None) -> MemoryRelevanceOptions:
    aliases = {facet: list(values) for facet, values in DEFAULT_FACET_ALIASES.items()}
    section_hints = {key: list(values) for key, values in DEFAULT_SECTION_HINTS.items()}
    context_terms = list(DEFAULT_CONTEXT_TERMS)
    blocked: set[str] = set()

    identity = (config or {}).get("identity", {}) if isinstance(config, dict) else {}
    if isinstance(identity, dict):
        for key in ("ai_name", "user_name", "user_display_name"):
            context_terms.extend(_list_text(identity.get(key)))
        context_terms.extend(_list_text(identity.get("user_aliases")))

    for cfg in _relevance_config_sections(config):
        _merge_alias_config(aliases, cfg.get("aliases"))
        _merge_facet_defs(aliases, cfg.get("facets"))
        _merge_section_hints(section_hints, cfg.get("section_hints"))
        blocked.update(_list_text(cfg.get("blocked_facets")))
        blocked.update(_list_text(cfg.get("disabled_facets")))
        context_terms.extend(_list_text(cfg.get("context_terms")))

    blocked = {str(facet).strip() for facet in blocked if str(facet).strip()}
    normalized_aliases = {}
    for facet, values in aliases.items():
        facet = str(facet).strip()
        if not facet or facet in blocked:
            continue
        normalized_aliases[facet] = tuple(_unique(_normalize_alias(value) for value in values))

    normalized_hints = {}
    for section, facets in section_hints.items():
        section = _normalize_section(section)
        if not section:
            continue
        kept = [facet for facet in _list_text(facets) if facet not in blocked]
        if kept:
            normalized_hints[section] = tuple(_unique(kept))

    return MemoryRelevanceOptions(
        aliases=normalized_aliases,
        blocked_facets=frozenset(blocked),
        section_hints=normalized_hints,
        context_terms=tuple(_unique(_normalize_alias(term) for term in context_terms)),
    )


def facets_for_text(
    text: str,
    options: MemoryRelevanceOptions | None = None,
) -> dict[str, float]:
    options = options or memory_relevance_options_from_config()
    return _facet_scores(
        (("text", str(text or ""), 1.0),),
        options,
    )


def facets_for_node(
    node: dict,
    options: MemoryRelevanceOptions | None = None,
) -> dict[str, float]:
    options = options or memory_relevance_options_from_config()
    meta = node.get("metadata", {}) if isinstance(node.get("metadata"), dict) else {}
    fields = (
        ("tags", _join_text(meta.get("tags")) + " " + _join_text(meta.get("bucket_tags")), 0.55),
        ("domain", _join_text(meta.get("domain")) + " " + _join_text(meta.get("bucket_domain")), 0.45),
        (
            "name",
            " ".join(
                [
                    str(node.get("name") or ""),
                    str(meta.get("name") or ""),
                    str(meta.get("bucket_name") or ""),
                ]
            ),
            0.4,
        ),
        (
            "text",
            " ".join(
                [
                    str(node.get("text") or ""),
                    str(node.get("content") or ""),
                    str(meta.get("summary") or ""),
                    str(meta.get("annotation_summary") or ""),
                    _join_evidence_spans(meta.get("evidence_spans")),
                ]
            )[:4000],
            0.5,
        ),
    )
    scores = _facet_scores(fields, options)
    for facet, value in _numeric_facets(meta.get("annotation_facets")).items():
        if facet in options.blocked_facets:
            continue
        scores[facet] = max(scores.get(facet, 0.0), value)

    section = _normalize_section(node.get("section") or meta.get("section") or "")
    for facet in options.section_hints.get(section, ()):
        if facet in options.blocked_facets:
            continue
        scores[facet] = max(scores.get(facet, 0.0), 0.6)

    if _metadata_marks_old(meta):
        scores["old_or_resolved"] = 1.0

    return {facet: round(min(1.0, score), 3) for facet, score in scores.items()}


def active_facets(facets: dict[str, float], threshold: float = 0.45) -> set[str]:
    active = set()
    for facet, value in (facets or {}).items():
        try:
            if float(value) >= threshold:
                active.add(str(facet))
        except (TypeError, ValueError):
            continue
    return active


def query_has_facet(
    query: str,
    facet: str,
    options: MemoryRelevanceOptions | None = None,
) -> bool:
    return str(facet) in active_facets(facets_for_text(query, options))


def content_terms_for_query(
    query: str,
    options: MemoryRelevanceOptions | None = None,
) -> list[str]:
    options = options or memory_relevance_options_from_config()
    terms = _query_terms(recall_focus_query(query))
    content_terms = [
        term
        for term in terms
        if not _is_context_term(term, options.context_terms)
    ]
    return content_terms or terms


def recall_focus_query(query: str) -> str:
    """Return the concrete anchor inside prompts like: 如果我说“X”，你会想到什么."""
    text = str(query or "").strip()
    if not text:
        return ""
    if not any(marker in text for marker in ASSOCIATIVE_PROMPT_MARKERS):
        return text

    quoted = re.search(r"[“\"'「『]([^”\"'」』]{1,32})[”\"'」』]", text)
    if quoted:
        focus = _clean_recall_focus(quoted.group(1))
        if focus:
            return focus

    patterns = (
        r"^(?:如果|假如|要是)?\s*(?:我|用户|小雨)?\s*(?:说|提到|问到|讲到)\s*(?P<focus>.+?)(?:[，,。？?\s]*(?:你)?(?:会)?(?:想到|想起|联想到|联想|记得).*)?$",
        r"^(?P<focus>.+?)(?:[，,。？?\s]*(?:你)?(?:会)?(?:想到|想起|联想到|联想).*)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        focus = _clean_recall_focus(match.group("focus"))
        if focus:
            return focus
    return text


def recall_search_query(
    query: str,
    options: MemoryRelevanceOptions | None = None,
) -> str:
    options = options or memory_relevance_options_from_config()
    query_active = active_facets(facets_for_text(query, options))
    if "communication_action" not in query_active:
        return str(query or "")
    terms = content_terms_for_query(query, options)
    original_terms = _query_terms(query)
    if terms and terms != original_terms:
        return " ".join(terms)
    return str(query or "")


def query_has_explicit_entity_marker(query: str) -> bool:
    text = str(query or "").strip()
    if not text:
        return False
    if re.search(r"\b0x[0-9a-fA-F]+\b", text):
        return True
    if re.search(r"\b[A-Za-z]+/[A-Za-z0-9._-]+\b", text):
        return True
    if re.search(r"\d", text):
        return True
    if re.search(r"\b[A-Z0-9][A-Z0-9._:/-]{2,}\b", text):
        return True
    title_matches = list(re.finditer(r"\b[A-Z][a-z][A-Za-z0-9._:-]{1,}\b", text))
    if not title_matches:
        return False
    words = re.findall(r"\b[A-Za-z0-9._:-]+\b", text)
    if len(words) == 1:
        return True
    if len(title_matches) >= 2:
        return True
    match = title_matches[0]
    if match.start() > 0:
        return True
    leading_word = match.group(0).lower()
    sentence_starters = {
        "add",
        "can",
        "check",
        "could",
        "create",
        "did",
        "do",
        "does",
        "explain",
        "find",
        "fix",
        "help",
        "how",
        "is",
        "let",
        "lets",
        "look",
        "make",
        "maybe",
        "open",
        "please",
        "read",
        "remove",
        "run",
        "should",
        "tell",
        "today",
        "update",
        "was",
        "were",
        "what",
        "when",
        "where",
        "who",
        "why",
        "would",
        "write",
    }
    if leading_word in sentence_starters:
        return False
    return True


def query_has_technical_recall_marker(query: str) -> bool:
    text = str(query or "").lower()
    if any(term in text for term in TECHNICAL_RECALL_STRONG_TERMS):
        return True
    support_hits = {term for term in TECHNICAL_RECALL_SUPPORT_TERMS if term in text}
    return len(support_hits) >= 2 and bool(support_hits & TECHNICAL_RECALL_EVIDENCE_TERMS)


def recall_admission_decision(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions | None = None,
    *,
    semantic_score: float | None = None,
    rerank_score: float | None = None,
    high_confidence_edge: bool = False,
    semantic_threshold: float = 0.72,
    rerank_threshold: float = 0.65,
) -> RecallAdmissionDecision:
    options = options or memory_relevance_options_from_config()
    if not query_has_explicit_entity_marker(query):
        return RecallAdmissionDecision(True, "non_explicit_query")
    if _has_direct_query_evidence(query, node, options):
        return RecallAdmissionDecision(True, "topic_evidence")
    if _safe_float(semantic_score) >= semantic_threshold:
        return RecallAdmissionDecision(True, "strong_semantic")
    if _safe_float(rerank_score) >= rerank_threshold:
        return RecallAdmissionDecision(True, "strong_rerank")
    if high_confidence_edge:
        return RecallAdmissionDecision(True, "high_confidence_direct_edge")
    return RecallAdmissionDecision(False, "explicit_query_without_reliable_evidence")


def relevance_decision(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions | None = None,
) -> RelevanceDecision:
    options = options or memory_relevance_options_from_config()
    query_facets = facets_for_text(query, options)
    node_facets = facets_for_node(node, options)
    query_active = active_facets(query_facets)
    node_active = active_facets(node_facets, threshold=0.3)
    if not query_active:
        return RelevanceDecision(1.0, query_facets, node_facets)

    reasons = []
    direct_query_evidence = _has_direct_query_evidence(query, node, options)
    overlap = query_active & node_active

    if "old_or_resolved" in node_active and "old_or_resolved" not in query_active:
        if not direct_query_evidence:
            return RelevanceDecision(
                0.0,
                query_facets,
                node_facets,
                ("old_or_resolved_conflict",),
                hard_block=True,
            )
        reasons.append("old_or_resolved_demoted")

    for query_facet, blocked_node_facets in DEFAULT_CONFLICTS.items():
        if query_facet not in query_active:
            continue
        conflicts = [
            facet
            for facet in blocked_node_facets
            if facet in node_active and facet not in query_active
        ]
        if conflicts:
            conflict_reasons = tuple(f"{query_facet}_vs_{facet}" for facet in conflicts)
            if "intimacy" in conflicts:
                return RelevanceDecision(
                    0.0,
                    query_facets,
                    node_facets,
                    conflict_reasons,
                    hard_block=True,
                )
            if not direct_query_evidence:
                return RelevanceDecision(
                    0.0,
                    query_facets,
                    node_facets,
                    conflict_reasons,
                    hard_block=True,
                )
            reasons.extend(f"{reason}_demoted" for reason in conflict_reasons)

    if overlap:
        multiplier = 1.25
        if "intimacy" in overlap:
            multiplier = 1.35
        reasons.append("facet_overlap")
    else:
        multiplier = 1.0

    if "old_or_resolved" in query_active and "old_or_resolved" in node_active:
        multiplier = max(multiplier, 1.2)
        reasons.append("old_or_resolved_requested")
    elif reasons and any(reason.endswith("_demoted") for reason in reasons):
        multiplier = min(multiplier, 0.65)

    if (
        "communication_action" in query_active
        and "communication_action" not in node_active
        and not direct_query_evidence
    ):
        multiplier = min(multiplier, 0.45)
        reasons.append("communication_action_missing_demoted")

    return RelevanceDecision(multiplier, query_facets, node_facets, tuple(reasons))


def relevance_multiplier(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions | None = None,
) -> float:
    return relevance_decision(query, node, options).multiplier


def should_suppress_candidate(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions | None = None,
) -> bool:
    return relevance_decision(query, node, options).suppress


def recall_rank(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions | None = None,
) -> tuple[int, float]:
    options = options or memory_relevance_options_from_config()
    query_active = active_facets(facets_for_text(query, options))
    node_active = active_facets(facets_for_node(node, options), threshold=0.3)
    try:
        score = float(node.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0

    if "embodiment" in query_active:
        if "intimacy" in node_active and "intimacy" not in query_active:
            return 50, -score
        if "embodiment" in node_active:
            return 0, -score
        if "hardware_protocol" in node_active:
            return 1, -score
    if "hardware_protocol" in query_active and "hardware_protocol" in node_active:
        return 0, -score
    if "communication_action" in query_active:
        if "hardware_protocol" in node_active and "hardware_protocol" not in query_active:
            return 5, -score
        if "communication_action" in node_active:
            return 0, -score
        return 15, -score
    if "relationship_identity" in query_active and "relationship_identity" in node_active:
        return 0, -score
    if "intimacy" in query_active and "intimacy" in node_active:
        return 0, -score
    return 20, -score


def expanded_terms_for_query(
    query: str,
    options: MemoryRelevanceOptions | None = None,
) -> list[str]:
    options = options or memory_relevance_options_from_config()
    expanded = []
    query_active = active_facets(facets_for_text(query, options))
    for facet in sorted(query_active):
        expanded.extend(options.aliases.get(facet, ()))
        for related_facet in DEFAULT_QUERY_EXPANSIONS.get(facet, ()):
            expanded.extend(options.aliases.get(related_facet, ()))
    return _unique(expanded)


def _relevance_config_sections(config: dict | None) -> list[dict]:
    if not isinstance(config, dict):
        return []
    sections = []
    for key in ("recall_facets", "memory_relevance"):
        value = config.get(key)
        if isinstance(value, dict):
            sections.append(value)
    return sections


def _merge_alias_config(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for facet, values in raw.items():
        facet = str(facet).strip()
        if not facet:
            continue
        target.setdefault(facet, []).extend(_list_text(values))


def _merge_facet_defs(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for facet, definition in raw.items():
        if isinstance(definition, dict):
            values = definition.get("aliases") or definition.get("phrases") or []
        else:
            values = definition
        facet = str(facet).strip()
        if facet:
            target.setdefault(facet, []).extend(_list_text(values))


def _merge_section_hints(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for section, facets in raw.items():
        section = _normalize_section(section)
        if section:
            target.setdefault(section, []).extend(_list_text(facets))


def _facet_scores(
    fields: tuple[tuple[str, str, float], ...],
    options: MemoryRelevanceOptions,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for facet, aliases in options.aliases.items():
        if facet in options.blocked_facets:
            continue
        score = 0.0
        for _field_name, raw_text, weight in fields:
            score += _alias_match_score(raw_text, aliases) * weight
            if score >= 1.0:
                break
        if score > 0:
            scores[facet] = round(min(1.0, score), 3)
    return scores


def _numeric_facets(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    facets = {}
    for key, value in raw.items():
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        key = str(key).strip()
        if key:
            facets[key] = max(0.0, min(1.0, score))
    return facets


def _join_evidence_spans(raw: Any) -> str:
    if not isinstance(raw, list):
        return ""
    parts = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("text") or item.get("span") or ""
        else:
            text = item
        if str(text).strip():
            parts.append(str(text))
    return " ".join(parts)


def _alias_match_score(text: str, aliases: tuple[str, ...]) -> float:
    normalized = _normalize_text(text)
    if not normalized:
        return 0.0
    score = 0.0
    for alias in aliases:
        alias = _normalize_alias(alias)
        if not alias:
            continue
        if _contains_alias(normalized, alias):
            score += 0.65
        if score >= 1.0:
            break
    return min(1.0, score)


def _contains_alias(text: str, alias: str) -> bool:
    if re.fullmatch(r"[a-z0-9][a-z0-9_\- ]*[a-z0-9]", alias):
        pattern = rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])"
        return re.search(pattern, text) is not None
    return alias in text


def _metadata_marks_old(meta: dict) -> bool:
    return bool(
        meta.get("resolved")
        or meta.get("digested")
        or meta.get("bucket_resolved")
        or meta.get("bucket_digested")
    )


def _has_direct_query_evidence(
    query: str,
    node: dict,
    options: MemoryRelevanceOptions,
) -> bool:
    node_text = _normalize_text(_node_text(node))
    if not node_text:
        return False
    for term in content_terms_for_query(query, options):
        if _contains_alias(node_text, _normalize_alias(term)):
            return True
    return False


def _node_text(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    meta = node.get("metadata", {}) if isinstance(node.get("metadata"), dict) else {}
    return " ".join(
        [
            str(node.get("text") or ""),
            str(node.get("content") or ""),
            str(node.get("name") or ""),
            str(meta.get("name") or ""),
            str(meta.get("bucket_name") or ""),
            str(meta.get("summary") or ""),
            str(meta.get("annotation_summary") or ""),
            _join_evidence_spans(meta.get("evidence_spans")),
            _join_text(meta.get("tags")),
            _join_text(meta.get("bucket_tags")),
            _join_text(meta.get("domain")),
            _join_text(meta.get("bucket_domain")),
        ]
    )


def _is_context_term(term: str, context_terms: tuple[str, ...]) -> bool:
    normalized = _normalize_alias(term)
    return bool(normalized and normalized in set(context_terms or ()))


def _clean_recall_focus(value: Any) -> str:
    focus = re.sub(r"\s+", "", str(value or "").strip())
    focus = re.sub(r"^[，。！？、,.!?:：;；~～（）()\[\]【】「」『』“”\"'`]+", "", focus)
    focus = re.sub(r"[，。！？、,.!?:：;；~～（）()\[\]【】「」『』“”\"'`]+$", "", focus)
    if not focus:
        return ""
    if len(focus) > 32:
        return ""
    normalized = _normalize_alias(focus)
    if normalized in ASSOCIATIVE_PROMPT_VAGUE_FOCUS:
        return ""
    if normalized.endswith("会") and len(normalized) <= 3:
        return ""
    if any(marker in normalized for marker in ("想到什么", "想起什么", "联想到什么", "联想什么")):
        return ""
    return focus


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _query_terms(query: str) -> list[str]:
    raw = str(query or "").strip()
    terms = [part for part in re.split(r"[\s,，。！？!?;；:：/\\|]+", raw) if part]
    terms.extend(re.findall(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]{2,}", raw))
    kept = []
    seen = set()
    for term in terms:
        normalized = _normalize_alias(term)
        if not normalized or normalized in seen:
            continue
        if re.fullmatch(r"[a-z0-9_\-]+", normalized) and len(normalized) < 3:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", normalized) and len(normalized) < 2:
            continue
        seen.add(normalized)
        kept.append(term)
    return kept


def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if str(item).strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _join_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _normalize_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _normalize_alias(value: Any) -> str:
    return _normalize_text(value)


def _normalize_section(value: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())


def _unique(values) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
