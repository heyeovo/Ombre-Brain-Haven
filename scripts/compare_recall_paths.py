#!/usr/bin/env python3
"""对比两条记忆召回路径的注入结果。

背景：hook 路（/api/hook/recall → _hook_recall_fast_cards）和请求链路
（/v1/messages → prepare_payload）是两套实现，但共用同一道
domain_sentinel 门控。这个脚本用同一批句子分别打两条路，输出对比表，
用来回答两个问题：
  1. 该召回的有没有召回（门控是否又把消息静默吞掉）
  2. 两条路召回的内容差多少（决定 claude code 前端能不能只靠 hook）

用法：
    export OMBRE_GATEWAY_TOKEN=<网关密码>
    python scripts/compare_recall_paths.py
    python scripts/compare_recall_paths.py --skip-messages   # 只测 hook，不花上游钱
    python scripts/compare_recall_paths.py --verbose         # 打印注入原文
    python scripts/compare_recall_paths.py --case "你还记得那次吗"

/v1/messages 那条会真的请求上游模型（max_tokens 压到最小），所以默认
每个用例只跑一次。加 --skip-messages 可以完全不产生上游费用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = os.environ.get("OMBRE_BASE_URL", "https://foryan.zeabur.app").rstrip("/")

# (分类, 句子, 期望召回)
# 期望召回 True = 这句话应该带回记忆；False = 对照组，不召回才是对的
CASES: list[tuple[str, str, bool]] = [
    ("模糊时间指向", "你还记得那次吗", True),
    ("模糊时间指向", "上次我们聊到的那个事情后来怎么样了", True),
    ("模糊时间指向", "我想起之前有一段时间我状态特别差", True),
    ("明确日期", "7月20号那天我们说了什么", True),
    ("明确日期", "昨天我提到的那件事", True),
    ("显式记忆需求", "帮我回忆一下我们关于记忆系统的讨论", True),
    ("关系类", "我最近觉得我们之间有点不一样了", True),
    ("闲聊对照组", "今天天气不错", False),
    ("闲聊对照组", "嗯", False),
]

# bucket_id 用例单独一组：只有带前缀的写法能被 EXPLICIT_BUCKET_ID_RE
# （gateway.py:225）认出来。裸 id（12 位 hex，数字开头）既不匹配那个正则，
# 也不匹配 _domain_sentinel_query_explicitly_needs_memory 里
# "字母开头带数字"的兜底（gateway.py:12908）。这一组就是为了证实这一点。
BUCKET_ID_CASES_TEMPLATE: list[tuple[str, str, bool]] = [
    ("bucket_id·裸写", "{bid} 这条记忆讲的是什么", True),
    ("bucket_id·带前缀", "bucket_id:{bid} 这条记忆讲的是什么", True),
    ("bucket_id·方括号", "[bucket_id:{bid}] 这条记忆讲的是什么", True),
]


def build_cases(bucket_id: str | None, only: str | None) -> list[tuple[str, str, bool]]:
    if only:
        return [("自定义", only, True)]
    cases = list(CASES)
    if bucket_id:
        cases += [
            (label, text.format(bid=bucket_id), expect)
            for label, text, expect in BUCKET_ID_CASES_TEMPLATE
        ]
    return cases


def pick_sample_bucket_id() -> str | None:
    """从本地 buckets/ 里挑一个真实 id，避免用不存在的 id 测出假阴性。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buckets")
    for sub in ("dynamic", "permanent", "feel", "archive"):
        folder = os.path.join(root, sub)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".md"):
                return name[:-3]
    return None


