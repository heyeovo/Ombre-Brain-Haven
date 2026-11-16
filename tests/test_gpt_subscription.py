import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway import GatewayService
from gpt_subscription import (
    GPTSubscriptionError, GPTSubscriptionService, public_error, validate_request,
)
from starlette.requests import Request


def request(path, body=None, token="test-token", method="POST"):
    wire = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": wire, "more_body": False}

    return Request({"type": "http", "method": method, "path": f"/api/gpt-subscription/{path}",
                    "headers": [(b"authorization", f"Bearer {token}".encode())], "query_string": b""}, receive)


def gateway(service):
    result = object.__new__(GatewayService)
    result.gateway_token = "test-token"
    result.persona_engine = SimpleNamespace(profile_id="trusted-profile")
    result.gpt_subscription = service
    return result


class GPTSubscriptionContractTest(unittest.IsolatedAsyncioTestCase):
    def body(self, **changes):
        return {"request_id": "req-1", "model": "test-model", "input": [{"role": "user", "content": "text"}], **changes}

    async def test_no_browser_or_body_override_of_profile_and_no_http_import(self):
        service = SimpleNamespace(key_file="configured", call=AsyncMock(return_value={"ok": True, "status": "connected"}))
        handler = gateway(service)
        response = await handler.handle_gpt_subscription(request("connection", method="GET"))
        self.assertEqual(response.status_code, 200)
        service.call.assert_awaited_once_with("trusted-profile", "status")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for body in ({"op": "import", "access_token": "do-not-echo"}, {"action": "disconnect", "profile_id": "attacker"}):
            response = await handler.handle_gpt_subscription(request("connection", body))
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(b"do-not-echo", response.body)
        service.call.assert_awaited_once()

    async def test_auth_precedes_any_helper_or_body_read(self):
        service = SimpleNamespace(call=AsyncMock())
        response = await gateway(service).handle_gpt_subscription(request("connection", token="wrong", method="GET"))
        self.assertEqual(response.status_code, 401)
        service.call.assert_not_called()

    async def test_disabled_integration_does_not_break_existing_gateway(self):
        service = SimpleNamespace(key_file="", call=AsyncMock())
        response = await gateway(service).handle_gpt_subscription(request("connection", method="GET"))
        state = json.loads(response.body)
        self.assertFalse(state["configured"])
        self.assertFalse(state["sharing"])
        service.call.assert_not_called()

    async def test_text_only_contract_rejects_unhandled_capabilities(self):
        for changes in ({"tools": []}, {"profile_id": "attacker"}, {"store": True}, {"input": "raw text"},
                        {"input": [{"role": "tool", "content": "result"}]},
                        {"input": [{"role": "user", "content": [{"type": "image_url"}]}]},
                        {"request_id": "../../outside"}, {"instructions": {}}, {"model": ""}):
            with self.subTest(changes=changes), self.assertRaises(GPTSubscriptionError):
                validate_request(self.body(**changes))
        self.assertEqual(validate_request(self.body())["input"][0]["content"], "text")

    async def test_stream_events_terminal_usage_and_authenticated_scope(self):
        quota_evidence = {"status": "observed", "snapshots": [{
            "source": "response_headers", "observed_at": "2026-11-16T00:00:00.000Z",
            "limit_id": "codex", "scope": "unverified", "secondary": None,
            "primary": {"used_percent": 15, "window_minutes": 300, "resets_at": 1900000000},
        }]}
        async def stream(profile, request_id, body):
            self.assertEqual(profile, "trusted-profile")
            self.assertNotIn("request_id", body)
            yield {"type": "delta", "request_id": request_id, "text": "reply"}
            yield {"type": "completed", "request_id": request_id, "usage": None, "quotaEvidence": quota_evidence}

        service = SimpleNamespace(reserve=Mock(), stream=stream)
        response = await gateway(service).handle_gpt_subscription(request("responses", self.body()))
        service.reserve.assert_called_once_with("trusted-profile", "req-1")
        data = "".join([part async for part in response.body_iterator])
        self.assertIn("event: delta", data)
        self.assertIn('"usage": null', data)
        self.assertEqual(data.count("event: completed"), 1)
        completed = next(json.loads(line.removeprefix("data: ")) for line in data.splitlines()
                         if line.startswith("data: ") and json.loads(line[6:]).get("type") == "completed")
        self.assertEqual(completed["quotaEvidence"], quota_evidence)

    async def test_quota_and_reauth_actions_and_status_are_not_conflated(self):
        self.assertEqual(public_error({"code": "invalid_grant"})["action"], "reconnect")
        self.assertEqual(public_error({"code": "subscription_sharing_usage_limit_exceeded"})["action"], "usage_settings")
        self.assertEqual(public_error({"code": "subscription_sharing_usage_unavailable", "retryable": True})["action"], "retry")
        self.assertEqual(public_error({"code": "api_error", "status": 401})["action"], "inspect")
        self.assertEqual(GatewayService._gpt_result_status({"ok": False, "error": {"status": 429}}), 429)

    async def test_request_registry_is_profile_scoped_and_cancellation_before_spawn_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GPTSubscriptionService(directory, key_file="dummy-key", node="node")
            service.reserve("a", "same")
            service.reserve("b", "same")
            with self.assertRaises(GPTSubscriptionError) as context:
                service.reserve("a", "same")
            self.assertEqual(context.exception.status, 409)
            self.assertFalse((await service.cancel("other", "same"))["cancel_requested"])
            self.assertTrue((await service.cancel("a", "same"))["cancel_requested"])
            events = [event async for event in service.stream("a", "same", validate_request(self.body()))]
            self.assertEqual(events[0]["error"]["code"], "cancelled")
            self.assertNotIn(("a", "same"), service._running)
            self.assertIn(("b", "same"), service._running)
            self.assertNotEqual(service.scope("a"), service.scope("../a"))

    async def test_process_protocol_preserves_partial_reply_on_failure_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GPTSubscriptionService(directory, key_file="dummy-key", node="node")
            process = SimpleNamespace()
            service._spawn = AsyncMock(return_value=process)
            service._stop = AsyncMock()
            service._read = AsyncMock(side_effect=[{"type": "delta", "text": "partial"}, GPTSubscriptionError("transport_interrupted", "中断")])
            service.reserve("a", "req")
            events = [event async for event in service.stream("a", "req", {})]
            self.assertEqual([event["type"] for event in events], ["delta", "error"])
            self.assertEqual(events[0]["text"], "partial")
            self.assertEqual(service._running, {})
            service._stop.assert_awaited_once_with(process)

    async def test_disconnect_cancels_current_profile_only_and_blocks_new_streams_during_change(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GPTSubscriptionService(directory, key_file="dummy-key", node="node")
            service.reserve("a", "req")
            service.reserve("b", "req")

            async def spawn(profile, command, **kwargs):
                with self.assertRaises(GPTSubscriptionError):
                    service.reserve("a", "new")
                return object()

            service._spawn = spawn
            service._read = AsyncMock(return_value={"type": "result", "data": {"status": "disconnected"}})
            service._stop = AsyncMock()
            self.assertTrue((await service.call("a", "disconnect"))["ok"])
            self.assertIn(("a", "req"), service._cancelled)
            self.assertNotIn(("b", "req"), service._cancelled)
            self.assertEqual(service._changing, set())

    async def test_actual_helper_process_returns_masked_state_without_network_and_missing_key_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            import base64
            key.write_text(base64.b64encode(os.urandom(32)).decode())
            key.chmod(0o600)
            service = GPTSubscriptionService(str(Path(directory) / "buckets"), key_file=str(key))
            state = await service.call("a", "status")
            self.assertEqual(state["status"], "disconnected")
            self.assertIsNone(state["limits"])
            sentinel = Path(service.root) / service.scope("a") / "sentinel"
            sentinel.write_bytes(b"preserve existing data")
            original = sentinel.read_bytes()
            key.unlink()
            failed = await service.call("a", "status")
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"]["code"], "encryption_key_unavailable")
            self.assertEqual(sentinel.read_bytes(), original)
            await service.close()


if __name__ == "__main__":
    unittest.main()
