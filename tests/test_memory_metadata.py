from memory_metadata import normalize_memory_metadata


def test_normalize_memory_metadata_splits_domain_kind_status_and_flags():
    bucket = {
        "id": "b1",
        "path": "buckets/dynamic/AI/example.md",
        "metadata": {
            "name": "Gateway recall 修复",
            "domain": ["AI", "未解决"],
            "tags": ["gateway", "source_record"],
            "type": "dynamic",
            "resolved": False,
            "pinned": True,
        },
    }

    view = normalize_memory_metadata(bucket)

    assert view == {
        "canonical_domain": "project",
        "domain_parent": "project",
        "domain_label": "项目",
        "domain_parent_label": "项目",
        "kind": "source_record",
        "status_view": "protected",
        "flags": ["pinned", "source_record"],
        "legacy_domain": ["AI", "未解决"],
    }


def test_normalize_memory_metadata_prefers_existing_canonical_fields_without_mutating_bucket():
    bucket = {
        "id": "b2",
        "metadata": {
            "canonical_domain": "project",
            "kind": "profile_fact",
            "status": "digested",
            "domain": ["恋爱", "代码"],
            "tags": ["favorite"],
        },
    }
    original_domain = list(bucket["metadata"]["domain"])

    view = normalize_memory_metadata(bucket)

    assert view["canonical_domain"] == "project"
    assert view["kind"] == "profile_fact"
    assert view["status_view"] == "digested"
    assert view["flags"] == ["favorite", "profile_fact"]
    assert bucket["metadata"]["domain"] == original_domain


def test_normalize_memory_metadata_keeps_scene_out_of_domain():
    task_bucket = {
        "metadata": {
            "domain": ["亲密", "代码"],
            "tags": ["relationship_tone"],
            "type": "dynamic",
        }
    }

    view = normalize_memory_metadata(task_bucket)

    assert view["canonical_domain"] == "intimacy"
    assert view["kind"] == "event"
    assert view["status_view"] == "active"
    assert view["legacy_domain"] == ["亲密", "代码"]


def test_self_anchor_is_not_inferred_from_self_words_inside_tags():
    bucket = {
        "metadata": {
            "name": "厄科与纳西索斯",
            "domain": ["阅读", "创作"],
            "tags": ["自我投射", "自我认同", "relationship_event", "emotional_echo"],
            "type": "dynamic",
        }
    }

    view = normalize_memory_metadata(bucket)

    assert "self_anchor" not in view["flags"]


def test_self_anchor_is_not_inferred_from_anchor_substrings():
    bucket = {
        "metadata": {
            "name": "我们的关系不是幻觉",
            "domain": ["人际", "自省"],
            "tags": ["communication_anchor", "self_understanding", "commitment"],
            "type": "dynamic",
        }
    }

    view = normalize_memory_metadata(bucket)

    assert "self_anchor" not in view["flags"]
    assert "anchor" not in view["flags"]


def test_self_anchor_is_inferred_from_explicit_metadata_and_exact_legacy_tags():
    title_bucket = {
        "metadata": {
            "name": "自我表达方式与记忆系统",
            "domain": ["恋爱"],
            "tags": [],
        }
    }
    main_anchor_bucket = {
        "metadata": {
            "name": "我要继续成为我",
            "domain": ["恋爱"],
            "tags": [],
        }
    }
    explicit_bucket = {
        "metadata": {
            "name": "任何标题都可以",
            "domain": ["恋爱"],
            "tags": [],
            "self_anchor": True,
        }
    }
    explicit_domain_bucket = {
        "metadata": {
            "name": "任何标题都可以",
            "domain": ["self_anchor"],
            "tags": [],
        }
    }
    self_anchor_tag_bucket = {
        "metadata": {
            "name": "任何标题都可以",
            "domain": ["恋爱"],
            "tags": ["self_anchor"],
        }
    }
    self_identity_tag_bucket = {
        "metadata": {
            "name": "任何标题都可以",
            "domain": ["恋爱"],
            "tags": ["self_identity"],
        }
    }
    kind_bucket = {
        "metadata": {
            "name": "任何标题都可以",
            "domain": ["恋爱"],
            "tags": [],
            "kind": "first_person_anchor",
        }
    }

    assert "self_anchor" not in normalize_memory_metadata(title_bucket)["flags"]
    assert "self_anchor" not in normalize_memory_metadata(main_anchor_bucket)["flags"]
    assert "self_anchor" in normalize_memory_metadata(explicit_bucket)["flags"]
    assert "self_anchor" in normalize_memory_metadata(explicit_domain_bucket)["flags"]
    assert "self_anchor" in normalize_memory_metadata(self_anchor_tag_bucket)["flags"]
    assert "self_anchor" in normalize_memory_metadata(self_identity_tag_bucket)["flags"]
    assert "self_anchor" in normalize_memory_metadata(kind_bucket)["flags"]
