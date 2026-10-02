import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent_wake_store import AgentWakeStore, initialize_agent_wake_schema
from gateway_state import GatewayStateStore, RequestIdReuseError


SCOPE = dict(profile_id='default', session_id='multi', lane_id='subscription')
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    store = GatewayStateStore(str(tmp_path / 'state.db'))
    initial = store.get_agent_wake_schedule(**SCOPE, create=True)
    store.patch_agent_wake_schedule(**SCOPE, expected_version=initial['schedule_version'],
                                    changes={'agent_wake_enabled': True})
    return store


def alarm(number, minutes):
    return dict(action='schedule', alarm_id=f'w_{number:06x}',
                at=(NOW + timedelta(minutes=minutes)).isoformat(), reason=f'闹钟{number}')


def commit(store, ops, round_id=0, **update):
    return store.commit_conversation_turn(
        **SCOPE, persona_id='ombre', request_id=f'turn-{round_id}', expected_last_round_id=round_id,
        user_text='hello' if not update else '', assistant_text='done', source='cc',
        turn_kind='agent_wake' if update else 'user',
        agent_wake_update={'wake_ops': ops, **update}, created_at=NOW)


def schedule(store):
    return store.get_agent_wake_schedule(**SCOPE)


def test_legacy_migration_is_idempotent_and_scope_safe(store):
    conn = store._connect()
    conn.execute("UPDATE agent_wake_schedules SET next_agent_wake_at = ?, wake_reason = '旧闹钟'",
                 ((NOW + timedelta(minutes=10)).isoformat(timespec='seconds'),))
    initialize_agent_wake_schema(conn)
    first = conn.execute('SELECT * FROM agent_wake_alarms').fetchone()
    initialize_agent_wake_schema(conn)
    assert conn.execute('SELECT count(*) FROM agent_wake_alarms').fetchone()[0] == 1
    assert first['alarm_id'].startswith('w_') and len(first['alarm_id']) == 8
    assert first['reason'] == '旧闹钟'
    conn.commit()
    conn.close()
    assert len(AgentWakeStore(store.db_path).get_schedule(**SCOPE)['alarms']) == 1
    assert store.get_agent_wake_schedule(**{**SCOPE, 'profile_id': 'other'}) == {}


def test_ordered_ops_mirror_cancel_and_followup(store):
    commit(store, [alarm(1, 30), alarm(2, 10), dict(action='followup', at=alarm(3, 20)['at']),
                   dict(action='cancel', alarm_id='w_000001')])
    value = schedule(store)
    assert [item['alarm_id'] for item in value['alarms']] == ['w_000002']
    assert value['next_agent_wake_at'] == value['alarms'][0]['at']
    assert value['wake_reason'] == '闹钟2' and value['followup_at']
    accepted = store.accept_user_activity_and_cancel_silence(**SCOPE, user_activity_at=NOW)
    assert accepted['alarms'] == value['alarms']
    commit(store, [dict(action='cancel', alarm_id='w_absent'), alarm(3, 5)], 1)
    assert schedule(store)['wake_reason'] == '闹钟3'
    commit(store, [dict(action='cancel')], 2)
    assert schedule(store)['alarms'] == []
    assert schedule(store)['next_agent_wake_at'] == schedule(store)['wake_reason'] == schedule(store)['followup_at'] == ''


def test_old_claimed_run_gets_migration_snapshot_and_is_consumed(store):
    due = (NOW - timedelta(minutes=1)).isoformat(timespec='seconds')
    conn = store._connect()
    conn.execute("UPDATE agent_wake_schedules SET next_agent_wake_at = ?, wake_reason = '旧闹钟'", (due,))
    conn.execute('DROP TABLE agent_wake_runs')
    conn.execute('''CREATE TABLE agent_wake_runs (wake_id TEXT PRIMARY KEY, profile_id TEXT,
        session_id TEXT, lane_id TEXT, cause TEXT, due_at TEXT, status TEXT, schedule_version INTEGER)''')
    version = schedule(store)['schedule_version']
    conn.execute("INSERT INTO agent_wake_runs VALUES ('wake_old', 'default', 'multi', 'subscription', 'agent_schedule', ?, 'claimed', ?)", (due, version))
    initialize_agent_wake_schema(conn)
    conn.commit()
    conn.close()
    control = AgentWakeStore(store.db_path)
    recovered = control.claim_due_schedule(owner='worker', now=NOW)
    assert recovered['recovered'] and recovered['run']['wake_id'] == 'wake_old'
    assert recovered['run']['fired_alarm_ids'] == [schedule(store)['alarms'][0]['alarm_id']]
    assert recovered['run']['reason'] == '旧闹钟'
    commit(store, [], wake_cause='agent_schedule', agent_wake={'wake_id': 'wake_old', 'at': due})
    assert schedule(store)['alarms'] == []


