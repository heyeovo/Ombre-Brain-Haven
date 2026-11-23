import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent_wake_store import AgentWakeStore, GPT_WAKE_LANE, HEARTBEAT_DEFAULTS
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
    assert not claim(store, NOW + timedelta(hours=10))
    replied_at = NOW + timedelta(minutes=12)
    complete_user_reply(gateway, "user-1", replied_at)
    assert receipt(gateway, "user-1", action="finish")["ok"]

    first_at = replied_at + timedelta(hours=1.5)
    assert not store.claim_due_schedule(owner="worker", now=first_at)
    first = claim(store, first_at)
    assert first["cause"] == "long_silence_first"
    begin(store, first, first_at)
    assert receipt(gateway, first["wake_id"], at=first_at, wake_version=version)["ok"]
    assert not receipt(gateway, first["wake_id"], at=first_at, wake_version=version)["ok"]
    finish(store, first)
    receipt(gateway, first["wake_id"], action="finish")
    assert not claim(store, first_at + timedelta(minutes=1))

    second_at = replied_at + timedelta(hours=6)
    assert not receipt(gateway, "user-1", at=second_at)["ok"]
    second = claim(AgentWakeStore(gateway.db_path), second_at)
    assert second["cause"] == "long_silence_second"
    begin(store, second, second_at)
    assert receipt(gateway, second["wake_id"], at=second_at, wake_version=version)["ok"]
    finish(store, second)
    receipt(gateway, second["wake_id"], action="finish")
    assert not claim(store, second_at + timedelta(minutes=1))
    assert receipt(gateway, "user-2", at=second_at)["ok"]
    assert not claim(store, second_at + timedelta(hours=2))
    new_reply_at = second_at + timedelta(minutes=3)
    complete_user_reply(gateway, "user-2", new_reply_at, last_round=1)
    assert claim(store, new_reply_at + timedelta(hours=1.5))["cause"] == "long_silence_first"


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



def test_classic_gpt_wakes_share_scheduler_and_keep_long_silence_anchor(window):
    gateway, _ = window
    store = AgentWakeStore(gateway.db_path)
    settings(gateway, agent_wake_enabled=True, conversation_silence_enabled=True,
             silence_min_minutes=8, silence_max_minutes=8, long_silence_enabled=True)
    assert receipt(gateway, "foreground")["ok"]

    def at(minutes):
        return NOW + timedelta(minutes=minutes)

    gateway.commit_conversation_turn(
        **SCOPE, persona_id="ombre", request_id="foreground", expected_last_round_id=0,
        user_text="fixture", assistant_text="reply", source=GPT_WAKE_LANE,
        agent_wake_update={
            "sample_silence": True,
            "arm_long_silence": True,
            "wake_ops": [
                {"action": "schedule", "alarm_id": "w_000001", "at": at(4).isoformat()},
                {"action": "followup", "at": at(6).isoformat()},
                {"action": "schedule", "alarm_id": "w_000002", "at": at(20).isoformat()},
            ],
        }, created_at=at(1),
    )
    assert receipt(gateway, "foreground", action="finish")["ok"]

    for index, (when, cause) in enumerate(
        [(4, "agent_schedule"), (6, "agent_followup"), (9, "conversation_silence")], start=1
    ):
        run = claim(store, at(when))
        assert run["cause"] == cause
        begin(store, run, at(when))
        with gateway._connect() as conn:
            version = conn.execute("""
                SELECT state_version FROM conversation_sessions WHERE profile_id=? AND session_id=?
            """, (SCOPE["profile_id"], SCOPE["session_id"])).fetchone()[0]
        assert receipt(gateway, run["wake_id"], at=at(when), wake_version=version)["ok"]
        assert not receipt(gateway, run["wake_id"], at=at(when), wake_version=version)["ok"]
        gateway.commit_conversation_turn(
            **SCOPE, persona_id="ombre", request_id=run["wake_id"], expected_last_round_id=index,
            user_text="", assistant_text="wake", source=GPT_WAKE_LANE, turn_kind="agent_wake",
            agent_wake_update={"wake_cause": cause, "agent_wake": {"wake_id": run["wake_id"]}},
            created_at=at(when),
        )
        assert receipt(gateway, run["wake_id"], action="finish")["ok"]
        assert not claim(store, at(when))

    current = gateway.get_agent_wake_schedule(**SCOPE)
    assert current["last_user_activity_at"] == NOW.isoformat()
    assert current["long_silence_anchor_at"] == at(1).isoformat()
    assert current["followup_count"] == 1
    assert [alarm["alarm_id"] for alarm in current["alarms"]] == ["w_000002"]
    assert current["due_at"] == at(20).isoformat()


