import ast
import hmac
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from device_event_store import DeviceEventStore, parse_event, derive_sessions, digest, timestamp

NOW = timestamp('2026-11-08T06:00:00+08:00')

def event(app='douyin', kind='opened', minute=0, **extra):
    return parse_event(dict(app_id=app, event=kind, occurred_at=(NOW + timedelta(minutes=minute)).isoformat(), **extra), NOW)


def test_lenient_parser():
    e = parse_event(dict(app_id='a', event='opened', battery_pct='0.58', occurred_at='2026-11-08T06:00:00', extra=True), NOW)
    assert e['battery_pct'] == 58 and e['app_name'] == 'a'
    assert e['occurred_at'].endswith('+08:00') and e['received_at'] == NOW.isoformat()
    assert parse_event(dict(app_id='a', event='closed', battery_pct='58'), NOW)['battery_pct'] == 58
    assert parse_event(dict(app_id='a', event='closed'), NOW)['occurred_at'] == NOW.isoformat()


@pytest.mark.parametrize('extra', [dict(event='bad'), dict(battery_pct='nan'), dict(source='bad'), dict(app_name='中'*1400)])
def test_invalid(extra):
    with pytest.raises(ValueError):
        parse_event(dict(app_id='a', event='opened') | extra)


def test_store_dedup_retention_and_existing_db(tmp_path):
    config = dict(state_dir=str(tmp_path))
    store = DeviceEventStore(config)
    raw = dict(app_id='a', event='opened', event_id='same')
    assert store.write({'events': [raw, raw]}, NOW) == dict(accepted=1, duplicates=1)
    assert store.write(dict(app_id='a', event='closed'), NOW)['accepted'] == 1
    assert store.write(dict(app_id='a', event='closed'), NOW)['accepted'] == 1
    assert len(DeviceEventStore(config).events()) == 3
    store.write(dict(app_id='b', event='opened'), NOW + timedelta(days=15))
    assert len(store.events()) == 1
    with pytest.raises(ValueError):
        store.write({'events': [raw]*51}, NOW)
    with pytest.raises(ValueError):
        store.write({'events': [raw, {}]}, NOW)
    assert len(store.events()) == 1


def test_sessions_pair_and_order():
    sessions = derive_sessions([event(kind='closed', minute=32), event()])
    assert sessions[0]['duration_seconds'] == 1920
    assert not sessions[0]['estimated']
    # A later real close takes precedence over switching apps.
    s = derive_sessions([event(), event('b', minute=2), event(kind='closed', minute=32)])
    assert next(x for x in s if x['app_id'] == 'douyin')['duration_seconds'] == 1920


def test_sessions_repeated_open_orphan_unclosed_estimate_cap():
    s = derive_sessions([event(), event(minute=2), event(kind='closed', minute=5)])
    assert len(s) == 1 and s[0]['duration_seconds'] == 300  # 半小时内重复打开接着算
    s = derive_sessions([event(), event(minute=31)])
    assert len(s) == 2 and all(x['duration_seconds'] is None for x in s)
    assert derive_sessions([event(kind='closed')])[0]['status'] == 'closed_only'
    assert derive_sessions([event()])[0]['status'] == 'unknown'
    s = derive_sessions([event(), event('b', minute=10)])
    inferred = next(x for x in s if x['app_id'] == 'douyin')
    assert inferred['estimated'] and inferred['duration_seconds'] == 600
    assert derive_sessions([event(), event(kind='closed', minute=241)])[0]['duration_seconds'] is None
    assert all(s['duration_seconds'] is None for s in derive_sessions([event(), event('b', minute=241)]))


def test_digest_three_modes():
    assert digest([], NOW) == ''
    crossing = digest([event(minute=-400), event(kind='closed', minute=-350)], NOW)
    assert '约 50 分钟' in crossing
    assert digest([event('微信', minute=-400)], NOW) == '[手机 最后记录 11-07 23:20 开了微信]'
    line = digest([event(minute=-32), event(kind='closed', battery_pct='58')], NOW)
    assert '05:28–06:00 douyin（约 32 分钟）' in line and '电量 58%' in line
    assert len(derive_sessions([event(str(i), minute=-i) for i in range(5)])) == 5
    assert digest([event(str(i), minute=-i) for i in range(5)], NOW).count(' · ') == 2


