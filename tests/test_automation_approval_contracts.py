import asyncio
import ast
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation_executor import AutomationExecutor  # noqa: E402
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


class FakeJSONResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code


class FakeDailyStore:
    profile_id = "default"


class ApprovalBucketManager:
    def __init__(self):
        self.open_journey = {
            "id": "journey-open",
            "content": "当前正文",
            "metadata": {
                "name": "当前阶段",
                "domain": ["journey"],
                "journey_status": "open",
                "journey_start": "2026-08-01",
                "journey_summary": "当前摘要",
                "journey_source_bucket_ids": [],
                "journey_operation_ids": [],
                "updated_at": "2026-08-02T00:00:00+08:00",
            },
        }
        self.evidence = {
            "evidence-1": {
                "id": "evidence-1",
                "content": "本周证据",
                "metadata": {"name": "证据一", "domain": ["relationship"]},
            },
        }
        self.stages = [self.open_journey]
        self.append_calls = []
        self.close_calls = []
        self.create_calls = []
        self.create_failures = 0
        self.append_delay = 0.0

    async def get(self, bucket_id):
        if self.open_journey and self.open_journey.get("id") == bucket_id:
            return self.open_journey
        for stage in self.stages:
            if stage.get("id") == bucket_id:
                return stage
        return self.evidence.get(bucket_id)

    async def list_journey_stages(self):
        return list(self.stages)

    async def get_open_journey_stage(self):
        return self.open_journey

    async def append_open_journey_stage(self, **kwargs):
        self.append_calls.append(dict(kwargs))
        if self.append_delay:
            await asyncio.sleep(self.append_delay)
        operation_id = kwargs["operation_id"]
        operations = self.open_journey["metadata"].setdefault("journey_operation_ids", [])
        if operation_id in operations:
            return {"status": "duplicate", "bucket_id": self.open_journey["id"]}
        operations.append(operation_id)
        self.open_journey["content"] += "\n\n" + kwargs["content"]
        self.open_journey["metadata"]["journey_summary"] = kwargs["summary"]
        return {"status": "appended", "bucket_id": self.open_journey["id"]}

    async def close_open_journey_stage(self, **kwargs):
        self.close_calls.append(dict(kwargs))
        operation_id = kwargs["operation_id"]
        for stage in self.stages:
            if operation_id in stage["metadata"].get("journey_operation_ids", []):
                return {"status": "duplicate", "bucket_id": stage["id"]}
        stage = self.open_journey
        stage["metadata"]["journey_operation_ids"].append(operation_id)
        stage["metadata"]["journey_status"] = "closed"
        stage["metadata"]["journey_end"] = kwargs["stage_end"]
        stage["metadata"]["journey_summary"] = kwargs["summary"]
        self.open_journey = None
        return {"status": "closed", "bucket_id": stage["id"]}

    async def create_journey_stage(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        operation_id = kwargs["operation_id"]
        for stage in self.stages:
            if operation_id in stage["metadata"].get("journey_operation_ids", []):
                return {"status": "duplicate", "bucket_id": stage["id"]}
        if self.create_failures:
            self.create_failures -= 1
            raise RuntimeError("temporary create failure")
        stage = {
            "id": "journey-created",
            "content": kwargs["content"],
            "metadata": {
                "name": kwargs["name"],
                "domain": ["journey"],
                "journey_status": "open",
                "journey_start": kwargs["stage_start"],
                "journey_summary": kwargs["summary"],
                "journey_source_bucket_ids": kwargs["source_bucket_ids"],
                "journey_operation_ids": [operation_id],
                "updated_at": "2026-08-12T00:00:00+08:00",
            },
        }
        self.stages.append(stage)
        self.open_journey = stage
        return {"status": "created", "bucket_id": stage["id"]}


class AutomationApprovalContractsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = AutomationStore(db_path=str(self.root / "automations.sqlite"))
        self.bucket_mgr = ApprovalBucketManager()
        self.engine = WeeklyJourneyEngine(
            {"state_dir": str(self.root)},
            self.store,
            self.bucket_mgr,
            FakeDailyStore(),
            candidate_generator=lambda snapshot: {},
        )
        self.executor = AutomationExecutor(self.store, self.bucket_mgr, self.engine)
        schedule = self.store.get_schedule(task_type="weekly_journey")
        policy = dict(schedule.get("policy") or {})
        policy["reviewed_through_date"] = "2026-08-02"
        self.store.update_schedule(
            task_type="weekly_journey", enabled=False, timezone="Asia/Hong_Kong",
            policy=policy, next_run_at="",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_candidate(self, candidate_type="append_current"):
        snapshot = {
            "cycle_key": "2026-08-03_2026-08-09",
            "window_start": "2026-08-03T04:00:00+08:00",
            "window_end": "2026-08-10T04:00:00+08:00",
            "reviewed_through_date": "2026-08-02",
            "review_start_date": "2026-08-03",
            "review_end_date": "2026-08-09",
            "timezone": "Asia/Hong_Kong",
            "current_journey": self.engine._journey_snapshot(self.bucket_mgr.open_journey),
            "materials": [
                {"bucket_id": "evidence-1", "bucket_name": "证据一"},
            ],
        }
        if candidate_type == "no_change":
            raw = {
                "candidate_type": "no_change",
                "rationale": ["本周没有实质变化"],
                "evidence_bucket_ids": [],
                "proposal": {},
            }
        elif candidate_type == "transition":
            raw = {
                "candidate_type": "transition",
                "rationale": ["相处模式发生变化"],
                "evidence_bucket_ids": ["evidence-1"],
                "proposal": {
                    "close": {"stage_end": "2026-08-06", "summary": "旧阶段完成"},
                    "create": {
                        "name": "新阶段",
                        "stage_start": "2026-08-06",
                        "summary": "进入新的相处模式",
                        "content": "我们开始新的共同生活。",
                        "evidence_bucket_ids": ["evidence-1"],
                    },
                },
            }
        else:
            raw = {
                "candidate_type": "append_current",
                "rationale": ["共同日常继续生长"],
                "evidence_bucket_ids": ["evidence-1"],
                "proposal": {
                    "append_content": "这一周，我们继续安顿生活。",
                    "summary": "继续安顿共同日常",
                    "evidence_bucket_ids": ["evidence-1"],
                },
            }
        normalized = self.engine.validate_candidate(raw, snapshot)
        run, _ = self.store.start_run(
            task_type="weekly_journey",
            cycle_key=snapshot["cycle_key"],
            window_start=snapshot["window_start"],
            window_end=snapshot["window_end"],
            timezone="Asia/Hong_Kong",
            trigger="manual",
            input_snapshot=snapshot,
            input_hash=f"hash-{candidate_type}-{len(self.store.list_candidates(task_type='weekly_journey'))}",
        )
        candidate, _ = self.store.create_candidate(
            run_id=run["run_id"], task_type="weekly_journey", **normalized,
        )
        return self.executor.candidate_for_review(candidate)

    async def confirm(self, candidate):
        return await self.executor.confirm_candidate(
            candidate["candidate_id"],
            expected_revision=candidate["revision"],
            approved_payload_hash=candidate["draft_payload_hash"],
        )

    async def test_edit_creates_revision_and_preserves_original_preview(self):
        candidate = self.make_candidate()
        original_preview = candidate["preview"]
        edited = self.executor.edit_candidate(
            candidate["candidate_id"],
            expected_revision=1,
            draft={
                "append_content": "人工编辑后的正文。",
                "summary": "人工编辑后的摘要",
                "evidence_bucket_ids": ["evidence-1"],
            },
        )
        self.assertEqual(edited["status"], "updated")
        self.assertEqual(edited["candidate"]["revision"], 2)
        self.assertEqual(edited["candidate"]["preview"], original_preview)
        self.assertEqual(edited["candidate"]["draft"]["append_content"], "人工编辑后的正文。")

    async def test_reject_is_zero_write(self):
        candidate = self.make_candidate()
        result = self.executor.reject_candidate(candidate["candidate_id"], expected_revision=1)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["candidate"]["status"], "rejected")
        self.assertEqual(self.bucket_mgr.append_calls + self.bucket_mgr.close_calls + self.bucket_mgr.create_calls, [])
        self.assertEqual(
            self.store.get_schedule(task_type="weekly_journey")["policy"]["reviewed_through_date"],
            "2026-08-02",
        )


    async def test_duplicate_confirmation_replays_one_append(self):
        candidate = self.make_candidate()
        first = await self.confirm(candidate)
        second = await self.confirm(candidate)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "replayed")
        self.assertEqual(len(self.bucket_mgr.append_calls), 1)
        self.assertEqual(first["result"], second["result"])

    async def test_concurrent_confirmation_has_one_executor(self):
        candidate = self.make_candidate()
        self.bucket_mgr.append_delay = 0.05
        first, second = await asyncio.gather(self.confirm(candidate), self.confirm(candidate))
        self.assertEqual({first["status"], second["status"]}, {"completed", "in_progress"})
        self.assertEqual(len(self.bucket_mgr.append_calls), 1)

    async def test_old_revision_confirmation_does_not_poison_new_revision(self):
        candidate = self.make_candidate()
        old_hash = candidate["draft_payload_hash"]
        edited = self.executor.edit_candidate(
            candidate["candidate_id"], expected_revision=1,
            draft={
                "append_content": "新 revision 正文",
                "summary": "新 revision 摘要",
                "evidence_bucket_ids": ["evidence-1"],
            },
        )["candidate"]
        result = await self.executor.confirm_candidate(
            candidate["candidate_id"], expected_revision=1, approved_payload_hash=old_hash,
        )
        self.assertEqual(result["status"], "revision_mismatch")
        self.assertEqual(self.store.get_candidate(candidate["candidate_id"])["status"], "pending")
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(self.bucket_mgr.append_calls, [])

    async def test_transition_retries_create_after_close_succeeded(self):
        candidate = self.make_candidate("transition")
        self.bucket_mgr.create_failures = 1
        first = await self.confirm(candidate)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(
            self.store.get_schedule(task_type="weekly_journey")["policy"]["reviewed_through_date"],
            "2026-08-02",
        )
        self.assertEqual(len(self.bucket_mgr.close_calls), 1)
        self.assertEqual(len(self.bucket_mgr.create_calls), 1)
        frozen = self.store.get_candidate(candidate["candidate_id"])
        second = await self.executor.confirm_candidate(
            candidate["candidate_id"],
            expected_revision=frozen["revision"],
            approved_payload_hash=frozen["approved_payload_hash"],
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.bucket_mgr.close_calls), 1)
        self.assertEqual(len(self.bucket_mgr.create_calls), 2)
        self.assertTrue(self.bucket_mgr.close_calls[0]["operation_id"].endswith(":close"))
        self.assertTrue(self.bucket_mgr.create_calls[0]["operation_id"].endswith(":create"))
        self.assertEqual(
            self.bucket_mgr.create_calls[0]["operation_id"],
            self.bucket_mgr.create_calls[1]["operation_id"],
        )

    async def test_transition_retry_conflicts_if_another_open_stage_appears(self):
        candidate = self.make_candidate("transition")
        self.bucket_mgr.create_failures = 1
        first = await self.confirm(candidate)
        self.assertEqual(first["status"], "failed")
        external = {
            "id": "journey-external",
            "content": "外部新阶段",
            "metadata": {
                "name": "外部新阶段", "domain": ["journey"], "journey_status": "open",
                "journey_start": "2026-08-07", "journey_summary": "外部变化",
                "journey_source_bucket_ids": [], "journey_operation_ids": [],
                "updated_at": "2026-08-12T01:00:00+08:00",
            },
        }
        self.bucket_mgr.stages.append(external)
        self.bucket_mgr.open_journey = external
        frozen = self.store.get_candidate(candidate["candidate_id"])
        second = await self.executor.confirm_candidate(
            candidate["candidate_id"], expected_revision=frozen["revision"],
            approved_payload_hash=frozen["approved_payload_hash"],
        )
        self.assertEqual(second["status"], "conflict")
        self.assertEqual(second["conflict"]["code"], "unexpected_open_journey")
        self.assertEqual(len(self.bucket_mgr.create_calls), 1)

    async def test_frozen_approved_payload_tampering_becomes_conflict(self):
        candidate = self.make_candidate("transition")
        self.bucket_mgr.create_failures = 1
        first = await self.confirm(candidate)
        self.assertEqual(first["status"], "failed")
        frozen = self.store.get_candidate(candidate["candidate_id"])
        conn = sqlite3.connect(self.store.db_path)
        conn.execute(
            "UPDATE automation_candidates SET approved_payload_json = ? WHERE candidate_id = ?",
            ('{"tampered":true}', candidate["candidate_id"]),
        )
        conn.commit()
        conn.close()
        second = await self.executor.confirm_candidate(
            candidate["candidate_id"], expected_revision=frozen["revision"],
            approved_payload_hash=frozen["approved_payload_hash"],
        )
        self.assertEqual(second["status"], "conflict")
        self.assertEqual(second["conflict"]["code"], "approved_payload_changed")
        self.assertEqual(len(self.bucket_mgr.create_calls), 1)

    async def test_unregistered_task_never_reaches_journey_methods(self):
        candidate = self.make_candidate()
        conn = sqlite3.connect(self.store.db_path)
        conn.execute(
            "UPDATE automation_candidates SET task_type = 'unknown_task' WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        )
        conn.commit()
        conn.close()
        result = await self.executor.confirm_candidate(
            candidate["candidate_id"], expected_revision=1,
            approved_payload_hash=candidate["draft_payload_hash"],
        )
        self.assertEqual(result["status"], "unsupported_task")
        self.assertEqual(self.bucket_mgr.append_calls + self.bucket_mgr.close_calls + self.bucket_mgr.create_calls, [])

    async def test_changed_open_journey_conflicts_without_write(self):
        candidate = self.make_candidate()
        self.bucket_mgr.open_journey["metadata"]["journey_summary"] = "外部修改"
        result = await self.confirm(candidate)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflict"]["code"], "open_journey_changed")
        self.assertEqual(self.bucket_mgr.append_calls, [])

    async def test_missing_evidence_conflicts_without_write(self):
        candidate = self.make_candidate()
        self.bucket_mgr.evidence.pop("evidence-1")
        result = await self.confirm(candidate)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflict"]["code"], "evidence_missing")
        self.assertEqual(self.bucket_mgr.append_calls, [])

    async def test_journey_bucket_cannot_be_evidence(self):
        candidate = self.make_candidate()
        self.bucket_mgr.evidence["evidence-1"]["metadata"]["domain"] = ["journey"]
        result = await self.confirm(candidate)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflict"]["code"], "evidence_is_journey")
        self.assertEqual(self.bucket_mgr.append_calls, [])

    async def test_no_change_completes_with_zero_lifecycle_calls(self):
        candidate = self.make_candidate("no_change")
        result = await self.confirm(candidate)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["write_count"], 0)
        self.assertEqual(result["result"]["reviewed_through_after"], "2026-08-09")
        self.assertEqual(
            self.store.get_schedule(task_type="weekly_journey")["policy"]["reviewed_through_date"],
            "2026-08-09",
        )
        self.assertEqual(self.bucket_mgr.append_calls + self.bucket_mgr.close_calls + self.bucket_mgr.create_calls, [])


