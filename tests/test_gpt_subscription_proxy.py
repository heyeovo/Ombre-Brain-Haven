import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from starlette.routing import Route

from gpt_subscription_proxy import mount_gpt_subscription_proxy


class GPTSubscriptionProxyTest(unittest.IsolatedAsyncioTestCase):
    """Real HTTP on both sides of the exact Brain route mount; no real account."""

    async def asyncSetUp(self):
        self.calls = []
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.behavior = "normal"
        self.servers = []
        self.tasks = []

        async def gateway(request):
            self.calls.append({"operation": request.path_params["operation"],
                "method": request.method, "body": await request.body(),
                "query": request.url.query, "headers": dict(request.headers)})
            if self.behavior == "quota":
                return JSONResponse({"ok": False, "error": {
                    "code": "subscription_sharing_usage_limit_exceeded", "action": "usage_settings"}},
                    status_code=429, headers={"Set-Cookie": "never-forward=synthetic"})
            if self.behavior == "redirect":
                return RedirectResponse("https://example.invalid/never-follow")
            if self.behavior == "invalid":
                return PlainTextResponse("synthetic-private-diagnostic", status_code=500)
            operation = request.path_params["operation"]
            if operation == "responses":
                async def output():
                    try:
                        yield b'event: delta\ndata: {"text":"first"}\n\n'
                        await self.release.wait()
                        yield b'event: completed\ndata: {"usage":null}\n\n'
                    finally:
                        self.closed.set()
                return StreamingResponse(output(), media_type="text/event-stream")
            if operation == "models":
                return JSONResponse({"ok": True, "models": [{"slug": "synthetic-model", "displayName": "Test"}]})
            return JSONResponse({"ok": True, "status": "connected", "sharing": True})

        gateway_app = Starlette(routes=[Route("/private/api/gpt-subscription/{operation}", gateway,
                                              methods=["GET", "POST"])])
        upstream = await self.serve(gateway_app)
        self.environment = patch.dict(os.environ, {
            "OMBRE_GATEWAY_TOKEN": "synthetic-gateway-token",
            "OMBRE_GATEWAY_ADMIN_URL": upstream + "/private/api/config",
        })
        self.environment.start()
        brain = Starlette()
        mount_gpt_subscription_proxy(brain)
        self.client = httpx.AsyncClient(base_url=await self.serve(brain), timeout=3,
            headers={"Authorization": "Bearer synthetic-gateway-token"}, trust_env=False)

    async def serve(self, app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        server.install_signal_handlers = lambda: None
        task = asyncio.create_task(server.serve(sockets=[sock]))
        self.servers.append(server)
        self.tasks.append(task)
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        return f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.release.set()
        await self.client.aclose()
        for server in self.servers:
            server.should_exit = True
        await asyncio.wait_for(asyncio.gather(*self.tasks), 5)
        self.environment.stop()

    def path(self, operation):
        return f"/gateway/api/gpt-subscription/{operation}"

    async def test_state_models_management_admission_and_cancel_reach_owner(self):
        for method, operation in [("GET", "connection"), ("POST", "connection"),
                ("GET", "connections"), ("GET", "models"), ("POST", "chat-requests"), ("POST", "cancel")]:
            with self.subTest(method=method, operation=operation):
                response = await self.client.request(method, self.path(operation),
                    content=b'{"request_id":"synthetic-request"}' if method == "POST" else b"",
                    headers={"Cookie": "must-not-forward=synthetic", "X-Profile-Id": "untrusted"},
                    params={"profile_id": "untrusted"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                call = self.calls[-1]
                self.assertEqual((call["operation"], call["method"]), (operation, method))
                self.assertEqual(call["headers"]["authorization"], "Bearer synthetic-gateway-token")
                self.assertNotIn("cookie", call["headers"])
                self.assertNotIn("x-profile-id", call["headers"])
                self.assertEqual(call["query"], "")
                if method == "POST":
                    self.assertEqual(json.loads(call["body"])["request_id"], "synthetic-request")
        self.assertEqual((await self.client.get(self.path("models"))).json()["models"][0]["slug"], "synthetic-model")

    async def test_authentication_is_required_before_upstream_access(self):
        response = await self.client.post(self.path("responses"), content=b"invalid",
                                         headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        with patch.dict(os.environ, {"OMBRE_GATEWAY_TOKEN": ""}):
            self.assertEqual((await self.client.get(self.path("connection"))).status_code, 503)
        self.assertEqual(self.calls, [])

    async def test_only_explicit_operations_and_methods_are_exposed(self):
        self.assertEqual((await self.client.get(self.path("responses"))).status_code, 405)
        self.assertEqual((await self.client.post(self.path("models"))).status_code, 405)
        self.assertEqual((await self.client.post(self.path("import"))).status_code, 404)
        self.assertEqual(self.calls, [])

    async def test_body_limits_are_checked_before_forwarding(self):
        self.assertEqual((await self.client.post(self.path("chat-requests"), content=b"x" * 4097)).status_code, 413)
        self.assertEqual((await self.client.post(self.path("responses"), content=b"x" * (4 * 1024 * 1024 + 1))).status_code, 413)
        self.assertEqual(self.calls, [])

    async def test_missing_or_failed_gateway_has_safe_errors_without_fallback(self):
        for url, status in [("", 503), ("http://127.0.0.1:1/api/config", 502),
                            ("http://user:secret@127.0.0.1/api/config", 503)]:
            with self.subTest(url=url), patch.dict(os.environ, {"OMBRE_GATEWAY_ADMIN_URL": url}):
                response = await self.client.get(self.path("connection"))
                self.assertEqual(response.status_code, status)
                self.assertNotIn("secret", response.text)
                self.assertNotIn("127.0.0.1", response.text)
        self.assertEqual(self.calls, [])

    async def test_gateway_error_and_action_preserved_without_response_cookies(self):
        self.behavior = "quota"
        response = await self.client.get(self.path("models"))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["action"], "usage_settings")
        self.assertNotIn("set-cookie", response.headers)

    async def test_redirects_and_non_json_diagnostics_are_not_forwarded(self):
        for behavior in ["redirect", "invalid"]:
            self.behavior = behavior
            response = await self.client.get(self.path("connection"))
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("location", response.headers)
            self.assertNotIn("synthetic-private", response.text)

    async def test_sse_delivers_first_delta_before_gateway_completes(self):
        async with self.client.stream("POST", self.path("responses"), json={"request_id": "stream"}) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            lines = response.aiter_lines()
            self.assertEqual(await anext(lines), "event: delta")
            self.assertFalse(self.release.is_set())
            self.release.set()
            rest = [line async for line in lines]
            self.assertIn("event: completed", rest)

    async def test_disconnect_closes_upstream_stream(self):
        async with self.client.stream("POST", self.path("responses"), json={"request_id": "disconnect"}) as response:
            self.assertEqual(await anext(response.aiter_lines()), "event: delta")
        await asyncio.wait_for(self.closed.wait(), 3)
        self.assertFalse(self.release.is_set())

    async def test_real_brain_startup_registers_public_gpt_entry(self):
        # Exercise server.py itself as well as the mount function, so a missing
        # registration in Brain cannot pass by testing Gateway in isolation.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "brain.log"
            environment = {**os.environ, "OMBRE_TRANSPORT": "streamable-http", "OMBRE_PORT": str(port),
                "OMBRE_BUCKETS_DIR": str(Path(directory) / "buckets"),
                "OMBRE_STATE_DIR": str(Path(directory) / "state"),
                "OMBRE_RUNTIME_CONFIG_PATH": str(Path(directory) / "runtime.yaml")}
            with log.open("wb") as output:
                process = await asyncio.create_subprocess_exec(sys.executable, "-B", "server.py",
                    cwd=Path(__file__).resolve().parents[1], env=environment,
                    stdout=output, stderr=asyncio.subprocess.STDOUT)
                try:
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                            headers={"Authorization": "Bearer synthetic-gateway-token"},
                            timeout=2, trust_env=False) as client:
                        deadline = asyncio.get_running_loop().time() + 20
                        while True:
                            try:
                                response = await client.get(self.path("models"))
                                break
                            except httpx.ConnectError:
                                if process.returncode is not None or asyncio.get_running_loop().time() >= deadline:
                                    self.fail("Brain failed to start: " + "\n".join(log.read_text(errors="replace").splitlines()[-10:]))
                                await asyncio.sleep(0.1)
                        self.assertEqual(response.status_code, 200, response.text[:200])
                        self.assertEqual(response.json()["models"][0]["slug"], "synthetic-model")
                        self.assertEqual(self.calls[-1]["operation"], "models")
                finally:
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()


if __name__ == "__main__":
    unittest.main()
