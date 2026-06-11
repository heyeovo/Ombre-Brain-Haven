from memory_relevance import (
    active_facets,
    content_terms_for_query,
    emotional_recall_plan,
    emotional_recall_terms,
    facets_for_node,
    facets_for_text,
    memory_relevance_options_from_config,
    query_has_explicit_entity_marker,
    recall_focus_query,
    recall_search_query,
    recall_topic_query,
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
            "content": "A private intimacy boundary note.",
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
        "用户 发邮件",
        {
            "content": "BLE protocol note with notify char and device service UUID.",
            "metadata": {"importance": 10},
        },
    )

    assert decision.suppress
    assert "communication_action_vs_hardware_protocol" in decision.reasons


def test_explicit_intimacy_query_allows_intimacy_candidate():
    decision = relevance_decision(
        "intimacy",
        {
            "content": "An intimacy memory about closeness.",
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
        {"identity": {"ai_name": "TestAI", "user_display_name": "用户"}}
    )

    assert content_terms_for_query("用户 发邮件", options) == ["发邮件"]
    assert recall_search_query("用户 发邮件", options) == "发邮件"
    assert recall_search_query("用户 蓝色", options) == "用户 蓝色"

    missing_action = relevance_decision(
        "用户 发邮件",
        {"text": "用户说月亮时进入工作模式。", "metadata": {"importance": 10}},
        options,
    )
    email_action = relevance_decision(
        "用户 发邮件",
        {"text": "QQ邮箱自动收发配置，可以给用户发邮件。", "metadata": {"importance": 4}},
        options,
    )

    assert missing_action.multiplier < 1
    assert "communication_action_missing_demoted" in missing_action.reasons
    assert email_action.multiplier > 1
    assert "facet_overlap" in email_action.reasons


def test_career_query_filters_unrelated_sleep_memory():
    sleep_memory = relevance_decision(
        "找工作 工作 面试",
        {
            "text": "凌晨一点五十二分，用户还在调试工作流，AI提醒她睡觉她嘴上答应手没停。",
            "metadata": {"bucket_name": "用户熬夜调试 2026-06-10"},
        },
    )
    career_memory = relevance_decision(
        "找工作 工作 面试",
        {
            "text": "用户在找工作，收到AI算法专家岗位，沟通后发现是实施而非研发。",
            "metadata": {"bucket_name": "用户求职分析", "bucket_tags": ["求职"]},
        },
    )

    assert sleep_memory.suppress
    assert sleep_memory.multiplier == 0
    assert "career_missing" in sleep_memory.reasons
    assert career_memory.multiplier > 1
    assert "facet_overlap" in career_memory.reasons


def test_work_alone_does_not_trigger_career_facet():
    assert active_facets(facets_for_text("工作模式")) == set()
    assert "career" in active_facets(facets_for_text("找工作 工作 面试"))


def test_associative_prompt_uses_quoted_focus_as_query_terms():
    assert recall_focus_query("如果我说“小狗”，你会想到什么") == "小狗"
    assert recall_focus_query("如果我说小狗，你会想到什么") == "小狗"
    assert recall_focus_query("小狗会想到什么") == "小狗"
    assert content_terms_for_query("如果我说“小狗”，你会想到什么") == ["小狗"]
    assert recall_search_query("如果我说“小狗”，你会想到什么") == "小狗"
    assert recall_search_query("如果我说小狗，你会想到什么") == "小狗"
    assert recall_search_query("小狗会想到什么") == "小狗"
    assert recall_focus_query("再来一次！记得“小狗”那次吗") == "小狗"
    assert recall_search_query("再来一次！记得“小狗”那次吗") == "小狗"
    assert content_terms_for_query("再来一次！记得“小狗”那次吗") == ["小狗"]
    assert recall_focus_query("你会想到什么") == ""
    assert content_terms_for_query("你会想到什么") == []
    assert recall_search_query("你会想到什么") == ""


def test_associative_prompt_uses_identity_user_terms_not_hardcoded_names():
    options = memory_relevance_options_from_config(
        {
            "identity": {
                "ai_name": "Lapis",
                "user_name": "Nina",
                "user_display_name": "妮娜",
                "user_aliases": ["对方"],
            }
        }
    )

    assert recall_focus_query("如果Nina说蓝色，你会想到什么", options) == "蓝色"
    assert recall_focus_query("如果妮娜提到流星，你会想到什么", options) == "流星"
    assert recall_focus_query("如果对方问到小狗，你会想到什么", options) == "小狗"


def test_query_terms_combine_jieba_and_regex_tokens():
    terms = content_terms_for_query("小狗设定")

    assert "小狗" in terms
    assert "小狗设定" in terms


def test_query_terms_filter_jieba_filler_words():
    terms = content_terms_for_query("那你知道我今天为什么激动哭了吗")

    assert "激动" in terms
    assert "知道" not in terms
    assert "今天" not in terms
    assert "为什么" not in terms

    memory_terms = content_terms_for_query("记忆工具跑通那次")
    assert "工具" in memory_terms
    assert "跑通" in memory_terms
    assert "那次" not in memory_terms


def test_query_topic_terms_strip_shell_words_and_keep_single_cjk_topic():
    for query in [
        "猫",
        "关于猫",
        "关于猫的",
        "我的猫",
        "我家的猫",
        "嗯…我没发图片啊，突然想起我家的猫",
        "再试试，不准搜记忆库，还记得我的猫吗",
        "测试一下，还记得我的猫吗",
    ]:
        assert recall_topic_query(query) == "猫"
        assert content_terms_for_query(query) == ["猫"]

    assert recall_topic_query("那狗呢") == "狗"
    assert content_terms_for_query("那狗呢") == ["狗"]


def test_query_topic_terms_strip_rephrase_shell_words():
    assert recall_topic_query("嗯...换种说法，还记得猫猫吗") == "猫猫"
    assert content_terms_for_query("嗯...换种说法，还记得猫猫吗") == ["猫猫", "猫"]

    assert recall_topic_query("再换种说法，还记得猫咪吗") == "猫咪"
    assert content_terms_for_query("再换种说法，还记得猫咪吗") == ["猫咪"]


def test_duplicated_cjk_query_terms_fold_only_safe_topic_nicknames():
    assert content_terms_for_query("狗狗") == ["狗狗", "狗"]
    assert content_terms_for_query("鸟鸟") == ["鸟鸟", "鸟"]
    assert recall_topic_query("哭哭") == ""
    assert content_terms_for_query("哭哭") == []
    assert content_terms_for_query("妈妈") == ["妈妈"]
    assert content_terms_for_query("伙伴") == ["伙伴"]


def test_query_topic_terms_drop_reactions_and_probe_only_words():
    for query in [
        "哈哈",
        "呵呵",
        "哈",
        "呜呜",
        "嗯",
        "啊",
        "哭哭",
        "测试",
        "试试",
        "测试一下",
        "试一下",
    ]:
        assert recall_topic_query(query) == ""
        assert content_terms_for_query(query) == []

    assert recall_topic_query("今天为什么焦虑哭了吗") == "焦虑哭"
    assert content_terms_for_query("今天为什么焦虑哭了吗") == ["焦虑哭", "焦虑", "哭"]


def test_compound_recall_terms_keep_individual_anchors():
    terms = content_terms_for_query("蓝鲸档案和风铃计划")
    wrapped_terms = content_terms_for_query("唉......期望召回的是蓝鲸档案和风铃计划......")

    assert "蓝鲸档案" in terms
    assert "风铃计划" in terms
    assert "蓝鲸档案" in wrapped_terms
    assert "风铃计划" in wrapped_terms


def test_emotional_recall_plan_builds_state_and_event_anchors():
    crying = emotional_recall_plan("那你知道我今天为什么激动哭了吗")
    event = emotional_recall_plan("被妈妈说得委屈")
    sleep = emotional_recall_plan("难过到睡不着")

    assert crying.triggered
    assert crying.strong_terms[0] == "激动哭"
    assert "激动" in crying.weak_terms
    assert event.triggered
    assert "妈妈" in event.event_terms
    assert "委屈" in event.weak_terms
    assert sleep.strong_terms[0] == "难过睡不着"


def test_emotional_recall_plan_keeps_bare_emotions_weak():
    assert not emotional_recall_plan("开心").triggered
    assert not emotional_recall_plan("哭").triggered
    assert emotional_recall_terms("今天怎么破防了") == ["破防"]


def test_explicit_entity_marker_handles_titlecase_entities_without_sentence_starters():
    assert query_has_explicit_entity_marker("Titans")
    assert query_has_explicit_entity_marker("Tell me about Titans")
    assert query_has_explicit_entity_marker("Ombre 补的写入心脏")
    assert not query_has_explicit_entity_marker("Can you help me remember")
    assert not query_has_explicit_entity_marker("What should I do today")
