import sqlite3

import pytest
from gateway_state import GatewayStateStore, SessionStateConflictError, ConversationPersonaConflictError


def patch(store, updates, profile="default", version=None):
    return store.patch_conversation_session_state(profile_id=profile, session_id="window", persona_id="ombre", updates=updates, expected_state_version=version)


def test_migration_idempotent_and_corrupt_read(tmp_path):
    path = str(tmp_path / "state.db")
    store = GatewayStateStore(path)
    patch(store, {"mode": "chat"})
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE conversation_sessions DROP COLUMN mcp_overrides")
    store = GatewayStateStore(path)
    assert patch(store, {"mode": "work"})["mcp_overrides"] == {}
    assert GatewayStateStore(path).get_conversation_session_state(profile_id="default", session_id="window")["mcp_overrides"] == {}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE conversation_sessions SET mcp_overrides = 'broken'")
    assert store.get_conversation_session_state(profile_id="default", session_id="window")["mcp_overrides"] == {}


@pytest.mark.parametrize("value", [None, [], {"bad.key": True}, {"": False}, {"a" * 65: True}, {"ok": 1}, {"ok": "false"}, {str(i): True for i in range(65)}])
def test_invalid_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="mcp_overrides"):
        patch(GatewayStateStore(str(tmp_path / "state.db")), {"mcp_overrides": value})


def test_roundtrip_replacement_and_isolation(tmp_path):
    store = GatewayStateStore(str(tmp_path / "state.db"))
    overrides = {"Games-4399": True, "send_image": False}
    saved = patch(store, {"mcp_overrides": overrides})
    assert saved["mcp_overrides"] == overrides
    assert patch(store, {"mode": "work"})["mcp_overrides"] == overrides
    assert patch(store, {"mcp_overrides": {}}, profile="other")["mcp_overrides"] == {}
    with pytest.raises(SessionStateConflictError):
        patch(store, {"mcp_overrides": {}}, version=0)
    with pytest.raises(ConversationPersonaConflictError):
        store.patch_conversation_session_state(profile_id="default", session_id="window", persona_id="other", updates={"mcp_overrides": {}})
    assert store.get_conversation_session_state(profile_id="default", session_id="window")["mcp_overrides"] == overrides
    assert patch(store, {"mcp_overrides": {}})["mcp_overrides"] == {}


@pytest.mark.asyncio
async def test_existing_session_route_auth_validation_and_roundtrip(tmp_path):
    import json
    from types import SimpleNamespace
    from starlette.requests import Request
    from gateway import GatewayService
    service = SimpleNamespace(gateway_token="secret", _conversation_profile_id="default", state_store=GatewayStateStore(str(tmp_path / "state.db")))
    service._authorize = lambda header: GatewayService._authorize(service, header)
    service._truthy_header = lambda value: GatewayService._truthy_header(service, value)

    async def call(method, value=None, token="secret"):
        async def receive():
            return {"type": "http.request", "body": json.dumps({"session_id": "window", "persona_id": "ombre", "mcp_overrides": value}).encode()}
        request = Request({"type": "http", "method": method, "query_string": b"session_id=window", "headers": [(b"authorization", f"Bearer {token}".encode())]}, receive)
        return await GatewayService.handle_conversation_session(service, request)

    assert (await call("PATCH", {}, token="wrong")).status_code == 401
    assert (await call("PATCH", {"bad key": True})).status_code == 400
    response = await call("PATCH", {"games": False})
    assert response.status_code == 200
    assert json.loads(response.body)["session"]["mcp_overrides"] == {"games": False}
    assert json.loads((await call("GET")).body)["session"]["mcp_overrides"] == {"games": False}
