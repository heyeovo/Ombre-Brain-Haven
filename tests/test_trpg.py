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


def test_pregens_field_whitelist_and_profile(store):
    module = json.loads(json.dumps(FAKE))
    module['pregens'][0]['keeper_text'] = 'KEEPER_PREGEN'
    module['pregens'][0]['sheet']['keeper_notes'] = 'KEEPER_SHEET'
    store.import_module(module)
    module_id = store.list_modules()[0]['id']
    pregens = store.list_pregens(module_id)
    assert set(pregens[0]) == {'index', 'name', 'occupation', 'sheet'}
    assert 'KEEPER_' not in json.dumps(pregens)
    with pytest.raises(ValueError):
        TrpgStore(store.path, 'other').list_pregens(module_id)


def test_p2b_settings_runtime_migration(store, game):
    assert store.game_settings(game) == {'yanzhi_model': 'claude-opus-4-6', 'persona_id': ''}
    for model in ('gpt-5', 'claude-haiku-4-5', 'Claude-opus-4-6', '', None):
        with pytest.raises(ValueError):
            store.game_settings(game, {'yanzhi_model': model})
    store.game_settings(game, {'yanzhi_model': 'claude-sonnet-5', 'persona_id': 'main'})
    store.yanzhi_runtime(game, {'session_id': 'sdk-session', 'session_tokens': 123, 'last_seen_seq': 2})
    store.initialize()
    restored = TrpgStore(store.path, 'test')
    assert restored.game_settings(game)['persona_id'] == 'main'
    assert restored.yanzhi_runtime(game)['session_id'] == 'sdk-session'
    with pytest.raises(ValueError):
        TrpgStore(store.path, 'other').yanzhi_runtime(game)
    with pytest.raises(ValueError):
        store.yanzhi_runtime(game, {'last_seen_seq': -1})


def test_p2b_http(http_server):
    client, _ = http_server
    headers = {'Authorization': 'Bearer gateway-secret'}
    client.post('/trpg/api/modules', headers=headers, json=FAKE)
    module = client.get('/trpg/api/modules', headers=headers).json()[0]['id']
    game = client.post('/trpg/api/games', headers=headers, json={'module_id': module}).json()['id']
    prefix = f'/trpg/api/games/{game}'
    assert client.get(prefix + '/settings', headers=headers).json()['yanzhi_model'] == 'claude-opus-4-6'
    assert client.patch(prefix + '/settings', headers=headers, json={'yanzhi_model': 'gpt-5'}).status_code == 400
    assert client.patch(prefix + '/settings', headers=headers, json={'yanzhi_model': 'claude-opus-5-5'}).status_code == 200
    assert client.put(prefix + '/yanzhi-runtime', headers=headers, json={'session_id': 'test'}).status_code == 200
    assert client.get(prefix + '/yanzhi-runtime', headers=headers).json()['session_id'] == 'test'
    client.post(prefix + '/settle', headers=headers, json={'expected_phase': 'players'})
    call(client, 'dm', 'dm-secret', 'narrate', {'public': 'PUBLIC', 'private': {'xiaoyang': 'X_PRIVATE', 'yanzhi': 'Y_PRIVATE'}, 'gm_note': 'GM_NOTE_SENTINEL'})
    call(client, 'dm', 'dm-secret', 'write_recap', {'public': 'PUBLIC_RECAP', 'keeper': 'KEEPER_RECAP'})
    call(client, 'dm', 'dm-secret', 'secret_roll', {'type': 'luck', 'reason': 'SECRET_ROLL_SENTINEL'})
    call(client, 'dm', 'dm-secret', 'reveal_clue', {'clue_id': 'c2', 'to': 'xiaoyang'})
    response = client.get(prefix + '/yanzhi-view', headers=headers)
    assert 'Y_PRIVATE' in response.text and response.json()['latest_recap'] == {'public': 'PUBLIC_RECAP'}
    for hidden in ('keeper_', 'KEEPER_', 'X_PRIVATE', 'GM_NOTE_SENTINEL', 'SECRET_ROLL_SENTINEL', 'XIAOYANG_CLUE', 'UNREVEALED_CLUE'):
        assert hidden not in response.text
    assert client.post(prefix + '/yanzhi-table-talk', headers=headers, json={'text': 'hello'}).status_code == 200
    view = client.get(prefix + '/yanzhi-view', headers=headers).json()
    assert view['phase'] == 'dm'
    assert view['log'][-1]['author'] == 'yanzhi' and view['log'][-1]['kind'] == 'table_talk'
    assert client.get(prefix + '/yanzhi-view?since_seq=999', headers=headers).json()['log'] == []
    for path in ('settings', 'yanzhi-runtime', 'yanzhi-view'):
        assert client.get(prefix + '/' + path).status_code == 401


def test_p2b_upgrade_existing_games(tmp_path):
    path = tmp_path / 'legacy-games.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE trpg_games(profile_id TEXT, id TEXT PRIMARY KEY, title TEXT, module_id TEXT, phase TEXT, scene_id TEXT, created_at TEXT, updated_at TEXT)')
        c.execute("INSERT INTO trpg_games VALUES('test','old','title','module','players',NULL,NULL,NULL)")
    store = TrpgStore(path, 'test')
    store.game_settings('old', {'persona_id': 'chosen'})
    store.yanzhi_runtime('old', {'session_id': 'resumed'})
    store.initialize()
    assert store.game_settings('old')['persona_id'] == 'chosen'
    assert store.yanzhi_runtime('old')['session_id'] == 'resumed'
    with pytest.raises(ValueError):
        TrpgStore(path, 'other').game_settings('old')


