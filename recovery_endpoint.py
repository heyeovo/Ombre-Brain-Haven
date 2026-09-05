"""
Recovery Console endpoint — completely independent of the Next.js dashboard.

Spawns the Claude Code CLI binary directly in --print mode with stream-json
output, so the recovery channel works even when the dashboard is down.

Routes:
  GET  /recovery       → serve the self-contained HTML console
  GET  /recovery/ping  → auth check (returns 200 if token valid)
  POST /recovery/chat  → stream a conversation turn via claude CLI
"""

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

logger = logging.getLogger("ombre_brain.recovery")

RECOVERY_HTML = (Path(__file__).parent / "recovery.html").read_text(encoding="utf-8")

CLAUDE_BINARY = "/usr/local/bin/claude"

RECOVERY_SYSTEM_PROMPT = """\
You are in RECOVERY MODE inside the Haven container. The main dashboard frontend may be broken.

Your job: diagnose and fix issues so the system comes back online.

Environment:
- You are inside the Haven (Ombre Brain) container.
- Haven code: /app (Python, Starlette + MCP server)
- Dashboard code is in a SEPARATE container and git repo (github.com/heyeovo/ob-dashboard2).
  To inspect dashboard code, clone it: git clone https://github.com/heyeovo/ob-dashboard2.git /tmp/dashboard
- The dashboard is a Next.js app deployed via Coolify.
- Haven and Dashboard are separate Docker services in the same Coolify stack.

What you can do:
- Read/edit Haven code at /app
- Run bash commands — check logs, inspect state
- Clone and inspect the dashboard repo if the issue is there
- Guide the user on what to do in Coolify (restart, rollback commit SHA, etc.)

What you CANNOT do:
- Directly restart the dashboard container (it's a separate service)
- Access MCP tools (Ombre Brain, agent wake, etc.)

Keep responses concise. Focus on diagnosing and fixing.\
"""


def _authorize(request: Request, gateway_token: str) -> JSONResponse | None:
    if not gateway_token:
        return JSONResponse({"error": "gateway token not configured"}, status_code=503)
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return JSONResponse({"error": "Bearer token required"}, status_code=401)
    if not secrets.compare_digest(token, gateway_token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    return None


async def recovery_page(request: Request) -> Response:
    return HTMLResponse(RECOVERY_HTML)


async def recovery_ping(request: Request) -> Response:
    gateway_token = request.app.state.gateway_token
    err = _authorize(request, gateway_token)
    if err:
        return err
    return JSONResponse({"ok": True})


async def recovery_chat(request: Request) -> Response:
    gateway_token = request.app.state.gateway_token
    err = _authorize(request, gateway_token)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    if not os.path.isfile(CLAUDE_BINARY):
        return JSONResponse(
            {"error": f"claude binary not found at {CLAUDE_BINARY}"},
            status_code=500,
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OMBRE_GATEWAY_UPSTREAM_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OMBRE_GATEWAY_UPSTREAM_BASE_URL", "")

    if not api_key:
        return JSONResponse(
            {"error": "No API key configured (set ANTHROPIC_API_KEY or OMBRE_GATEWAY_UPSTREAM_API_KEY)"},
            status_code=500,
        )

    cmd = [
        CLAUDE_BINARY,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--bare",
        "--no-session-persistence",
        "--system-prompt", RECOVERY_SYSTEM_PROMPT,
    ]

    cmd.append(prompt)

    child_env = {**os.environ, "ANTHROPIC_API_KEY": api_key}
    if base_url:
        child_env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")

    async def stream():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                cwd="/app",
            )

            async def read_stderr():
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    logger.debug("claude stderr: %s", line.decode(errors="replace").rstrip())

            stderr_task = asyncio.create_task(read_stderr())

            buffer = b""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode(errors="replace").strip()
                    if not text:
                        continue
                    # Parse the stream-json line and re-emit as SSE
                    try:
                        event = json.loads(text)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except json.JSONDecodeError:
                        logger.debug("non-json line from claude: %s", text)

            if buffer.strip():
                text = buffer.decode(errors="replace").strip()
                try:
                    event = json.loads(text)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except json.JSONDecodeError:
                    pass

            await proc.wait()
            await stderr_task
            yield "data: [DONE]\n\n"

        except Exception as exc:
            logger.exception("recovery chat error")
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
