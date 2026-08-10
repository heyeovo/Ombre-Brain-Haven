import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def load_server_function(name: str, namespace: dict):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace[name]


class FakeBucketManager:
    def __init__(self, buckets=None):
        self.buckets = list(buckets or [])
        self.created = []
        self.comments = []

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return f"feel-{len(self.created)}"

    async def get(self, bucket_id):
        return {"id": bucket_id, "metadata": {"name": "源记忆"}}

    async def add_comment(self, bucket_id, content, **kwargs):
        self.comments.append((bucket_id, content, kwargs))
        return {"id": "ring-1"}


class FeelWhisperContractTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def hold_namespace(bucket_mgr):
        return {
            "decay_engine": SimpleNamespace(ensure_started=AsyncMock()),
            "bucket_mgr": bucket_mgr,
            "TODO_DOMAINS": {"tech", "emotional"},
            "_queue_embedding_refresh": lambda bucket_id: None,
            "_hold_success": lambda action, bucket_id, bucket_name: {
                "status": "success",
                "action": action,
                "bucket_id": bucket_id,
                "bucket_name": bucket_name,
            },
        }

    @staticmethod
    def breath_namespace(bucket_mgr):
        def int_between(value, default, low, high):
            number = default if value is None else int(value)
            return max(low, min(high, number))

        def trim_to_budget(text, budget):
            return str(text)[:max(0, budget)]

        return {
            "decay_engine": SimpleNamespace(ensure_started=AsyncMock()),
            "bucket_mgr": bucket_mgr,
            "_int_between": int_between,
            "_bool_value": lambda value, default: bool(value) if value is not None else default,
            "_float_between": lambda value, default, low, high: max(low, min(high, float(value))),
            "_normalize_direct_render_mode": lambda value: value,
            "_normalize_retrieval_mode": lambda value: value,
            "_normalize_breath_mode": lambda value: value,
            "parse_human_date_reference": lambda value: None,
            "_breath_query_requests_date_read": lambda value: False,
            "_is_self_anchor_domain": lambda value: False,
            "_is_pending_followup_domain": lambda value: False,
            "_breath_query_requests_pending_followups": lambda value: False,
            "_is_daily_impression_feel_bucket": lambda bucket: "daily_impression" in bucket["metadata"].get("tags", []),
            "_bucket_matches_breath_date": lambda bucket, date_key: True,
            "strip_wikilinks": lambda value: value,
            "count_tokens_approx": len,
            "_trim_text_to_token_budget": trim_to_budget,
            "logger": SimpleNamespace(error=lambda *args, **kwargs: None),
        }

    async def test_hold_feel_creates_standalone_feel_without_whisper_tag(self):
        bucket_mgr = FakeBucketManager()
        hold = load_server_function("hold", self.hold_namespace(bucket_mgr))

        result = await hold(
            content="我忽然感到安心。",
            tags="quiet",
            feel=True,
            valence=0.8,
            arousal=0.2,
        )

        self.assertEqual(result["action"], "created")
        self.assertEqual(bucket_mgr.created[0]["bucket_type"], "feel")
        self.assertEqual(bucket_mgr.created[0]["tags"], ["quiet"])
        self.assertNotIn("whisper", bucket_mgr.created[0]["tags"])
        self.assertEqual(bucket_mgr.created[0]["valence"], 0.8)
        self.assertEqual(bucket_mgr.created[0]["arousal"], 0.2)

    async def test_hold_whisper_remains_compatible_but_separate(self):
        bucket_mgr = FakeBucketManager()
        hold = load_server_function("hold", self.hold_namespace(bucket_mgr))

        result = await hold(content="旧客户端悄悄话", whisper=True)

        self.assertEqual(result["action"], "created")
        self.assertEqual(bucket_mgr.created[0]["bucket_type"], "feel")
        self.assertIn("whisper", bucket_mgr.created[0]["tags"])

    async def test_sourced_feel_is_directed_to_comment_bucket(self):
        bucket_mgr = FakeBucketManager()
        hold = load_server_function("hold", self.hold_namespace(bucket_mgr))

        result = await hold(content="新的感受", feel=True, source_bucket="memory-1")

        self.assertIn("comment_bucket", result)
        self.assertEqual(bucket_mgr.created, [])
        self.assertEqual(bucket_mgr.comments, [])

    async def test_comment_bucket_keeps_feel_as_source_ring(self):
        bucket_mgr = FakeBucketManager()
        namespace = {
            "bucket_mgr": bucket_mgr,
            "_coerce_memory_id": lambda value: value,
            "MEMORY_ID_RE": re.compile(r"^[A-Za-z0-9_-]+$"),
            "_ai_author_name": lambda: "言之",
            "_queue_embedding_refresh": lambda bucket_id: None,
        }
        comment_bucket = load_server_function("comment_bucket", namespace)

        result = await comment_bucket("memory-1", "我仍然在意。", kind="feel", valence=0.4)

        self.assertEqual(result, "年轮→memory-1#ring-1")
        bucket_id, content, kwargs = bucket_mgr.comments[0]
        self.assertEqual((bucket_id, content), ("memory-1", "我仍然在意。"))
        self.assertEqual(kwargs["kind"], "feel")
        self.assertEqual(kwargs["source"], "comment_bucket")

    async def test_breath_feel_excludes_whisper_and_obeys_max_results(self):
        buckets = [
            {
                "id": f"feel-{index}",
                "content": f"独立感受 {index}",
                "metadata": {"type": "feel", "tags": [], "created": f"2026-08-{index + 1:02d}"},
            }
            for index in range(5)
        ]
        buckets.extend([
            {
                "id": "legacy-whisper",
                "content": "旧 whisper",
                "metadata": {"type": "feel", "tags": ["whisper"], "created": "2026-08-10"},
            },
            {
                "id": "daily-feel",
                "content": "日印象",
                "metadata": {"type": "feel", "tags": ["daily_impression"], "created": "2026-08-09"},
            },
        ])
        bucket_mgr = FakeBucketManager(buckets)
        breath = load_server_function("breath", self.breath_namespace(bucket_mgr))

        result = await breath(domain="feel", max_results=3, max_tokens=1000)

        self.assertEqual(result.count("[bucket_id:"), 3)
        self.assertNotIn("legacy-whisper", result)
        self.assertNotIn("daily-feel", result)

    async def test_breath_feel_obeys_token_budget_and_whisper_channel(self):
        buckets = [
            {
                "id": "standalone-feel",
                "content": "很长的独立感受" * 30,
                "metadata": {"type": "feel", "tags": [], "created": "2026-08-09"},
            },
            {
                "id": "legacy-whisper",
                "content": "兼容悄悄话",
                "metadata": {"type": "feel", "tags": ["whisper"], "created": "2026-08-10"},
            },
        ]
        bucket_mgr = FakeBucketManager(buckets)
        breath = load_server_function("breath", self.breath_namespace(bucket_mgr))

        feel_result = await breath(domain="feel", max_results=20, max_tokens=80)
        whisper_result = await breath(domain="whisper", max_results=20, max_tokens=1000)

        self.assertLessEqual(len(feel_result), 80)
        self.assertNotIn("legacy-whisper", feel_result)
        self.assertIn("legacy-whisper", whisper_result)
        self.assertNotIn("standalone-feel", whisper_result)


if __name__ == "__main__":
    unittest.main()