def post_json(url: str, token: str, body: dict, timeout: float = 90.0) -> tuple[int, dict | str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "X-Ombre-Session-Id": body.pop("_session_id", "recall-compare"),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # 网络层失败
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def get_json(url: str, token: str, timeout: float = 30.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def probe_hook(base_url: str, token: str, query: str, session_id: str) -> dict:
    """打 /api/hook/recall，返回 {ok, chars, cards, domains, error}。"""
    started = time.time()
    status, payload = post_json(
        f"{base_url}/api/hook/recall",
        token,
        {
            "query": query,
            "session_id": session_id,
            "include_debug": "1",
            "_session_id": session_id,
        },
    )
    elapsed = time.time() - started
    if status != 200 or not isinstance(payload, dict):
        return {
            "ok": False,
            "error": f"HTTP {status}: {str(payload)[:200]}",
            "chars": 0,
            "cards": 0,
            "elapsed": elapsed,
            "context": "",
            "domains": [],
        }
    context = str(payload.get("additional_context") or "")
    # include_debug=1 时，handle_hook_recall 把完整 debug 合进 response["debug"]
    # （gateway.py:2460），domain_sentinel_debug 在那里面
    debug = payload.get("debug") or {}
    sentinel = debug.get("domain_sentinel_debug") or {}
    return {
        "ok": True,
        "error": "",
        "chars": len(context),
        "cards": len(payload.get("cards") or []),
        "elapsed": elapsed,
        "context": context,
        "domains": list(debug.get("domains") or []),
        "recalled_ids": list(payload.get("recalled_ids") or []),
        "sentinel_reason": str(sentinel.get("reason") or ""),
        "search_query": str(debug.get("query") or ""),
    }


# /api/debug/injections 里跟召回相关的字段（不同版本字段名可能不同，
# 存在就取，不存在跳过）
INJECTION_RECALL_KEYS = (
    "recalled_memory",
    "diffused_memory",
    "date_recall",
    "dynamic_context",
    "just_now_context",
)


def _flatten_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    return str(value)


def probe_messages(base_url: str, token: str, query: str, session_id: str, model: str | None) -> dict:
    """打 /v1/messages 走完整请求链，再从 /api/debug/injections 读回这一轮注入了什么。

    max_tokens 压到 1，上游只回一个 token，费用可以忽略。
    """
    started = time.time()
    body = {
        "model": model or "claude-sonnet-4-20250514",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": query}],
        "_session_id": session_id,
    }
    status, payload = post_json(f"{base_url}/v1/messages", token, body, timeout=180.0)
    elapsed = time.time() - started
    if status != 200:
        return {
            "ok": False,
            "error": f"HTTP {status}: {str(payload)[:200]}",
            "chars": 0,
            "elapsed": elapsed,
            "context": "",
            "parts": {},
        }

    dbg_status, dbg = get_json(
        f"{base_url}/api/debug/injections?session_id={urllib.parse.quote(session_id)}&limit=1",
        token,
    )
    if dbg_status != 200 or not isinstance(dbg, dict):
        return {
            "ok": False,
            "error": f"injections debug HTTP {dbg_status}: {str(dbg)[:160]}",
            "chars": 0,
            "elapsed": elapsed,
            "context": "",
            "parts": {},
        }
    items = dbg.get("items") or []
    if not items:
        return {
            "ok": True,
            "error": "no injection record",
            "chars": 0,
            "elapsed": elapsed,
            "context": "",
            "parts": {},
        }
    item = items[0] if isinstance(items[0], dict) else {}
    parts: dict[str, int] = {}
    chunks: list[str] = []
    for key in INJECTION_RECALL_KEYS:
        text = _flatten_text(item.get(key)).strip()
        if text:
            parts[key] = len(text)
            chunks.append(f"--- {key} ---\n{text}")
    return {
        "ok": True,
        "error": "",
        "chars": sum(parts.values()),
        "elapsed": elapsed,
        "context": "\n".join(chunks),
        "parts": parts,
    }


def verdict(expect_recall: bool, hook_chars: int) -> str:
    if expect_recall and hook_chars > 0:
        return "OK"
    if expect_recall and hook_chars == 0:
        return "MISS <<<"     # 该召回却是空的 —— 门控又吞了
    if not expect_recall and hook_chars == 0:
        return "OK(静默)"
    return "多余"              # 对照组反而召回了


def main() -> int:
    # Windows 终端默认 GBK，中文用例会变乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="对比 hook 召回与请求链召回")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", default=os.environ.get("OMBRE_GATEWAY_TOKEN", ""))
    parser.add_argument("--model", default=os.environ.get("OMBRE_TEST_MODEL", ""))
    parser.add_argument("--skip-messages", action="store_true", help="只测 hook，不打上游")
    parser.add_argument("--verbose", action="store_true", help="打印注入原文")
    parser.add_argument("--case", default="", help="只测这一句")
    parser.add_argument("--bucket-id", default="", help="bucket_id 用例用的 id，默认从本地 buckets/ 挑一个")
    parser.add_argument("--session-prefix", default="recall-compare")
    args = parser.parse_args()

    if not args.token:
        print("缺少网关密码。先设置环境变量：export OMBRE_GATEWAY_TOKEN=<密码>", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    bucket_id = args.bucket_id or pick_sample_bucket_id()
    cases = build_cases(bucket_id, args.case or None)

    print(f"网关: {base_url}")
    print(f"用例: {len(cases)} 条" + (f"（bucket_id 样本 {bucket_id}）" if bucket_id else ""))
    print(f"请求链路: {'跳过' if args.skip_messages else '开启（max_tokens=1）'}")
    print()

    header = f"{'分类':<16} {'期望':<5} {'hook字数':>9} {'链路字数':>9}  {'判定':<10} 句子"
    print(header)
    print("-" * len(header))

    rows = []
    for index, (label, text, expect) in enumerate(cases):
        # 每个用例用独立 session，避免 Haven 的去重/冷却互相干扰
        session_id = f"{args.session_prefix}-{index}-{int(time.time())}"
        hook = probe_hook(base_url, args.token, text, session_id)
        chain = (
            {"ok": True, "chars": -1, "context": "", "error": "skipped", "parts": {}}
            if args.skip_messages
            else probe_messages(base_url, args.token, text, session_id + "-chain", args.model or None)
        )
        mark = verdict(expect, hook["chars"]) if hook["ok"] else "ERR"
        chain_display = "-" if chain["chars"] < 0 else str(chain["chars"])
        print(
            f"{label:<16} {'要':<5} " if expect else f"{label:<16} {'不要':<4} ",
            end="",
        )
        print(f"{hook['chars']:>9} {chain_display:>9}  {mark:<10} {text[:34]}")
        if hook.get("error"):
            print(f"{'':<16} hook 错误: {hook['error']}")
        if chain.get("error") and chain["error"] != "skipped":
            print(f"{'':<16} 链路错误: {chain['error']}")
        rows.append((label, text, expect, hook, chain))

    print()
    misses = [r for r in rows if r[2] and r[3]["ok"] and r[3]["chars"] == 0]
    extras = [r for r in rows if not r[2] and r[3]["ok"] and r[3]["chars"] > 0]
    print(f"该召回没召回: {len(misses)} 条" + ("  <<< 门控还有问题" if misses else "  ✓"))
    for _, text, _, hook, _ in misses:
        print(f"  · {text}   sentinel_reason={hook.get('sentinel_reason') or '(空)'} "
              f"domains={hook.get('domains')}")
    if extras:
        print(f"对照组多余召回: {len(extras)} 条（不一定是问题，看内容是否离题）")
        for _, text, _, _, _ in extras:
            print(f"  · {text}")

    if not args.skip_messages:
        both = [r for r in rows if r[3]["ok"] and r[4]["ok"] and r[4]["chars"] >= 0]
        if both:
            print()
            print("两条路的规模对比（字数，不代表内容相同）:")
            for label, text, _, hook, chain in both:
                ratio = "-" if chain["chars"] == 0 else f"{hook['chars'] / chain['chars']:.2f}x"
                print(f"  {label:<16} hook={hook['chars']:>6}  链路={chain['chars']:>6}  比值={ratio}  {text[:26]}")

    if args.verbose:
        print()
        print("=" * 70)
        for label, text, _, hook, chain in rows:
            print(f"\n### [{label}] {text}")
            print(f"\n--- hook ({hook['chars']} 字) ---")
            print(hook["context"] or "(空)")
            if chain["chars"] >= 0:
                print(f"\n--- 请求链路 ({chain['chars']} 字, 分布 {chain['parts']}) ---")
                print(chain["context"] or "(空)")
            print("-" * 70)

    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