def test_cap_keeps_earliest_five_and_disabled_ignores_schedule(store):
    commit(store, [alarm(i, 70-i*10) for i in range(1, 7)])
    assert [item['reason'] for item in schedule(store)['alarms']] == ['闹钟6', '闹钟5', '闹钟4', '闹钟3', '闹钟2']
    store.patch_agent_wake_schedule(**SCOPE, expected_version=schedule(store)['schedule_version'],
                                    changes={'agent_wake_enabled': False, 'next_agent_wake_at': ''})
    commit(store, [alarm(9, 10)], 1)
    assert schedule(store)['alarms'] == []


def test_claim_merges_due_alarms_recovers_snapshot_and_consumes_only_fired(store):
    commit(store, [alarm(1, -1), alarm(2, -1), alarm(3, 10)])
    control = AgentWakeStore(store.db_path)
    claimed = control.claim_due_schedule(owner='worker', now=NOW)
    run = claimed['run']
    assert run['fired_alarm_ids'] == ['w_000001', 'w_000002']
    assert run['reason'] == '闹钟1；闹钟2'
    assert [item['alarm_id'] for item in run['pending_alarms']] == ['w_000003']
    assert control.claim_due_schedule(owner='second', now=NOW) == {}
    recovered = control.claim_due_schedule(owner='new', now=NOW + timedelta(minutes=11))
    assert recovered['recovered'] and recovered['run']['fired_alarm_ids'] == run['fired_alarm_ids']
    assert recovered['run']['pending_alarms'] == run['pending_alarms']
    commit(store, [alarm(4, 20)], 1, wake_cause='agent_schedule',
           agent_wake={'wake_id': run['wake_id'], 'at': run['due_at']})
    assert [item['alarm_id'] for item in schedule(store)['alarms']] == ['w_000003', 'w_000004']
    assert schedule(store)['wake_reason'] == '闹钟3'


@pytest.mark.parametrize('status', ['failed', 'deferred'])
def test_failed_or_deferred_run_preserves_alarms(store, status):
    commit(store, [alarm(1, -1), alarm(2, 10)])
    control = AgentWakeStore(store.db_path)
    claimed = control.claim_due_schedule(owner='worker', now=NOW)
    control.finish_run(wake_id=claimed['run']['wake_id'], owner='worker', status=status)
    assert len(schedule(store)['alarms']) == 2


def test_new_alarm_invalidates_old_run_version(store):
    commit(store, [alarm(1, -1)])
    control = AgentWakeStore(store.db_path)
    claimed = control.claim_due_schedule(owner='worker', now=NOW)
    commit(store, [alarm(2, 10)], 1)
    result = control.begin_run(wake_id=claimed['run']['wake_id'], owner='worker', now=NOW)
    assert result['status'] == 'superseded'
    assert len(schedule(store)['alarms']) == 2


def test_failed_turn_rolls_back_ops_and_replay_does_not_duplicate(store):
    committed = commit(store, [alarm(1, 10)])
    assert commit(store, [alarm(1, 10)])['idempotent_replay']
    assert len(schedule(store)['alarms']) == 1
    with pytest.raises(RequestIdReuseError):
        commit(store, [alarm(2, 20)], 0)
    assert len(schedule(store)['alarms']) == 1
    # Fail after applying the ops but before committing the transaction.
    apply = store._apply_agent_wake_turn_update
    def fail_after_apply(*args, **kwargs):
        apply(*args, **kwargs)
        raise RuntimeError('late failure')
    with patch.object(store, '_apply_agent_wake_turn_update', side_effect=fail_after_apply):
        with pytest.raises(RuntimeError, match='late failure'):
            commit(store, [alarm(2, 20)], 1)
    assert len(schedule(store)['alarms']) == 1
    conn = store._connect()
    raw = json.loads(conn.execute('SELECT raw_json FROM conversation_turns WHERE id = ?', (committed['turn']['id'],)).fetchone()[0])
    conn.close()
    assert raw['wake_ops'] == [{key: alarm(1, 10)[key] for key in ('action', 'at', 'reason')}]
    assert 'next_wake' not in raw


def test_patch_replaces_or_clears_and_soft_delete_is_scoped(store):
    commit(store, [alarm(1, 10), alarm(2, 20)])
    value = store.patch_agent_wake_schedule(**SCOPE, expected_version=schedule(store)['schedule_version'],
                                           changes={'next_agent_wake_at': alarm(3, 30)['at'], 'wake_reason': '替换'})
    assert len(value['alarms']) == 1 and value['wake_reason'] == '替换'
    value = store.patch_agent_wake_schedule(**SCOPE, expected_version=value['schedule_version'],
                                           changes={'next_agent_wake_at': ''})
    assert value['alarms'] == [] and value['wake_reason'] == ''
    commit(store, [alarm(4, 10)], 1)
    control = AgentWakeStore(store.db_path)
    control.create_schedule(**{**SCOPE, 'profile_id': 'other'}, next_agent_wake_at=alarm(5, 10)['at'])
    store.soft_delete_conversation_session(profile_id='default', session_id='multi')
    assert control.get_schedule(**SCOPE) == {}
    assert len(control.get_schedule(**{**SCOPE, 'profile_id': 'other'})['alarms']) == 1
