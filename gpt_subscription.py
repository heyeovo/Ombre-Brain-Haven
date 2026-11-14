"""GPT subscription transport only. No persona/history/runner integration here.

Credentials belong to the Node helper; Python communicates over private pipes.
Runtime request handles are deliberately disposable. Window 2 owns durable turns.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import AsyncIterator

HELPER = Path(__file__).resolve().parent / "integrations" / "gpt_subscription" / "helper.mjs"
MAX_BODY_BYTES = 4 * 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
TERMINAL = {"completed", "error"}
REAUTH_CODES = {
    "sign_in_required", "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
    "invalid_id_token", "account_mismatch",
}


class GPTSubscriptionError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, retryable: bool = False):
        super().__init__(code)  # Do not put request contents in exception strings.
        self.detail = {"code": code, "message": message, "retryable": retryable}
        self.status = status


def public_error(detail: dict) -> dict:
    # The helper already sanitizes upstream diagnostics; keep only its declared fields.
    value = {k: detail[k] for k in ("code", "message", "retryable", "status", "requestId", "param", "responseShape") if k in detail}
    code = value.get("code", "transport_error")
    value["action"] = "reconnect" if code in REAUTH_CODES else (
        "usage_settings" if code == "subscription_sharing_usage_limit_exceeded" else (
            "enable_sharing" if code == "sharing_not_enabled" else "retry" if value.get("retryable") else "inspect"
        )
    )
    return value


def validate_request(body: object) -> dict:
    if not isinstance(body, dict) or set(body) - {"request_id", "model", "input", "instructions"}:
        raise GPTSubscriptionError("invalid_request", "首版只接受文字消息、模型和人设指令。", 400)
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id):
        raise GPTSubscriptionError("invalid_request", "请提供有效的 request_id。", 400)
    model = body.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", model):
        raise GPTSubscriptionError("invalid_request", "请选择有效模型。", 400)
    messages = body.get("input")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 2000:
        raise GPTSubscriptionError("invalid_request", "input 必须为文字消息列表。", 400)
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("user", "assistant", "developer")
                or not isinstance(message["content"], str)):
            raise GPTSubscriptionError("unsupported_capability", "此线路首版不接受工具、图片或文件。", 400)
    if "instructions" in body and not isinstance(body["instructions"], str):
        raise GPTSubscriptionError("invalid_request", "instructions 必须为文字。", 400)
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_BODY_BYTES - 1024:
        raise GPTSubscriptionError("invalid_request", "请求过大，请缩短上下文。", 413)
    return {k: v for k, v in body.items() if k != "request_id"}


class GPTSubscriptionService:
    def __init__(self, buckets_dir: str, *, key_file: str | None = None, node: str | None = None):
        self.root = str(Path(buckets_dir).resolve() / "gpt-subscription")
        self.key_file = key_file if key_file is not None else os.environ.get("OMBRE_GPT_KEY_FILE", "")
        self.node = node or shutil.which("node")
        self._running: dict[tuple[str, str], asyncio.subprocess.Process | None] = {}
        self._cancelled: set[tuple[str, str]] = set()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._changing: set[str] = set()
        self._closing = False

    @staticmethod
    def scope(profile_id: str) -> str:
        if not isinstance(profile_id, str) or not profile_id:
            raise GPTSubscriptionError("invalid_profile", "缺少服务端身份。", 400)
        return hashlib.sha256(profile_id.encode()).hexdigest()

    def _check_config(self):
        if not self.key_file:
            raise GPTSubscriptionError("not_configured", "GPT 订阅授权尚未配置。")
        if not self.node or not (HELPER.parent / "vendor" / "dist" / "index.js").is_file():
            raise GPTSubscriptionError("runtime_unavailable", "GPT 授权运行环境未安装。")
        if self._closing:
            raise GPTSubscriptionError("service_stopping", "服务正在停止，请稍后重试。", retryable=True)

    async def _spawn(self, profile_id: str, command: dict):
        self._check_config()
        # No shell, credentials in argv, inherited OpenAI keys, NODE_OPTIONS or debug logs.
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG") if k in os.environ}
        env.update(HAVEN_GPT_ROOT=self.root, HAVEN_GPT_SCOPE=self.scope(profile_id), OMBRE_GPT_KEY_FILE=self.key_file)
        try:
            creation = asyncio.create_task(asyncio.create_subprocess_exec(
                self.node, str(HELPER), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=env, limit=2 * 1024 * 1024,
            ))
            try:
                process = await asyncio.shield(creation)
            except asyncio.CancelledError:
                process = await creation
                await self._stop(process)
                raise
        except OSError:
            raise GPTSubscriptionError("runtime_unavailable", "GPT 授权运行环境无法启动。") from None
        self._processes.add(process)
        try:
            process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()
        except (OSError, ConnectionError):
            await self._stop(process)
            raise GPTSubscriptionError("transport_error", "GPT 授权进程无法接收请求。", retryable=True) from None
        except asyncio.CancelledError:
            await self._stop(process)
            raise
        return process

    @staticmethod
    async def _signal_cancel(process):
        if process.returncode is None and process.stdin and not process.stdin.is_closing():
            try:
                process.stdin.write(b'{"cancel":true}\n')
                await process.stdin.drain()
            except (OSError, ConnectionError):
                pass

    async def _stop(self, process):
        # Never immediately kill a rotating refresh grant. The helper finishes and fsyncs it.
        await self._signal_cancel(process)
        try:
            # Drain discarded output so an interrupted SSE client cannot leave
            # the helper blocked flushing its private stdout pipe.
            await asyncio.wait_for(asyncio.gather(process.wait(), process.stdout.read()), timeout=100)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        finally:
            if process.stdin:
                process.stdin.close()
            self._processes.discard(process)

    async def _read(self, process) -> dict:
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=300)
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("type") not in {"result", "delta", *TERMINAL}:
                raise ValueError()
            if event["type"] == "error":
                event["error"] = public_error(event["error"])
            return event
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError):
            raise GPTSubscriptionError("transport_interrupted", "GPT 授权进程中断；请检查连接状态后重试。", retryable=True) from None

    async def call(self, profile_id: str, op: str, **params) -> dict:
        if op not in {"status", "models", "profiles", "select", "disconnect"}:
            raise GPTSubscriptionError("invalid_request", "不支持的授权操作。", 400)
        if op in {"select", "disconnect"}:
            if profile_id in self._changing:
                raise GPTSubscriptionError("connection_busy", "授权正在变更，请稍后重试。", 409, True)
            self._changing.add(profile_id)
        process = None
        try:
            if op in {"select", "disconnect"}:
                for profile, request_id in list(self._running):
                    if profile == profile_id:
                        await self.cancel(profile, request_id)
            process = await self._spawn(profile_id, {"op": op, **params})
            event = await self._read(process)
            if event["type"] == "error":
                return {"ok": False, "error": event["error"]}
            if event["type"] != "result":
                raise GPTSubscriptionError("transport_error", "GPT 授权进程响应无效。")
            data = event["data"]
            if isinstance(data.get("error"), dict):
                data["error"] = public_error(data["error"])
            return {"ok": True, **data}
        finally:
            if process:
                await self._stop(process)
            if op in {"select", "disconnect"}:
                self._changing.discard(profile_id)

    def reserve(self, profile_id: str, request_id: str):
        self._check_config()
        if profile_id in self._changing:
            raise GPTSubscriptionError("connection_busy", "授权正在变更，请稍后重试。", 409, True)
        key = (profile_id, request_id)
        if key in self._running:
            raise GPTSubscriptionError("request_in_progress", "同一请求正在执行。", 409)
        self._running[key] = None

    async def stream(self, profile_id: str, request_id: str, request: dict) -> AsyncIterator[dict]:
        key = (profile_id, request_id)
        process = None
        terminal = False
        try:
            if key not in self._running:
                raise GPTSubscriptionError("request_not_reserved", "请求未登记。", 409)
            if key in self._cancelled:
                yield {"type": "error", "request_id": request_id, "error": public_error({"code": "cancelled", "message": "已停止回复。", "retryable": False})}
                return
            process = await self._spawn(profile_id, {"op": "stream", "request": request})
            self._running[key] = process
            if key in self._cancelled:
                await self._signal_cancel(process)
            while not terminal:
                event = await self._read(process)
                if event["type"] not in {"delta", *TERMINAL}:
                    raise GPTSubscriptionError("transport_error", "GPT 回复事件无效。")
                terminal = event["type"] in TERMINAL
                if key in self._cancelled and event["type"] != "error":
                    if terminal:
                        event = {"type": "error", "error": public_error({"code": "cancelled", "message": "已停止回复。", "retryable": False})}
                    else:
                        continue
                yield {**event, "request_id": request_id}
        except GPTSubscriptionError as exc:
            yield {"type": "error", "request_id": request_id, "error": public_error(exc.detail)}
        finally:
            if process:
                await self._stop(process)
            self._running.pop(key, None)
            self._cancelled.discard(key)

    async def cancel(self, profile_id: str, request_id: str) -> dict:
        key = (profile_id, request_id)
        if key not in self._running:
            return {"ok": True, "cancel_requested": False, "request_id": request_id}
        self._cancelled.add(key)
        process = self._running[key]
        if process:
            await self._signal_cancel(process)
        return {"ok": True, "cancel_requested": True, "request_id": request_id}

    async def close(self):
        self._closing = True
        await asyncio.gather(*(self.cancel(*key) for key in list(self._running)))
        processes = list(self._processes)
        await asyncio.gather(*(self._signal_cancel(process) for process in processes))
        # Active call/stream tasks own their readers and normal cleanup. Do not
        # race their stdout reads during shutdown.
        try:
            await asyncio.wait_for(asyncio.gather(*(process.wait() for process in processes)), 105)
        except asyncio.TimeoutError:
            for process in processes:
                if process.returncode is None:
                    process.kill()
            await asyncio.gather(*(process.wait() for process in processes))
