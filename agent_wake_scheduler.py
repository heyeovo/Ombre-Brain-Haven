"""Persistent 30-second bridge from Haven wake schedules to Dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from agent_wake_store import AgentWakeStore


class AgentWakeScheduler:
    def __init__(
        self,
        store: AgentWakeStore,
        *,
        runner_url: str,
        token: str,
        owner: str,
        timeout_seconds: float = 390.0,
    ) -> None:
        self.store = store
        self.runner_url = str(runner_url or "").strip()
        self.token = str(token or "").strip()
        self.owner = str(owner or "").strip()
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        if not self.runner_url or not self.token or not self.owner:
            raise ValueError("agent wake runner_url, token and owner are required")

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claimed = self.store.claim_due_schedule(
            owner=self.owner,
            now=current,
            lease_seconds=max(420, int(self.timeout_seconds) + 30),
        )
        if not claimed:
            return {"status": "idle"}

        schedule = claimed["schedule"]
        run = claimed["run"]
        wake_id = str(run["wake_id"])
        payload = {
            "wake_id": wake_id,
            "profile_id": schedule["profile_id"],
            "session_id": schedule["session_id"],
            "lane_id": schedule["lane_id"],
            "schedule_version": run["schedule_version"],
            "lease_owner": self.owner,
            "cause": run["cause"],
            "due_at": run["due_at"],
            "reason": schedule.get("wake_reason", "") if run["cause"] == "agent_schedule" else "",
            "silence_source_turn_id": schedule.get("silence_source_turn_id", 0),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.runner_url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Dashboard wake runner returned non-object JSON")
            status = str(data.get("status") or "failed")
            if status == "in_progress":
                return {"status": status, "wake_id": wake_id, "response": data}
            if status not in {"completed", "deferred", "failed", "superseded"}:
                status = "failed"
            finished = self.store.finish_run(
                wake_id=wake_id,
                owner=self.owner,
                status=status,
                turn_id=int(data["turn_id"]) if data.get("turn_id") else None,
                error=str(data.get("error") or data.get("reason") or ""),
            )
            return {"status": finished["status"], "wake_id": wake_id, "response": data}
        except Exception as exc:
            finished = self.store.finish_run(
                wake_id=wake_id,
                owner=self.owner,
                status="failed",
                error=str(exc),
            )
            return {"status": finished["status"], "wake_id": wake_id, "error": str(exc)}
