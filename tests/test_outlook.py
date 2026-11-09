import asyncio
import json
from pathlib import Path

import httpx
import pytest

from outlook_store import GRAPH, MISSING, REAUTH, OutlookError, OutlookStore, graph_time, parse_time


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTLOOK_CLIENT_ID', 'test-client')
    return OutlookStore({'state_dir': str(tmp_path)})


def mock(store, handler):
    store.transport = httpx.MockTransport(handler)


def reply(data=None, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers) if status != 204 else httpx.Response(204)


def authorize(store, expires=3600):
    store.save_token({'access_token': 'access-secret', 'refresh_token': 'refresh-secret', 'expires_in': expires}, account='she@example.com')


@pytest.mark.asyncio
async def test_poll_pending_slow_success_duplicate(store):
    calls, intervals = [], []
    gate = asyncio.Event()
    responses = iter([{'error': 'authorization_pending'}, {'error': 'slow_down'},
                      {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}])
    async def sleep(delay):
        intervals.append(delay)
        await gate.wait()
    store.sleep = sleep
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/devicecode'):
            return reply(dict(device_code='private-code', user_code='ABCD', verification_uri='https://microsoft.com/devicelogin', expires_in=900, interval=1, message='登录'))
        if request.url.path.endswith('/me'):
            return reply({'mail': 'she@example.com'})
        data = next(responses)
        return reply(data, 400 if 'error' in data else 200)
    mock(store, handler)
    first, second = await asyncio.gather(store.start_auth(), store.start_auth())
    assert first == second and 'device_code' not in first
    assert sum(x.endswith('/devicecode') for x in calls) == 1
    task = store.auth_task
    gate.set()
    await task
    assert intervals == [1, 1, 6]
    assert store.token()['refresh_token'] == 'new-refresh'
    assert store.token()['account'] == 'she@example.com'
    assert store.pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error,word', [('expired_token', '过期'), ('authorization_declined', '拒绝')])
async def test_poll_ends(store, error, word):
    async def sleep(_):
        pass
    store.sleep = sleep
    mock(store, lambda r: reply({'error': error}, 400))
    store.pending = {'expires_at': store.clock()+100}
    await store.poll_auth({'device_code': 'd', 'expires_at': store.clock()+100}, 'test')
    assert store.pending is None and word in store.meta('last_error') and store.token() is None


@pytest.mark.asyncio
async def test_local_expiry(store):
    now = [100]
    store.clock = lambda: now[0]
    async def sleep(seconds):
        now[0] += seconds
    store.sleep = sleep
    mock(store, lambda r: pytest.fail('expired code must not poll'))
    await store.poll_auth({'device_code': 'd', 'expires_at': 102}, 'test')
    assert '过期' in store.meta('last_error')


@pytest.mark.asyncio
async def test_refresh_rotation_concurrent_invalid_grant(store):
    authorize(store, 100)
    calls = []
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)  # overlap calls while the first refresh holds the lock
        assert 'refresh-secret' in request.content.decode()
        return reply({'access_token': 'rotated', 'refresh_token': 'rotated-refresh', 'expires_in': 3600})
    mock(store, handler)
    assert await asyncio.gather(store.access_token(), store.access_token()) == ['rotated', 'rotated']
    assert len(calls) == 1 and store.token()['refresh_token'] == 'rotated-refresh'
    authorize(store, 1)
    mock(store, lambda r: reply({'error': 'invalid_grant'}, 400))
    with pytest.raises(OutlookError, match=REAUTH):
        await store.access_token()
    assert store.token() is None and store.meta('last_error') == REAUTH


@pytest.mark.asyncio
async def test_status_disconnect_no_secrets(store):
    authorize(store)
    mock(store, lambda r: reply({'mail': 'she@example.com'}))
    store.pending = {'device_code': 'device-secret', 'expires_at': 123}
    status = await store.status()
    assert status['connected'] and status['account'] == 'she@example.com'
    assert status['pending'] == {'expires_at': 123}
    assert all(x not in json.dumps(status) for x in ('access-secret', 'refresh-secret', 'device-secret'))
    store.meta('calendar_id', 'old')
    store.short_id('calendar', 'event', 'old')
    await store.disconnect()
    assert store.token() is None and not store.meta('calendar_id')
    assert store.resolve('calendar', 'event')['parent_id'] == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,root,field,key', [('calendar','/me/calendars','name','calendar_id'), ('reminders','/me/todo/lists','displayName','todo_list_id')])
