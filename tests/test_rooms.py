import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse

from room_store import RoomStore


@pytest.fixture
def store(tmp_path):
    return RoomStore({"state_dir": str(tmp_path)})


def rid(result):
    return result.split("[")[1].split("]")[0]


def visit(room_id, **extra):
    return {"id": "visit-one", "room_id": room_id, "session_id": "s", "request_id": "r", "turn_kind": "chat",
            "entered_at": "2026-10-01T00:00:00Z", "left_at": "2026-10-01T00:01:00Z", "duration_ms": 60000,
            "process": [{"type": "thinking", "text": "私密过程"}], **extra}


def route(name, store, auth=lambda request: None):
    source = Path(__file__).parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    ns = {"darkroom_store": store, "_require_room_bearer": auth}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    return ns[name]


def test_migration_preserves_entries_and_repeats(store):
    store.base_dir.mkdir(parents=True)
    entries = [
        {"id": "a", "room_id": "room_aaa", "note": "第一行标题\n旧正文", "created_at": "2026-01-01", "visibility": "active", "locked_until": "2099-01-01T00:00:00+08:00"},
        {"id": "b", "room_id": "room_aaa", "note": "续写", "created_at": "2026-01-02", "visibility": "active", "locked_until": "2099-02-01T00:00:00+08:00"},
        {"id": "c", "room_id": "room_bbb", "note": "公开", "visibility": "active"},
        {"id": "d", "room_id": "room_ccc", "note": "撤回", "visibility": "retracted"},
    ]
    raw = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
    store.entries_path.write_text(raw, encoding="utf-8")
    store.release_log_path.write_text(json.dumps({"entry_id": "c", "created_at": "2026-02-01"}) + "\n", encoding="utf-8")
    rooms = {r["id"]: r for r in store.public_rooms()}
    assert set(rooms) == {"room_aaa", "room_bbb"}
    assert rooms["room_aaa"]["title"] == "第一行标题"
    assert rooms["room_aaa"]["lock_until"].startswith("2099-02")
    assert rooms["room_bbb"]["status"] == "opened"
    assert store.entries_path.read_text(encoding="utf-8") == raw
    assert RoomStore(store.config).public_rooms() == list(rooms.values())


def test_lock_only_restricts_open_and_opened_writes_visible(store):
    room = rid(store.act("enter", title="礼物", lock_until="2099-01-01 00:00"))
    store.act("write", content="私密正文")
    store.act("leave", note="续写便条")
    assert "私密正文" in store.act("read", room)
    assert "续写便条" in store.act("read", room)
    assert "私密正文" not in store.act("open", room)
    assert store.public_detail(room)["status"] == "closed"
    assert "房间已打开" in store.act("open", room, lock_until="2000-01-01 00:00")
    store.act("write", room, content="新增公开内容")
    assert len(store.public_detail(room)["entries"]) == 2
    assert "便条" not in json.dumps(store.public_detail(room), ensure_ascii=False)
    assert "锁已清除" in store.act("leave", room, lock_until="none")


def test_visits_idempotent_conflicts_and_missing_room(store):
    room = rid(store.act("enter"))
    payload = visit(room)
    assert store.save_visit(payload)["ok"]
    assert store.save_visit(payload)["replayed"]
    assert store.public_rooms()[0]["visit_count"] == 1
    assert store.public_rooms()[0]["total_duration_ms"] == 60000
    with pytest.raises(ValueError):
        store.save_visit(visit(room, duration_ms=10))
    with pytest.raises(ValueError):
        store.save_visit(visit("room_missing"))
    assert "私密过程" not in json.dumps(store.public_visits(), ensure_ascii=False)
    assert store.public_visits(before="2026-09-01") == []


def test_snapshot_frozen_per_session_and_key(store):
    room = rid(store.act("enter", title="我的礼物"))
    store.act("leave", note="旧便条")
    first = store.door_snapshot("s", "1:2026-10-02")
    assert first["content"].startswith("【我的房间 · 今天的门牌】")
    store.act("leave", room, note="新便条")
    assert store.door_snapshot("s", "1:2026-10-02") == first
    assert "新便条" in store.door_snapshot("s", "2:2026-10-02")["content"]
    assert "新便条" in store.door_snapshot("other", "1:2026-10-02")["content"]


def test_old_snapshot_title_upgrade_keeps_frozen_content(store):
    room = rid(store.act("enter"))
    store.act("leave", room, note="新便条")
    store.snapshots_path.write_text(json.dumps({"s": {"key": "same", "content": "【我的房间】\n旧便条", "created_at": "old"}}), encoding="utf-8")
    snapshot = store.door_snapshot("s", "same")
    assert snapshot == {"key": "same", "content": "【我的房间 · 今天的门牌】\n旧便条", "created_at": "old"}


