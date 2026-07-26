#!/usr/bin/env python3
"""打一次 /v1/messages，再读回 /api/debug/injections，列出这条记录真实有哪些字段。

用途：compare_recall_paths.py 里 INJECTION_RECALL_KEYS 那 5 个字段名是猜的
（脚本注释自己写了"不同版本字段名可能不同"）。链路那一列全是 0、连 bucket_id
用例也是 0，怀疑就是字段名对不上。这个脚本用来确认真实字段名。

用法：
    $env:OMBRE_GATEWAY_TOKEN="<网关密码>"
    python scripts/probe_injection_keys.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("OMBRE_PROBE_BASE_URL", "https://foryan.zeabur.app/gateway").rstrip("/")
MODEL = os.environ.get("OMBRE_TEST_MODEL", "[Kiro] claude-opus-4-6-thinking [不补]")
# bucket_id 用例是最硬的：hook 那条路能出 1573 字，所以链路这边必须有东西。
QUERY = os.environ.get("OMBRE_PROBE_QUERY", "bucket_id:020598dd8df8 这条记忆讲的是什么")


def call(url: str, token: str, body: dict | None, timeout: float,
         session_id: str = "") -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        # /v1/messages 的 session id 只从这个请求头读（gateway.py:2113），
        # body 里的 _session_id 网关压根不看。漏了它请求就都跑在默认
        # session "main" 上，按自定义 session_id 查 injections 必然 0 条。
        headers["X-Ombre-Session-Id"] = session_id
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def describe(value: object) -> str:
    if isinstance(value, str):
        preview = value.replace("\n", "⏎")[:60]
        return f"str 长度={len(value)}  {preview!r}"
    if isinstance(value, list):
        return f"list {len(value)} 项"
    if isinstance(value, dict):
        return f"dict 键={list(value.keys())[:8]}"
    return f"{type(value).__name__} = {value!r}"


def main() -> int:
    token = os.environ.get("OMBRE_GATEWAY_TOKEN", "")
    if not token:
        print('缺少网关密码。先执行： $env:OMBRE_GATEWAY_TOKEN="<密码>"', file=sys.stderr)
        return 2

    session_id = f"probe-keys-{int(time.time())}"
    print(f"网关: {BASE}")
    print(f"模型: {MODEL}")
    print(f"句子: {QUERY}")
    print(f"会话: {session_id}")
    print()

    print("1/2 打 /v1/messages（max_tokens=1）...")
    status, payload = call(
        f"{BASE}/v1/messages",
        token,
        {
            "model": MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": QUERY}],
            "_session_id": session_id,
        },
        180.0,
        session_id=session_id,
    )
    if status != 200:
        print(f"  失败 HTTP {status}: {str(payload)[:400]}")
        return 1
    print("  OK")

    print("2/2 读 /api/debug/injections ...")
    url = f"{BASE}/api/debug/injections?session_id={urllib.parse.quote(session_id)}&limit=1"
    status, dbg = call(url, token, None, 30.0)
    if status != 200 or not isinstance(dbg, dict):
        print(f"  失败 HTTP {status}: {str(dbg)[:400]}")
        return 1

    print(f"  顶层键: {list(dbg.keys())}")
    items = dbg.get("items") or []
    print(f"  记录数: {len(items)}")
    if not items:
        print()
        print("没有注入记录。可能是 session_id 没对上，或者这个接口按别的参数名过滤。")
        print("完整响应（前 1500 字）：")
        print(json.dumps(dbg, ensure_ascii=False, indent=2)[:1500])
        return 1

    item = items[0] if isinstance(items[0], dict) else {}
    print()
    print("=== 记录顶层字段 ===")
    for key in sorted(item.keys()):
        print(f"  {key:<32} {describe(item[key])}")

    # 记忆内容不在顶层，在 payload 里（顶层只有 created_at/id/payload/round_id/session_id）。
    payload_obj = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    print()
    print("=== payload 字段 ===")
    for key in sorted(payload_obj.keys()):
        print(f"  {key:<32} {describe(payload_obj[key])}")

    detail = payload_obj.get("debug_detail")
    if isinstance(detail, dict):
        print()
        print("=== payload.debug_detail 字段 ===")
        for key in sorted(detail.keys()):
            print(f"  {key:<32} {describe(detail[key])}")

    print()
    print("=== 哪些字段装着长文本（>80 字，注入内容的候选）===")

    def walk(node: object, path: str, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, str):
            if len(node) > 80:
                print(f"  {path:<52} 长度={len(node)}")
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k), depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node[:5]):
                walk(v, f"{path}[{i}]", depth + 1)

    walk(item, "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