async def test_owned_create_reuse_deleted_cache(store, kind, root, field, key):
    authorize(store)
    state, posts = [], []
    def handler(r):
        path = r.url.path.removeprefix('/v1.0')
        if r.method == 'POST':
            assert json.loads(r.content) == {field: '言之'}
            item = {'id': 'own'+str(len(posts)), field: '言之'}
            posts.append(item)
            state.append(item)
            return reply(item, 201)
        if path == root:
            return reply({'value': state})
        item = next((x for x in state if path.endswith('/'+x['id'])), None)
        return reply(item or {'error': {'message': 'deleted'}}, 200 if item else 404)
    mock(store, handler)
    assert await store.owned(kind) == 'own0'
    assert await store.owned(kind) == 'own0' and len(posts) == 1
    store.meta(key, '')
    assert await store.owned(kind) == 'own0' and len(posts) == 1
    state.clear()
    assert await store.owned(kind) == 'own1' and len(posts) == 2


@pytest.mark.asyncio
async def test_create_only_owned_and_times(store):
    authorize(store)
    writes = []
    def handler(r):
        path = r.url.path
        assert r.headers['Prefer'] == 'outlook.timezone="China Standard Time"'
        if r.method == 'GET':
            return reply({'value': [{'id': 'own', 'name': '言之', 'displayName': '言之'}]})
        writes.append((path, json.loads(r.content)))
        return reply({'id': 'new-event' if 'events' in path else 'new-task'}, 201)
    mock(store, handler)
    await store.calendar('create', title='日程', start='2026-11-10 09:00')
    path, body = writes[-1]
    assert path == '/v1.0/me/calendars/own/events'
    assert body['end']['dateTime'] == '2026-11-10T10:00:00'
    assert body['start']['timeZone'] == 'China Standard Time'
    assert body['isReminderOn'] and body['reminderMinutesBeforeStart'] == 15
    await store.calendar('create', title='全天', start='2026-11-10', all_day=True, remind_minutes=-1)
    assert writes[-1][1]['end']['dateTime'] == '2026-11-11T00:00:00'
    assert writes[-1][1]['isReminderOn'] is False
    await store.reminders('create', title='提醒', remind_at='2026-11-10T01:00:00Z', due='2026-11-10', notes='记事')
    path, body = writes[-1]
    assert path == '/v1.0/me/todo/lists/own/tasks'
    assert body['isReminderOn'] and body['reminderDateTime'] == graph_time('2026-11-10 09:00')
    assert body['dueDateTime'] == graph_time('2026-11-10')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,action', [('calendar','update'),('calendar','delete'),('reminders','update'),('reminders','delete'),('reminders','complete')])
async def test_foreign_guard_full_and_short_id(store, kind, action):
    authorize(store)
    writes = []
    def handler(r):
        path = r.url.path
        if r.method != 'GET':
            writes.append(r)
            return reply({'id':'long-id'}) if r.method == 'PATCH' else reply(status=204)
        if path.endswith('/calendar'):
            return reply({'id':'foreign','name':'小羊'})
        if path.endswith('/tasks/long-id'):
            return reply({'id':'long-id'})
        if path.endswith('/calendars'):
            return reply({'value':[{'id':'own','name':'言之'}]})
        if path.endswith('/lists'):
            return reply({'value':[{'id':'foreign','displayName':'小羊'}, {'id':'own','displayName':'言之'}]})
        return reply({'id':'own','name':'言之','displayName':'言之'})
    mock(store, handler)
    code = store.short_id(kind, 'long-id', 'own')  # deliberately stale parent must not bypass guard
    for value in ('long-id', code):
        result = await store.tool(kind, action, id=value, title='改')
        assert '先问她' in result and not writes
    result = await store.tool(kind, action, id=code, title='改', confirmed=True)
    assert '已' in result and len(writes) == 1
    if action == 'complete':
        assert json.loads(writes[0].content)['status'] == 'completed'


def test_ids_dates_init_migration(store):
    code = store.short_id('calendar', 'long', 'parent')
    assert len(code) == 8
    assert store.resolve('calendar', code)['full_id'] == 'long'
    assert store.resolve('calendar', 'long')['parent_id'] == 'parent'
    store.short_id('calendar', 'long', 'moved')
    assert store.resolve('calendar', code)['parent_id'] == 'moved'
    assert store.resolve('reminders', code)['full_id'] == code
    # An existing unrelated table survives repeat initialization (old state directory).
    with store.db() as db:
        db.execute('CREATE TABLE legacy (value TEXT)')
        db.execute("INSERT INTO legacy VALUES ('keep')")
    again = OutlookStore({'state_dir': str(Path(store.db_path).parent)})
    OutlookStore({'state_dir': str(Path(store.db_path).parent)})
    assert again.resolve('calendar', code)['full_id'] == 'long'
    with again.db() as db:
        assert db.execute('SELECT value FROM legacy').fetchone()[0] == 'keep'
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    assert parse_time('2026-11-10T01:00:00Z') == parse_time('2026-11-10 09:00')
    assert graph_time('2026-11-10')['dateTime'] == '2026-11-10T00:00:00'
    with pytest.raises(OutlookError):
        parse_time('wrong')


