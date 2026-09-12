"""Offline generation and human-review workflow for CC conversation slices.

This module deliberately has no retrieval or Context-injection behavior. It turns
persisted, immutable day/session snapshots into versioned slice batches and keeps
daily-review and slice persistence independent even when they share one model call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from typing import Any

from conversation_slice_store import SLICE_SCHEMA_VERSION, ConversationSliceStore
from daily_review_engine import DailyReviewEngine
from utils import count_tokens_approx


SEGMENTER_VERSION = "natural-topic-boundaries-v1"
SLICE_PROMPT_VERSION = "first-person-life-memory-v1"

SLICE_STYLE_PROMPT = """把值得长期记住的生活事件、感受、关系互动、偏好变化、决定、计划和结果，写成自然的一小段第一人称回忆。语气像我在回望自己的经历，保留具体而有意义的细节，不写成标签、报告或流水账。纯代码过程、工具输出、普通问答、寒暄和助手泛泛建议应忽略；技术工作只有在体现我的压力、成就、选择或项目阶段时才保留其个人意义。"""

SLICE_HARD_CONSTRAINTS = """你正在为一个用户生成离线聊天日回顾和聊天切片。只允许依据本次材料，不得编造事实、日期、情绪、因果、原话或已完成动作。第一人称“我”固定指 profile 所属用户；助手内容只用于理解指代和事件发展，不得把助手推测写成用户事实。

只输出一个合法 JSON 对象，不输出 Markdown 或解释。顶层字段固定为 daily_review 和 session_slices。daily_review 是 null 或 {"content":"..."}。session_slices 是数组，每个输入 session 必须恰好出现一次，绝不能跨 session 或跨 chat_day 合并。

每个 session 项格式：{"session_id":"...","slices":[],"ignored_source_ranges":[],"zero_slice_reason":""}。每个 slice 格式：{"source_start_message_id":"msg_...","source_end_message_id":"msg_...","source_message_ids":["msg_..."],"summary":"...","event_time_start":null,"event_time_end":null,"boundary_reason":"topic_shift"}。

