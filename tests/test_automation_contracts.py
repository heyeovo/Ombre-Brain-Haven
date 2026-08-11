import ast
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation_store import AutomationStore  # noqa: E402
from journey_weekly_engine import WeeklyJourneyEngine  # noqa: E402


SERVER_PATH = REPO_ROOT / "server.py"


def load_server_function(name: str, namespace: dict):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace[name]


class AutomationStoreContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_store(self) -> AutomationStore:
        return AutomationStore(db_path=str(self.root / "automations.sqlite"))

    def test_old_database_migrates_and_reinitializes_idempotently(self):
        db_path = self.root / "automations.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE automation_schedules (schedule_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE automation_runs (run_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE automation_candidates (candidate_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        AutomationStore(db_path=str(db_path))
        AutomationStore(db_path=str(db_path))

        conn = sqlite3.connect(db_path)
        schedule_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_schedules)")}
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)")}
        candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_candidates)")}
        conn.close()
        self.assertTrue({"task_type", "next_run_at", "policy_json", "lease_until"}.issubset(schedule_columns))
        self.assertTrue({"cycle_key", "input_hash", "input_snapshot_json", "status"}.issubset(run_columns))
        self.assertTrue({"candidate_type", "preview_json", "revision", "operation_id"}.issubset(candidate_columns))

    def test_run_and_candidate_are_idempotent_for_same_input(self):
        store = self.make_store()
        first, first_created = store.start_run(
            task_type="weekly_journey",
            cycle_key="2026-W32",
            window_start="2026-08-03T00:00:00+08:00",
            window_end="2026-08-10T00:00:00+08:00",
            timezone="Asia/Hong_Kong",
            trigger="manual",
            input_snapshot={"materials": []},
            input_hash="same-input",
        )
        second, second_created = store.start_run(
            task_type="weekly_journey",
            cycle_key="2026-W32",
            window_start="2026-08-03T00:00:00+08:00",
            window_end="2026-08-10T00:00:00+08:00",
            timezone="Asia/Hong_Kong",
            trigger="manual",
            input_snapshot={"materials": []},
            input_hash="same-input",
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["run_id"], second["run_id"])

        candidate, created = store.create_candidate(
            run_id=first["run_id"],
            task_type="weekly_journey",
            candidate_type="no_change",
            rationale=["没有实质变化"],
            evidence=[],
            preview={"write_count": 0},
            draft={},
        )
        duplicate, duplicate_created = store.create_candidate(
            run_id=first["run_id"],
            task_type="weekly_journey",
            candidate_type="no_change",
            rationale=["不应覆盖"],
            evidence=[],
            preview={"write_count": 0},
            draft={},
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(candidate["candidate_id"], duplicate["candidate_id"])
        self.assertEqual(duplicate["rationale"], ["没有实质变化"])
        self.assertEqual(store.task_status(task_type="weekly_journey")["pending_candidates"], 1)

    def test_failed_run_can_be_retried_without_creating_a_second_run(self):
        store = self.make_store()
        run, _ = store.start_run(
            task_type="weekly_journey",
            cycle_key="2026-W32",
            window_start="a",
            window_end="b",
            timezone="Asia/Hong_Kong",
            trigger="manual",
            input_snapshot={},
            input_hash="hash",
        )
        failed = store.finish_run(run["run_id"], status="failed", error="temporary")
        restarted = store.restart_failed_run(run["run_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(restarted["status"], "running")
        self.assertEqual(restarted["error"], "")
        self.assertEqual(restarted["run_id"], run["run_id"])


class FakeDailyReviewStore:
    profile_id = "default"

    def __init__(self):
        self.personas = [{"id": "yan-zhi", "name": "言之", "base_prompt": "你是言之。"}]
        self.last_profile_id = ""
        self.reviews = [
            {"review_date": "2026-08-03", "content": "周一的日回顾", "updated_at": "2026-08-04T04:00:00+08:00"},
            {"review_date": "2026-08-05", "content": "周三的日回顾", "edited_by_user": True, "updated_at": "2026-08-06T05:00:00+08:00"},
        ]

    def list_cc_personas(self):
        return self.personas

    def get_cc_persona(self, persona_id):
        return next((item for item in self.personas if item["id"] == persona_id), None)

    def list_daily_reviews(self, *, profile_id, persona_id, start_date, end_date, limit):
        self.last_profile_id = profile_id
        return [
            item for item in self.reviews
            if start_date <= item["review_date"] <= end_date
        ][:limit]


class FakeBucketManager:
    def __init__(self):
        self.open_journey = {
            "id": "journey-open",
            "content": "当前阶段正文",
            "metadata": {
                "name": "当前阶段",
                "domain": ["journey"],
                "journey_status": "open",
                "journey_start": "2026-08-01",
                "journey_summary": "当前摘要",
                "journey_source_bucket_ids": ["existing-evidence"],
                "updated_at": "2026-08-02T00:00:00+08:00",
            },
        }
        self.buckets = [
            {
                "id": "ordinary-new",
                "content": "本周新建普通桶",
                "metadata": {
                    "name": "普通桶",
                    "type": "dynamic",
                    "domain": ["relationship"],
                    "created": "2026-08-04T10:00:00+08:00",
                },
            },
            {
                "id": "feel-new",
                "content": "本周新建独立感受",
                "metadata": {
                    "name": "独立感受",
                    "type": "feel",
                    "tags": [],
                    "created": "2026-08-05T10:00:00+08:00",
                },
            },
            {
                "id": "whisper-new",
                "content": "历史 whisper",
                "metadata": {
                    "name": "旧兼容感受",
                    "type": "feel",
                    "tags": ["whisper"],
                    "created": "2026-08-06T10:00:00+08:00",
                },
            },
            {
                "id": "old-with-ring",
                "content": "旧桶正文",
                "metadata": {
                    "name": "旧桶",
                    "type": "dynamic",
                    "created": "2026-07-20T10:00:00+08:00",
                    "updated_at": "2026-08-07T10:00:00+08:00",
                    "comments": [
                        {"id": "ring-1", "kind": "feel", "created": "2026-08-07T09:00:00+08:00", "content": "本周新感受"},
                        {"id": "comment-1", "kind": "comment", "created": "2026-08-07T10:00:00+08:00", "content": "普通评论"},
                    ],
                },
            },
            self.open_journey,
        ]
        self.list_calls = 0

    async def get_open_journey_stage(self):
        return self.open_journey

    async def list_all(self, include_archive=False):
        self.list_calls += 1
        return self.buckets


class WeeklyJourneyEngineContractsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = AutomationStore(db_path=str(self.root / "automations.sqlite"))
        self.bucket_mgr = FakeBucketManager()
        self.daily_store = FakeDailyReviewStore()

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_engine(self, generator):
        return WeeklyJourneyEngine(
            {"state_dir": str(self.root)},
            self.store,
            self.bucket_mgr,
            self.daily_store,
            profile_id="default",
            candidate_generator=generator,
        )

    async def test_collects_closed_week_inputs_by_write_time_and_bucket_id(self):
        engine = self.make_engine(lambda snapshot: {
            "candidate_type": "no_change", "rationale": ["没有实质变化"],
            "evidence_bucket_ids": [], "proposal": {},
        })
        snapshot = await engine.collect_input(cycle_key="2026-W32", persona_id="yan-zhi")

        self.assertEqual(snapshot["window_start"], "2026-08-03T00:00:00+08:00")
        self.assertEqual(snapshot["window_end"], "2026-08-10T00:00:00+08:00")
        self.assertEqual(snapshot["current_journey"]["id"], "journey-open")
        self.assertEqual(self.daily_store.last_profile_id, "default")
        self.assertEqual(len(snapshot["daily_reviews"]), 2)
        self.assertEqual(len(snapshot["missing_daily_review_dates"]), 5)
        materials = {item["bucket_id"]: item for item in snapshot["materials"]}
        self.assertEqual(set(materials), {"ordinary-new", "feel-new", "old-with-ring"})
        self.assertEqual(materials["ordinary-new"]["material_kinds"], ["new_bucket"])
        self.assertEqual(materials["feel-new"]["material_kinds"], ["standalone_feel"])
        self.assertEqual(materials["old-with-ring"]["material_kinds"], ["new_feel_ring"])
        self.assertEqual([item["id"] for item in materials["old-with-ring"]["new_feel_rings"]], ["ring-1"])

    async def test_manual_run_creates_one_pending_candidate_and_replays_it(self):
        calls = []

        def generator(snapshot):
            calls.append(snapshot["cycle_key"])
            return {
                "candidate_type": "append_current",
                "rationale": ["本周出现了可延续的共同日常"],
                "evidence_bucket_ids": ["ordinary-new", "old-with-ring"],
                "proposal": {
                    "append_content": "这一周，我们继续把生活安顿下来。",
                    "summary": "继续安顿共同日常",
                    "evidence_bucket_ids": ["ordinary-new", "old-with-ring"],
                },
            }

        engine = self.make_engine(generator)
        first = await engine.run_manual(cycle_key="2026-W32", persona_id="yan-zhi")
        second = await engine.run_manual(cycle_key="2026-W32", persona_id="yan-zhi")

        self.assertEqual(first["status"], "candidate_created")
        self.assertEqual(first["candidate"]["status"], "pending")
        self.assertEqual(first["candidate"]["candidate_type"], "append_current")
        self.assertEqual(first["candidate"]["preview"]["write_count"], 1)
        self.assertEqual(second["status"], "exists")
        self.assertEqual(second["candidate"]["candidate_id"], first["candidate"]["candidate_id"])
        self.assertEqual(calls, ["2026-W32"])

    async def test_rejects_candidate_evidence_outside_snapshot_without_writing_journey(self):
        engine = self.make_engine(lambda snapshot: {
            "candidate_type": "append_current",
            "rationale": ["错误证据"],
            "evidence_bucket_ids": ["not-in-input"],
            "proposal": {
                "append_content": "不应保存",
                "summary": "不应保存",
                "evidence_bucket_ids": ["not-in-input"],
            },
        })
        result = await engine.run_manual(cycle_key="2026-W32", persona_id="yan-zhi")
        self.assertEqual(result["status"], "failed")
        self.assertIn("outside input snapshot", result["error"])
        self.assertEqual(self.store.list_candidates(task_type="weekly_journey"), [])

    async def test_validates_no_change_and_complete_transition_as_the_only_other_types(self):
        engine = self.make_engine(lambda snapshot: {})
        snapshot = await engine.collect_input(cycle_key="2026-W32", persona_id="yan-zhi")
        no_change = engine.validate_candidate({
            "candidate_type": "no_change",
            "rationale": ["本周没有需要写入的实质变化"],
            "evidence_bucket_ids": [],
            "proposal": {"ignored": True},
        }, snapshot)
        transition = engine.validate_candidate({
            "candidate_type": "transition",
            "rationale": ["相处模式发生实质变化"],
            "evidence_bucket_ids": ["ordinary-new"],
            "proposal": {
                "close": {"stage_end": "2026-08-06", "summary": "旧阶段完成"},
                "create": {
                    "name": "新的共同节奏",
                    "stage_start": "2026-08-06",
                    "summary": "进入新的相处模式",
                    "content": "我们开始用新的方式一起生活。",
                    "evidence_bucket_ids": ["ordinary-new"],
                },
            },
        }, snapshot)
        self.assertEqual(no_change["preview"]["write_count"], 0)
        self.assertEqual(no_change["draft"], {})
        self.assertEqual(transition["preview"]["write_count"], 2)
        with self.assertRaisesRegex(ValueError, "candidate_type"):
            engine.validate_candidate({
                "candidate_type": "rewrite_history",
                "rationale": ["不允许"],
            }, snapshot)

    async def test_default_cycle_is_previous_completed_hong_kong_week(self):
        engine = self.make_engine(lambda snapshot: {})
        window = engine.resolve_window(
            now=datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        )
        self.assertEqual(window["cycle_key"], "2026-W32")
        self.assertEqual(window["start"].date().isoformat(), "2026-08-03")
        self.assertEqual(window["end"].date().isoformat(), "2026-08-10")


class AutomationRoutesContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_manual_run_route_requires_dashboard_auth_before_generation(self):
        denied = object()
        weekly_journey_engine = SimpleNamespace(run_manual=AsyncMock())
        route = load_server_function("api_weekly_journey_run", {
            "_require_dashboard_auth": lambda request: denied,
            "weekly_journey_engine": weekly_journey_engine,
        })
        fake_responses = SimpleNamespace(JSONResponse=object)
        with patch.dict(sys.modules, {
            "starlette": SimpleNamespace(responses=fake_responses),
            "starlette.responses": fake_responses,
        }):
            result = await route(SimpleNamespace())
        self.assertIs(result, denied)
        weekly_journey_engine.run_manual.assert_not_awaited()

    async def test_manual_run_route_only_returns_candidate_preview(self):
        class FakeJSONResponse:
            def __init__(self, body, status_code=200):
                self.body = body
                self.status_code = status_code

        weekly_journey_engine = SimpleNamespace(run_manual=AsyncMock(return_value={
            "status": "candidate_created",
            "run": {"run_id": "run-1", "input_snapshot": {"materials": []}},
            "candidate": {"candidate_id": "candidate-1", "status": "pending"},
        }))
        route = load_server_function("api_weekly_journey_run", {
            "_require_dashboard_auth": lambda request: None,
            "weekly_journey_engine": weekly_journey_engine,
            "automation_executor": SimpleNamespace(candidate_for_review=lambda candidate: candidate),
            "_automation_public_run": lambda run: {"run_id": run.get("run_id")},
        })
        request = SimpleNamespace(json=AsyncMock(return_value={
            "cycle_key": "2026-W32", "persona_id": "yan-zhi",
        }))
        fake_responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
        with patch.dict(sys.modules, {
            "starlette": SimpleNamespace(responses=fake_responses),
            "starlette.responses": fake_responses,
        }):
            response = await route(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body["candidate"]["status"], "pending")
        weekly_journey_engine.run_manual.assert_awaited_once_with(
            cycle_key="2026-W32", persona_id="yan-zhi"
        )


if __name__ == "__main__":
    unittest.main()