def functions(names, store):
    source = Path(__file__).parents[1] / 'server.py'
    nodes = [n for n in ast.parse(source.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    ns = dict(os=os, config={}, hmac=hmac, _json_lib=json, device_event_store=store,
              derive_sessions=derive_sessions, digest=digest,
              _bearer_token=lambda headers: headers.get('authorization', '').removeprefix('Bearer '))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)
    return ns


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['api_device_events', 'api_device_events_digest'])
async def test_routes_require_gateway_bearer(tmp_path, monkeypatch, name):
    monkeypatch.setenv('OMBRE_GATEWAY_TOKEN', 'gateway')
    ns = functions({'_require_room_bearer', name}, DeviceEventStore(dict(state_dir=str(tmp_path))))
    assert (await ns[name](SimpleNamespace(headers={}))).status_code == 401


@pytest.mark.asyncio
async def test_routes_write_read_digest(tmp_path, monkeypatch):
    monkeypatch.setenv('OMBRE_GATEWAY_TOKEN', 'gateway')
    store = DeviceEventStore(dict(state_dir=str(tmp_path)))
    ns = functions({'_require_room_bearer', 'api_device_events', 'api_device_events_digest'}, store)
    async def stream():
        yield b'{"app_id":"a","event":"opened"}'
    req = SimpleNamespace(headers={'authorization': 'Bearer gateway'}, method='POST', stream=stream)
    assert json.loads((await ns['api_device_events'](req)).body)['accepted'] == 1
    req.method, req.query_params = 'GET', {}
    assert len(json.loads((await ns['api_device_events'](req)).body)['events']) == 1
    assert '手机' in json.loads((await ns['api_device_events_digest'](req)).body)['digest']
    assert '关闭事件可能漏记' in store.activity()


def test_battery_one_is_one_percent_and_bad_time_falls_back():
    e = parse_event(dict(app_id='a', event='closed', battery_pct=1, occurred_at='2026年11月8日 05:58'), NOW)
    assert e['battery_pct'] == 1
    assert e['occurred_at'] == e['received_at']


def test_activity_now_line_merged_details_and_today_totals(tmp_path):
    from device_event_store import day_start
    store = DeviceEventStore({'state_dir': str(tmp_path)})
    now = timestamp('2026-11-08T23:40:00+08:00')
    rows = [('home', '我们家', 'opened', '23:00:00'), ('home', '我们家', 'closed', '23:10:00'),
            ('home', '我们家', 'opened', '23:10:20'), ('home', '我们家', 'closed', '23:10:40'),
            ('home', '我们家', 'opened', '23:11:00'), ('home', '我们家', 'closed', '23:15:00'),
            ('douyin', '抖音', 'opened', '23:28:00'), ('douyin', '抖音', 'closed', '23:30:00'),
            ('home', '我们家', 'opened', '23:30:05')]
    store.write({'events': [dict(app_id=a, app_name=n, event=e, occurred_at=f'2026-11-08T{t}+08:00', battery_pct=36)
                            for a, n, e, t in rows]}, now)
    store.write(dict(app_id='douyin', app_name='抖音', event='opened', occurred_at='2026-11-08T03:30:00+08:00'), now)
    text = store.activity(1, now)
    lines = text.splitlines()
    assert lines[0] == '现在：在我们家（23:30 打开，已经 10 分）'
    assert [l for l in lines if '23:15 我们家' in l] == ['- 23:00–23:15 我们家（约 14 分钟）']  # 三段碎记录并成一段
    assert '今天（4 点起）：我们家 14 分 · 抖音 2 分' in text
    assert '电量：36%' in text
    assert day_start(timestamp('2026-11-09T03:59:00+08:00')).day == 8
