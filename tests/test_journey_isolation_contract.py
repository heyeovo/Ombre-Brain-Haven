import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

from memory_layers import (
    LAYER_JOURNEY,
    can_bucket_be_related_target,
    can_moment_be_direct_seed,
    can_moment_be_related_target,
    infer_bucket_layer,
)


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
BUCKET_MANAGER_PATH = SERVER_PATH.with_name("bucket_manager.py")


def load_server_functions(*names: str, namespace: dict) -> dict:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    for node in selected:
        node.decorator_list = []
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace


def load_bucket_manager_class():
    tree = ast.parse(BUCKET_MANAGER_PATH.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BucketManager"
    )
    method_names = {
        "search",
        "list_journey_stages",
        "get_open_journey_stage",
        "create_journey_stage",
        "append_open_journey_stage",
        "close_open_journey_stage",
    }
    methods = [
        node for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in method_names
    ]
    isolated_class = ast.ClassDef(
        name="BucketManager",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.Module(body=[isolated_class], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "Optional": Optional,
        "logger": SimpleNamespace(warning=lambda *args, **kwargs: None),
    }
    exec(compile(module, str(BUCKET_MANAGER_PATH), "exec"), namespace)
    return namespace["BucketManager"]


BucketManager = load_bucket_manager_class()


def journey_bucket(**metadata):
    return {
        "id": "journey-1",
        "content": "# 完整轨迹\n这是一句目录摘要。\n这是不应进入目录的完整正文。",
        "metadata": {
            "name": "靠近与信任",
            "domain": ["journey"],
            "created": "2026-05-01",
            **metadata,
        },
    }


class JourneyLayerContractTest(unittest.TestCase):
    def test_journey_layer_wins_over_closed_and_pinned_flags(self):
        bucket = journey_bucket(resolved=True, pinned=True)

        self.assertEqual(infer_bucket_layer(bucket), LAYER_JOURNEY)
        self.assertFalse(can_bucket_be_related_target(bucket, explicit_lookup=True))

    def test_journey_moments_are_neither_direct_seeds_nor_related_targets(self):
        moment = {
            "bucket_id": "journey-1",
            "section": "body",
            "metadata": {"bucket_domain": ["journey"], "bucket_type": "dynamic"},
        }

        self.assertFalse(can_moment_be_direct_seed(moment))
        self.assertFalse(can_moment_be_direct_seed(moment, explicit_lookup=True))
        self.assertFalse(can_moment_be_related_target(moment, explicit_lookup=True))


class JourneySearchContractTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager_with(buckets):
        manager = object.__new__(BucketManager)
        manager.max_results = 5
        manager.embedding_engine = None
        manager.fuzzy_threshold = 0
        manager.keyword_bypass = False
        manager.precise_match_mode = False
        manager.token_exact_match = False
        manager.content_weight = 1.0
        manager.w_topic = 4.0
        manager.w_emotion = 2.0
        manager.w_time = 1.5
        manager.w_importance = 1.0
        manager.w_warmth = 0.0
        manager.title_hit_bonus = 0.0
        manager.keyword_first_sort = False
        manager.list_all = AsyncMock(return_value=list(buckets))
        manager.calc_topic_scores = lambda query, candidates: {
            bucket["id"]: 1.0 for bucket in candidates
        }
        manager._calc_topic_match = lambda query, bucket: {
            "score": 1.0,
            "field_scores": {"content": 100.0},
            "matched_in": ["content"],
        }
        manager._calc_emotion_score = lambda *args: 0.5
        manager._calc_time_score = lambda *args: 0.5
        return manager

    async def test_internal_search_excludes_journey_but_manual_search_can_opt_in(self):
        ordinary = {
            "id": "ordinary-1",
            "content": "信任",
            "metadata": {"name": "普通记忆", "domain": ["relationship"], "importance": 5},
        }
        journey = journey_bucket(importance=8)
        manager = self.manager_with([ordinary, journey])

        internal = await manager.search("信任", record_stats=False)
        manual = await manager.search("信任", record_stats=False, include_journey=True)

        self.assertEqual([item["id"] for item in internal], ["ordinary-1"])
        self.assertEqual({item["id"] for item in manual}, {"ordinary-1", "journey-1"})


class JourneyLifecycleContractTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager_with_store():
        manager = object.__new__(BucketManager)
        store = {}

        async def list_all(include_archive=False):
            return list(store.values())

        async def create(**kwargs):
            bucket_id = f"journey-{len(store) + 1}"
            metadata = {
                "name": kwargs.get("name"),
                "domain": kwargs.get("domain", []),
                "event_time": kwargs.get("event_time"),
                "source": kwargs.get("source"),
                **(kwargs.get("extra_metadata") or {}),
            }
            store[bucket_id] = {
                "id": bucket_id,
                "content": kwargs.get("content", ""),
                "metadata": metadata,
            }
            return bucket_id

        async def update(bucket_id, **kwargs):
            bucket = store.get(bucket_id)
            if not bucket:
                return False
            if "content" in kwargs:
                bucket["content"] = kwargs["content"]
            bucket["metadata"].update(kwargs.get("extra_metadata") or {})
            return True

        manager.list_all = list_all
        manager.create = create
        manager.update = update
        return manager, store

    async def test_open_stage_is_unique_and_background_append_is_idempotent(self):
        manager, store = self.manager_with_store()

        created = await manager.create_journey_stage(
            content="阶段初始状态",
            name="正在靠近",
            stage_start="2026-08-01",
            operation_id="weekly-create-1",
        )
        with self.assertRaises(ValueError):
            await manager.create_journey_stage(
                content="另一个开放阶段",
                name="不应创建",
                stage_start="2026-08-02",
            )

        first_append = await manager.append_open_journey_stage(
            content="本周关系状态延续。",
            summary="仍在靠近",
            source_bucket_ids=["memory-1"],
            operation_id="weekly-append-1",
        )
        duplicate = await manager.append_open_journey_stage(
            content="本周关系状态延续。",
            operation_id="weekly-append-1",
        )

        bucket = store[created["bucket_id"]]
        self.assertEqual(first_append["status"], "appended")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(bucket["content"].count("本周关系状态延续。"), 1)
        self.assertEqual(bucket["metadata"]["journey_source_bucket_ids"], ["memory-1"])

    async def test_closed_stage_rejects_background_append_but_stays_in_store(self):
        manager, store = self.manager_with_store()
        created = await manager.create_journey_stage(
            content="阶段初始状态",
            name="正在靠近",
            stage_start="2026-08-01",
        )

        result = await manager.close_open_journey_stage(
            stage_end="2026-08-10",
            operation_id="weekly-close-1",
        )
        duplicate = await manager.close_open_journey_stage(
            stage_end="2026-08-10",
            operation_id="weekly-close-1",
        )

        self.assertEqual(result["status"], "closed")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(store[created["bucket_id"]]["metadata"]["journey_status"], "closed")
        with self.assertRaises(ValueError):
            await manager.append_open_journey_stage(content="不应追加")


class JourneyPublicWriteContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_hold_journey_is_rejected_without_creating_bucket(self):
        bucket_mgr = SimpleNamespace(create=AsyncMock())
        namespace = {
            "decay_engine": SimpleNamespace(ensure_started=AsyncMock()),
            "bucket_mgr": bucket_mgr,
            "TODO_DOMAINS": {"tech", "emotional"},
            "_normalize_memory_sections_for_write": lambda text: text,
        }
        functions = load_server_functions("hold", namespace=namespace)

        result = await functions["hold"](content="阶段变化", journey=True)

        self.assertIn("普通聊天窗口不能创建", result)
        bucket_mgr.create.assert_not_awaited()

        domain_result = await functions["hold"](content="阶段变化", domain="journey")
        self.assertIn("普通聊天窗口不能创建", domain_result)
        bucket_mgr.create.assert_not_awaited()

    async def test_comment_and_trace_cannot_modify_journey(self):
        bucket = journey_bucket()
        bucket_mgr = SimpleNamespace(
            get=AsyncMock(return_value=bucket),
            add_comment=AsyncMock(),
            delete_comment=AsyncMock(),
            update=AsyncMock(),
        )
        namespace = {
            "bucket_mgr": bucket_mgr,
            "_coerce_memory_id": lambda value: value,
            "MEMORY_ID_RE": re.compile(r"^[A-Za-z0-9_-]+$"),
            "TODO_DOMAINS": {"tech", "emotional"},
        }
        functions = load_server_functions(
            "comment_bucket",
            "delete_bucket_comment",
            "trace",
            namespace=namespace,
        )

        comment_result = await functions["comment_bucket"]("journey-1", "尝试追加")
        delete_comment_result = await functions["delete_bucket_comment"]("journey-1", "ring-1")
        trace_result = await functions["trace"]("journey-1", content="尝试改正文")

        self.assertIn("不能修改轨迹桶", comment_result)
        self.assertIn("不能修改轨迹桶", delete_comment_result)
        self.assertIn("不能修改或删除轨迹桶", trace_result)
        bucket_mgr.add_comment.assert_not_awaited()
        bucket_mgr.delete_comment.assert_not_awaited()
        bucket_mgr.update.assert_not_awaited()


class JourneyDirectoryContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_directory_returns_metadata_and_one_line_summary_not_full_body(self):
        bucket = journey_bucket(
            journey_start="2026-05-01",
            journey_end="2026-06-15",
        )
        namespace = {
            "bucket_mgr": SimpleNamespace(list_all=AsyncMock(return_value=[bucket])),
            "logger": SimpleNamespace(error=lambda *args, **kwargs: None),
            "strip_wikilinks": lambda text: text,
            "_clip_text": lambda text, limit: text[:limit],
            "count_tokens_approx": len,
            "_trim_text_to_token_budget": lambda text, limit: text[:limit],
        }
        functions = load_server_functions(
            "_journey_directory_summary",
            "_journey_directory_entry",
            "_read_journey_directory",
            namespace=namespace,
        )

        result = await functions["_read_journey_directory"](max_results=20, max_tokens=1000)

        self.assertIn("[bucket_id:journey-1] 靠近与信任", result)
        self.assertIn("2026-05-01 → 2026-06-15", result)
        self.assertIn("这是一句目录摘要。", result)
        self.assertNotIn("这是不应进入目录的完整正文", result)

    def test_breath_routes_explicit_journey_to_directory_reader(self):
        tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
        breath_node = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "breath"
        )
        source = ast.get_source_segment(SERVER_PATH.read_text(encoding="utf-8"), breath_node) or ""

        self.assertIn('domain.strip().lower() == "journey"', source)
        self.assertIn("_read_journey_directory", source)


if __name__ == "__main__":
    unittest.main()
