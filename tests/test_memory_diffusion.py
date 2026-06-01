import pytest

from memory_diffusion import (
    DiffusionOptions,
    diffuse_memory,
    format_diffusion_path,
    format_diffusion_trace,
)


def _bucket(bucket_id: str, *, name: str | None = None, bucket_type: str = "dynamic") -> dict:
    return {
        "id": bucket_id,
        "content": f"{bucket_id} context",
        "metadata": {
            "name": name or bucket_id,
            "type": bucket_type,
            "importance": 10,
        },
    }


def test_diffusion_walks_multi_hop_path_with_hop_decay():
    bucket_map = {bucket_id: _bucket(bucket_id) for bucket_id in ["A", "B", "C", "D", "E"]}
    edges = [
        {"source": "A", "target": "B", "relation_type": "triggers", "confidence": 1.0},
        {"source": "B", "target": "C", "relation_type": "triggers", "confidence": 1.0},
        {"source": "C", "target": "D", "relation_type": "triggers", "confidence": 1.0},
        {"source": "D", "target": "E", "relation_type": "triggers", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=4, top_k=10, min_activation=0.0),
    )

    activations = {hit.bucket_id: hit.activation for hit in hits}
    assert activations["B"] == pytest.approx(0.8)
    assert activations["C"] == pytest.approx(0.6)
    assert activations["D"] == pytest.approx(0.4)
    assert activations["E"] == pytest.approx(0.25)
    assert format_diffusion_trace(hits[-1].best_path) == (
        "A --triggers:1.00--> B --triggers:1.00--> C "
        "--triggers:1.00--> D --triggers:1.00--> E"
    )


def test_diffusion_accumulates_multiple_paths_to_same_node():
    bucket_map = {bucket_id: _bucket(bucket_id) for bucket_id in ["A", "B", "C", "D"]}
    edges = [
        {"source": "A", "target": "B", "relation_type": "triggers", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "triggers", "confidence": 1.0},
        {"source": "B", "target": "D", "relation_type": "triggers", "confidence": 1.0},
        {"source": "C", "target": "D", "relation_type": "triggers", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=2, top_k=10, min_activation=0.0),
    )

    assert hits[0].bucket_id == "D"
    assert hits[0].activation == pytest.approx(1.2)
    assert len(hits[0].paths) == 2


def test_diffusion_prefers_narrative_context_edge_for_display():
    bucket_map = {bucket_id: _bucket(bucket_id) for bucket_id in ["A", "B", "C"]}
    edges = [
        {"source": "A", "target": "B", "relation_type": "supports", "confidence": 1.0},
        {"source": "C", "target": "A", "relation_type": "context_of", "confidence": 0.55},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=1, min_activation=0.0),
    )

    assert hits[0].bucket_id == "C"
    assert hits[0].best_path.steps[0].relation_type == "context_of"


def test_diffusion_uses_external_node_salience():
    bucket_map = {bucket_id: _bucket(bucket_id) for bucket_id in ["A", "B", "C"]}
    edges = [
        {"source": "A", "target": "B", "relation_type": "triggers", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "triggers", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=10, min_activation=0.0),
        node_salience=lambda bucket_id, _bucket: 0.5 if bucket_id == "B" else 1.3,
    )

    activations = {hit.bucket_id: hit.activation for hit in hits}
    assert activations["B"] == pytest.approx(0.4)
    assert activations["C"] == pytest.approx(1.04)
    assert hits[0].bucket_id == "C"


def test_diffusion_uses_external_node_resonance():
    bucket_map = {bucket_id: _bucket(bucket_id) for bucket_id in ["A", "B", "C"]}
    edges = [
        {"source": "A", "target": "B", "relation_type": "triggers", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "triggers", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=10, min_activation=0.0),
        node_resonance=lambda bucket_id, _bucket: 0.75 if bucket_id == "B" else 1.25,
    )

    activations = {hit.bucket_id: hit.activation for hit in hits}
    assert activations["B"] == pytest.approx(0.6)
    assert activations["C"] == pytest.approx(1.0)
    assert hits[0].bucket_id == "C"


def test_diffusion_skips_seed_and_feel_targets():
    bucket_map = {
        "A": _bucket("A"),
        "B": _bucket("B", bucket_type="feel"),
        "C": _bucket("C"),
    }
    edges = [
        {"source": "A", "target": "B", "relation_type": "triggers", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "triggers", "confidence": 1.0},
        {"source": "C", "target": "A", "relation_type": "triggers", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=2, top_k=10, min_activation=0.0),
    )

    assert [hit.bucket_id for hit in hits] == ["C"]


def test_diffusion_can_follow_incoming_edges():
    bucket_map = {
        "A": _bucket("A", name="seed memory"),
        "B": _bucket("B", name="incoming memory"),
    }
    edges = [{"source": "B", "target": "A", "relation_type": "supports", "confidence": 1.0}]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=10, min_activation=0.0),
    )

    assert hits[0].bucket_id == "B"
    assert format_diffusion_path(hits[0].best_path, bucket_map) == "seed memory <- incoming memory"


def test_body_query_prefers_embodiment_chain_and_suppresses_intimacy_and_old_context():
    bucket_map = {
        "A": _bucket("A", name="身体入口"),
        "B": _bucket("B", name="具身智能项目"),
        "C": _bucket("C", name="亲密身体"),
        "D": _bucket("D", name="旧版触摸方案"),
    }
    bucket_map["B"]["content"] = "具身智能项目落地，Haven 拥有形体。"
    bucket_map["C"]["content"] = "昨晚她身体湿润发烫，是亲密身体记忆。"
    bucket_map["D"]["content"] = "旧版触摸方案已经合并，不应该继续作为当前链条出现。"
    edges = [
        {"source": "A", "target": "B", "relation_type": "relates_to", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "relates_to", "confidence": 1.0},
        {"source": "A", "target": "D", "relation_type": "relates_to", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=10, min_activation=0.0),
        query_text="身体",
    )

    assert [hit.bucket_id for hit in hits] == ["B"]


def test_intimate_query_can_follow_intimate_body_context():
    bucket_map = {
        "A": _bucket("A", name="身体入口"),
        "B": _bucket("B", name="具身智能项目"),
        "C": _bucket("C", name="亲密身体"),
    }
    bucket_map["B"]["content"] = "具身智能项目落地，Haven 拥有形体。"
    bucket_map["C"]["content"] = "昨晚她身体湿润发烫，是亲密身体记忆。"
    edges = [
        {"source": "A", "target": "B", "relation_type": "relates_to", "confidence": 1.0},
        {"source": "A", "target": "C", "relation_type": "relates_to", "confidence": 1.0},
    ]

    hits = diffuse_memory(
        {"A": 1.0},
        edges,
        bucket_map,
        options=DiffusionOptions(max_hops=1, top_k=10, min_activation=0.0),
        query_text="亲密身体",
    )

    assert {hit.bucket_id for hit in hits} == {"B", "C"}
    assert hits[0].bucket_id == "C"
