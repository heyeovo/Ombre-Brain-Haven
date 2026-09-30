import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import sys

import frontmatter

from bucket_manager import BucketManager
from todo_store import TodoStore


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def load_route(namespace):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "api_todo_delete")
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace["api_todo_delete"]


def load_helper(name, namespace):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace[name]


def load_api_route(name, namespace):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace[name]


class FakeJSONResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code


class TodoDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_count_only_does_not_send_completed_rows(self):
        route = load_api_route("api_todos", {
            "_require_dashboard_auth": lambda request: None,
            "_list_todo_items": AsyncMock(return_value=[{"id": "one"}, {"id": "two"}]),
            "_int_between": lambda value, default, low, high: default,
        })
        responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
        with patch.dict(sys.modules, {"starlette.responses": responses}):
            result = await route(SimpleNamespace(query_params={"done": "true", "count_only": "1"}))
        self.assertEqual(result.body, {"count": 2, "todos": []})

    async def test_standalone_delete_and_missing(self):
        with tempfile.TemporaryDirectory() as root:
            store = TodoStore({"todo_db_path": str(Path(root) / "todos.sqlite")})
            item = store.create(content="待办", domain="tech", context="背景")
            route = load_route({
                "_require_dashboard_auth": lambda request: None,
                "todo_store": store,
            })
            responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
            with patch.dict(sys.modules, {"starlette.responses": responses}):
                result = await route(SimpleNamespace(path_params={"todo_id": item["id"]}))
                missing = await route(SimpleNamespace(path_params={"todo_id": item["id"]}))
            self.assertEqual(result.body, {"status": "deleted", "id": item["id"]})
            self.assertIsNone(store.get(item["id"]))
            self.assertEqual(missing.status_code, 404)

    async def test_bucket_todo_clears_only_todo_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "memory-1.md"
            path.write_text(frontmatter.dumps(frontmatter.Post("桶正文", id="memory-1", name="原桶", todo="待办", todo_done=True, todo_domain="tech")), encoding="utf-8")
            manager = BucketManager.__new__(BucketManager)
            manager._find_bucket_file = lambda bucket_id: str(path) if bucket_id == "memory-1" else None
            manager.get = AsyncMock(side_effect=lambda bucket_id: {"id": bucket_id, "metadata": dict(frontmatter.load(path).metadata), "content": frontmatter.load(path).content})
            manager.list_all = AsyncMock(side_effect=lambda **kwargs: [{"id": "memory-1", "metadata": dict(frontmatter.load(path).metadata), "content": frontmatter.load(path).content}])
            payload = load_helper("_todo_bucket_payload", {"TODO_DOMAINS": {"tech", "emotional"}})
            list_items = load_helper("_list_todo_items", {
                "TODO_DOMAINS": {"tech", "emotional"},
                "bucket_mgr": manager,
                "todo_store": SimpleNamespace(list=lambda **kwargs: []),
                "_todo_bucket_payload": payload,
            })
            route = load_route({
                "_require_dashboard_auth": lambda request: None,
                "bucket_mgr": manager,
                "_todo_bucket_payload": payload,
            })
            responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
            with patch.dict(sys.modules, {"starlette.responses": responses}):
                result = await route(SimpleNamespace(path_params={"todo_id": "bucket:memory-1"}))
            saved = frontmatter.load(path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(saved.content, "桶正文")
            self.assertEqual(saved["name"], "原桶")
            for key in ("todo", "todo_done", "todo_domain"):
                self.assertNotIn(key, saved.metadata)
            self.assertIsNone(payload(await manager.get("memory-1")))
            self.assertEqual(await list_items(), [])

    async def test_auth_before_mutation(self):
        denied = FakeJSONResponse({"error": "unauthorized"}, status_code=401)
        store = SimpleNamespace(delete=unittest.mock.Mock())
        route = load_route({"_require_dashboard_auth": lambda request: denied, "todo_store": store})
        responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
        with patch.dict(sys.modules, {"starlette.responses": responses}):
            result = await route(SimpleNamespace(path_params={"todo_id": "anything"}))
        self.assertIs(result, denied)
        store.delete.assert_not_called()