def test_gpt_user_admission_cancels_short_silence_and_followup_immediately(window):
    gateway, _ = window
    settings(gateway, agent_wake_enabled=True, conversation_silence_enabled=True)
    assert receipt(gateway, "first-user")["ok"]
    gateway.commit_conversation_turn(
        **SCOPE, persona_id="ombre", request_id="first-user", expected_last_round_id=0,
        user_text="fixture", assistant_text="reply", source=GPT_WAKE_LANE,
        agent_wake_update={"sample_silence": True, "wake_ops": [
            {"action": "followup", "at": (NOW + timedelta(minutes=5)).isoformat()},
        ]}, created_at=NOW,
    )
    receipt(gateway, "first-user", action="finish")
    assert receipt(gateway, "new-user", at=NOW + timedelta(minutes=1))["ok"]
    current = gateway.get_agent_wake_schedule(**SCOPE)
    assert current["followup_at"] == ""
    assert current["conversation_silence_check_at"] == ""



def complete_user_reply(gateway, request_id, at, *, last_round=0):
    gateway.commit_conversation_turn(
        **SCOPE, persona_id="ombre", request_id=request_id, expected_last_round_id=last_round,
        user_text="fixture", assistant_text="reply", source=GPT_WAKE_LANE, created_at=at,
        agent_wake_update={"arm_long_silence": True, "model_activity_at": at.isoformat()},
    )


def test_heartbeat_policy_is_configurable_with_sleep_intervals_and_daily_cap(window):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    policy = {**HEARTBEAT_DEFAULTS, "min_minutes": 96, "max_minutes": 100, "daily_limit": 1}
    config = settings(gateway, heartbeat_enabled=True, heartbeat_policy=policy)
    assert config["heartbeat_policy"] == policy
    with pytest.raises(ValueError):
        settings(gateway, heartbeat_policy={**policy, "sleep_start": "3:00"})

    sleep_start = NOW - timedelta(hours=5)
    with patch("agent_wake_store.random.randint", side_effect=lambda lower, _upper: lower) as draw:
        assert not claim(store, sleep_start)
        draw.assert_called_once_with(180, 240)
        first_at = sleep_start + timedelta(hours=3)
        run = claim(AgentWakeStore(gateway.db_path), first_at)
        assert run["cause"] == "agent_heartbeat"
        begin(store, run, first_at)
        assert receipt(gateway, run["wake_id"], at=first_at, wake_version=version)["ok"]
        assert not receipt(gateway, run["wake_id"], at=first_at, wake_version=version)["ok"]
        finish(store, run)
        next_due = gateway.get_agent_wake_schedule(**SCOPE)
        assert next_due["heartbeat_count"] == 1
        next_day = NOW + timedelta(hours=16)
        assert next_due["heartbeat_at"] == (next_day + timedelta(minutes=96)).isoformat()
        assert not claim(store, next_day - timedelta(seconds=1))
        assert claim(store, next_day + timedelta(minutes=96))["cause"] == "agent_heartbeat"


@pytest.mark.parametrize("neighbor", ["random", "alarm"])
def test_heartbeat_skips_nearby_wake_without_consuming_daily_count(window, neighbor):
    gateway, version = window
    store = AgentWakeStore(gateway.db_path)
    start = NOW + timedelta(hours=2)
    settings(gateway, heartbeat_enabled=True)
    if neighbor == "random":
        settings(gateway, random_wake_enabled=True)
    else:
        settings(gateway, agent_wake_enabled=True, next_agent_wake_at=(start + timedelta(minutes=120)).isoformat())
    with patch("agent_wake_store.random.sample", return_value=[7200, 14400]), patch(
        "agent_wake_store.random.randint", return_value=90
    ):
        assert not claim(store, start)
        first_at = start + timedelta(minutes=90)
        skipped = claim(store, first_at)
        assert skipped["cause"] == "agent_heartbeat"
        begin(store, skipped, first_at)
        assert receipt(gateway, skipped["wake_id"], at=first_at, wake_version=version)["code"] == "wake_superseded"
        finish(store, skipped, "superseded")
    status = gateway.get_agent_wake_schedule(**SCOPE)
    assert status["heartbeat_count"] == 0
    assert status["heartbeat_at"] == (start + timedelta(minutes=180)).isoformat()
    neighbor_run = claim(store, start + timedelta(minutes=120))
    assert neighbor_run["cause"] == ("random_wake_first" if neighbor == "random" else "agent_schedule")



@pytest.mark.parametrize("has_alarm", [False, True])
def test_migration_discards_only_the_old_user_send_based_deadline(window, has_alarm):
    gateway, _ = window
    old_due = (NOW + timedelta(hours=2)).isoformat()
    alarm_at = (NOW + timedelta(hours=12)).isoformat() if has_alarm else ""
    settings(gateway, long_silence_enabled=True, agent_wake_enabled=True, next_agent_wake_at=alarm_at)
    assert receipt(gateway, "before-upgrade")["ok"]
    with gateway._connect() as conn:
        conn.execute("ALTER TABLE agent_wake_schedules DROP COLUMN long_silence_anchor_at")
        conn.execute("UPDATE agent_wake_schedules SET due_at=?", (old_due,))

    restored = AgentWakeStore(gateway.db_path)
    assert not claim(restored, NOW + timedelta(hours=3))
    schedule = gateway.get_agent_wake_schedule(**SCOPE)
    assert schedule["due_at"] == alarm_at
    assert schedule["long_silence_anchor_at"] == ""
    assert AgentWakeStore(gateway.db_path).get_schedule(**SCOPE)["due_at"] == alarm_at
