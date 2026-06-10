from memory_relevance import (
    active_facets,
    content_terms_for_query,
    facets_for_node,
    facets_for_text,
    memory_relevance_options_from_config,
    query_has_explicit_entity_marker,
    recall_focus_query,
    recall_search_query,
    relevance_decision,
)


def test_ai_relationship_query_is_identity_not_intimacy():
    facets = facets_for_text("人机恋 / AI relationship")

    assert facets["relationship_identity"] > 0
    assert facets.get("intimacy", 0) == 0


def test_identity_query_suppresses_intimacy_candidate():
    decision = relevance_decision(
        "AI relationship",
        {
            "content": "A private sexual intimacy memory.",
            "metadata": {"importance": 10},
        },
    )

    assert decision.suppress


def test_non_sensitive_conflict_with_direct_evidence_is_demoted_not_suppressed():
    decision = relevance_decision(
        "给客户发邮件 email",
        {
            "content": "客户 hardware protocol note that mentions sending email to vendor.",
            "metadata": {"tags": ["hardware_protocol"], "importance": 10},
        },
    )

    assert not decision.suppress
    assert 0 < decision.multiplier < 1
    assert "communication_action_vs_hardware_protocol_demoted" in decision.reasons


def test_action_query_filters_hardware_protocol_without_direct_action_evidence():
    decision = relevance_decision(
        "小雨 发邮件",
        {
            "content": "BLE protocol note with notify char and device service UUID.",
            "metadata": {"importance": 10},
        },
    )

    assert decision.suppress
    assert "communication_action_vs_hardware_protocol" in decision.reasons


def test_explicit_intimacy_query_allows_intimacy_candidate():
    decision = relevance_decision(
        "亲密身体",
        {
            "content": "A private intimacy memory about body closeness.",
            "metadata": {"importance": 10},
        },
    )

    assert not decision.suppress
    assert decision.multiplier > 1


def test_config_aliases_blocked_facets_and_section_hints_extend_defaults():
    options = memory_relevance_options_from_config(
        {
            "memory_relevance": {
                "aliases": {"communication_action": ["工单回复"]},
                "blocked_facets": ["intimacy"],
                "section_hints": {"protocol_note": ["hardware_protocol"]},
            }
        }
    )

    query_facets = facets_for_text("工单回复", options)
    node_facets = facets_for_node({"section": "protocol_note", "text": ""}, options)

    assert "communication_action" in active_facets(query_facets)
    assert "hardware_protocol" in active_facets(node_facets)
    assert facets_for_text("亲密", options).get("intimacy", 0) == 0


def test_annotation_facets_drive_node_relevance_without_alias_text():
    decision = relevance_decision(
        "人机恋",
        {
            "text": "opaque remembered sentence",
            "metadata": {
                "annotation_facets": {"relationship_identity": 0.92},
                "evidence_spans": [{"facet": "relationship_identity", "text": "model evidence"}],
            },
        },
    )

    assert not decision.suppress
    assert decision.multiplier > 1
    assert "facet_overlap" in decision.reasons


def test_context_name_does_not_override_action_intent():
    options = memory_relevance_options_from_config(
        {"identity": {"ai_name": "Haven", "user_display_name": "小雨"}}
    )

    assert content_terms_for_query("小雨 发邮件", options) == ["发邮件"]
    assert recall_search_query("小雨 发邮件", options) == "发邮件"
    assert recall_search_query("小雨 蓝色", options) == "小雨 蓝色"

    missing_action = relevance_decision(
        "小雨 发邮件",
        {"text": "小雨说月亮时进入工作模式。", "metadata": {"importance": 10}},
        options,
    )
    email_action = relevance_decision(
        "小雨 发邮件",
        {"text": "QQ邮箱自动收发配置，可以给小雨发邮件。", "metadata": {"importance": 4}},
        options,
    )

    assert missing_action.multiplier < 1
    assert "communication_action_missing_demoted" in missing_action.reasons
    assert email_action.multiplier > 1
    assert "facet_overlap" in email_action.reasons


def test_associative_prompt_uses_quoted_focus_as_query_terms():
    assert recall_focus_query("如果我说“小狗”，你会想到什么") == "小狗"
    assert recall_focus_query("如果我说小狗，你会想到什么") == "小狗"
    assert recall_focus_query("小狗会想到什么") == "小狗"
    assert content_terms_for_query("如果我说“小狗”，你会想到什么") == ["小狗"]
    assert recall_focus_query("你会想到什么") == "你会想到什么"


def test_explicit_entity_marker_handles_titlecase_entities_without_sentence_starters():
    assert query_has_explicit_entity_marker("Titans")
    assert query_has_explicit_entity_marker("Tell me about Titans")
    assert query_has_explicit_entity_marker("Ombre 补的写入心脏")
    assert not query_has_explicit_entity_marker("Can you help me remember")
    assert not query_has_explicit_entity_marker("What should I do today")
