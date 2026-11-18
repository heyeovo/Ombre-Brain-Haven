import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent_wake_store import AgentWakeStore, GPT_WAKE_LANE
from bark_notifications import BarkNotificationStore
from gateway_state import GatewayStateStore
from gpt_chat_requests import GPTChatRequests


NOW = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)
SCOPE = {"profile_id": "default", "session_id": "gpt-window", "lane_id": GPT_WAKE_LANE}


@pytest.fixture
def window(tmp_path):
    gateway = GatewayStateStore(str(tmp_path / "state.db"))
    state = gateway.patch_conversation_session_state(
        profile_id=SCOPE["profile_id"], session_id=SCOPE["session_id"], persona_id="ombre",
        updates={"local_engine_preference": GPT_WAKE_LANE},
    )
    return gateway, state["state_version"]


def settings(gateway, **changes):
    return gateway.patch_agent_wake_schedule(**SCOPE, expected_version=None, changes=changes)


def receipt(gateway, request_id, *, at=NOW, wake_version=None, profile="default", action="claim"):
    body = {
        "action": action, "request_id": request_id, "session_id": SCOPE["session_id"],
        "fingerprint": "a" * 64, "owner": "b" * 32,
        **({"user_activity": True} if wake_version is None else {"wake": True, "expected_state_version": wake_version}),
    }
    with patch("gpt_chat_requests.datetime") as clock:
        clock.now.return_value = at
        return GPTChatRequests(gateway.db_path).control(profile, body)


def claim(store, at):
    return store.claim_due_schedule(owner="worker", now=at, gpt_enabled=True).get("run")


def begin(store, run, at):
    assert store.begin_run(wake_id=run["wake_id"], owner="worker", now=at)["status"] == "started"


def finish(store, run, status="completed", **kwargs):
    return store.finish_run(wake_id=run["wake_id"], owner="worker", status=status, **kwargs)