@pytest.mark.asyncio
async def test_missing_config_all_entries(store, monkeypatch):
    monkeypatch.delenv('OUTLOOK_CLIENT_ID')
    for call in (store.start_auth, store.status, store.disconnect):
        with pytest.raises(OutlookError, match='OUTLOOK_CLIENT_ID'):
            await call()
    for kind in ('calendar','reminders'):
        assert await store.tool(kind, 'list') == MISSING


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429,503])
async def test_retry_error_message_and_pagination(store, status):
    authorize(store)
    calls, sleeps = [], []
    async def sleep(delay):
        sleeps.append(delay)
    store.sleep = sleep
    def handler(r):
        calls.append(r)
        if len(calls) == 1:
            return reply({'error':{'message':'busy'}}, status, {'Retry-After':'2'})
        if len(calls) == 2:
            return reply({'value':[1], '@odata.nextLink':GRAPH+'/next?page=2'})
        return reply({'value':[2]})
    mock(store, handler)
    assert await store.values('/things', params={'first':'yes'}) == [1,2]
    assert sleeps == [2] and str(calls[-1].url).endswith('/next?page=2')
    mock(store, lambda r: reply({'error':{'message':'原文 error'}}, 403))
    with pytest.raises(OutlookError, match='读取失败：原文 error'):
        await store.graph('GET','/things',step='读取')


@pytest.mark.asyncio
async def test_calendar_all_calendars_and_limit(store):
    authorize(store)
    seen = []
    def handler(r):
        seen.append(str(r.url))
        if r.url.path.endswith('/calendars'):
            return reply({'value':[{'id':'a','name':'私人'},{'id':'b','name':'言之'}]})
        assert r.url.params['startDateTime'].endswith('+08:00')
        return reply({'value':[{'id':r.url.path+str(i),'subject':'事件','start':graph_time('2026-11-10 09:00'),
                    'end':graph_time('2026-11-10 10:00'),'location':{'displayName':'家'}} for i in range(26)]})
    mock(store, handler)
    result = await store.calendar('list',start='2026-11-10')
    assert '［私人］' in result and '［言之］' in result and result.endswith('还有 2 条')
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_reminder_list_filter_done(store):
    authorize(store)
    def handler(r):
        if r.url.path.endswith('/lists'):
            return reply({'value':[{'id':'a','displayName':'私人'},{'id':'b','displayName':'言之'}]})
        assert '/lists/b/' in r.url.path
        return reply({'value':[{'id':'one','title':'未完成','dueDateTime':graph_time('2026-11-10'), 'isReminderOn':True,
                               'reminderDateTime':graph_time('2026-11-10 09:00')},
                              {'id':'two','title':'已做','status':'completed'}]})
    mock(store, handler)
    text = await store.reminders('list',list='言之')
    assert '未完成' in text and '已做' not in text and '2026-11-10 09:00' in text
    assert '已做' in await store.reminders('list',list='言之',include_done=True)


@pytest.mark.asyncio
async def test_disconnect_cancels_poll_and_refresh_waits(store):
    authorize(store, 10)
    started, resume = asyncio.Event(), asyncio.Event()
    async def handler(r):
        started.set()
        await resume.wait()
        return reply({'access_token':'new', 'refresh_token':'rotated', 'expires_in':3600})
    mock(store, handler)
    refresh = asyncio.create_task(store.access_token())
    await started.wait()
    disconnect = asyncio.create_task(store.disconnect())
    await asyncio.sleep(0)
    resume.set()
    await asyncio.gather(refresh, disconnect)
    assert store.token() is None
    store.pending = {'expires_at':store.clock()+100}
    store.auth_task = asyncio.create_task(asyncio.Event().wait())
    await store.disconnect()
    assert store.auth_task.cancelled() and store.pending is None


