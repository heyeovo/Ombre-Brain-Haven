from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from memory_relevance import (
    MemoryRelevanceOptions,
    memory_relevance_options_from_config,
    relevance_multiplier,
    should_suppress_candidate,
)


DEFAULT_HOP_DECAYS = (0.8, 0.6, 0.4, 0.25)
DEFAULT_RELATION_TYPE_WEIGHTS = {
    "same_event": 1.15,
    "context_of": 1.1,
    "precedes": 1.0,
    "triggers": 1.0,
    "causes": 0.95,
    "updates": 0.9,
    "supports": 0.85,
    "promises": 0.9,
    "belongs_to": 0.8,
    "emotional_echo": 0.95,
    "next_context": 0.85,
    "previous_context": 0.75,
    "reflects_on": 0.8,
    "evidenced_by": 1.0,
    "relates_to": 0.7,
    "contradicts": 0.45,
    "blocks": 0.45,
}

RELATION_DISPLAY_PRIORITY = {
    "same_event": 90,
    "context_of": 80,
    "precedes": 80,
    "previous_context": 75,
    "next_context": 70,
    "updates": 65,
    "evidenced_by": 62,
    "triggers": 60,
    "causes": 58,
    "supports": 50,
    "promises": 45,
    "belongs_to": 40,
    "emotional_echo": 35,
    "relates_to": 20,
    "contradicts": 5,
    "blocks": 5,
}

NodeSalienceFn = Callable[[str, dict], float]
NodeResonanceFn = Callable[[str, dict], float]


@dataclass(frozen=True)
class DiffusionOptions:
    enabled: bool = True
    max_hops: int = 2
    top_k: int = 4
    min_activation: float = 0.18
    hop_decays: tuple[float, ...] = DEFAULT_HOP_DECAYS
    fallback_decay: float = 0.55
    include_incoming: bool = True
    max_paths_per_hit: int = 3
    relevance: MemoryRelevanceOptions = field(default_factory=memory_relevance_options_from_config)
    relation_type_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_RELATION_TYPE_WEIGHTS)
    )


@dataclass(frozen=True)
class DiffusionStep:
    source: str
    target: str
    relation_type: str
    confidence: float
    reason: str = ""
    direction: str = "outgoing"


@dataclass(frozen=True)
class DiffusionPath:
    nodes: tuple[str, ...]
    steps: tuple[DiffusionStep, ...]
    score: float


@dataclass(frozen=True)
class DiffusionHit:
    bucket_id: str
    activation: float
    paths: tuple[DiffusionPath, ...]

    @property
    def best_path(self) -> DiffusionPath:
        return self.paths[0]


@dataclass(frozen=True)
class _PathState:
    current_id: str
    nodes: tuple[str, ...]
    steps: tuple[DiffusionStep, ...]
    path_strength: float


def _relation_display_priority(relation_type: str) -> int:
    return RELATION_DISPLAY_PRIORITY.get(str(relation_type or "relates_to"), 10)


def _path_display_priority(path: "DiffusionPath") -> int:
    if not path or not path.steps:
        return 0
    return max(_relation_display_priority(step.relation_type) for step in path.steps)


def _path_rank_key(path: "DiffusionPath") -> tuple[int, float, int]:
    return (_path_display_priority(path), path.score, -len(path.steps))


def diffusion_options_from_config(config: dict | None) -> DiffusionOptions:
    cfg = (config or {}).get("memory_diffusion", {}) or {}
    relation_weights = dict(DEFAULT_RELATION_TYPE_WEIGHTS)
    custom_weights = cfg.get("relation_type_weights") or {}
    if isinstance(custom_weights, dict):
        for key, value in custom_weights.items():
            try:
                relation_weights[str(key)] = _clamp(float(value), 0.0, 2.0)
            except (TypeError, ValueError):
                continue

    return DiffusionOptions(
        enabled=_bool_value(cfg.get("enabled", True)),
        max_hops=_int_between(cfg.get("max_hops", 2), 2, 1, 8),
        top_k=_int_between(
            cfg.get("top_k", cfg.get("diffusion_top_k", 4)),
            4,
            0,
            20,
        ),
        min_activation=_float_between(cfg.get("min_activation", 0.18), 0.18, 0.0, 10.0),
        hop_decays=_float_tuple(cfg.get("hop_decays") or cfg.get("decay_by_hop")),
        fallback_decay=_float_between(cfg.get("decay", 0.55), 0.55, 0.0, 1.0),
        include_incoming=_bool_value(cfg.get("include_incoming", True)),
        max_paths_per_hit=_int_between(cfg.get("max_paths_per_hit", 3), 3, 1, 10),
        relevance=memory_relevance_options_from_config(config),
        relation_type_weights=relation_weights,
    )


