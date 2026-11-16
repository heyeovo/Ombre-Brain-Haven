"""Brain's public GPT paths relay to the single credential-owning Gateway."""
import hmac
import os
import json
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def failure(code, status, retryable=False):
    return JSONResponse({"ok": False, "error": {
        "code": code, "message": "GPT Gateway 暂不可用，请检查服务连接。",
        "retryable": retryable, "action": "retry" if retryable else "inspect",
    }}, status_code=status, headers={"Cache-Control": "no-store"})


async def forward_gpt_subscription(request):
    token = os.environ.get("OMBRE_GATEWAY_TOKEN", "").strip()
    if not token:
        return failure("gateway_not_configured", 503)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return failure("unauthorized", 401)

    # Derive only from the trusted existing admin endpoint, never caller input.
    admin = urlsplit(os.environ.get("OMBRE_GATEWAY_ADMIN_URL", "").strip())
    path = admin.path.rstrip("/")
    if (admin.scheme not in {"http", "https"} or not admin.hostname
            or admin.username or admin.password or admin.query or admin.fragment
            or not path.endswith("/api/config")):
        return failure("gateway_not_configured", 503)
    operation = request.url.path.rsplit("/", 1)[-1]
    url = urlunsplit((admin.scheme, admin.netloc,
        path[:-len("/api/config")] + f"/api/gpt-subscription/{operation}", "", ""))
    body = bytearray()
    if request.method == "POST":
        limit = 4 * 1024 * 1024 if operation == "responses" else 512 * 1024 if operation == 'chat-requests' else 4096
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return failure("request_too_large", 413)
        if operation == 'chat-requests' and len(body) > 4096:
            try:
                value = json.loads(body)
                if not isinstance(value, dict) or value.get('action') != 'prepare':
                    return failure('request_too_large', 413)
            except ValueError:
                return failure('request_too_large', 413)

    client = httpx.AsyncClient(follow_redirects=False, trust_env=False,
        timeout=httpx.Timeout(connect=10, read=300 if operation == "responses" else 45,
                              write=30, pool=10))
    try:
        upstream = await client.send(client.build_request(request.method, url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "text/event-stream, application/json"},
            content=bytes(body)), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return failure("gateway_unavailable", 502, True)
    except BaseException:
        await client.aclose()
        raise

    async def close():
        with anyio.CancelScope(shield=True):
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    content_type = upstream.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if (300 <= upstream.status_code < 400 or media_type not in
            {"application/json", "text/event-stream"}):
        await close()
        return failure("invalid_gateway_response", 502, True)

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            # Closing the upstream HTTP subscription propagates cancellation to
            # the owning Gateway. No second Node/auth instance exists in Brain.
            await close()

    return StreamingResponse(stream(), status_code=upstream.status_code,
        headers={"Content-Type": content_type, "Cache-Control": "no-store"},
        background=BackgroundTask(close))


def mount_gpt_subscription_proxy(app):
    app.routes.extend([
        Route("/gateway/api/gpt-subscription/connection", forward_gpt_subscription, methods=["GET", "POST"]),
        Route("/gateway/api/gpt-subscription/connections", forward_gpt_subscription, methods=["GET"]),
        Route("/gateway/api/gpt-subscription/models", forward_gpt_subscription, methods=["GET"]),
        Route("/gateway/api/gpt-subscription/responses", forward_gpt_subscription, methods=["POST"]),
        Route("/gateway/api/gpt-subscription/cancel", forward_gpt_subscription, methods=["POST"]),
        Route("/gateway/api/gpt-subscription/chat-requests", forward_gpt_subscription, methods=["POST"]),
    ])
