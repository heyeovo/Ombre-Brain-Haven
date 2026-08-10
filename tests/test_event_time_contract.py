import ast
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
BUCKET_MANAGER_PATH = SERVER_PATH.with_name("bucket_manager.py")
UTILS_PATH = SERVER_PATH.with_name("utils.py")


def load_top_level_function(path: Path, name: str, namespace: dict):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


LOCAL_TZ = timezone(timedelta(hours=8))
local_date_key = load_top_level_function(
    UTILS_PATH,
    "local_date_key",
    {
        "re": re,
        "datetime": datetime,
        "LOCAL_TZ": LOCAL_TZ,
        "parse_human_date_reference": lambda value, tz: None,
    },
)


def load_server_functions(*names: str) -> dict:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    namespace = {"local_date_key": local_date_key, "re": re}
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace


class EventTimeBreathContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        functions = load_server_functions(
            "_bucket_matches_breath_date",
            "_breath_date_bucket_sort_key",
            "_date_yyyy_mm_dd",
            "_bucket_date_meta_parts",
        )
        cls.matches_date = staticmethod(functions["_bucket_matches_breath_date"])
        cls.sort_key = staticmethod(functions["_breath_date_bucket_sort_key"])
        cls.date_parts = staticmethod(functions["_bucket_date_meta_parts"])

    def test_event_time_matches_local_calendar_day_not_created_day(self):
        bucket = {
            "metadata": {
                "event_time": "2026-07-31T18:30:00+00:00",
                "created": "2026-08-10T12:00:00+08:00",
                "updated_at": "2026-08-10T12:30:00+08:00",
            }
        }

        self.assertTrue(self.matches_date(bucket, "2026-08-01"))
        self.assertFalse(self.matches_date(bucket, "2026-08-10"))

    def test_legacy_date_precedes_created_when_event_time_is_missing(self):
        bucket = {
            "metadata": {
                "date": "2026-08-01",
                "created": "2026-08-10T12:00:00+08:00",
            }
        }

        self.assertTrue(self.matches_date(bucket, "2026-08-01"))
        self.assertFalse(self.matches_date(bucket, "2026-08-10"))

    def test_created_timestamps_are_only_fallback_without_event_dates(self):
        bucket = {"metadata": {"created": "2026-08-10T12:00:00+08:00"}}

        self.assertTrue(self.matches_date(bucket, "2026-08-10"))

    def test_sort_and_render_use_event_time_before_legacy_date(self):
        bucket = {
            "metadata": {
                "event_time": "2026-08-01T09:15:00+08:00",
                "date": "2026-08-02",
                "created": "2026-08-10T12:00:00+08:00",
                "importance": 7,
            }
        }

        self.assertEqual(self.sort_key(bucket), ("2026-08-01T09:15:00+08:00", 7))
        self.assertEqual(self.date_parts(bucket), ["[date:2026-08-01]"])


class EventTimeBreathReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_date_read_returns_event_day_and_not_bucket_creation_day(self):
        bucket = {
            "id": "event-on-august-first",
            "content": "这件事发生在八月一日。",
            "metadata": {
                "name": "八月一日事件",
                "event_time": "2026-08-01T15:32:18+08:00",
                "created": "2026-08-10T12:00:00+08:00",
                "type": "dynamic",
            },
        }

        class FakeBucketManager:
            async def list_all(self, include_archive=False):
                return [bucket]

        helpers = load_server_functions(
            "_bucket_matches_breath_date",
            "_breath_date_bucket_sort_key",
            "_date_yyyy_mm_dd",
            "_bucket_date_meta_parts",
        )
        namespace = {
            **helpers,
            "bucket_mgr": FakeBucketManager(),
            "logger": type("Logger", (), {"error": staticmethod(lambda *args: None)})(),
            "_breath_date_topic_terms": lambda query: [],
            "is_self_anchor_bucket": lambda item: False,
            "_breath_date_text_has_topic_terms": lambda text, terms: True,
            "_breath_date_bucket_text": lambda item: item.get("content", ""),
            "_clip_text": lambda text, limit: text[:limit],
            "_rendered_bucket_content": lambda item: item.get("content", ""),
            "count_tokens_approx": len,
        }
        read_breath_date = load_top_level_function(SERVER_PATH, "_read_breath_date", namespace)

        event_day = await read_breath_date(
            date_key="2026-08-01",
            label="2026-08-01",
            query="",
            max_tokens=1000,
            max_results=20,
        )
        created_day = await read_breath_date(
            date_key="2026-08-10",
            label="2026-08-10",
            query="",
            max_tokens=1000,
            max_results=20,
        )

        self.assertIn("event-on-august-first", event_day)
        self.assertIn("这件事发生在八月一日", event_day)
        self.assertEqual(created_day, "2026-08-10 没有找到普通记忆。")


class EventTimeMigrationContractTest(unittest.TestCase):
    def test_migration_preserves_existing_event_time_and_prefers_legacy_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directories = [root / name for name in ("permanent", "dynamic", "feel", "archive")]
            for directory in directories:
                directory.mkdir()

            paths = [
                directories[0] / "existing.md",
                directories[1] / "legacy.md",
                directories[2] / "created.md",
            ]
            posts = {
                str(paths[0]): {
                    "event_time": "2026-08-03T14:25:00+08:00",
                    "date": "2026-08-01",
                    "created": "2026-08-10T12:00:00+08:00",
                },
                str(paths[1]): {
                    "date": "2026-08-01",
                    "created": "2026-08-10T12:00:00+08:00",
                },
                str(paths[2]): {"created": "2026-08-09T08:00:00+08:00"},
            }
            for path in paths:
                path.write_text("placeholder", encoding="utf-8")

            class FakeFrontmatter:
                @staticmethod
                def load(path):
                    return posts[str(path)]

                @staticmethod
                def dumps(post):
                    return str(post)

            tree = ast.parse(BUCKET_MANAGER_PATH.read_text(encoding="utf-8"))
            class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BucketManager")
            migrate_method = next(
                node for node in class_node.body
                if isinstance(node, ast.FunctionDef) and node.name == "_migrate_event_time"
            )
            isolated_class = ast.ClassDef(
                name="BucketManager",
                bases=[],
                keywords=[],
                body=[migrate_method],
                decorator_list=[],
            )
            module = ast.Module(body=[isolated_class], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {
                "os": os,
                "frontmatter": FakeFrontmatter,
                "logger": type("Logger", (), {"info": staticmethod(lambda *args: None)})(),
            }
            exec(compile(module, str(BUCKET_MANAGER_PATH), "exec"), namespace)

            manager = namespace["BucketManager"]()
            manager.permanent_dir, manager.dynamic_dir, manager.feel_dir, manager.archive_dir = map(str, directories)
            manager._migrate_event_time()

            self.assertEqual(posts[str(paths[0])].get("event_time"), "2026-08-03T14:25:00+08:00")
            self.assertEqual(posts[str(paths[1])].get("event_time"), "2026-08-01")
            self.assertEqual(posts[str(paths[2])].get("event_time"), "2026-08-09T08:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
