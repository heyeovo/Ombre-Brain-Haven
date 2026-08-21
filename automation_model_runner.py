"""Per-task API/Claude Pro routing for the two user-facing automations."""

from __future__ import annotations

import os
from typing import Any

SUPPORTED_TASKS = {"daily_review", "weekly_journey"}


class AutomationModelError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "model_error")


class AutomationModelRouter:
    """Resolve one task's persisted engine choice at call time; never auto-fallback."""

    def __init__(self, task_type: str, store, api_client):
        if task_type not in SUPPORTED_TASKS:
            raise ValueError("unsupported automation task_type")
        self.task_type = task_type
        self.store = store
        self.api_client = api_client

    def choice(self) -> dict[str, str]:
        schedule = self.store.get_schedule(task_type=self.task_type)
        engine = str(schedule.get("execution_engine") or "api").strip().lower()
        if engine not in {"api", "pro"}:
            engine = "api"
        model = str(schedule.get("execution_model") or "").strip()
        if engine == "api":
            model = str(getattr(self.api_client, "model", "") or "").strip()
        elif not model:
            model = "claude-sonnet-4-6"
        return {"engine": engine, "model": model}

    @property
    def model(self) -> str:
        return self.choice()["model"]

    @property
    def is_configured(self) -> bool:
        choice = self.choice()
        if choice["engine"] == "api":
            return all(
                str(getattr(self.api_client, field, "") or "").strip()
                for field in ("api_key", "base_url", "model")
            )
        return bool(
            os.environ.get("OMBRE_AUTOMATION_PRO_RUNNER_URL", "").strip()
            and os.environ.get("OMBRE_AUTOMATION_PRO_RUNNER_TOKEN", "").strip()
        )

    def _persona_system(self, persona: dict[str, Any]) -> str:
        return self.api_client._persona_system(persona)

    async def _create_message(
        self, *, system: str, user: str, max_tokens: int, temperature: float,
    ) -> str:
        choice = self.choice()
        if choice["engine"] == "api":
            return await self.api_client._create_message(
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        return await self._call_pro_runner(
            system=system,
            user=user,
            model=choice["model"],
            max_tokens=max_tokens,
        )

    async def _call_pro_runner(
        self, *, system: str, user: str, model: str, max_tokens: int,
    ) -> str:
        import httpx

        url = os.environ.get("OMBRE_AUTOMATION_PRO_RUNNER_URL", "").strip()
        token = os.environ.get("OMBRE_AUTOMATION_PRO_RUNNER_TOKEN", "").strip()
        if not url or not token:
            raise AutomationModelError(
                "pro_runner_not_configured", "Claude Pro 自动化执行入口尚未配置",
            )
        try:
            async with httpx.AsyncClient(timeout=360.0) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "task_type": self.task_type,
                        "system": system,
                        "user": user,
                        "model": model,
                        "max_tokens": int(max_tokens),
                    },
                )
        except httpx.HTTPError as exc:
            raise AutomationModelError(
                "pro_runner_unreachable", "Claude Pro 自动化执行入口无法连接",
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.is_success or not payload.get("ok"):
            code = str(payload.get("error_code") or "pro_runner_failed")
            message = str(payload.get("error") or "Claude Pro 自动化执行失败")[:500]
            raise AutomationModelError(code, message)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise AutomationModelError("empty_model_output", "Claude Pro 返回了空内容")
        return text