def test_public_visit_kind_and_last_duration_without_process(store):
    room = rid(store.act("enter"))
    store.save_visit(visit(room, turn_kind="agent_wake"))
    public = store.public_visits()[0]
    assert public["turn_kind"] == "agent_wake" and "process" not in public
    assert store.public_detail(room)["last_visit_duration_ms"] == 60000


@pytest.mark.asyncio
async def test_handoff_has_quantity_only_room_section(store):
    import re
    room = rid(store.act("enter", title="私密标题"))
    store.act("leave", room, note="私密便条")
    source = Path(__file__).parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_build_handoff_breath", "_format_handoff_darkroom_door", "_handoff_portrait_stable_body"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    ns = {"re": re, "darkroom_store": store, "bucket_mgr": SimpleNamespace(list_all=AsyncMock(return_value=[])),
          "portrait_engine": SimpleNamespace(build_handoff_sections=lambda **kwargs: {}), "SELF_ANCHOR_TAG": "self",
          "logger": SimpleNamespace(warning=lambda *args: None),
          "_handoff_recent_continuity_is_natural": lambda value: False,
          "_merge_handoff_recent_continuity": lambda *args, **kwargs: "",
          "_remove_handoff_current_focus_overlap": lambda *args: "",
          "_trim_handoff_text_to_token_budget": lambda text, budget: text,
          "_format_budgeted_handoff_sections": lambda intro, sections, budget: intro + "\n" + "\n".join(f"=== {title} ===\n{body}" for title, body, *_ in sections)}
    for name in ("_format_handoff_personal_recent_continuity", "_format_handoff_recent_continuity", "_format_handoff_self_anchor", "_format_handoff_anchors", "_format_handoff_care_memos"):
        ns[name] = lambda *args, **kwargs: ""
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    text = await ns["_build_handoff_breath"]()
    assert "=== 言之的房间 ===\n言之有 1 间房间（1 间未打开），用 room list 查看" in text
    assert "私密标题" not in text and "私密便条" not in text


@pytest.mark.asyncio
async def test_rest_privacy_auth_and_opened_process(store):
    room = rid(store.act("enter", title="礼物"))
    store.act("write", content="私密正文")
    store.act("leave", note="续写便条")
    post = route("api_room_visits", store)
    req = SimpleNamespace(method="POST", json=AsyncMock(return_value=visit(room)))
    assert (await post(req)).status_code == 200
    for name, req in [
        ("api_rooms", SimpleNamespace()),
        ("api_room_visits", SimpleNamespace(method="GET", query_params={})),
        ("api_room_detail", SimpleNamespace(path_params={"id": room})),
    ]:
        result = await route(name, store)(req)
        text = result.body.decode()
        assert "私密正文" not in text and "私密过程" not in text and "续写便条" not in text
        assert "process" not in text and "note" not in text
        denied = await route(name, store, lambda request: JSONResponse({}, status_code=401))(req)
        assert denied.status_code == 401
    bad = await post(SimpleNamespace(method="POST", json=AsyncMock(return_value=visit("room_missing"))))
    assert bad.status_code == 400
    store.act("open", room)
    response = await route("api_room_detail", store)(SimpleNamespace(path_params={"id": room}))
    assert "私密正文" in response.body.decode() and "私密过程" in response.body.decode()
    assert "续写便条" not in response.body.decode()


def test_visit_read_limits_text_and_tool_summary(store):
    room = rid(store.act("enter"))
    store.save_visit(visit(room, process=[{"type": "thinking", "text": "x" * 5000},
                                        {"type": "text", "text": "y" * 1000},
                                        {"type": "tool", "tool": {"name": "Read", "input": {"path": "a"}, "result": "secret output"}}]))
    text = store.act("read", room, include_visits=True)
    assert text.count("x") == 4000 and "yyyy" not in text and "secret output" not in text


def test_opened_process_still_omits_tool_notes(store):
    room = rid(store.act("enter"))
    process = [{"type": "tool", "tool": {"name": "mcp__renamed__room", "input": {"action": "leave", "note": "私密便条"}, "result": "私密便条"}}]
    store.save_visit(visit(room, process=process))
    store.act("open", room)
    assert "私密便条" not in json.dumps(store.public_detail(room), ensure_ascii=False)
    assert "私密便条" in store.act("read", room, include_visits=True)


def test_room_auth_rejects_cookies_and_requires_service_bearer(monkeypatch):
    import hmac
    import os
    source = Path(__file__).parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_bearer_token", "_require_room_bearer")]
    ns = {"hmac": hmac, "os": os, "config": {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "service-token")
    auth = ns["_require_room_bearer"]
    assert auth(SimpleNamespace(headers={"authorization": "Bearer service-token"})) is None
    assert auth(SimpleNamespace(headers={"authorization": "Bearer wrong"})).status_code == 401
    assert auth(SimpleNamespace(headers={"cookie": "ombre_session=browser-cookie"})).status_code == 401