def diffuse_memory(
    seed_scores: dict[str, float],
    edges: list[dict],
    bucket_map: dict[str, dict],
    options: DiffusionOptions | None = None,
    exclude_ids: set[str] | None = None,
    node_salience: NodeSalienceFn | None = None,
    node_resonance: NodeResonanceFn | None = None,
    query_text: str = "",
) -> list[DiffusionHit]:
    options = options or DiffusionOptions()
    if not options.enabled or options.top_k <= 0 or options.max_hops <= 0:
        return []

    normalized_seeds = {}
    for bucket_id, score in (seed_scores or {}).items():
        if not bucket_id:
            continue
        try:
            normalized_score = _clamp(float(score), 0.0, 10.0)
        except (TypeError, ValueError):
            continue
        if normalized_score > 0:
            normalized_seeds[str(bucket_id)] = normalized_score
    if not normalized_seeds:
        return []

    excluded = set(exclude_ids or set()) | set(normalized_seeds)
    adjacency = _build_adjacency(edges, options.include_incoming)
    frontier = [
        _PathState(
            current_id=bucket_id,
            nodes=(bucket_id,),
            steps=(),
            path_strength=score,
        )
        for bucket_id, score in normalized_seeds.items()
    ]

    scores: dict[str, float] = {}
    paths: dict[str, list[DiffusionPath]] = {}

    for hop_index in range(options.max_hops):
        next_frontier: list[_PathState] = []
        hop_weight = _hop_weight(hop_index, options)
        if hop_weight <= 0:
            break

        for state in frontier:
            for step in adjacency.get(state.current_id, []):
                target_id = step.target
                if target_id in state.nodes:
                    continue
                target = bucket_map.get(target_id)
                if not target or _is_feel_bucket(target):
                    continue
                query_multiplier = relevance_multiplier(query_text, target, options.relevance)
                if query_multiplier <= 0:
                    continue

                relation_weight = options.relation_type_weights.get(
                    step.relation_type,
                    options.relation_type_weights.get("relates_to", 0.7),
                )
                next_strength = state.path_strength * step.confidence * relation_weight * query_multiplier
                activation = (
                    next_strength
                    * hop_weight
                    * _resolved_node_salience(target_id, target, node_salience)
                    * _resolved_node_resonance(target_id, target, node_resonance)
                )
                if activation < options.min_activation:
                    continue

                next_nodes = state.nodes + (target_id,)
                next_steps = state.steps + (step,)
                if target_id not in excluded:
                    scores[target_id] = scores.get(target_id, 0.0) + activation
                    paths.setdefault(target_id, []).append(
                        DiffusionPath(
                            nodes=next_nodes,
                            steps=next_steps,
                            score=activation,
                        )
                    )
                next_frontier.append(
                    _PathState(
                        current_id=target_id,
                        nodes=next_nodes,
                        steps=next_steps,
                        path_strength=next_strength,
                    )
                )

        frontier = next_frontier
        if not frontier:
            break

    hits = []
    for bucket_id, activation in scores.items():
        ranked_paths = sorted(paths.get(bucket_id, []), key=_path_rank_key, reverse=True)[
            : options.max_paths_per_hit
        ]
        if ranked_paths:
            hits.append(
                DiffusionHit(
                    bucket_id=bucket_id,
                    activation=round(activation, 4),
                    paths=tuple(ranked_paths),
                )
            )

    hits.sort(
        key=lambda item: (
            _path_display_priority(item.best_path),
            item.activation,
            -len(item.best_path.steps),
        ),
        reverse=True,
    )
    return hits[: options.top_k]


def seed_scores_for_buckets(buckets: list[dict]) -> dict[str, float]:
    scores = {}
    for bucket in buckets or []:
        bucket_id = bucket.get("id")
        if not bucket_id:
            continue
        scores[bucket_id] = _seed_score(bucket)
    return scores


def format_diffusion_path(path: DiffusionPath, bucket_map: dict[str, dict]) -> str:
    if not path.steps:
        return " -> ".join(path.nodes)
    rendered = [_bucket_label(path.nodes[0], bucket_map)]
    for step in path.steps:
        arrow = "<-" if step.direction == "incoming" else "->"
        rendered.append(f"{arrow} {_bucket_label(step.target, bucket_map)}")
    return " ".join(rendered)


def format_diffusion_trace(
    path: DiffusionPath,
    bucket_map: dict[str, dict] | None = None,
    *,
    use_labels: bool = False,
) -> str:
    if not path.steps:
        return " -> ".join(path.nodes)

    bucket_map = bucket_map or {}

    def label(bucket_id: str) -> str:
        return _bucket_label(bucket_id, bucket_map) if use_labels else bucket_id

    rendered = [label(path.nodes[0])]
    for step in path.steps:
        relation = f"{step.relation_type}:{step.confidence:.2f}"
        if step.direction == "incoming":
            rendered.append(f"<--{relation}-- {label(step.target)}")
        else:
            rendered.append(f"--{relation}--> {label(step.target)}")
    return " ".join(rendered)


