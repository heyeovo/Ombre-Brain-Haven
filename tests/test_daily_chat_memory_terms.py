from reflection_engine import ReflectionEngine


def _no_api_config(test_config: dict) -> dict:
    test_config["dehydration"]["api_key"] = ""
    test_config["persona"]["api_key"] = ""
    test_config["reflection"] = {
        "enabled": True,
        "auto_enabled": False,
        "api_key": "",
        "base_url": "",
        "model": "",
        "daily_chat_memory_api_key_env": "__OMBRE_TEST_NO_DAILY_CHAT_KEY__",
        "daily_chat_memory_api_key": "",
    }
    return test_config


def test_daily_chat_memory_adds_semantic_terms_for_word_map(test_config):
    cfg = _no_api_config(test_config)
    engine = ReflectionEngine(cfg)

    candidates = engine._normalize_daily_chat_memory_candidates(
        "2026-07-07",
        [
            {
                "should_write": True,
                "kind": "project_state",
                "title": "默认改走脱水模型",
                "content": (
                    "Ombre 自动记忆链路默认不再使用本地 handoff 测试 key；"
                    "VPS 未显式配置 daily_chat_memory 模型时，summary 和候选抽取都回退到脱水模型。"
                ),
                "domain": "project",
                "tags": ["project_state", "daily_chat_extract"],
                "confidence": 0.9,
                "source_event_ids": [904],
            },
        ],
        [{"id": 1, "raw_event_ids": [904]}],
    )

    candidate = candidates[0]
    assert "from_daily_chat" in candidate["tags"]
    assert "daily_chat_extract" in candidate["tags"]
    assert "entity:Ombre-Brain" in candidate["tags"]
    assert "entity:VPS" in candidate["tags"]
    assert "topic:自动记忆" in candidate["tags"]
    assert "topic:脱水模型" in candidate["tags"]
    assert "自动记忆" in candidate["keywords"]
    assert "脱水模型" in candidate["keywords"]