@pytest.mark.asyncio
async def test_owned_mutations_and_update_duration(store):
    authorize(store)
    writes = []
    def handler(r):
        path = r.url.path
        if r.method != 'GET':
            writes.append(r)
            return reply({'id':'event'}) if r.method == 'PATCH' else reply(status=204)
        if path.endswith('/calendar'):
            return reply({'id':'own','name':'言之'})
        if path.endswith('/events/event'):
            return reply({'id':'event','start':graph_time('2026-11-10 09:00'), 'end':graph_time('2026-11-10 11:00')})
        if path.endswith('/calendars'):
            return reply({'value':[{'id':'own','name':'言之'}]})
        if path.endswith('/lists'):
            return reply({'value':[{'id':'own','displayName':'言之'}]})
        return reply({'id':'own','name':'言之','displayName':'言之'})
    mock(store, handler)
    result = await store.tool('calendar','update',id='event',start='2026-11-11 10:00')
    assert '已更新' in result
    body = json.loads(writes[-1].content)
    assert body['end'] == graph_time('2026-11-11 12:00')
    assert 'isReminderOn' not in body  # partial update preserves existing reminders
    assert '已删除' in await store.tool('calendar','delete',id='event')
    for action in ('update','complete','delete'):
        assert '已' in await store.tool('reminders',action,id='event',notes='新备注')


def test_short_collision_and_type_isolation(store):
    import hashlib
    prefix = hashlib.sha1(b'calendar:full').hexdigest()[:8]
    with store.db() as db:
        db.execute('INSERT INTO outlook_ids VALUES (?,?,?,?)', (prefix,'reminders','other','p'))
    short = store.short_id('calendar','full','p')
    assert len(short) == 9
    assert store.resolve('calendar',short)['full_id'] == 'full'
    assert store.resolve('reminders',prefix)['full_id'] == 'other'


@pytest.mark.asyncio
async def test_rest_bearer_and_tool_wiring(store):
    # Exercise the actual server handlers without bootstrapping memory/embedding services.
    import ast
    from starlette.responses import JSONResponse
    tree = ast.parse(Path('server.py').read_text())
    names = {'calendar','reminders','_outlook_auth_response','api_outlook_auth_start',
             'api_outlook_auth_status','api_outlook_auth_disconnect'}
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    assert len(nodes) == 6
    for node in nodes:
        node.decorator_list = []
    namespace = {'outlook_store':store,'OutlookError':OutlookError,'httpx':httpx,
                 '_require_room_bearer':lambda r: JSONResponse({'error':'unauthorized'}, status_code=401)}
    exec(compile(ast.Module(body=nodes,type_ignores=[]), 'server.py', 'exec'),namespace)
    for name in names - {'calendar','reminders','_outlook_auth_response'}:
        assert (await namespace[name](None)).status_code == 401
    namespace['_require_room_bearer'] = lambda r: None
    authorize(store)
    mock(store,lambda r: reply({'mail':'she@example.com'}))
    response = await namespace['api_outlook_auth_status'](None)
    assert response.status_code == 200 and 'secret' not in response.body.decode()
    await namespace['api_outlook_auth_disconnect'](None)
    assert store.token() is None
    assert '尚未授权' in await namespace['calendar']('list')
    assert '尚未授权' in await namespace['reminders']('list')


@pytest.mark.asyncio
async def test_refresh_without_rotation_and_status_invalid_grant(store):
    authorize(store, 1)
    mock(store, lambda r: reply({'access_token':'fresh','expires_in':3600}))
    assert await store.access_token() == 'fresh'
    assert store.token()['refresh_token'] == 'refresh-secret'
    authorize(store, 1)
    mock(store, lambda r: reply({'error':'invalid_grant'},400))
    status = await store.status()
    assert status['connected'] is False and status['last_error'] == REAUTH
    assert 'refresh-secret' not in json.dumps(status)


@pytest.mark.asyncio
async def test_cache_renamed_calendar_does_not_receive_creates(store):
    authorize(store)
    store.meta('calendar_id','old')
    writes = []
    def handler(r):
        if r.url.path.endswith('/calendars/old'):
            return reply({'id':'old','name':'已改名'})
        if r.url.path.endswith('/calendars'):
            return reply({'value':[{'id':'new','name':'言之'}]})
        writes.append(r)
        return reply({'id':'event'},201)
    mock(store,handler)
    await store.calendar('create',title='日程',start='2026-11-10')
    assert writes[0].url.path == '/v1.0/me/calendars/new/events'


@pytest.mark.asyncio
async def test_validation_does_not_write(store):
    authorize(store)
    mock(store,lambda r: pytest.fail('invalid create should not request Graph'))
    for kwargs in ({'title':'x'}, {'title':'x','start':'bad'},
                   {'title':'x','start':'2026-11-10 09:00','all_day':True},
                   {'title':'x','start':'2026-11-10','end':'2026-11-09'}):
        text = await store.tool('calendar','create',**kwargs)
        assert '已创建' not in text
    assert '需要 title' in await store.tool('reminders','create')
