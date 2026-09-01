import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_wake_scheduler import AgentWakeScheduler
from agent_wake_store import AgentWakeStore


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("runner failed", request=None, response=None)

    def json(self):
        return self.payload


class _Client:
    response = _Response({"status": "completed", "turn_id": 7})
    calls = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return self.response


class AgentWakeSchedulerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "gateway_state.db"
        self.store = AgentWakeStore(str(self.db_path))
        _Client.calls = []
        _Client.response = _Response({"status": "completed", "turn_id": 7})

    def tearDown(self):
        self.temp_dir.cleanup()

    def arm(self):
        self.store.create_schedule(
            profile_id="default", session_id="window-1", lane_id="subscription",
            keepalive_enabled=True,
            cache_keepalive_deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    async def test_completed_callback_finishes_the_persisted_run(self):
        self.arm()
        scheduler = AgentWakeScheduler(
            self.store, runner_url="https://dashboard.test/api/cc-agent-wake-runner",
            token="secret", owner="owner-a",
        )
        with patch("agent_wake_scheduler.httpx.AsyncClient", _Client):
            result = await scheduler.run_once()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(_Client.calls[0][1]["Authorization"], "Bearer secret")
        runs = self.store.list_runs(profile_id="default", session_id="window-1")
        self.assertEqual(runs[0]["turn_id"], 7)

    async def test_network_failure_is_persisted_and_survives_store_restart(self):
        self.arm()
        _Client.response = _Response({}, status_code=502)
        scheduler = AgentWakeScheduler(
            self.store, runner_url="https://dashboard.test/api/cc-agent-wake-runner",
            token="secret", owner="owner-a",
        )
        with patch("agent_wake_scheduler.httpx.AsyncClient", _Client):
            result = await scheduler.run_once()
        self.assertEqual(result["status"], "failed")
        restored = AgentWakeStore(str(self.db_path)).get_schedule(
            profile_id="default", session_id="window-1", lane_id="subscription"
        )
        self.assertTrue(restored["retry_at"])
        self.assertEqual(restored["consecutive_failures"], 1)


if __name__ == "__main__":
    unittest.main()
