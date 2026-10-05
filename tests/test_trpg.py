import json
import socket
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from trpg_dice import roll, loss
from trpg_mcp import mount_trpg
from trpg_store import Conflict, TrpgStore

FAKE = json.loads((Path(__file__).parent / 'fixtures/trpg/fake-module.json').read_text())


@pytest.fixture
def store(tmp_path):
    return TrpgStore(tmp_path / 'state/trpg.sqlite', 'test', randbelow=lambda n: 0)


@pytest.fixture
def game(store):
    store.import_module(FAKE)
    return store.create_game(store.list_modules()[0]['id'])['id']


def point(value, target=50, **kwargs):
    values = iter([value % 10, (value % 100) // 10])
    return roll(target, randbelow=lambda n: next(values), **kwargs)


@pytest.mark.parametrize('value,target,grade', [(1,0,'critical'), (100,50,'fumble'), (96,49,'fumble'), (95,49,'failure'), (96,50,'failure'), (99,50,'failure'), (100,49,'fumble'), (10,50,'extreme'), (25,50,'hard'), (50,50,'regular')])
def test_grades(value, target, grade):
    assert point(value, target)['grade'] == grade


@pytest.mark.parametrize('value,difficulty,success', [(40,'regular',True), (40,'hard',False), (20,'hard',True), (20,'extreme',False), (10,'extreme',True)])
def test_difficulties(value, difficulty, success):
    assert point(value, difficulty=difficulty)['success'] == success


def test_bonus_penalty_and_zero():
    for bonus, penalty, expected in [(1,0,20), (0,1,100), (1,1,100)]:
        values = iter([0,0,2])
        result = roll(50, bonus=bonus, penalty=penalty, randbelow=lambda n: next(values))
        assert result['value'] == expected
    assert loss('2d4', lambda n: 3) == 8
    with pytest.raises(ValueError):
        loss('1d0')


def test_turns_cas_and_checks(store, game):
    with pytest.raises(Conflict):
        store.submit(game, 'yanzhi', 'action')
    store.submit(game, 'xiaoyang', '行动')
    assert store.view_for(game, 'dm')['phase'] == 'yanzhi'
    with pytest.raises(Conflict):
        store.submit(game, 'xiaoyang', '再次行动')
    with pytest.raises(Conflict):
        store.advance(game, 'players', 'dm')
    store.submit(game, 'yanzhi', '行动')
    a = store.request_check(game, 'xiaoyang', 'san', san_loss='1/1d4')['id']
    b = store.request_check(game, 'yanzhi', 'skill', skill='侦查')['id']
    with pytest.raises(Conflict):
        store.advance(game, 'checks', 'dm')
    with pytest.raises(ValueError):
        store.roll_check(game, a, 'yanzhi')
    result = store.roll_check(game, a, 'xiaoyang')
    assert result['san_loss'] == 1 and result['san_after'] == 49  # injected d100=100 -> failure
    assert store.view_for(game, 'dm')['phase'] == 'checks'
    with pytest.raises(Conflict):
        store.roll_check(game, a, 'xiaoyang')
    store.roll_check(game, b, 'yanzhi')
    assert store.view_for(game, 'dm')['phase'] == 'dm'
    store.advance(game, 'dm', 'players')
    store.submit(game, 'yanzhi', '桌边话', table_talk=True)
    assert store.view_for(game, 'dm')['phase'] == 'players'


def seed_secrets(store, game):
    store.advance(game, 'players', 'dm')
    store.narrate(game, 'PUBLIC_NARRATION', {'xiaoyang': 'X_PRIVATE', 'yanzhi': 'Y_PRIVATE'}, 'GM_NOTE_SENTINEL')
    store.reveal_clue(game, 'c1', 'all')
    store.reveal_clue(game, 'c2', 'xiaoyang')
    store.request_check(game, 'yanzhi', 'luck', reason='SECRET_ROLL_SENTINEL', secret=True)


def test_visibility_and_profile(store, game):
    seed_secrets(store, game)
    for viewer, own, other in [('xiaoyang', 'X_PRIVATE', 'Y_PRIVATE'), ('yanzhi', 'Y_PRIVATE', 'X_PRIVATE')]:
        data = json.dumps(store.view_for(game, viewer))
        assert own in data and other not in data
        for hidden in ('keeper_', 'KEEPER_', 'GM_NOTE_SENTINEL', 'UNREVEALED_CLUE', 'SECRET_ROLL_SENTINEL'):
            assert hidden not in data
    assert 'XIAOYANG_CLUE' not in json.dumps(store.view_for(game, 'yanzhi'))
    assert 'XIAOYANG_CLUE' in json.dumps(store.view_for(game, 'xiaoyang'))
    assert 'GM_NOTE_SENTINEL' in json.dumps(store.view_for(game, 'dm'))
    other = TrpgStore(store.path, 'other')
    assert other.list_games() == [] and other.list_modules() == []
    with pytest.raises(ValueError):
        other.view_for(game, 'dm')
    with pytest.raises(ValueError):
        other.advance(game, 'dm', 'players')
    with pytest.raises(ValueError):
        other.read_module(game, 'overview')
    assert store.view_for(game, 'yanzhi', 999)['log'] == []


def test_repeat_initialize_and_restart(store, game):
    store.initialize()
    restored = TrpgStore(store.path, 'test')
    assert restored.view_for(game, 'xiaoyang')['my_character']['san'] == 50
    assert len(restored.list_games()) == 1


def test_old_database_upgrade(tmp_path):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE trpg_modules(id TEXT PRIMARY KEY,title TEXT,public_intro TEXT,keeper_json TEXT)')
        c.execute('INSERT INTO trpg_modules VALUES(?,?,?,?)', ('old', FAKE['title'], FAKE['public_intro'], json.dumps(FAKE)))
    default = TrpgStore(path)
    default.initialize()
    assert default.list_modules() == [{'id': 'old', 'title': FAKE['title']}]
    assert TrpgStore(path, 'other').list_modules() == []


@pytest.fixture
def http_server(tmp_path, monkeypatch):
    for key, token in [('TRPG_PLAYER_MCP_TOKEN','player-secret'), ('TRPG_DM_MCP_TOKEN','dm-secret'), ('OMBRE_GATEWAY_TOKEN','gateway-secret')]:
        monkeypatch.setenv(key, token)
    monkeypatch.delenv('OMBRE_STATE_DIR', raising=False)
    parent = Starlette()
    child = mount_trpg(parent, {'state_dir': str(tmp_path / 'state'), 'buckets_dir': str(tmp_path)}, 'test')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(parent, log_level='error', lifespan='on'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', timeout=10) as client:
        yield client, child
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


def rpc(client, endpoint, token, method, params=None):
    return client.post('/trpg/' + endpoint + '/mcp', headers={
        'Authorization': ('Bearer ' + token) if token else 'Bearer', 'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2025-03-26', 'Host': 'brain.internal:8000'}, json={'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params or {}})


def call(client, endpoint, token, name, arguments=None):
    response = rpc(client, endpoint, token, 'tools/call', {'name':name, 'arguments':arguments or {}})
    assert response.status_code == 200, response.text
    return response.json()['result']


def test_real_http_isolation_and_tools(http_server):
    client, _ = http_server
    headers = {'Authorization':'Bearer gateway-secret'}
    uploaded = client.post('/trpg/api/modules', headers=headers, json=FAKE)
    assert uploaded.status_code == 200
    assert set(uploaded.json()) == {'title', 'scenes_count', 'clues_count', 'npcs_count'}
    assert 'KEEPER_' not in uploaded.text
    modules = client.get('/trpg/api/modules', headers=headers).json()
    assert set(modules[0]) == {'id','title'}
    created = client.post('/trpg/api/games', headers=headers, json={'module_id':modules[0]['id']})
    assert created.status_code == 200, created.text
    game = created.json()['id']
    prefix = '/trpg/api/games/' + game
    initialized = rpc(client, 'player', 'player-secret', 'initialize', {'protocolVersion':'2025-03-26', 'capabilities':{}, 'clientInfo':{'name':'test','version':'1'}})
    assert initialized.status_code == 200
    assert rpc(client, 'dm', 'dm-secret', 'initialize', {'protocolVersion':'2025-03-26', 'capabilities':{}, 'clientInfo':{'name':'test','version':'1'}}).status_code == 200
    dm_tools = rpc(client, 'dm', 'dm-secret', 'tools/list').json()['result']['tools']
    assert {tool['name'] for tool in dm_tools} == {'get_state','get_log','search_module','read_module','narrate','request_check','secret_roll','reveal_clue','update_character','set_scene','create_character','write_recap'}
    tools = rpc(client, 'player', 'player-secret', 'tools/list').json()['result']['tools']
    assert {t['name'] for t in tools} == {'get_table','get_my_character','get_clues','submit_action','roll_check'}
    assert call(client,'player','player-secret','submit_action',{'text':'wrong turn'})['isError']
    client.post(prefix+'/settle', headers=headers, json={'expected_phase':'players'})
    assert not call(client,'dm','dm-secret','narrate',{'public':'PUBLIC_NARRATION','private':{'xiaoyang':'X_PRIVATE','yanzhi':'Y_PRIVATE'},'gm_note':'GM_NOTE_SENTINEL'}).get('isError')
    for clue, to in [('c1','all'),('c2','xiaoyang')]:
        call(client,'dm','dm-secret','reveal_clue',{'clue_id':clue,'to':to})
    call(client,'dm','dm-secret','secret_roll',{'owner':'yanzhi','type':'luck','reason':'SECRET_ROLL_SENTINEL'})
    player = call(client,'player','player-secret','get_table')
    rest = client.get(prefix+'/table', headers=headers)
    for data, own, other in [(json.dumps(player),'Y_PRIVATE','X_PRIVATE'), (rest.text,'X_PRIVATE','Y_PRIVATE')]:
        assert own in data and other not in data
        for hidden in ('keeper_', 'KEEPER_', 'GM_NOTE_SENTINEL', 'UNREVEALED_CLUE', 'SECRET_ROLL_SENTINEL'):
            assert hidden not in data
    assert 'XIAOYANG_CLUE' not in json.dumps(player)
    assert 'XIAOYANG_CLUE' in rest.text
    assert 'KEEPER_OVERVIEW_SENTINEL' in json.dumps(call(client,'dm','dm-secret','read_module',{'id':'overview'}))
    assert 'GM_NOTE_SENTINEL' in json.dumps(call(client,'dm','dm-secret','get_log'))
    assert call(client,'player','player-secret','read_module',{'id':'overview'})['isError']
    assert client.post(prefix+'/phase', headers=headers, json={'expected_phase':'players','phase':'dm'}).status_code == 409
    assert client.post(prefix+'/table-talk', headers=headers, json={'text':'桌边话'}).status_code == 200
    pending = call(client,'dm','dm-secret','request_check',{'owner':'xiaoyang','type':'san','san_loss':'1/1d4','reason':'SAN'})
    check = json.loads(pending['content'][0]['text'])['id']
    assert call(client,'player','player-secret','roll_check',{'check_id':check})['isError']
    assert client.post(prefix+'/checks/'+check+'/roll',headers=headers).status_code == 200
    assert client.get(prefix+'/table',headers=headers).json()['phase'] == 'dm'


@pytest.mark.parametrize('endpoint,token', [('player','dm-secret'),('dm','player-secret'),('player','gateway-secret'),('dm','gateway-secret'),('player','oauth-token'),('dm','oauth-token'),('player',''),('dm','')])
def test_wrong_token(http_server, endpoint, token):
    assert rpc(http_server[0],endpoint,token,'tools/list').status_code == 401


def test_missing_tokens_and_duplicate_tokens(http_server, monkeypatch):
    client, _ = http_server
    for key, endpoint, token in [('TRPG_PLAYER_MCP_TOKEN','player','player-secret'),('TRPG_DM_MCP_TOKEN','dm','dm-secret')]:
        monkeypatch.delenv(key)
        assert rpc(client,endpoint,token,'tools/list').status_code == 401
    monkeypatch.setenv('TRPG_PLAYER_MCP_TOKEN','shared')
    monkeypatch.setenv('TRPG_DM_MCP_TOKEN','shared')
    assert rpc(client,'player','shared','tools/list').status_code == 401
    assert rpc(client,'dm','shared','tools/list').status_code == 401
    assert client.get('/trpg/api/games',headers={'Authorization':'Bearer player-secret'}).status_code == 401
    monkeypatch.delenv('OMBRE_GATEWAY_TOKEN')
    assert client.get('/trpg/api/games',headers={'Authorization':'Bearer gateway-secret'}).status_code == 401


def test_invalid_module(store):
    for field in ('keeper_overview','scenes','clues','npcs','pregens'):
        broken = {k:v for k,v in FAKE.items() if k != field}
        with pytest.raises(ValueError):
            store.import_module(broken)
    assert store.list_modules() == []


def test_legacy_character_keys_migrate(tmp_path):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE trpg_characters(game_id TEXT,owner TEXT,sheet_json TEXT,PRIMARY KEY(game_id,owner))')
        c.execute('INSERT INTO trpg_characters VALUES(?,?,?)', ('old-game', 'yanzhi', '{"san":42}'))
    default = TrpgStore(path)
    default.initialize()
    with default.db() as c:
        assert default._sheet(c, 'old-game', 'yanzhi')['san'] == 42
        default._character(c, 'old-game', 'yanzhi', {'san':41})
    other = TrpgStore(path, 'other')
    with other.db() as c:
        other._character(c, 'old-game', 'yanzhi', {'san':99})
    with default.db() as c:
        assert default._sheet(c, 'old-game', 'yanzhi')['san'] == 41


@pytest.mark.parametrize('success,expected_loss', [(True,1),(False,4)])
def test_sanity_success_failure_and_atomicity(store, game, success, expected_loss):
    store.randbelow = (lambda n: 1 if n == 10 else 0) if success else (lambda n: n-1)
    store.advance(game,'players','dm')
    check = store.request_check(game,'xiaoyang','san',san_loss='1/1d4')['id']
    result = store.roll_check(game,check,'xiaoyang')
    assert result['success'] is success
    assert result['san_loss'] == expected_loss
    assert store.view_for(game,'xiaoyang')['my_character']['san'] == 50 - expected_loss
    with pytest.raises(Conflict):
        store.roll_check(game,check,'xiaoyang')
    assert store.view_for(game,'xiaoyang')['my_character']['san'] == 50 - expected_loss


def test_concurrent_action_and_cas(store, game):
    from concurrent.futures import ThreadPoolExecutor
    def action():
        try:
            store.submit(game,'xiaoyang','并发行动')
            return 'ok'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: action(), range(2))) == ['conflict','ok']
    assert len([r for r in store.view_for(game,'dm')['log'] if r['kind'] == 'action']) == 1
    def advance():
        try:
            store.advance(game,'yanzhi','dm')
            return 'ok'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: advance(), range(2))) == ['conflict','ok']


def test_dm_character_scene_and_reveal_idempotence(store, game):
    store.advance(game,'players','dm')
    store.create_character(game,'yanzhi',{'pregen':1})
    store.create_character(game,'npc:n1',{'name':'NPC','hp':10,'keeper_text':'STRIPPED'})
    store.update_character(game,'yanzhi',{'hp':9,'notes':'玩家备注'},'受伤')
    assert store.view_for(game,'yanzhi')['my_character']['hp'] == 9
    assert 'STRIPPED' not in json.dumps(store.view_for(game,'dm'))
    store.set_scene(game,'s2')
    assert store.view_for(game,'yanzhi')['scene'] == {'id':'s2','title':'站台'}
    for _ in range(2):
        store.reveal_clue(game,'c1','all')
    assert len(store.view_for(game,'dm')['reveals']) == 1
    assert store.read_module(game,'pregen:1')['name'] == '测试乙'
    with pytest.raises(ValueError):
        store.set_scene(game,'missing')
    with pytest.raises(ValueError):
        store.request_check(game,'npc:n1','luck')


def test_latest_recaps(store, game):
    assert store.latest_recap(game, 'dm') == {}
    store.write_recap(game, 'old public', 'KEEPER_RECAP')
    store.write_recap(game, public='new public')
    for viewer in ('xiaoyang', 'yanzhi'):
        assert store.latest_recap(game, viewer) == {'public': 'new public'}
    assert store.latest_recap(game, 'dm') == {'public': 'new public', 'keeper': 'KEEPER_RECAP'}
    assert store.view_for(game, 'dm')['phase'] == 'players'
    for args in ((None, None), (' ', ''), ('valid', 42)):
        with pytest.raises(ValueError):
            store.write_recap(game, *args)
    store.initialize()
    assert TrpgStore(store.path, 'test').latest_recap(game, 'xiaoyang') == {'public': 'new public'}
    with pytest.raises(ValueError):
        TrpgStore(store.path, 'other').latest_recap(game, 'dm')


def test_http_pregens_and_recaps(http_server):
    client, _ = http_server
    headers = {'Authorization': 'Bearer gateway-secret'}
    client.post('/trpg/api/modules', headers=headers, json=FAKE)
    module = client.get('/trpg/api/modules', headers=headers).json()[0]['id']
    response = client.get(f'/trpg/api/modules/{module}/pregens', headers=headers)
    assert response.status_code == 200
    assert response.json() == [dict(index=i, **p) for i, p in enumerate(FAKE['pregens'])]
    for hidden in ('keeper_', 'scenes', 'clues', 'npcs'):
        assert hidden not in response.text
    game = client.post('/trpg/api/games', headers=headers, json={'module_id': module}).json()['id']
    result = call(client, 'dm', 'dm-secret', 'write_recap', {'public': 'PUBLIC_RECAP', 'keeper': 'KEEPER_RECAP'})
    assert not result.get('isError')
    for data in (client.get(f'/trpg/api/games/{game}/table', headers=headers).text,
                 json.dumps(call(client, 'player', 'player-secret', 'get_table'))):
        assert 'PUBLIC_RECAP' in data and 'KEEPER_RECAP' not in data
    dm = json.dumps(call(client, 'dm', 'dm-secret', 'get_log'))
    assert 'PUBLIC_RECAP' in dm and 'KEEPER_RECAP' in dm