def test_end_archive_and_new_game(store, game):
    module = store.list_modules()[0]['id']
    store.advance(game, 'players', 'dm')
    store.write_recap(game, 'public recap', 'keeper recap')
    store.narrate(game, 'story')
    store.request_check(game, 'xiaoyang', 'luck')
    before = store.view_for(game, 'xiaoyang')
    store.yanzhi_runtime(game, {'running_since': 'now', 'last_error': 'error'})
    ended = store.end_game(game)
    assert ended['ended_at'] and store.end_game(game) == ended
    assert store.yanzhi_runtime(game)['running_since'] is None
    assert store.yanzhi_runtime(game)['last_error'] is None
    assert store.list_games()[0]['module_id'] == module
    assert store.list_games()[0]['ended_at'] == ended['ended_at']
    with pytest.raises(ValueError):
        store.active_game()
    new = store.create_game(module)['id']
    assert store.active_game() == new
    assert store.view_for(game, 'xiaoyang')['log'] == before['log']
    assert store.view_for(game, 'xiaoyang')['my_character'] == before['my_character']
    assert store.view_for(game, 'xiaoyang')['checks'] == before['checks']
    assert store.latest_recap(game, 'xiaoyang') == {'public': 'public recap'}


@pytest.mark.parametrize('operation', [
    lambda s,g: s.submit(g, 'xiaoyang', 'action'),
    lambda s,g: s.submit(g, 'xiaoyang', 'talk', True),
    lambda s,g: s.submit(g, 'yanzhi', 'talk', True),
    lambda s,g: s.advance(g, 'players', 'dm'),
    lambda s,g: s.narrate(g, 'story'),
    lambda s,g: s.write_recap(g, 'recap'),
    lambda s,g: s.request_check(g, 'xiaoyang', 'luck'),
    lambda s,g: s.roll_check(g, 'missing', 'xiaoyang'),
    lambda s,g: s.set_scene(g, 'scene'),
    lambda s,g: s.reveal_clue(g, 'clue', 'all'),
    lambda s,g: s.create_character(g, 'xiaoyang', {}),
    lambda s,g: s.update_character(g, 'xiaoyang', {'hp': 1}, 'reason'),
    lambda s,g: s.game_settings(g, {}),
    lambda s,g: s.yanzhi_runtime(g, {}),
])
def test_ended_rejects_every_write(store, game, operation):
    store.end_game(game)
    with pytest.raises(Conflict, match='game ended'):
        operation(store, game)


def test_soft_delete_archive_restore_and_isolation(store, game):
    module = store.list_modules()[0]['id']
    with pytest.raises(Conflict):
        store.delete_module(module)
    other = TrpgStore(store.path, 'other')
    with pytest.raises(ValueError):
        other.end_game(game)
    with pytest.raises(ValueError):
        other.delete_module(module)
    store.end_game(game)
    store.delete_module(module)
    store.delete_module(module)
    assert store.list_modules() == []
    with pytest.raises(ValueError, match='module not found'):
        store.create_game(module)
    for viewer in ('xiaoyang', 'yanzhi', 'dm'):
        assert store.view_for(game, viewer)['ended_at']
    assert store.yanzhi_view(game)['ended_at']
    assert store.read_module(game, item_id='overview')
    store.import_module({**FAKE, 'id': module, 'title': 'Restored'})
    assert store.list_modules() == [{'id': module, 'title': 'Restored'}]
    assert store.create_game(module)


def test_end_columns_legacy_migration(store, game):
    with store.db() as c:
        c.execute('ALTER TABLE trpg_games DROP COLUMN ended_at')
        c.execute('ALTER TABLE trpg_modules DROP COLUMN deleted_at')
    store.initialize()
    store.initialize()
    assert store.list_games()[0]['ended_at'] is None
    assert store.list_modules()
    assert store.view_for(game, 'xiaoyang')['ended_at'] is None


def test_end_delete_rest(http_server):
    client, _ = http_server
    headers = {'Authorization': 'Bearer gateway-secret'}
    client.post('/trpg/api/modules', headers=headers, json=FAKE)
    module = client.get('/trpg/api/modules', headers=headers).json()[0]['id']
    game = client.post('/trpg/api/games', headers=headers, json={'module_id': module}).json()['id']
    end = f'/trpg/api/games/{game}/end'
    delete = f'/trpg/api/modules/{module}'
    assert client.post(end).status_code == 401
    assert client.delete(delete).status_code == 401
    assert client.delete(delete, headers=headers).status_code == 409
    result = client.post(end, headers=headers)
    assert result.status_code == 200 and result.json()['ended_at']
    assert client.post(end, headers=headers).json() == result.json()
    assert client.post(f'/trpg/api/games/{game}/table-talk', headers=headers, json={'text':'hi'}).status_code == 409
    assert client.delete(delete, headers=headers).status_code == 200
    assert client.get(f'/trpg/api/games/{game}/table', headers=headers).status_code == 200
    assert client.post('/trpg/api/games', headers=headers, json={'module_id':module}).status_code == 400


def test_module_import_id_conflicts_and_profile_boundary(store):
    store.import_module({**FAKE, 'id': 'fixed'})
    with pytest.raises(Conflict):
        store.import_module({**FAKE, 'id': 'fixed'})
    store.delete_module('fixed')
    other = TrpgStore(store.path, 'other')
    with pytest.raises(Conflict):
        other.import_module({**FAKE, 'id': 'fixed'})
    assert other.list_modules() == []
    with pytest.raises(ValueError, match='invalid module id'):
        store.import_module({**FAKE, 'id': '../invalid'})
    store.import_module({**FAKE, 'id': 'fixed'})
    assert store.list_modules()[0]['id'] == 'fixed'