class AutomationApprovalRoutesTest(unittest.IsolatedAsyncioTestCase):
    def response_modules(self):
        responses = SimpleNamespace(JSONResponse=FakeJSONResponse)
        return patch.dict(sys.modules, {
            "starlette": SimpleNamespace(responses=responses),
            "starlette.responses": responses,
        })

    async def test_edit_route_dispatches_expected_revision_and_draft(self):
        executor = SimpleNamespace(edit_candidate=lambda *args, **kwargs: {
            "status": "updated", "candidate": {"revision": 2},
        })
        route = load_server_function("api_automation_candidate_edit", {
            "_require_dashboard_auth": lambda request: None,
            "MEMORY_ID_RE": re.compile(r"^[A-Za-z0-9_-]+$"),
            "automation_executor": executor,
            "_automation_expected_revision": lambda body: int(body["expected_revision"]),
            "_automation_mutation_status": lambda result: 200,
        })
        request = SimpleNamespace(
            path_params={"candidate_id": "candidate-1"},
            json=AsyncMock(return_value={"expected_revision": 1, "draft": {"summary": "编辑"}}),
        )
        with self.response_modules():
            response = await route(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body["candidate"]["revision"], 2)

    async def test_reject_route_requires_auth_before_mutation(self):
        denied = object()
        executor = SimpleNamespace(reject_candidate=AsyncMock())
        route = load_server_function("api_automation_candidate_reject", {
            "_require_dashboard_auth": lambda request: denied,
            "automation_executor": executor,
        })
        with self.response_modules():
            result = await route(SimpleNamespace())
        self.assertIs(result, denied)
        executor.reject_candidate.assert_not_awaited()

    async def test_confirm_route_passes_only_revision_and_hash(self):
        executor = SimpleNamespace(confirm_candidate=AsyncMock(return_value={
            "status": "completed", "result": {"write_count": 0},
        }))
        route = load_server_function("api_automation_candidate_confirm", {
            "_require_dashboard_auth": lambda request: None,
            "MEMORY_ID_RE": re.compile(r"^[A-Za-z0-9_-]+$"),
            "automation_executor": executor,
            "_automation_expected_revision": lambda body: int(body["expected_revision"]),
            "_automation_mutation_status": lambda result: 200,
            "re": re,
        })
        digest = "a" * 64
        request = SimpleNamespace(
            path_params={"candidate_id": "candidate-1"},
            json=AsyncMock(return_value={
                "expected_revision": 2,
                "approved_payload_hash": digest,
                "draft": {"must_not_be_forwarded": True},
            }),
        )
        with self.response_modules():
            response = await route(request)
        self.assertEqual(response.status_code, 200)
        executor.confirm_candidate.assert_awaited_once_with(
            "candidate-1", expected_revision=2, approved_payload_hash=digest,
        )


if __name__ == "__main__":
    unittest.main()