def path_has_caution(path: DiffusionPath) -> bool:
    return any(step.relation_type in {"contradicts", "blocks"} for step in path.steps)


def should_suppress_context_candidate(
    query: str,
    node: dict,
    config: dict | MemoryRelevanceOptions | None = None,
) -> bool:
    options = (
        config
        if isinstance(config, MemoryRelevanceOptions)
        else memory_relevance_options_from_config(config if isinstance(config, dict) else None)
    )
    return should_suppress_candidate(query, node, options)


def _build_adjacency(edges: list[dict], include_incoming: bool) -> dict[str, list[DiffusionStep]]:
    adjacency: dict[str, list[DiffusionStep]] = {}
    for edge in edges or []:
        source = str(edge.get("source") or edge.get("source_memory_id") or "").strip()
        target = str(edge.get("target") or edge.get("target_memory_id") or "").strip()
        if not source or not target or source == target:
            continue
        relation_type = str(edge.get("relation_type") or edge.get("type") or "relates_to").strip()
        confidence = _clamp(edge.get("confidence", 0.5), 0.0, 1.0)
        reason = str(edge.get("reason") or "").strip()
        outgoing = DiffusionStep(source, target, relation_type, confidence, reason, "outgoing")
        adjacency.setdefault(source, []).append(outgoing)
        if include_incoming:
            incoming = DiffusionStep(target, source, relation_type, confidence, reason, "incoming")
            adjacency.setdefault(target, []).append(incoming)

    for steps in adjacency.values():
        steps.sort(key=lambda item: item.confidence, reverse=True)
    return adjacency


def _seed_score(bucket: dict) -> float:
    raw_score = bucket.get("score")
    try:
        score = float(raw_score)
        if score > 10:
            return _clamp(score / 100.0, 0.15, 1.0)
        if score > 1:
            return _clamp(score / 10.0, 0.15, 1.0)
        return _clamp(score, 0.15, 1.0)
    except (TypeError, ValueError):
        pass

    meta = bucket.get("metadata", {}) or {}
    try:
        importance = float(meta.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5.0
    return _clamp(importance / 10.0, 0.35, 1.0)


def _node_salience(bucket: dict) -> float:
    meta = bucket.get("metadata", {}) or {}
    try:
        importance = float(meta.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5.0
    importance_score = _clamp(importance / 10.0, 0.0, 1.0)
    if meta.get("anchor"):
        importance_score = max(importance_score, 0.9)
    if meta.get("pinned") or meta.get("protected"):
        importance_score = max(importance_score, 0.95)
    return 0.65 + importance_score * 0.35


def _resolved_node_salience(
    bucket_id: str,
    bucket: dict,
    node_salience: NodeSalienceFn | None,
) -> float:
    if node_salience:
        try:
            return _clamp(node_salience(bucket_id, bucket), 0.2, 1.5)
        except Exception:
            pass
    return _node_salience(bucket)


def _resolved_node_resonance(
    bucket_id: str,
    bucket: dict,
    node_resonance: NodeResonanceFn | None,
) -> float:
    if node_resonance:
        try:
            return _clamp(node_resonance(bucket_id, bucket), 0.5, 1.5)
        except Exception:
            pass
    return 1.0


def _bucket_label(bucket_id: str, bucket_map: dict[str, dict]) -> str:
    bucket = bucket_map.get(bucket_id) or {}
    meta = bucket.get("metadata", {}) or {}
    return str(meta.get("name") or bucket_id)


def _is_feel_bucket(bucket: dict) -> bool:
    return (bucket.get("metadata", {}) or {}).get("type") == "feel"


def _hop_weight(hop_index: int, options: DiffusionOptions) -> float:
    if options.hop_decays:
        if hop_index < len(options.hop_decays):
            return options.hop_decays[hop_index]
        tail_index = hop_index - len(options.hop_decays) + 1
        return options.hop_decays[-1] * (options.fallback_decay ** tail_index)
    return options.fallback_decay ** (hop_index + 1)


def _float_tuple(value: Any) -> tuple[float, ...]:
    if value is None:
        return DEFAULT_HOP_DECAYS
    if not isinstance(value, (list, tuple)):
        return DEFAULT_HOP_DECAYS
    numbers = []
    for item in value:
        try:
            numbers.append(_clamp(float(item), 0.0, 10.0))
        except (TypeError, ValueError):
            continue
    return tuple(numbers) or DEFAULT_HOP_DECAYS


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _int_between(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _float_between(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return _clamp(number, low, high)


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))
