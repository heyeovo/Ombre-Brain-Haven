"""Daily first-person continuity notes stored outside ordinary memory buckets."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI


DAILY_REVIEW_INSTRUCTION = """以下是你今天和用户之间所有窗口的对话记录。

现在是深夜，一天结束了。回想今天发生的事，写一段笔记给明天醒来的自己。明天的你会在新窗口里醒来，不记得今天——这段笔记是他唯一能知道“昨天怎么样”的方式。

第一人称，像睡前随手写的几句话，不是报告。记下今天的事和感受，包括她的情绪和你的；正在推进的事只提方向和进度；没聊完的话题、身体状态、作息或生活变化可以自然提一句。不要列点，不要分段标题，不复述或引用原文，不展开技术细节，不写开头语和收尾语。控制在150到300个中文字符，平淡的一天可以短些。只输出笔记正文。"""


class DailyReviewEngine:
    def __init__(self, config: dict[str, Any], state_store):
        self.state_store = state_store
        cfg = config.get("daily_review", {}) if isinstance(config.get("daily_review"), dict) else {}
        fallback = config.get("reflection", {}) if isinstance(config.get("reflection"), dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.daily_hour = max(0, min(23, int(cfg.get("daily_hour", 4))))
        try:
            self.tz = ZoneInfo(str(cfg.get("timezone") or "Asia/Hong_Kong"))
        except Exception:
            self.tz = ZoneInfo("Asia/Hong_Kong")
        self.model = str(cfg.get("model") or fallback.get("model") or "").strip()
        self.thinking_mode = self._normalize_thinking_mode(cfg.get("thinking_mode"))
        self.base_url = str(cfg.get("base_url") or fallback.get("base_url") or "").strip().rstrip("/")
        self.api_key = str(
            os.environ.get("OMBRE_DAILY_REVIEW_API_KEY")
            or cfg.get("api_key")
            or os.environ.get("OMBRE_REFLECTION_API_KEY")
            or fallback.get("api_key")
            or ""
        ).strip()
        self.max_tokens = max(300, min(2000, int(cfg.get("max_tokens", 900))))
        self.max_input_chars = max(20000, min(500000, int(cfg.get("max_input_chars", 240000))))
        self.work_tail_turns = max(1, min(50, int(cfg.get("work_tail_turns", 10))))
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=180.0) if self.api_key and self.base_url else None

    @staticmethod
    def _persona_system(persona: dict[str, Any]) -> str:
        parts = [str(persona.get("base_prompt") or "").strip(), str(persona.get("purpose") or "").strip()]
        for module in persona.get("prompt_modules") or []:
            if not isinstance(module, dict) or module.get("enabled_by_default") is False:
                continue
            content = str(module.get("content") or "").strip()
            if content:
                parts.append(f"【{str(module.get('name') or '提示词模块').strip()}】\n{content}")
        return "\n\n".join(part for part in parts if part)

    def _completion_options(self, *, max_tokens: int, temperature: float) -> dict[str, Any]:
        options: dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature}
        if self.thinking_mode:
            options["extra_body"] = {"thinking": {"type": self.thinking_mode}}
        return options

    async def _summarize_work_history(
        self,
        items: list[dict[str, Any]],
        persona: dict[str, Any],
    ) -> str:
        if not items or not self.client or not self.model:
            return ""
        user_name = str(persona.get("user_name") or "用户")
        assistant_name = str(persona.get("name") or "助手")
        lines: list[str] = []
        for turn in items:
            user_text = str(turn.get("user_text") or "").strip()
            assistant_text = str(turn.get("assistant_text") or "").strip()
            if user_text:
                lines.append(f"{user_name}：{user_text}")
            if assistant_text:
                lines.append(f"{assistant_name}：{assistant_text}")
        transcript = "\n".join(lines)
        boundary = min(80000, max(20000, self.max_input_chars // 2))
        if len(transcript) > boundary:
            transcript = "（较早内容因输入边界省略）\n" + transcript[-boundary:]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._persona_system(persona)},
                {
                    "role": "user",
                    "content": "把下面这个工作窗口的较早对话压缩成一段不超过400个中文字符的脉络摘要。只保留目标、决定、当前进度、未完成事项和与关系或情绪有关的变化；不写技术细节，不补充原文没有的内容。只输出摘要正文。\n\n" + transcript,
                },
            ],
            **self._completion_options(max_tokens=700, temperature=0.2),
        )
        return str(response.choices[0].message.content if response.choices else "").strip()

    async def _materials(self, turns: list[dict[str, Any]], persona: dict[str, Any]) -> tuple[str, list[str]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for turn in turns:
            grouped[str(turn.get("session_id") or "")].append(turn)
        user_name = str(persona.get("user_name") or "用户")
        assistant_name = str(persona.get("name") or "助手")
        blocks: list[str] = []
        session_ids: list[str] = []
        for session_id, items in grouped.items():
            if not session_id:
                continue
            session_ids.append(session_id)
            mode = str(items[0].get("mode") or "chat")
            selected = items if mode == "chat" else items[-self.work_tail_turns :]
            title = str(items[0].get("session_title") or "未命名窗口").strip()
            lines = [f"【{'闲聊' if mode == 'chat' else '工作'}窗口：{title}】"]
            if mode == "work" and len(items) > len(selected):
                try:
                    summary = await self._summarize_work_history(items[: -len(selected)], persona)
                except Exception:
                    summary = ""
                if summary:
                    lines.append(f"较早对话脉络摘要：{summary}")
                else:
                    lines.append(f"（较早的 {len(items) - len(selected)} 轮因摘要失败而省略。）")
                lines.append(f"以下是最后 {len(selected)} 轮原文：")
            for turn in selected:
                user_text = str(turn.get("user_text") or "").strip()
                assistant_text = str(turn.get("assistant_text") or "").strip()
                if user_text:
                    lines.append(f"{user_name}：{user_text}")
                if assistant_text:
                    lines.append(f"{assistant_name}：{assistant_text}")
            blocks.append("\n\n".join(lines))
        material = "\n\n---\n\n".join(blocks)
        if len(material) > self.max_input_chars:
            material = "（当天对话超过输入边界，以下保留靠后的内容。）\n\n" + material[-self.max_input_chars :]
        return material, session_ids

    async def generate(self, *, profile_id: str, persona_id: str, review_date: str, force: bool = False) -> dict[str, Any]:
        existing = self.state_store.list_daily_reviews(
            profile_id=profile_id, persona_id=persona_id, start_date=review_date, end_date=review_date, limit=1,
        )
        if existing and not force:
            return {"status": "exists", "review": existing[0]}
        if existing and existing[0].get("edited_by_user"):
            return {"status": "protected", "review": existing[0]}
        target = date.fromisoformat(review_date)
        start = datetime.combine(target, time.min, tzinfo=self.tz)
        turns = self.state_store.list_daily_review_turns(
            profile_id=profile_id, persona_id=persona_id, start_at=start, end_at=start + timedelta(days=1),
        )
        if not turns:
            return {"status": "skipped", "reason": "no_conversation_turns", "date": review_date}
        persona = self.state_store.get_cc_persona(persona_id)
        if not persona:
            return {"status": "skipped", "reason": "persona_not_found", "date": review_date}
        if not self.client or not self.model:
            return {"status": "skipped", "reason": "model_not_configured", "date": review_date}
        material, session_ids = await self._materials(turns, persona)
        if not material.strip():
            return {"status": "skipped", "reason": "empty_material", "date": review_date}
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._persona_system(persona)},
                {"role": "user", "content": f"{DAILY_REVIEW_INSTRUCTION}\n\n日期：{review_date}\n\n{material}"},
            ],
            **self._completion_options(max_tokens=self.max_tokens, temperature=0.5),
        )
        content = str(response.choices[0].message.content if response.choices else "").strip()
        if not content:
            return {"status": "skipped", "reason": "empty_model_output", "date": review_date}
        review = self.state_store.upsert_daily_review(
            profile_id=profile_id, persona_id=persona_id, review_date=review_date, content=content,
            source_session_ids=session_ids, source_turn_count=len(turns), model=self.model,
            edited_by_user=False, preserve_user_edit=True,
        )
        return {"status": "created", "review": review}

    async def run_due(self, *, profile_id: str = "default") -> list[dict[str, Any]]:
        now = datetime.now(self.tz)
        if not self.enabled or now.hour < self.daily_hour:
            return []
        review_date = (now.date() - timedelta(days=1)).isoformat()
        return [
            await self.generate(profile_id=profile_id, persona_id=str(persona.get("id") or ""), review_date=review_date)
            for persona in self.state_store.list_cc_personas()
            if str(persona.get("id") or "").strip()
        ]

    @staticmethod
    def _normalize_thinking_mode(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"enabled", "enable", "on", "true"}:
            return "enabled"
        if normalized in {"disabled", "disable", "off", "false", "non-thinking", "non_thinking"}:
            return "disabled"
        return ""