source_message_ids 必须是该 session 输入中连续、有序、无重叠的永久 message ID；起止 ID 必须等于数组首尾。相邻切片不得重叠。boundary_reason 只能是 topic_shift、event_complete、time_gap、day_end、length_limit。未进入切片的每条消息必须放入 ignored_source_ranges，格式为 {"message_ids":[...],"reason":"..."}。全部消息必须且只能被 slices 或 ignored_source_ranges 覆盖一次。允许 0 切片，但必须填写 zero_slice_reason。summary 必须是一段、不超过 1200 字，并使用用户第一人称“我”。"""


class ConversationSliceEngine:
    def __init__(
        self,
        config: dict[str, Any],
        state_store,
        *,
        message_client,
        daily_review_engine: DailyReviewEngine,
        slice_store: ConversationSliceStore | None = None,
    ):
        self.config = config
        self.state_store = state_store
        self.message_client = message_client
        self.daily_review_engine = daily_review_engine
        self.slice_store = slice_store or ConversationSliceStore(state_store.db_path)
        cfg = config.get("conversation_slices", {})
        if not isinstance(cfg, dict):
            cfg = {}
        self.max_tokens = max(1200, min(8000, int(cfg.get("max_tokens", 4200))))
        self.max_input_chars = max(20000, min(800000, int(cfg.get("max_input_chars", 500000))))

    @staticmethod
    def _json_hash(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("slice model output is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("slice model output must be a JSON object")
        return parsed

    @staticmethod
    def _provider_and_model(message_client) -> tuple[str, str]:
        choice = message_client.choice() if hasattr(message_client, "choice") else {}
        if not isinstance(choice, dict):
            choice = {}
        return (
            str(choice.get("engine") or "api"),
            str(choice.get("model") or getattr(message_client, "model", "") or ""),
        )

    @staticmethod
    def _render_snapshot(snapshot: dict[str, Any], *, title: str = "", mode: str = "chat") -> str:
        lines = [
            f"<session id={json.dumps(str(snapshot['session_id']), ensure_ascii=False)} "
            f"title={json.dumps(str(title or ''), ensure_ascii=False)} mode={json.dumps(mode)}>",
        ]
        for message in snapshot.get("messages") or []:
            lines.append(
                f"<{message['role']} message_id={json.dumps(str(message['message_id']))} "
                f"created_at={json.dumps(str(message.get('created_at') or ''))}>"
            )
            lines.append(str(message.get("content") or ""))
            for attachment in message.get("attachments") or []:
                visible = str(attachment.get("text_content") or "").strip()
                if visible:
                    lines.append(
                        f"<attachment id={json.dumps(str(attachment.get('attachment_id') or ''))} "
                        f"filename={json.dumps(str(attachment.get('filename') or ''), ensure_ascii=False)}>"
                    )
                    lines.append(visible)
                    lines.append("</attachment>")
            lines.append(f"</{message['role']}>")
        lines.append("</session>")
        return "\n".join(lines)

    def _snapshot_for_scope(
        self, *, profile_id: str, persona_id: str, session_id: str, chat_day: str,
    ) -> dict[str, Any]:
        return self.slice_store.build_source_snapshot(
            profile_id=profile_id,
            persona_id=persona_id,
            session_id=session_id,
            chat_day=chat_day,
        )

    def _task_for_snapshot(
        self,
        *,
        snapshot: dict[str, Any],
        trigger_type: str,
        reslice_revision: int = 1,
    ) -> dict[str, Any]:
        batch_key = self.slice_store.batch_idempotency_key(
            profile_id=str(snapshot["profile_id"]),
            persona_id=str(snapshot["persona_id"]),
            session_id=str(snapshot["session_id"]),
            chat_day=str(snapshot["chat_day"]),
            session_source_snapshot_hash=str(snapshot["session_source_snapshot_hash"]),
            segmenter_version=SEGMENTER_VERSION,
            slice_prompt_version=SLICE_PROMPT_VERSION,
            slice_schema_version=SLICE_SCHEMA_VERSION,
            reslice_revision=reslice_revision,
        )
        rendered = self._render_snapshot(snapshot)
        return self.slice_store.create_task(
            profile_id=str(snapshot["profile_id"]),
            persona_id=str(snapshot["persona_id"]),
            session_id=str(snapshot["session_id"]),
            chat_day=str(snapshot["chat_day"]),
            trigger_type=trigger_type,
            batch_idempotency_key=batch_key,
            reslice_revision=reslice_revision,
            message_count=int(snapshot["source_message_count"]),
            estimated_input_tokens=count_tokens_approx(rendered + SLICE_STYLE_PROMPT + SLICE_HARD_CONSTRAINTS),
            estimated_call_count=1,
        )

    def _task_id_for_snapshot(
        self,
        *,
        snapshot: dict[str, Any],
        trigger_type: str,
        reslice_revision: int,
    ) -> str:
        batch_key = self.slice_store.batch_idempotency_key(
            profile_id=str(snapshot["profile_id"]),
            persona_id=str(snapshot["persona_id"]),
            session_id=str(snapshot["session_id"]),
            chat_day=str(snapshot["chat_day"]),
            session_source_snapshot_hash=str(snapshot["session_source_snapshot_hash"]),
            segmenter_version=SEGMENTER_VERSION,
            slice_prompt_version=SLICE_PROMPT_VERSION,
            slice_schema_version=SLICE_SCHEMA_VERSION,
            reslice_revision=int(reslice_revision),
        )
        return "cst_" + self.slice_store.task_idempotency_key(
            trigger_type=trigger_type,
            batch_idempotency_key=batch_key,
        )

    def enqueue_scope(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        chat_day: str,
        trigger_type: str,
        manual_reslice: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self._snapshot_for_scope(
            profile_id=profile_id, persona_id=persona_id,
            session_id=session_id, chat_day=chat_day,
        )
        if int(snapshot["source_message_count"]) == 0:
            raise ValueError("conversation slice source day has no messages")
        revision = 1
        if manual_reslice:
            current = self.slice_store.get_reslice_revision(
                profile_id=profile_id, persona_id=persona_id, session_id=session_id,
                chat_day=chat_day,
                session_source_snapshot_hash=str(snapshot["session_source_snapshot_hash"]),
                segmenter_version=SEGMENTER_VERSION,
                slice_prompt_version=SLICE_PROMPT_VERSION,
            )
            revision = self.slice_store.allocate_reslice_revision(
                profile_id=profile_id, persona_id=persona_id, session_id=session_id,
                chat_day=chat_day,
                session_source_snapshot_hash=str(snapshot["session_source_snapshot_hash"]),
                segmenter_version=SEGMENTER_VERSION,
                slice_prompt_version=SLICE_PROMPT_VERSION,
                expected_revision=current if expected_revision is None else int(expected_revision),
            )
        return self._task_for_snapshot(
            snapshot=snapshot, trigger_type=trigger_type, reslice_revision=revision,
        )

    def estimate_backfill(
        self,
        *,
        profile_id: str,
        persona_id: str,
        session_id: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if start > end:
            raise ValueError("start_date must not be after end_date")
        context_days = self.state_store.list_conversation_context_days(
            profile_id=profile_id, session_id=session_id, persona_id=persona_id,
        )
        scopes: list[dict[str, Any]] = []
        total_messages = 0
        total_tokens = 0
        for item in sorted(context_days, key=lambda row: str(row.get("day") or "")):
            day = str(item.get("day") or "")
            if not day or day < start_date or day > end_date or int(item.get("turn_count") or 0) <= 0:
                continue
            if self.slice_store.has_active_batch(
                profile_id=profile_id, persona_id=persona_id, session_id=session_id, chat_day=day,
            ):
                continue
            snapshot = self._snapshot_for_scope(
                profile_id=profile_id, persona_id=persona_id, session_id=session_id, chat_day=day,
            )
            messages = int(snapshot["source_message_count"])
            tokens = count_tokens_approx(
                self._render_snapshot(snapshot) + SLICE_STYLE_PROMPT + SLICE_HARD_CONSTRAINTS
            )
            scopes.append({"chat_day": day, "message_count": messages, "estimated_input_tokens": tokens})
            total_messages += messages
            total_tokens += tokens
        if len(scopes) > 14:
            raise ValueError("first historical backfill may include at most 14 actual chat days")
        estimate = {
            "profile_id": profile_id,
            "persona_id": persona_id,
            "session_id": session_id,
            "start_date": start_date,
            "end_date": end_date,
            "message_count": total_messages,
            "estimated_input_tokens": total_tokens,
            "estimated_call_count": len(scopes),
            "scopes": scopes,
        }
        estimate["estimate_signature"] = self._json_hash(estimate)
        return estimate

    def create_backfill_tasks(self, estimate: dict[str, Any], *, estimate_signature: str) -> list[dict[str, Any]]:
        expected = str(estimate.get("estimate_signature") or "")
        if not expected or str(estimate_signature or "") != expected:
            raise ValueError("backfill estimate changed; review the latest estimate before creating tasks")
        tasks = []
        for scope in estimate.get("scopes") or []:
            snapshot = self._snapshot_for_scope(
                profile_id=str(estimate["profile_id"]),
                persona_id=str(estimate["persona_id"]),
                session_id=str(estimate["session_id"]),
                chat_day=str(scope["chat_day"]),
            )
            tasks.append(self._task_for_snapshot(snapshot=snapshot, trigger_type="manual_backfill"))
        return tasks

    @staticmethod
    def _coverage_from_output(output: dict[str, Any], slices: list[dict[str, Any]]) -> dict[str, Any]:
        ignored = output.get("ignored_source_ranges")
        if not isinstance(ignored, list):
            ignored = []
        coverage: dict[str, Any] = {"ignored_ranges": ignored}
        zero_reason = str(output.get("zero_slice_reason") or "").strip()
        if not slices and zero_reason:
            coverage["zero_slice_reason"] = zero_reason
        return coverage

    def _save_session_output(
        self,
        *,
        snapshot: dict[str, Any],
        output: dict[str, Any],
        reslice_revision: int,
        daily_source_snapshot_hash: str | None,
    ) -> dict[str, Any]:
        if str(output.get("session_id") or "") != str(snapshot["session_id"]):
            raise ValueError("slice output session_id does not match requested session")
        slices = output.get("slices")
        if not isinstance(slices, list) or any(not isinstance(item, dict) for item in slices):
            raise ValueError("slice output slices must be an array of objects")
        for item in slices:
            ids = [str(value or "") for value in item.get("source_message_ids") or []]
            if not ids or str(item.get("source_start_message_id") or "") != ids[0]:
                raise ValueError("slice source_start_message_id does not match source_message_ids")
            if str(item.get("source_end_message_id") or "") != ids[-1]:
                raise ValueError("slice source_end_message_id does not match source_message_ids")
        provider, model = self._provider_and_model(self.message_client)
        batch = self.slice_store.create_candidate_batch(
            profile_id=str(snapshot["profile_id"]),
            persona_id=str(snapshot["persona_id"]),
            session_id=str(snapshot["session_id"]),
            chat_day=str(snapshot["chat_day"]),
            segmenter_version=SEGMENTER_VERSION,
            slice_prompt_version=SLICE_PROMPT_VERSION,
            slices=slices,
            coverage=self._coverage_from_output(output, slices),
            reslice_revision=reslice_revision,
            daily_source_snapshot_hash=daily_source_snapshot_hash,
            generator_provider=provider,
            generator_model=model,
        )
        return self.slice_store.activate_batch(str(batch["batch_id"]))

    async def _call_model(self, *, persona: dict[str, Any], user: str) -> dict[str, Any]:
        if not bool(getattr(self.message_client, "is_configured", False)):
            raise ValueError("slice model is not configured")
        text = await self.message_client._create_message(
            system="\n\n".join(
                part for part in [
                    self.daily_review_engine._persona_system(persona),
                    SLICE_HARD_CONSTRAINTS,
                ] if part
            ),
            user=user,
            max_tokens=self.max_tokens,
            temperature=0.3,
        )
        return self._parse_json(text)

    async def run_task(self, task_id: str, *, profile_id: str = "default") -> dict[str, Any]:
        task = self.slice_store.get_task(profile_id=profile_id, task_id=task_id)
        if not task:
            raise ValueError("conversation slice task not found")
        if task["status"] == "paused":
            return {"status": "paused", "task": task}
        if task["status"] == "completed":
            return {"status": "completed", "task": task}
        if task["status"] != "queued":
            return {"status": task["status"], "task": task}
        claimed = self.slice_store.claim_task(
            profile_id=task["profile_id"], task_id=task_id,
        )
        if not claimed:
            current = self.slice_store.get_task(profile_id=profile_id, task_id=task_id)
            return {"status": current.get("status", "running"), "task": current}
        task = claimed
        try:
            snapshot = self._snapshot_for_scope(
                profile_id=task["profile_id"], persona_id=task["persona_id"],
                session_id=task["session_id"], chat_day=task["chat_day"],
            )
        except Exception as exc:
            failed = self.slice_store.set_task_status(
                task_id, profile_id=task["profile_id"], status="failed",
                error_code="source_unavailable", error_detail=str(exc),
            )
            return {"status": "failed", "task": failed}
        expected_task_id = self._task_id_for_snapshot(
            snapshot=snapshot,
            trigger_type=str(task["trigger_type"]),
            reslice_revision=int(task["reslice_revision"]),
        )
        if expected_task_id != task_id:
            failed = self.slice_store.set_task_status(
                task_id, profile_id=task["profile_id"], status="failed",
                error_code="source_changed",
                error_detail="source messages changed after this task was queued",
            )
            replacement = self._task_for_snapshot(
                snapshot=snapshot,
                trigger_type="slice_recovery",
                reslice_revision=int(task["reslice_revision"]),
            )
            return {"status": "failed", "task": failed, "replacement_task": replacement}
        if task["reslice_revision"] == 1 and self.slice_store.has_active_batch(
            profile_id=task["profile_id"], persona_id=task["persona_id"],
            session_id=task["session_id"], chat_day=task["chat_day"],
        ):
            done = self.slice_store.set_task_status(task_id, profile_id=task["profile_id"], status="completed")
            return {"status": "completed", "task": done, "reused_active": True}
        persona = self.state_store.get_cc_persona(task["persona_id"])
        if not persona:
            raise ValueError("persona not found")
        user = "\n\n".join([
            f"<slice_style_prompt version={json.dumps(SLICE_PROMPT_VERSION)}>\n{SLICE_STYLE_PROMPT}\n</slice_style_prompt>",
            "daily_review 必须返回 null。只处理下面一个 session，并返回该 session 的完整覆盖。",
            f"<today_conversations date={json.dumps(task['chat_day'])}>\n{self._render_snapshot(snapshot)}\n</today_conversations>",
        ])
        if len(user) > self.max_input_chars:
            failed = self.slice_store.set_task_status(
                task_id, status="failed", error_code="input_too_large",
                profile_id=task["profile_id"],
                error_detail="slice source exceeds configured input limit",
            )
            return {"status": "failed", "task": failed}
        try:
            payload = await self._call_model(persona=persona, user=user)
            outputs = payload.get("session_slices")
            if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict):
                raise ValueError("slice-only output must contain exactly one session")
            batch = self._save_session_output(
                snapshot=snapshot,
                output=outputs[0],
                reslice_revision=int(task["reslice_revision"]),
                daily_source_snapshot_hash=None,
            )
            done = self.slice_store.set_task_status(
                task_id, status="completed", batch_id=str(batch["batch_id"]),
                profile_id=task["profile_id"],
            )
            return {"status": "completed", "task": done, "batch": batch}
        except Exception as exc:
            failed = self.slice_store.set_task_status(
                task_id, status="failed",
                profile_id=task["profile_id"],
                error_code=str(getattr(exc, "code", "generation_failed")),
                error_detail=str(exc),
            )
            return {"status": "failed", "task": failed}

    async def generate_daily_bundle(
        self,
        *,
        profile_id: str,
        persona_id: str,
        review_date: str,
        force_daily_review: bool = False,
        override_user_edit: bool = False,
    ) -> dict[str, Any]:
        target = date.fromisoformat(review_date)
        start = datetime.combine(target, time(self.daily_review_engine.day_start_hour), tzinfo=self.daily_review_engine.tz)
        turns = self.state_store.list_daily_review_turns(
            profile_id=profile_id, persona_id=persona_id,
            start_at=start, end_at=start + timedelta(days=1),
        )
        if not turns:
            return {"status": "skipped", "reason": "no_conversation_turns", "date": review_date}
        persona = self.state_store.get_cc_persona(persona_id)
        if not persona:
            return {"status": "skipped", "reason": "persona_not_found", "date": review_date}
        meta = {
            str(item.get("session_id") or ""): {
                "title": str(item.get("session_title") or ""),
                "mode": str(item.get("mode") or "chat"),
            }
            for item in turns if str(item.get("session_id") or "")
        }
        snapshots = [
            self._snapshot_for_scope(
                profile_id=profile_id, persona_id=persona_id,
                session_id=session_id, chat_day=review_date,
            )
            for session_id in sorted(meta)
        ]
        snapshots = [item for item in snapshots if int(item["source_message_count"]) > 0]
        if not snapshots:
            return {"status": "skipped", "reason": "empty_material", "date": review_date}
        existing = self.state_store.list_daily_reviews(
            profile_id=profile_id, persona_id=persona_id,
            start_date=review_date, end_date=review_date, limit=1,
        )
        daily_allowed = not existing or force_daily_review
        if existing and existing[0].get("edited_by_user") and not override_user_edit:
            daily_allowed = False
        pending_snapshots = [
            snapshot for snapshot in snapshots
            if not self.slice_store.has_active_batch(
                profile_id=profile_id, persona_id=persona_id,
                session_id=str(snapshot["session_id"]), chat_day=review_date,
            )
        ]
        if not daily_allowed:
            tasks = [
                self._task_for_snapshot(snapshot=snapshot, trigger_type="slice_recovery")
                for snapshot in pending_snapshots
            ]
            results = [await self.run_task(task["task_id"]) for task in tasks]
            return {
                "status": "slice_only",
                "daily_review": {"status": "protected" if existing and existing[0].get("edited_by_user") else "exists"},
                "slice_results": results,
            }
        if not pending_snapshots:
            result = await self.daily_review_engine.generate(
                profile_id=profile_id, persona_id=persona_id, review_date=review_date,
                force=force_daily_review, override_user_edit=override_user_edit,
            )
            return {"status": result.get("status", "completed"), "daily_review": result, "slice_results": []}

        daily_hash = self.slice_store.daily_source_snapshot_hash(snapshots)
        tasks = [
            self._task_for_snapshot(snapshot=snapshot, trigger_type="daily_bundle")
            for snapshot in pending_snapshots
        ]
        for task in tasks:
            self.slice_store.set_task_status(task["task_id"], profile_id=profile_id, status="running", increment_attempt=True)
        material = "\n\n".join(
            self._render_snapshot(
                snapshot,
                title=meta[str(snapshot["session_id"])]["title"],
                mode=meta[str(snapshot["session_id"])]["mode"],
            ) for snapshot in snapshots
        )
        continuity = self.daily_review_engine._continuity_reference(
            profile_id=profile_id, persona_id=persona_id, target=target,
        )
        user_parts = [
            f"<daily_review_product_prompt>\n{self.daily_review_engine._product_prompt()}\n</daily_review_product_prompt>",
            f"<slice_style_prompt version={json.dumps(SLICE_PROMPT_VERSION)}>\n{SLICE_STYLE_PROMPT}\n</slice_style_prompt>",
        ]
        if continuity:
            user_parts.append(continuity)
        user_parts.append(
            f"<today_conversations date={json.dumps(review_date)}>\n{material}\n</today_conversations>"
        )
        user = "\n\n".join(user_parts)
        if len(user) > self.max_input_chars:
            for task in tasks:
                self.slice_store.set_task_status(
                    task["task_id"], status="failed", error_code="input_too_large",
                    profile_id=profile_id,
                    error_detail="daily bundle exceeds configured input limit",
                )
            return {"status": "failed", "reason": "input_too_large", "slice_results": []}
        try:
            payload = await self._call_model(persona=persona, user=user)
        except Exception as exc:
            failed_tasks = [
                self.slice_store.set_task_status(
                    task["task_id"], status="failed",
                    profile_id=profile_id,
                    error_code=str(getattr(exc, "code", "generation_failed")),
                    error_detail=str(exc),
                ) for task in tasks
            ]
            for snapshot in pending_snapshots:
                self._task_for_snapshot(snapshot=snapshot, trigger_type="slice_recovery")
            return {"status": "failed", "reason": str(exc), "slice_results": failed_tasks}

        daily_result: dict[str, Any]
        try:
            daily = payload.get("daily_review")
            content = str(daily.get("content") or "").strip() if isinstance(daily, dict) else ""
            if not content:
                raise ValueError("joint output daily_review content is empty")
            review = self.state_store.upsert_daily_review(
                profile_id=profile_id, persona_id=persona_id, review_date=review_date,
                content=content,
                source_session_ids=[str(item["session_id"]) for item in snapshots],
                source_turn_count=len(turns),
                model=self._provider_and_model(self.message_client)[1],
                edited_by_user=False,
                preserve_user_edit=not override_user_edit,
            )
            daily_result = {"status": "created", "review": review}
        except Exception as exc:
            daily_result = {"status": "failed", "error": str(exc)}

        raw_outputs = payload.get("session_slices")
        outputs = {
            str(item.get("session_id") or ""): item
            for item in raw_outputs if isinstance(item, dict)
        } if isinstance(raw_outputs, list) else {}
        slice_results: list[dict[str, Any]] = []
        task_by_session = {task["session_id"]: task for task in tasks}
        for snapshot in pending_snapshots:
            session_id = str(snapshot["session_id"])
            task = task_by_session[session_id]
            try:
                output = outputs.get(session_id)
                if output is None:
                    raise ValueError("joint output omitted a requested session")
                batch = self._save_session_output(
                    snapshot=snapshot, output=output, reslice_revision=1,
                    daily_source_snapshot_hash=daily_hash,
                )
                saved_task = self.slice_store.set_task_status(
                    task["task_id"], status="completed", batch_id=str(batch["batch_id"]),
                    profile_id=profile_id,
                )
                slice_results.append({"status": "completed", "task": saved_task, "batch": batch})
            except Exception as exc:
                failed = self.slice_store.set_task_status(
                    task["task_id"], status="failed",
                    profile_id=profile_id,
                    error_code="slice_validation_failed", error_detail=str(exc),
                )
                slice_results.append({"status": "failed", "task": failed})
                self._task_for_snapshot(snapshot=snapshot, trigger_type="slice_recovery")
        status = "completed" if daily_result["status"] == "created" and all(
            item["status"] == "completed" for item in slice_results
        ) else "partial"
        return {"status": status, "daily_review": daily_result, "slice_results": slice_results}

    async def run_recovery(self, *, profile_id: str, limit: int = 2) -> list[dict[str, Any]]:
        tasks = self.slice_store.list_tasks(
            profile_id=profile_id,
            statuses=("queued",),
            trigger_types=("raw_exit", "slice_recovery"),
            limit=max(1, min(10, int(limit or 2))),
        )
        return [await self.run_task(task["task_id"]) for task in reversed(tasks)]