def test_thresholds_are_editable_and_consumed_once_until_a_new_user_send(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    schedule = settings(gateway, long_silence_enabled=True, long_silence_first_hours=1.5)
    assert schedule["long_silence_first_hours"] == 1.5 and schedule["long_silence_second_hours"] == 6
    assert not claim(store, NOW + timedelta(hours=10))
    assert receipt(gateway, "user-1")["ok"]
    assert receipt(gateway, "user-1", action="finish")["ok"]

    first_at = NOW + timedelta(hours=1.5)
    assert not store.claim_due_schedule(owner="worker", now=first_at)
    first = claim(store, first_at)
    assert first["cause"] == "long_silence_first"
    begin(store, first, first_at)
    assert receipt(gateway, first["wake_id"], at=first_at, wake_version=version)["ok"]
    assert not receipt(gateway, first["wake_id"], at=first_at, wake_version=version)["ok"]
    finish(store, first)
    receipt(gateway, first["wake_id"], action="finish")
    assert not claim(store, first_at + timedelta(minutes=1))

    second_at = NOW + timedelta(hours=6)
    assert not receipt(gateway, "user-1", at=second_at)["ok"]
    second = claim(AgentWakeStore(gateway.db_path), second_at)
    assert second["cause"] == "long_silence_second"
    begin(store, second, second_at)
    assert receipt(gateway, second["wake_id"], at=second_at, wake_version=version)["ok"]
    finish(store, second)
    receipt(gateway, second["wake_id"], action="finish")
    assert not claim(store, second_at + timedelta(minutes=1))
    assert receipt(gateway, "user-2", at=second_at)["ok"]
    assert claim(store, second_at + timedelta(hours=1.5))["cause"] == "long_silence_first"


def test_random_draws_survive_restart_defer_and_expire_at_local_midnight(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, random_wake_enabled=True)
    with patch("agent_wake_store.random.sample", return_value=[60, 120]) as draw:
        assert not claim(store, NOW)
        assert not claim(AgentWakeStore(gateway.db_path), NOW + timedelta(seconds=1))
        assert draw.call_count == 1
    first_at = NOW + timedelta(minutes=1)
    first = claim(store, first_at)
    assert first["cause"] == "random_wake_first"
    with patch("agent_wake_store._utc_now", return_value=first_at):
        finish(store, first, "deferred")
    assert not claim(store, first_at + timedelta(seconds=10))
    recovered_at = first_at + timedelta(seconds=30)
    first_again = claim(store, recovered_at)
    assert first_again["cause"] == first["cause"] and first_again["due_at"] == first["due_at"]
    begin(store, first_again, recovered_at)
    assert receipt(gateway, first_again["wake_id"], at=recovered_at, wake_version=version)["ok"]
    finish(store, first_again)
    assert not gateway.get_agent_wake_schedule(**SCOPE)["last_user_activity_at"]

    midnight = NOW + timedelta(hours=16)
    with patch("agent_wake_store.random.sample", return_value=[60, 120]):
        assert not claim(AgentWakeStore(gateway.db_path), midnight)
    current = gateway.get_agent_wake_schedule(**SCOPE)
    assert current["random_wake_day"] == "2026-11-20"
    assert current["random_wake_first_at"] == (midnight + timedelta(minutes=1)).isoformat()
    assert gateway.get_agent_wake_schedule(**{**SCOPE, "profile_id": "other"}) == {}


def test_stop_invalidates_admitted_wake_before_turn_and_bark_commit(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, random_wake_enabled=True, bark_notification_enabled=True)
    with patch("agent_wake_store.random.sample", return_value=[0, 60]):
        run = claim(store, NOW)
    begin(store, run, NOW)
    assert receipt(gateway, run["wake_id"], wake_version=version)["ok"]
    with gateway._connect() as conn:
        conn.execute("UPDATE gpt_chat_requests SET audit_json='{}' WHERE request_id=?", (run["wake_id"],))
    settings(gateway, long_silence_enabled=False, random_wake_enabled=False)

    with pytest.raises(ValueError, match="no longer active"):
        GPTChatRequests(gateway.db_path).audit_event("default", run["wake_id"], {"type": "request"})
    with pytest.raises(ValueError, match="no longer active"):
        gateway.commit_conversation_turn(
            **SCOPE, persona_id="ombre", request_id=run["wake_id"], expected_last_round_id=0,
            user_text="", assistant_text="wake", source=GPT_WAKE_LANE, turn_kind="agent_wake",
            agent_wake_update={"agent_wake": {"wake_id": run["wake_id"]}}, created_at=NOW,
        )
    with gateway._connect() as conn:
        assert conn.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0


def test_committed_gpt_wake_and_notification_are_idempotent(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, random_wake_enabled=True, bark_notification_enabled=True)
    BarkNotificationStore(gateway.db_path).save_config(profile_id="default", changes={
        "enabled": True, "server_url": "https://api.day.app", "device_key": "fake-test-key",
        "dashboard_base_url": "https://dashboard.example.com",
    })
    with patch("agent_wake_store.random.sample", return_value=[0, 60]):
        run = claim(store, NOW)
    begin(store, run, NOW)
    assert receipt(gateway, run["wake_id"], wake_version=version)["ok"]
    payload = dict(
        **SCOPE, persona_id="ombre", request_id=run["wake_id"], expected_last_round_id=0,
        user_text="", assistant_text="wake", source=GPT_WAKE_LANE, turn_kind="agent_wake",
        raw_json=json.dumps({"display_segments": {"version": 3,
            "segments": [{"kind": "text", "markdown": "wake"}]}}),
        agent_wake_update={"agent_wake": {"wake_id": run["wake_id"], "delivery": "quiet"}}, created_at=NOW,
    )
    saved = gateway.commit_conversation_turn(**payload)
    replay = gateway.commit_conversation_turn(**payload)
    assert replay["idempotent_replay"] and replay["turn"]["id"] == saved["turn"]["id"]
    assert store.get_run(run["wake_id"])["status"] == "completed"
    with gateway._connect() as conn:
        rows = conn.execute("SELECT title, level FROM notification_outbox").fetchall()
        assert [tuple(row) for row in rows] == [("GPT 主动消息", "passive")]


def test_interrupted_admitted_slot_is_not_rerun_after_lease_expiry(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, random_wake_enabled=True)
    with patch("agent_wake_store.random.sample", return_value=[0, 600]):
        first = claim(store, NOW)
    begin(store, first, NOW)
    assert receipt(gateway, first["wake_id"], wake_version=version)["ok"]
    restored = AgentWakeStore(gateway.db_path)
    assert not claim(restored, NOW + timedelta(minutes=3))
    assert restored.get_run(first["wake_id"])["error"] == "gpt_wake_interrupted"
    assert claim(restored, NOW + timedelta(minutes=10))["cause"] == "random_wake_second"


@pytest.mark.parametrize("kind", ["authentication", "pro_limit"])
def test_gpt_failure_pauses_future_opportunities(window, kind):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, random_wake_enabled=True)
    with patch("agent_wake_store.random.sample", return_value=[0, 60]):
        run = claim(store, NOW)
    begin(store, run, NOW)
    assert receipt(gateway, run["wake_id"], wake_version=version)["ok"]
    with patch("agent_wake_store._utc_now", return_value=NOW):
        finish(store, run, "failed", gpt_failure_kind=kind)
    receipt(gateway, run["wake_id"], action="finish")
    assert not claim(store, NOW + timedelta(minutes=59))
    later = claim(store, NOW + timedelta(hours=1))
    if kind == "pro_limit":
        assert later["cause"] == "random_wake_second"
    else:
        assert not later
        assert receipt(gateway, "next-user", at=NOW + timedelta(hours=1))["ok"]
        assert claim(store, NOW + timedelta(hours=1))["cause"] == "random_wake_second"
