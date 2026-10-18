import asyncio
import json
import sqlite3
from types import SimpleNamespace

from starlette.requests import Request
from gateway import GatewayService
from gateway_state import GatewayStateStore


def test_old_database_repeat_init_and_lifecycle(tmp_path):
    path = str(tmp_path / 'gateway_state.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE legacy (id TEXT)')
    conn.commit()
    conn.close()
    store = GatewayStateStore(path)
    assert store.load_cc_agent_wake_prompt() is None
    GatewayStateStore(path)
    saved = store.save_cc_agent_wake_prompt('醒来', '工具')
    assert saved['updated_at']
    assert GatewayStateStore(path).load_cc_agent_wake_prompt() == saved
    replacement = store.save_cc_agent_wake_prompt('新说明', '新工具')
    assert store.load_cc_agent_wake_prompt() == replacement
    store.delete_cc_agent_wake_prompt()
    store.delete_cc_agent_wake_prompt()
    assert GatewayStateStore(path).load_cc_agent_wake_prompt() is None


def test_routes_auth_validation_and_lifecycle(tmp_path):
    service = SimpleNamespace(gateway_token='secret', state_store=GatewayStateStore(str(tmp_path / 'state.db')))
    service._authorize = lambda header: GatewayService._authorize(service, header)

    async def call(method, body=None, token='secret'):
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(body).encode()}
        request = Request({'type': 'http', 'method': method, 'headers': [(b'authorization', f'Bearer {token}'.encode())]}, receive)
        return await GatewayService.handle_cc_agent_wake_prompt(service, request)

    async def run():
        for method in ('GET', 'PUT', 'DELETE'):
            assert (await call(method, token='wrong')).status_code == 401
            assert (await call(method, token='')).status_code == 401
        assert (await call('PUT', {})).status_code == 400
        assert json.loads((await call('GET')).body)['config'] is None
        response = await call('PUT', {'instructions': '说明', 'tool_description': '工具'})
        assert response.status_code == 200
        assert json.loads((await call('GET')).body)['config'] == json.loads(response.body)['config']
        assert (await call('DELETE')).status_code == 200
        assert json.loads((await call('GET')).body)['config'] is None
    asyncio.run(run())
