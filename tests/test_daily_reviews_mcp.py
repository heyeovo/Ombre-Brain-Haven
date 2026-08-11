import ast
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
LOCAL_TZ = timezone(timedelta(hours=8))


def load_server_functions(*names: str, namespace: dict) -> dict:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    for node in selected:
        node.decorator_list = []
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace


def count_tokens_approx(text: str) -> int:
    return max(1, (len(str(text or "")) + 2) // 3)


def trim_text(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    value = str(text or "")
    while value and count_tokens_approx(value) > budget:
        value = value[: max(0, int(len(value) * 0.8))].rstrip()
    return value


class FakeDailyReviewStore:
    def __init__(self):
        self.personas = [{"id": "yan", "name": "言之"}]
        self.rows = []
        self.list_args = None
        self.frozen_snapshot = [{"review_date": "2026-08-09", "content": "创建窗口时的旧快照"}]

    def list_cc_personas(self):
        return list(self.personas)

    def list_daily_reviews(self, **kwargs):
        self.list_args = kwargs
        return list(self.rows)


def tool_namespace(store: FakeDailyReviewStore):
    def int_between(value, default, low, high):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "LOCAL_TZ": LOCAL_TZ,
        "gateway_state_store": store,
        "persona_engine": SimpleNamespace(profile_id="profile-a"),
        "_int_between": int_between,
        "count_tokens_approx": count_tokens_approx,
        "_trim_text_to_token_budget": trim_text,
        "_json_lib": json,
    }
    return load_server_functions(
        "_daily_review_date_range",
        "_resolve_daily_review_persona",
        "_daily_review_payload_with_budget",
        "read_daily_reviews",
        namespace=namespace,
    )


class DailyReviewMcpContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_range_is_inclusive_and_hides_source_sessions(self):
        store = FakeDailyReviewStore()
        store.rows = [
            {
                "review_date": "2026-08-08",
                "content": "第一天",
                "edited_by_user": False,
                "updated_at": "2026-08-09T04:00:00+00:00",
                "source_session_ids": ["secret-window"],
            },
            {
                "review_date": "2026-08-10",
                "content": "第三天手动修订",
                "edited_by_user": True,
                "updated_at": "2026-08-11T05:00:00+00:00",
                "source_session_ids": ["another-secret"],
            },
        ]
        functions = tool_namespace(store)

        result = await functions["read_daily_reviews"](
            start_date="2026-08-08",
            end_date="2026-08-10",
            persona_id="yan",
            max_tokens=2000,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual([item["date"] for item in result["reviews"]], ["2026-08-08", "2026-08-10"])
        self.assertEqual(result["missing_dates"], ["2026-08-09"])
        self.assertTrue(result["reviews"][1]["edited_by_user"])
        self.assertEqual(store.list_args["profile_id"], "profile-a")
        self.assertEqual(store.list_args["limit"], 3)
        self.assertNotIn("source_session_ids", json.dumps(result, ensure_ascii=False))

    async def test_last_days_uses_latest_ended_hong_kong_days(self):
        store = FakeDailyReviewStore()
        functions = tool_namespace(store)

        start, end, dates = functions["_daily_review_date_range"](
            last_days=3,
            today=date(2026, 8, 12),
        )

        self.assertEqual(start, "2026-08-09")
        self.assertEqual(end, "2026-08-11")
        self.assertEqual(dates, ["2026-08-09", "2026-08-10", "2026-08-11"])

    async def test_omitted_persona_requires_a_choice_when_multiple_exist(self):
        store = FakeDailyReviewStore()
        store.personas.append({"id": "ombre", "name": "Ombre"})
        functions = tool_namespace(store)

        result = await functions["read_daily_reviews"](
            start_date="2026-08-09",
            end_date="2026-08-09",
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("persona_id is required", result["error"])
        self.assertIsNone(store.list_args)

    async def test_max_tokens_truncates_without_mutating_reviews_or_frozen_snapshot(self):
        store = FakeDailyReviewStore()
        long_content = "很长的日回顾。" * 600
        store.rows = [{
            "review_date": "2026-08-09",
            "content": long_content,
            "edited_by_user": False,
            "updated_at": "2026-08-10T04:00:00+00:00",
        }]
        original_rows = [dict(item) for item in store.rows]
        original_snapshot = [dict(item) for item in store.frozen_snapshot]
        functions = tool_namespace(store)

        result = await functions["read_daily_reviews"](
            start_date="2026-08-09",
            end_date="2026-08-09",
            persona_id="yan",
            max_tokens=240,
        )

        self.assertTrue(result["truncated"])
        self.assertLessEqual(count_tokens_approx(json.dumps(result, ensure_ascii=False, sort_keys=True)), 240)
        self.assertEqual(store.rows, original_rows)
        self.assertEqual(store.frozen_snapshot, original_snapshot)
        self.assertFalse(hasattr(store, "upsert_daily_review"))

    async def test_rejects_ambiguous_range_and_last_days(self):
        store = FakeDailyReviewStore()
        functions = tool_namespace(store)
        result = await functions["read_daily_reviews"](
            start_date="2026-08-09",
            end_date="2026-08-10",
            last_days=2,
            persona_id="yan",
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("either", result["error"])
        self.assertIsNone(store.list_args)


if __name__ == "__main__":
    unittest.main()
