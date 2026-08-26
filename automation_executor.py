"""Whitelisted execution of manually approved automation candidates."""

from __future__ import annotations

import hashlib
import json
import uuid

from automation_store import AutomationStore
from journey_weekly_engine import TASK_TYPE as WEEKLY_JOURNEY_TASK_TYPE, WeeklyJourneyEngine


class ExecutionConflict(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = str(code)
        self.details = details or {}


class AutomationExecutor:
    """Execute only registered task handlers after a persisted human approval."""

    def __init__(
        self,
        store: AutomationStore,
        bucket_mgr,
        weekly_journey_engine: WeeklyJourneyEngine,
    ):
        self.store = store
        self.bucket_mgr = bucket_mgr
        self.weekly_journey_engine = weekly_journey_engine
        self._handlers = {
            WEEKLY_JOURNEY_TASK_TYPE: self._execute_weekly_journey,
        }

    @staticmethod
    def payload_hash(payload: dict) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _normalized_candidate(self, candidate: dict, run: dict) -> dict:
        if candidate.get("task_type") != WEEKLY_JOURNEY_TASK_TYPE:
            raise ValueError("unsupported automation task_type")
        snapshot = run.get("input_snapshot") if isinstance(run.get("input_snapshot"), dict) else {}
        raw = {
            "candidate_type": candidate.get("candidate_type"),
            "rationale": candidate.get("rationale") or [],
            "evidence_bucket_ids": [],
            "proposal": candidate.get("draft") if isinstance(candidate.get("draft"), dict) else {},
        }
        return self.weekly_journey_engine.validate_candidate(raw, snapshot)

    def approved_payload(self, candidate: dict, run: dict) -> dict:
        normalized = self._normalized_candidate(candidate, run)
        return {
            "task_type": str(candidate.get("task_type") or ""),
            "candidate_type": normalized["candidate_type"],
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "revision": int(candidate.get("revision") or 0),
            "run_id": str(candidate.get("run_id") or ""),
            "input_hash": str(run.get("input_hash") or ""),
            "draft": normalized["draft"],
            "evidence_bucket_ids": [
                str(item.get("id") or "") for item in normalized.get("evidence", [])
                if str(item.get("id") or "")
            ],
        }

    def candidate_for_review(self, candidate: dict) -> dict:
        if not candidate:
            return {}
        payload = dict(candidate)
        run = self.store.get_run(str(candidate.get("run_id") or ""))
        if candidate.get("status") == "pending" and run:
            normalized = self._normalized_candidate(candidate, run)
            approval = self.approved_payload(candidate, run)
            payload["draft_preview"] = normalized["preview"]
            payload["draft_evidence"] = normalized["evidence"]
            payload["draft_payload_hash"] = self.payload_hash(approval)
        else:
            payload["draft_payload_hash"] = ""
        return payload

    def edit_candidate(
        self, candidate_id: str, *, expected_revision: int, draft: dict,
    ) -> dict:
        candidate = self.store.get_candidate(candidate_id)
        if not candidate:
            return {"status": "not_found", "candidate": {}}
        if candidate.get("status") != "pending":
            return {"status": "not_pending", "candidate": candidate}
        if int(candidate.get("revision") or 0) != int(expected_revision):
            return {"status": "revision_mismatch", "candidate": self.candidate_for_review(candidate)}
        run = self.store.get_run(str(candidate.get("run_id") or ""))
        if not run:
            raise ValueError("automation run not found")
        edited = dict(candidate)
        edited["draft"] = draft
        normalized = self._normalized_candidate(edited, run)
        outcome, saved = self.store.update_candidate_draft(
            candidate_id,
            expected_revision=int(expected_revision),
            draft=normalized["draft"],
        )
        return {"status": outcome, "candidate": self.candidate_for_review(saved) if saved else {}}

    def reject_candidate(self, candidate_id: str, *, expected_revision: int) -> dict:
        outcome, candidate = self.store.reject_candidate(
            candidate_id, expected_revision=int(expected_revision),
        )
        return {"status": outcome, "candidate": candidate}

    async def _find_journey_operation(self, operation_id: str) -> dict:
        safe_id = str(operation_id or "").strip()
        if not safe_id:
            return {}
        for stage in await self.bucket_mgr.list_journey_stages():
            operations = {
                str(item) for item in stage.get("metadata", {}).get("journey_operation_ids", []) or []
            }
            if safe_id in operations:
                return {"status": "duplicate", "bucket_id": str(stage.get("id") or "")}
        return {}

    async def _validate_evidence(self, evidence_ids: list[str]) -> None:
        for bucket_id in evidence_ids:
            bucket = await self.bucket_mgr.get(bucket_id)
            if not bucket:
                raise ExecutionConflict(
                    "evidence_missing",
                    f"证据桶不存在: {bucket_id}",
                    {"bucket_id": bucket_id},
                )
            if self.weekly_journey_engine._is_journey(bucket):
                raise ExecutionConflict(
                    "evidence_is_journey",
                    f"journey 桶不能作为候选证据: {bucket_id}",
                    {"bucket_id": bucket_id},
                )

    async def _validate_current_snapshot(self, run: dict) -> None:
        snapshot = run.get("input_snapshot") if isinstance(run.get("input_snapshot"), dict) else {}
        expected = snapshot.get("current_journey")
        if not isinstance(expected, dict):
            raise ExecutionConflict("open_journey_changed", "候选缺少开放 journey 快照")
        current = await self.bucket_mgr.get_open_journey_stage()
        actual = self.weekly_journey_engine._journey_snapshot(current) if current else {}
        if actual != expected:
            raise ExecutionConflict(
                "open_journey_changed",
                "当前开放 journey 已与候选快照不同，请重新生成候选。",
                {
                    "expected_journey_id": str(expected.get("id") or ""),
                    "actual_journey_id": str(actual.get("id") or ""),
                },
            )

    def _validate_review_cursor(self, run: dict) -> None:
        snapshot = run.get("input_snapshot") if isinstance(run.get("input_snapshot"), dict) else {}
        expected = str(snapshot.get("reviewed_through_date") or "").strip()
        schedule = self.store.get_schedule(task_type=WEEKLY_JOURNEY_TASK_TYPE)
        policy = schedule.get("policy") if isinstance(schedule.get("policy"), dict) else {}
        actual = str(policy.get("reviewed_through_date") or "").strip()
        if not expected or actual != expected:
            raise ExecutionConflict(
                "review_cursor_changed",
                "轨迹已梳理截止日已变化，请重新生成候选。",
                {"expected": expected, "actual": actual},
            )

    def _conflict_result(self, conflict: ExecutionConflict, result: dict | None = None) -> dict:
        payload = dict(result or {})
        payload["conflict"] = {
            "code": conflict.code,
            "message": str(conflict),
            "details": conflict.details,
        }
        return payload

    async def _validate_initial_approval(self, approved: dict, run: dict) -> None:
        self._validate_review_cursor(run)
        await self._validate_current_snapshot(run)
        await self._validate_evidence(list(approved.get("evidence_bucket_ids") or []))

    async def _validate_retry(self, candidate: dict, approved: dict, run: dict) -> None:
        self._validate_review_cursor(run)
        base = str(candidate.get("operation_id") or "")
        candidate_type = str(approved.get("candidate_type") or "")
        await self._validate_evidence(list(approved.get("evidence_bucket_ids") or []))
        if candidate_type == "no_change":
            await self._validate_current_snapshot(run)
            return
        if candidate_type == "append_current":
            if await self._find_journey_operation(f"{base}:append"):
                return
            await self._validate_current_snapshot(run)
            return
        created = await self._find_journey_operation(f"{base}:create")
        if created:
            return
        closed = await self._find_journey_operation(f"{base}:close")
        if not closed:
            await self._validate_current_snapshot(run)
            return
        current = await self.bucket_mgr.get_open_journey_stage()
        if current:
            raise ExecutionConflict(
                "unexpected_open_journey",
                "旧阶段已关闭，但当前已有新的开放 journey，不能继续创建候选阶段。",
                {"actual_journey_id": str(current.get("id") or "")},
            )

    async def _execute_weekly_journey(self, candidate: dict, approved: dict) -> dict:
        base = str(candidate.get("operation_id") or "")
        candidate_type = str(approved.get("candidate_type") or "")
        draft = approved.get("draft") if isinstance(approved.get("draft"), dict) else {}
        evidence_ids = list(approved.get("evidence_bucket_ids") or [])
        result = dict(candidate.get("result") or {})
        result.update({"operation_id": base, "candidate_type": candidate_type})
        steps = dict(result.get("steps") or {})
        result["steps"] = steps

        if candidate_type == "no_change":
            result["write_count"] = 0
            return result

        if candidate_type == "append_current":
            operation_id = f"{base}:append"
            duplicate = await self._find_journey_operation(operation_id)
            steps["append"] = duplicate or await self.bucket_mgr.append_open_journey_stage(
                content=str(draft.get("revised_content") or ""),
                summary=str(draft.get("summary") or ""),
                source_bucket_ids=evidence_ids,
                operation_id=operation_id,
            )
            result["write_count"] = 1
            return result

        close_payload = draft.get("close") if isinstance(draft.get("close"), dict) else {}
        create_payload = draft.get("create") if isinstance(draft.get("create"), dict) else {}
        close_operation = f"{base}:close"
        close_duplicate = await self._find_journey_operation(close_operation)
        steps["close"] = close_duplicate or await self.bucket_mgr.close_open_journey_stage(
            stage_end=str(close_payload.get("stage_end") or ""),
            summary=str(close_payload.get("summary") or ""),
            operation_id=close_operation,
        )
        self.store.set_candidate_execution(
            str(candidate.get("candidate_id") or ""),
            status="applying",
            result=result,
        )

        create_operation = f"{base}:create"
        create_duplicate = await self._find_journey_operation(create_operation)
        if not create_duplicate:
            current = await self.bucket_mgr.get_open_journey_stage()
            if current:
                raise ExecutionConflict(
                    "unexpected_open_journey",
                    "旧阶段已关闭，但创建前发现新的开放 journey。",
                    {"actual_journey_id": str(current.get("id") or "")},
                )
            await self._validate_evidence(evidence_ids)
        steps["create"] = create_duplicate or await self.bucket_mgr.create_journey_stage(
            content=str(create_payload.get("content") or ""),
            name=str(create_payload.get("name") or ""),
            stage_start=str(create_payload.get("stage_start") or ""),
            summary=str(create_payload.get("summary") or ""),
            source_bucket_ids=evidence_ids,
            operation_id=create_operation,
        )
        result["write_count"] = 2
        return result

    async def confirm_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        approved_payload_hash: str,
    ) -> dict:
        candidate = self.store.get_candidate(candidate_id)
        if not candidate:
            return {"status": "not_found", "candidate": {}}
        if candidate.get("task_type") not in self._handlers:
            return {"status": "unsupported_task", "candidate": candidate}
        if int(candidate.get("revision") or 0) != int(expected_revision):
            return {"status": "revision_mismatch", "candidate": self.candidate_for_review(candidate)}
        requested_hash = str(approved_payload_hash or "").strip()
        if candidate.get("status") == "completed":
            if requested_hash != str(candidate.get("approved_payload_hash") or ""):
                return {"status": "approved_payload_changed", "candidate": candidate}
            return {"status": "replayed", "candidate": candidate, "result": candidate.get("result") or {}}
        if candidate.get("status") in {"rejected", "conflict"}:
            return {"status": "not_pending", "candidate": candidate}

        owner = f"approve:{candidate_id}:{uuid.uuid4().hex[:8]}"
        if not self.store.acquire_task_lease(
            task_type=str(candidate.get("task_type") or ""), owner=owner,
        ):
            return {"status": "in_progress", "candidate": self.store.get_candidate(candidate_id)}

        try:
            candidate = self.store.get_candidate(candidate_id)
            run = self.store.get_run(str(candidate.get("run_id") or ""))
            if not run:
                conflict = ExecutionConflict(
                    "run_missing", "候选对应的自动化运行记录不存在，不能执行。",
                )
                saved = self.store.set_candidate_execution(
                    candidate_id, status="conflict", result=self._conflict_result(conflict),
                    error=str(conflict),
                )
                return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
            if int(candidate.get("revision") or 0) != int(expected_revision):
                return {"status": "revision_mismatch", "candidate": self.candidate_for_review(candidate)}
            if candidate.get("status") == "completed":
                if requested_hash != str(candidate.get("approved_payload_hash") or ""):
                    return {"status": "approved_payload_changed", "candidate": candidate}
                return {"status": "replayed", "candidate": candidate, "result": candidate.get("result") or {}}

            if candidate.get("status") == "pending":
                approved = self.approved_payload(candidate, run)
                digest = self.payload_hash(approved)
                if requested_hash != digest:
                    conflict = ExecutionConflict(
                        "approved_payload_changed",
                        "当前候选批准稿与页面确认的 hash 不一致。",
                    )
                    saved = self.store.set_candidate_execution(
                        candidate_id, status="conflict", result=self._conflict_result(conflict),
                        error=str(conflict),
                    )
                    return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
                try:
                    await self._validate_initial_approval(approved, run)
                except ExecutionConflict as conflict:
                    saved = self.store.set_candidate_execution(
                        candidate_id, status="conflict", result=self._conflict_result(conflict),
                        error=str(conflict),
                    )
                    return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
                base_operation = (
                    f"journey-weekly:{run.get('cycle_key')}:{candidate_id}:r{expected_revision}"
                )
                outcome, candidate = self.store.freeze_candidate_approval(
                    candidate_id,
                    expected_revision=int(expected_revision),
                    approved_payload=approved,
                    approved_payload_hash=digest,
                    operation_id=base_operation,
                )
                if outcome != "frozen":
                    return {"status": outcome, "candidate": candidate}
            elif candidate.get("status") in {"failed", "applying"}:
                approved = candidate.get("approved_payload")
                stored_hash = str(candidate.get("approved_payload_hash") or "")
                if not isinstance(approved, dict) or not approved or self.payload_hash(approved) != stored_hash:
                    conflict = ExecutionConflict(
                        "approved_payload_changed", "已冻结的批准稿或 hash 已发生变化。",
                    )
                    saved = self.store.set_candidate_execution(
                        candidate_id, status="conflict",
                        result=self._conflict_result(conflict, candidate.get("result")), error=str(conflict),
                    )
                    return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
                if requested_hash != stored_hash:
                    return {"status": "approved_payload_changed", "candidate": candidate}
                try:
                    await self._validate_retry(candidate, approved, run)
                except ExecutionConflict as conflict:
                    saved = self.store.set_candidate_execution(
                        candidate_id, status="conflict",
                        result=self._conflict_result(conflict, candidate.get("result")), error=str(conflict),
                    )
                    return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
                candidate = self.store.set_candidate_execution(
                    candidate_id, status="applying", result=candidate.get("result") or {},
                )
            else:
                return {"status": "not_pending", "candidate": candidate}

            try:
                result = await self._handlers[candidate["task_type"]](candidate, approved)
            except ExecutionConflict as conflict:
                saved = self.store.set_candidate_execution(
                    candidate_id, status="conflict",
                    result=self._conflict_result(conflict, self.store.get_candidate(candidate_id).get("result")),
                    error=str(conflict),
                )
                return {"status": "conflict", "candidate": saved, "conflict": saved.get("result", {}).get("conflict")}
            except Exception as exc:
                current = self.store.get_candidate(candidate_id)
                saved = self.store.set_candidate_execution(
                    candidate_id, status="failed", result=current.get("result") or {}, error=str(exc),
                )
                return {"status": "failed", "candidate": saved, "error": str(exc)}

            snapshot = run.get("input_snapshot") if isinstance(run.get("input_snapshot"), dict) else {}
            result = dict(result or {})
            result["reviewed_through_before"] = str(snapshot.get("reviewed_through_date") or "")
            result["reviewed_through_after"] = str(snapshot.get("review_end_date") or "")
            try:
                saved = self.store.complete_candidate_and_advance_review_cursor(
                    candidate_id,
                    result=result,
                    expected_reviewed_through_date=str(snapshot.get("reviewed_through_date") or ""),
                    reviewed_through_date=str(snapshot.get("review_end_date") or ""),
                )
            except Exception as exc:
                current = self.store.get_candidate(candidate_id)
                saved = self.store.set_candidate_execution(
                    candidate_id, status="failed", result=current.get("result") or result,
                    error=str(exc),
                )
                return {"status": "failed", "candidate": saved, "error": str(exc)}
            return {"status": "completed", "candidate": saved, "result": result}
        finally:
            self.store.release_task_lease(
                task_type=str(candidate.get("task_type") or WEEKLY_JOURNEY_TASK_TYPE), owner=owner,
            )
