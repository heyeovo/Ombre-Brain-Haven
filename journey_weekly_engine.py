"""Weekly relationship-journey candidate generation without lifecycle writes."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from automation_store import AutomationStore


TASK_TYPE = "weekly_journey"
CANDIDATE_TYPES = {"no_change", "append_current", "transition"}

WEEKLY_JOURNEY_PRODUCT_PROMPT = """用当前协作者的第一人称写关系轨迹，保留情感在场和具体变化，不写成周报。
判断保持克制，已有阶段已经覆盖的内容不要重复追加。
摘要要短，正文可以保留关键细节。"""

WEEKLY_JOURNEY_HARD_CONSTRAINTS = """你正在生成每周关系轨迹候选，并且只能提出预览。
绝不能声称已经写入、关闭或创建 journey，也不能调用或模拟任何写入动作。
只能依据本次 weekly_journey_input 固定快照；证据 ID 只能来自 materials。
只输出一个 JSON 对象，不要 Markdown。candidate_type 只允许 no_change、append_current、transition。
no_change 的 proposal 必须为空对象；append_current 的 proposal 必须是 {append_content, summary, evidence_bucket_ids}；transition 的 proposal 必须是 {close:{stage_end,summary}, create:{name,stage_start,summary,content,evidence_bucket_ids}}。
所有类型都必须包含 rationale 字符串数组和 evidence_bucket_ids 字符串数组。没有实质变化时必须选择 no_change。
只有关系状态或相处模式发生实质变化时才允许选择 transition。
不得编造固定输入快照之外的精确日期、事件或原话。
输出仍会由服务端重新校验候选白名单、日期、证据快照、revision/hash 和零自动写入边界。"""


def _plain_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda match: match.group(2) or match.group(1), text)


class WeeklyJourneyEngine:
    """Collect a closed calendar week and generate one review-only candidate."""

    def __init__(
        self,
        config: dict[str, Any],
        automation_store: AutomationStore,
        bucket_mgr,
        daily_review_store,
        *,
        profile_id: str = "default",
        message_client=None,
        candidate_generator: Callable[[dict], dict | Awaitable[dict]] | None = None,
        prompt_resolver: Callable[[str], str] | None = None,
    ):
        self.config = config
        self.automation_store = automation_store
        self.bucket_mgr = bucket_mgr
        self.daily_review_store = daily_review_store
        self.profile_id = str(profile_id or "default").strip() or "default"
        self.message_client = message_client
        self.candidate_generator = candidate_generator
        self.prompt_resolver = prompt_resolver
        cfg = config.get("weekly_journey", {}) if isinstance(config.get("weekly_journey"), dict) else {}
        try:
            self.tz = ZoneInfo(str(cfg.get("timezone") or "Asia/Hong_Kong"))
        except Exception:
            self.tz = ZoneInfo("Asia/Hong_Kong")
        self.max_material_chars = max(500, min(12000, int(cfg.get("max_material_chars", 4000))))
        self.max_journey_chars = max(1000, min(40000, int(cfg.get("max_journey_chars", 16000))))
        self.max_input_chars = max(20000, min(500000, int(cfg.get("max_input_chars", 240000))))
        self.max_tokens = max(800, min(6000, int(cfg.get("max_tokens", 2400))))
        self.automation_store.ensure_schedule(
            schedule_id=TASK_TYPE,
            task_type=TASK_TYPE,
            handler_key=TASK_TYPE,
            timezone=str(self.tz.key),
            enabled=False,
            policy={"weekday": 0, "hour": 4, "minute": 30, "candidate_only": True},
        )

    def resolve_window(self, cycle_key: str = "", *, now: datetime | None = None) -> dict:
        key = str(cycle_key or "").strip().upper()
        if key:
            match = re.fullmatch(r"(\d{4})-W(\d{2})", key)
            if not match:
                raise ValueError("cycle_key must use YYYY-Www")
            year, week = int(match.group(1)), int(match.group(2))
            try:
                start_date = date.fromisocalendar(year, week, 1)
            except ValueError as exc:
                raise ValueError("invalid ISO week") from exc
        else:
            local_now = now.astimezone(self.tz) if now and now.tzinfo else (now.replace(tzinfo=self.tz) if now else datetime.now(self.tz))
            current_monday = local_now.date() - timedelta(days=local_now.weekday())
            start_date = current_monday - timedelta(days=7)
            iso = start_date.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        end_date = start_date + timedelta(days=7)
        return {
            "cycle_key": key,
            "start": datetime.combine(start_date, time.min, tzinfo=self.tz),
            "end": datetime.combine(end_date, time.min, tzinfo=self.tz),
        }

    @staticmethod
    def _is_journey(bucket: dict) -> bool:
        meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
        return "journey" in {
            str(item).strip().lower() for item in meta.get("domain", []) or []
        }

    def _parse_timestamp(self, value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(raw[:10]), time.min)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed.astimezone(self.tz)

    @staticmethod
    def _clip(text: Any, limit: int) -> str:
        value = _plain_text(text).strip()
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)].rstrip() + "…"

    def _resolve_persona(self, persona_id: str) -> dict:
        personas = [
            item for item in self.daily_review_store.list_cc_personas()
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        requested = str(persona_id or "").strip()
        if requested:
            persona = next((item for item in personas if str(item.get("id")) == requested), None)
            if not persona:
                raise ValueError("persona_id not found")
            return persona
        if len(personas) == 1:
            return personas[0]
        if not personas:
            raise ValueError("no persona is available for weekly journey")
        available = ", ".join(str(item.get("id")) for item in personas)
        raise ValueError(f"persona_id is required; available: {available}")

    @staticmethod
    def _journey_snapshot(bucket: dict) -> dict:
        meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
        return {
            "id": str(bucket.get("id") or ""),
            "name": str(meta.get("name") or bucket.get("id") or ""),
            "status": str(meta.get("journey_status") or "").strip().lower(),
            "start": str(meta.get("journey_start") or meta.get("event_time") or meta.get("created") or ""),
            "end": str(meta.get("journey_end") or ""),
            "summary": str(meta.get("journey_summary") or ""),
            "source_bucket_ids": list(dict.fromkeys(
                str(item).strip()
                for item in meta.get("journey_source_bucket_ids", []) or []
                if str(item).strip()
            )),
            "updated_at": str(meta.get("updated_at") or ""),
            "content": _plain_text(bucket.get("content")).strip(),
        }

    def _material_payload(self, bucket: dict, material_kinds: list[str], feel_rings: list[dict]) -> dict:
        meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
        return {
            "bucket_id": str(bucket.get("id") or ""),
            "bucket_name": str(meta.get("name") or bucket.get("id") or ""),
            "material_kinds": list(dict.fromkeys(material_kinds)),
            "bucket_type": str(meta.get("type") or "dynamic"),
            "created_at": str(meta.get("created") or ""),
            "domain": [str(item) for item in meta.get("domain", []) or []],
            "tags": [str(item) for item in meta.get("tags", []) or []],
            "content": self._clip(bucket.get("content", ""), self.max_material_chars),
            "new_feel_rings": feel_rings,
        }

    async def collect_input(self, *, cycle_key: str = "", persona_id: str = "", now: datetime | None = None) -> dict:
        window = self.resolve_window(cycle_key, now=now)
        start, end = window["start"], window["end"]
        persona = self._resolve_persona(persona_id)
        open_journey = await self.bucket_mgr.get_open_journey_stage()
        if not open_journey:
            raise ValueError("current open journey is required")

        review_end = (end.date() - timedelta(days=1)).isoformat()
        reviews = self.daily_review_store.list_daily_reviews(
            profile_id=self.profile_id,
            persona_id=str(persona.get("id") or ""),
            start_date=start.date().isoformat(),
            end_date=review_end,
            limit=7,
        )
        reviews_by_date = {
            str(item.get("review_date") or ""): item
            for item in reviews
            if isinstance(item, dict) and str(item.get("review_date") or "")
        }
        expected_dates = [
            (start.date() + timedelta(days=offset)).isoformat() for offset in range(7)
        ]

        material_by_id: dict[str, dict] = {}
        buckets = await self.bucket_mgr.list_all(include_archive=True)
        for bucket in buckets:
            if not isinstance(bucket, dict) or self._is_journey(bucket):
                continue
            bucket_id = str(bucket.get("id") or "").strip()
            if not bucket_id:
                continue
            meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
            created_at = self._parse_timestamp(meta.get("created"))
            bucket_type = str(meta.get("type") or "dynamic").strip().lower()
            tags = {str(item).strip().lower() for item in meta.get("tags", []) or []}
            material_kinds: list[str] = []
            feel_rings: list[dict] = []
            if created_at and start <= created_at < end:
                if bucket_type == "feel":
                    if "whisper" not in tags:
                        material_kinds.append("standalone_feel")
                else:
                    material_kinds.append("new_bucket")
            elif created_at and created_at < start:
                for comment in meta.get("comments", []) or []:
                    if not isinstance(comment, dict) or str(comment.get("kind") or "").strip().lower() != "feel":
                        continue
                    comment_at = self._parse_timestamp(comment.get("created"))
                    if not comment_at or not (start <= comment_at < end):
                        continue
                    feel_rings.append({
                        "id": str(comment.get("id") or ""),
                        "created_at": str(comment.get("created") or ""),
                        "content": self._clip(comment.get("content", ""), self.max_material_chars),
                        "author": str(comment.get("author") or ""),
                    })
                if feel_rings:
                    material_kinds.append("new_feel_ring")
            if material_kinds:
                material_by_id[bucket_id] = self._material_payload(bucket, material_kinds, feel_rings)

        journey = self._journey_snapshot(open_journey)
        journey["content"] = self._clip(journey["content"], self.max_journey_chars)
        materials = sorted(material_by_id.values(), key=lambda item: (item["created_at"], item["bucket_id"]))
        return {
            "task_type": TASK_TYPE,
            "cycle_key": window["cycle_key"],
            "timezone": str(self.tz.key),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "persona": {
                "id": str(persona.get("id") or ""),
                "name": str(persona.get("name") or persona.get("id") or ""),
            },
            "current_journey": journey,
            "daily_reviews": [
                {
                    "review_date": review_date,
                    "content": self._clip(reviews_by_date[review_date].get("content", ""), self.max_material_chars),
                    "edited_by_user": bool(reviews_by_date[review_date].get("edited_by_user")),
                    "updated_at": str(reviews_by_date[review_date].get("updated_at") or ""),
                }
                for review_date in expected_dates if review_date in reviews_by_date
            ],
            "missing_daily_review_dates": [
                review_date for review_date in expected_dates if review_date not in reviews_by_date
            ],
            "materials": materials,
        }

    @staticmethod
    def input_hash(snapshot: dict) -> str:
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_model_json(raw: str) -> dict:
        text = str(raw or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("weekly journey model did not return JSON")
            try:
                parsed = json.loads(text[start : end + 1])
            except ValueError as exc:
                raise ValueError("weekly journey model returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("weekly journey candidate must be an object")
        return parsed

    async def _generate_raw_candidate(self, snapshot: dict) -> dict:
        if self.candidate_generator:
            result = self.candidate_generator(snapshot)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict):
                raise ValueError("candidate generator must return an object")
            return result
        client = self.message_client
        if not client or not all(str(getattr(client, field, "") or "").strip() for field in ("api_key", "base_url", "model")):
            raise RuntimeError("weekly journey model is not configured")
        persona = self.daily_review_store.get_cc_persona(snapshot["persona"]["id"]) or {}
        persona_system = client._persona_system(persona) if hasattr(client, "_persona_system") else ""
        system = "\n\n".join(part for part in [
            persona_system,
            WEEKLY_JOURNEY_HARD_CONSTRAINTS,
        ] if part)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        if len(snapshot_json) > self.max_input_chars:
            raise ValueError(
                f"weekly journey input exceeds max_input_chars: {len(snapshot_json)}"
            )
        product_prompt = WEEKLY_JOURNEY_PRODUCT_PROMPT
        if self.prompt_resolver:
            product_prompt = str(self.prompt_resolver("weekly_journey") or "").strip() or product_prompt
        user = (
            f"<weekly_journey_product_prompt>\n{product_prompt}\n</weekly_journey_product_prompt>\n\n"
            f"<weekly_journey_input>\n{snapshot_json}\n</weekly_journey_input>"
        )
        raw = await client._create_message(
            system=system,
            user=user,
            max_tokens=self.max_tokens,
            temperature=0.2,
        )
        return self._parse_model_json(raw)

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @staticmethod
    def _date_value(value: Any, field: str) -> str:
        text = str(value or "").strip()
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field} must be YYYY-MM-DD") from exc
        return text

    def validate_candidate(self, raw: dict, snapshot: dict) -> dict:
        candidate_type = str(raw.get("candidate_type") or "").strip().lower()
        if candidate_type not in CANDIDATE_TYPES:
            raise ValueError("invalid weekly journey candidate_type")
        rationale = self._string_list(raw.get("rationale"))
        if not rationale:
            raise ValueError("weekly journey rationale is required")
        allowed_rows = {item["bucket_id"]: item for item in snapshot.get("materials", [])}
        evidence_ids = self._string_list(raw.get("evidence_bucket_ids"))
        proposal = raw.get("proposal") if isinstance(raw.get("proposal"), dict) else {}
        if candidate_type in {"append_current", "transition"}:
            proposal_ids = self._string_list(
                proposal.get("evidence_bucket_ids")
                if candidate_type == "append_current"
                else (proposal.get("create") or {}).get("evidence_bucket_ids")
            )
            evidence_ids = list(dict.fromkeys(evidence_ids + proposal_ids))
        unknown = [bucket_id for bucket_id in evidence_ids if bucket_id not in allowed_rows]
        if unknown:
            raise ValueError(f"candidate evidence is outside input snapshot: {', '.join(unknown)}")
        if candidate_type in {"append_current", "transition"} and not evidence_ids:
            raise ValueError("journey write candidate requires at least one evidence bucket")
        evidence = [
            {"id": bucket_id, "name": allowed_rows[bucket_id]["bucket_name"]}
            for bucket_id in evidence_ids
        ]
        current = snapshot["current_journey"]

        if candidate_type == "no_change":
            proposal = {}
            preview = {"writes": [], "write_count": 0, "current_journey_id": current["id"]}
        elif candidate_type == "append_current":
            append_content = str(proposal.get("append_content") or "").strip()
            summary = str(proposal.get("summary") or "").strip()
            if not append_content or not summary:
                raise ValueError("append candidate requires append_content and summary")
            before_ids = self._string_list(current.get("source_bucket_ids"))
            after_ids = list(dict.fromkeys(before_ids + evidence_ids))
            proposal = {
                "append_content": append_content,
                "summary": summary,
                "evidence_bucket_ids": evidence_ids,
            }
            preview = {
                "current_journey_id": current["id"],
                "content_append": append_content,
                "summary_before": current.get("summary", ""),
                "summary_after": summary,
                "evidence_before": before_ids,
                "evidence_after": after_ids,
                "added_evidence": [item for item in evidence if item["id"] not in before_ids],
                "write_count": 1,
            }
        else:
            close = proposal.get("close") if isinstance(proposal.get("close"), dict) else {}
            create = proposal.get("create") if isinstance(proposal.get("create"), dict) else {}
            stage_end = self._date_value(close.get("stage_end"), "close.stage_end")
            close_summary = str(close.get("summary") or "").strip()
            stage_start = self._date_value(create.get("stage_start"), "create.stage_start")
            name = str(create.get("name") or "").strip()
            new_summary = str(create.get("summary") or "").strip()
            content = str(create.get("content") or "").strip()
            if not all((close_summary, name, new_summary, content)):
                raise ValueError("transition candidate requires complete close and create previews")
            window_start = date.fromisoformat(snapshot["window_start"][:10])
            window_end = date.fromisoformat(snapshot["window_end"][:10])
            close_date = date.fromisoformat(stage_end)
            start_date = date.fromisoformat(stage_start)
            if not (window_start <= close_date < window_end):
                raise ValueError("close.stage_end must fall within the reviewed week boundary")
            if not (window_start <= start_date < window_end):
                raise ValueError("create.stage_start must fall within the reviewed week")
            if close_date > start_date:
                raise ValueError("close.stage_end cannot be after create.stage_start")
            proposal = {
                "close": {"stage_end": stage_end, "summary": close_summary},
                "create": {
                    "name": name,
                    "stage_start": stage_start,
                    "summary": new_summary,
                    "content": content,
                    "evidence_bucket_ids": evidence_ids,
                },
            }
            preview = {
                "close": {
                    "journey_id": current["id"],
                    "status_before": current.get("status", ""),
                    "status_after": "closed",
                    "end_before": current.get("end", ""),
                    "end_after": stage_end,
                    "summary_before": current.get("summary", ""),
                    "summary_after": close_summary,
                },
                "create": proposal["create"],
                "evidence": evidence,
                "write_count": 2,
            }
        return {
            "candidate_type": candidate_type,
            "rationale": rationale,
            "evidence": evidence,
            "preview": preview,
            "draft": proposal,
        }

    async def run_manual(self, *, cycle_key: str = "", persona_id: str = "") -> dict:
        snapshot = await self.collect_input(cycle_key=cycle_key, persona_id=persona_id)
        digest = self.input_hash(snapshot)
        run, created = self.automation_store.start_run(
            task_type=TASK_TYPE,
            cycle_key=snapshot["cycle_key"],
            window_start=snapshot["window_start"],
            window_end=snapshot["window_end"],
            timezone=snapshot["timezone"],
            trigger="manual",
            input_snapshot=snapshot,
            input_hash=digest,
        )
        if not created:
            candidate = self.automation_store.get_candidate_for_run(run.get("run_id", ""))
            if candidate:
                return {"status": "exists", "run": run, "candidate": candidate}
            if run.get("status") == "running":
                return {"status": "running", "run": run, "candidate": {}}
            if run.get("status") == "failed":
                run = self.automation_store.restart_failed_run(run["run_id"])
        try:
            raw = await self._generate_raw_candidate(snapshot)
            candidate_data = self.validate_candidate(raw, snapshot)
            candidate, candidate_created = self.automation_store.create_candidate(
                run_id=run["run_id"],
                task_type=TASK_TYPE,
                **candidate_data,
            )
            run = self.automation_store.finish_run(run["run_id"], status="completed")
            return {
                "status": "candidate_created" if candidate_created else "exists",
                "run": run,
                "candidate": candidate,
            }
        except Exception as exc:
            run = self.automation_store.finish_run(run["run_id"], status="failed", error=str(exc))
            return {"status": "failed", "run": run, "candidate": {}, "error": str(exc)}
