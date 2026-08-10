import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
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
    search_method = next(
        node for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "search"
    )
    isolated_class = ast.ClassDef(
        name="BucketManager",
        bases=[],
        keywords=[],
        body=[search_method],
        decorator_list=[],
    )
    module = ast.Module(body=[isolated_class], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
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
