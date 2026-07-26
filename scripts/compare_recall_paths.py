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
    python scripts/compare_recall_paths.py --no-semantic     # hook 不开语义（复现默认行为）
    python scripts/compare_recall_paths.py --verbose         # 打印注入原文
    python scripts/compare_recall_paths.py --case "你还记得那次吗"

/v1/messages 那条会真的请求上游模型（max_tokens 压到最小），所以默认
每个用例只跑一次。加 --skip-messages 可以完全不产生上游费用。

⚠️ 看结果前必须知道这四个坑（都踩过，2026-07-26）：
  1. hook **现在有 date_recall 了**（2026-07-26 接上，见 _hook_recall_fast_cards
     开头）。所以「昨天/前天」这类相对日期用例在 --skip-messages 下也能验，
     不用再去掉它看链路那一列。但注意 date_recall **不产卡** —— 它是
     additional_context 里独立的 [date_recall] 段，本脚本按 date_recall_chars
     补算 1 条，否则会误报 MISS。
  2. hook 默认 allow_semantic=false，只做关键词匹配。本脚本默认帮你开着
     （单次 4-6s），--no-semantic 可以关掉复现默认行为。
  3. **判定看条数，不看字数。** dynamic_context 是注入总壳，一条记忆都没
     召回时它也有 208 字固定框架文字。拿字数判会让「嗯」显示 208 字、
     判成"多余召回"。所以字数只作参考，判定用 id 列表条数。
  4. **新 session 第一轮走换窗交接**（skip_reason=session_start_handoff），
     压根不做召回。所以链路每条用例先发一句「在吗」热身，第二轮才是真实
     场景。--no-warmup 可以关掉，但结果不可用。

/api/debug/injections 的字段名和层级可以用 scripts/probe_injection_keys.py
随时重新确认 —— 注入内容在 item["payload"] 里，不在顶层。
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
    # 相对日期组：跟上面那条绝对日期是同一种需求。2026-07-26 之前这三条
    # 全被 vague 闸吞掉（「昨天」不算 locatable term），已在 recall_policy.py
    # 加 _query_has_relative_date_recall_hint 放行。
    ("相对日期", "昨天我提到的那件事", True),
    ("相对日期", "昨天我们聊了什么", True),
    ("相对日期", "前天你说的那个事", True),
    ("显式记忆需求", "帮我回忆一下我们关于记忆系统的讨论", True),
    ("关系类", "我最近觉得我们之间有点不一样了", True),
    ("闲聊对照组", "今天天气不错", False),
    ("闲聊对照组", "嗯", False),
    # 相对日期的反面对照：有日期词但没有回忆动作，必须继续静默。
    # 放行条件写成「日期 + 回忆动作」就是为了守住这两条。
    ("闲聊对照组", "今晚吃什么", False),
    ("闲聊对照组", "昨晚睡得怎么样", False),
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


def build_cases(
    bucket_id: str | None,
    only: str | None,
    only_group: str | None = None,
) -> list[tuple[str, str, bool]]:
    if only:
        return [("自定义", only, True)]
    cases = list(CASES)
    if bucket_id:
        cases += [
            (label, text.format(bid=bucket_id), expect)
            for label, text, expect in BUCKET_ID_CASES_TEMPLATE
        ]
    # --only-group 按分类名子串筛，只改一处判定时不用把 16 条全跑一遍
    if only_group:
        needle = only_group.strip()
        cases = [c for c in cases if needle in c[0]]
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


def probe_hook(
    base_url: str,
    token: str,
    query: str,
    session_id: str,
    *,
    semantic: bool = True,
) -> dict:
    """打 /api/hook/recall，返回 {ok, chars, cards, domains, error}。

    semantic=True 时显式打开 allow_semantic / allow_rerank / allow_query_planner。
    hook 默认这三个都是关的，即只做关键词匹配、不跑向量检索 —— 不传的话
    「帮我回忆一下…记忆系统…」这类语义命中的桶会全部显示 0 张卡，看起来像
    门控在拦，实际是没开检索。单次耗时会从 <1s 涨到 4-6s。
    """
    started = time.time()
    body = {
        "query": query,
        "session_id": session_id,
        "include_debug": "1",
        "_session_id": session_id,
    }
    if semantic:
        body.update(
            allow_semantic="1",
            allow_rerank="1",
            allow_query_planner="1",
        )
    status, payload = post_json(f"{base_url}/api/hook/recall", token, body)
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
    # 注意：规则模式下 domain_sentinel_debug 里没有 reason 这个键（gateway.py:12884
    # 只在显式记忆需求 / LLM 哨兵分支才写），所以 sentinel_reason 为空是常态，
    # 不代表被门控拦了。真正的跳过理由在 query_planner_debug.skip_reason。
    planner = debug.get("query_planner_debug") or {}
    hook_debug = debug.get("hook_recall_debug") or {}
    # date_recall（2026-07-26 接到 hook 路径上）捞的是当天原始对话，**不产卡** ——
    # 它在 additional_context 里是独立的 [date_recall] 段，不走 memory_card。
    # 所以只数 cards 会把已经注入 800+ 字对话原文的「昨天我们聊了什么」判成 MISS。
    # date_recall_chars 是纯正文长度，不含 [Ombre Gateway Hook Recall] 那三行框架，
    # 可以放心当命中信号用（框架文字污染的是 chars，不是这个字段）。
    date_dbg = debug.get("date_recall_debug") or {}
    date_chars = int(payload.get("date_recall_chars") or 0)
    date_cards = 1 if date_chars > 0 else 0
    return {
        "ok": True,
        "error": "",
        "chars": len(context),
        "cards": len(payload.get("cards") or []) + date_cards,
        "bucket_cards": len(payload.get("cards") or []),
        "date_chars": date_chars,
        "date_status": str(date_dbg.get("status") or ""),
        "date_skip": str(date_dbg.get("skip_reason") or ""),
        "date_label": str(date_dbg.get("label") or ""),
        "elapsed": elapsed,
        "context": context,
        "domains": list(debug.get("domains") or []),
        "recalled_ids": list(payload.get("recalled_ids") or []),
        "sentinel_reason": str(sentinel.get("reason") or ""),
        "skip_reason": str(planner.get("skip_reason") or hook_debug.get("skip_reason") or ""),
        "search_query": str(debug.get("query") or ""),
    }


# /api/debug/injections 里跟召回相关的字段（不同版本字段名可能不同，
# 存在就取，不存在跳过）
# ⚠️ 这些字段在 item["payload"] 里，不在 item 顶层。
# 顶层只有 created_at / id / payload / round_id / session_id —— 之前直接
# item.get(key) 取，链路那一列必然全是 0，看起来像"链路完全不召回"。
# 用 scripts/probe_injection_keys.py 可以随时重新确认真实字段名。
#
# ⚠️⚠️ 刻意**不含** dynamic_context。那个字段是注入总壳，里面有 200 多字
# 固定框架说明（"Live private context for the current turn..."），一条记忆
# 都没召回时它也有 208 字。拿它算字数会让「嗯」「今天天气不错」全部显示
# 200+ 字、判成"多余召回"，全是假数。
INJECTION_RECALL_KEYS = (
    "recalled_memory",
    "diffused_memory",
    "date_recall",
    "just_now_context",
    # bucket_id 精确取记忆的内容落在这个字段，不在上面那几个里
    "targeted_memory_detail",
)

# 判"有没有召回"用 id 列表条数，比字数可靠：字数会被框架文字污染，
# id 列表是空就是空。
INJECTION_ID_KEYS = (
    "injected_bucket_ids",
    "recalled_bucket_ids",
    "date_recall_bucket_ids",
    "diffused_bucket_ids",
    "recalled_moment_ids",
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
            "cards": 0,
            "ids": {},
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
            "cards": 0,
            "ids": {},
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
            "cards": 0,
            "ids": {},
        }
    item = items[0] if isinstance(items[0], dict) else {}
    # 注入内容在 payload 子字典里，不在顶层
    source = item.get("payload") if isinstance(item.get("payload"), dict) else item
    parts: dict[str, int] = {}
    chunks: list[str] = []
    for key in INJECTION_RECALL_KEYS:
        text = _flatten_text(source.get(key)).strip()
        if text:
            parts[key] = len(text)
            chunks.append(f"--- {key} ---\n{text}")
    ids: dict[str, int] = {}
    for key in INJECTION_ID_KEYS:
        value = source.get(key)
        if isinstance(value, list) and value:
            ids[key] = len(value)
    # date_recall 捞的是当天的原始对话，不是记忆桶，压根没有 bucket id 可数。
    # 上一轮就因为只数 id，把已经注入 643 字的「昨天我们聊了什么」判成了 MISS。
    # 有正文就按一条算。
    if parts.get("date_recall") and not ids.get("date_recall_bucket_ids"):
        ids["date_recall(原始对话)"] = 1
    planner = source.get("query_planner_debug") or {}
    date_dbg = source.get("date_recall_debug") or {}
    return {
        "ok": True,
        "error": "",
        "chars": sum(parts.values()),
        "cards": sum(ids.values()),
        "ids": ids,
        "elapsed": elapsed,
        "context": "\n".join(chunks),
        "parts": parts,
        "skip_reason": str((planner or {}).get("skip_reason") or ""),
        # date_recall 是按日期捞当天原始对话的子系统，只在链路这条路上有。
        # 相对日期用例要看它的 status/skip_reason 才知道有没有真正触发。
        "date_status": str((date_dbg or {}).get("status") or ""),
        "date_skip": str((date_dbg or {}).get("skip_reason") or ""),
        "date_label": str((date_dbg or {}).get("label") or ""),
    }


def verdict(expect_recall: bool, hook_cards: int, chain_cards: int = -1) -> str:
    """判定看**召回条数**，不看字数。

    字数会被注入框架文字污染（dynamic_context 空手也有 208 字），
    条数是空就是空。chain_cards < 0 表示链路被跳过（--skip-messages）。

    任一条路召回到就算成功：hook 能力比链路少（没有 date_recall），
    只看 hook 会把"链路其实召回了"误报成 MISS。
    """
    got = hook_cards > 0 or chain_cards > 0
    if expect_recall:
        if got:
            return "OK" if hook_cards > 0 else "OK(仅链路)"
        return "MISS <<<"
    if not got:
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
    parser.add_argument(
        "--no-semantic",
        dest="semantic",
        action="store_false",
        help="hook 不开语义检索（复现 hook 的默认行为，只做关键词匹配）",
    )
    parser.set_defaults(semantic=True)
    parser.add_argument(
        "--no-warmup",
        dest="warmup",
        action="store_false",
        help="链路不发热身轮（会测在 session_start_handoff 交接轮上，结果不可用）",
    )
    parser.set_defaults(warmup=True)
    parser.add_argument("--verbose", action="store_true", help="打印注入原文")
    parser.add_argument("--case", default="", help="只测这一句")
    parser.add_argument(
        "--only-group", default="",
        help="只测分类名含此字串的用例，如 --only-group 相对日期",
    )
    parser.add_argument("--bucket-id", default="", help="bucket_id 用例用的 id，默认从本地 buckets/ 挑一个")
    parser.add_argument("--session-prefix", default="recall-compare")
    args = parser.parse_args()

    if not args.token:
        print("缺少网关密码。先设置环境变量：export OMBRE_GATEWAY_TOKEN=<密码>", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    bucket_id = args.bucket_id or pick_sample_bucket_id()
    cases = build_cases(bucket_id, args.case or None, args.only_group or None)
    if not cases:
        print(f"--only-group {args.only_group!r} 没匹配到任何用例", file=sys.stderr)
        return 2

    print(f"网关: {base_url}")
    print(f"用例: {len(cases)} 条" + (f"（bucket_id 样本 {bucket_id}）" if bucket_id else ""))
    print(f"请求链路: {'跳过' if args.skip_messages else '开启（max_tokens=1）'}")
    print(f"hook 语义检索: {'开' if args.semantic else '关（只关键词）'}")
    if not args.skip_messages:
        print(f"链路热身轮: {'开（每条用例先发一句「在吗」）' if args.warmup else '关'}")
    print("判定看**召回条数**，不看字数（字数含注入框架文字，空手也有 200+ 字）")
    if args.skip_messages:
        print("ℹ️  只测 hook。hook 自 2026-07-26 起也有 date_recall（按日期捞当天原始对话），")
        print("   所以「昨天/前天」这类相对日期用例这一列就能验，不用去掉 --skip-messages。")
        print("   date_recall 不产卡，本脚本按 date_recall_chars 补算 1 条（hook条数列的 +日 标记）。")
    print()

    header = f"{'分类':<16} {'期望':<5} {'hook条数':>9} {'链路条/字':>11}  {'判定':<10} 句子"
    print(header)
    print("-" * len(header))

    rows = []
    for index, (label, text, expect) in enumerate(cases):
        # 每个用例用独立 session，避免 Haven 的去重/冷却互相干扰
        session_id = f"{args.session_prefix}-{index}-{int(time.time())}"
        hook = probe_hook(base_url, args.token, text, session_id, semantic=args.semantic)
        if args.skip_messages:
            chain = {"ok": True, "chars": -1, "cards": -1, "ids": {},
                     "context": "", "error": "skipped", "parts": {}}
        else:
            chain_session = session_id + "-chain"
            # 新 session 的第一轮走「换窗交接」分支（skip_reason=session_start_handoff），
            # 压根不做召回。先发一句无关的把这一轮用掉，第二轮才是日常聊天的
            # 真实召回场景。不热身的话每条用例都测在交接轮上，全是假数。
            if args.warmup:
                probe_messages(base_url, args.token, "在吗", chain_session, args.model or None)
            chain = probe_messages(base_url, args.token, text, chain_session, args.model or None)
        mark = verdict(expect, hook["cards"], chain["cards"]) if hook["ok"] else "ERR"
        chain_display = "-" if chain["cards"] < 0 else f"{chain['cards']}/{chain['chars']}"
        print(
            f"{label:<16} {'要':<5} " if expect else f"{label:<16} {'不要':<4} ",
            end="",
        )
        # 「+日」= 这一条里有 date_recall 段（它不产卡，条数是脚本补算的），
        # 免得看到 1/885 以为是桶检索出的卡
        hook_display = f"{hook['cards']}/{hook['chars']}"
        if hook.get("date_chars"):
            hook_display += "+日"
        print(f"{hook_display:>9} {chain_display:>11}  {mark:<10} {text[:34]}")
        if hook.get("error"):
            print(f"{'':<16} hook 错误: {hook['error']}")
        if chain.get("error") and chain["error"] != "skipped":
            print(f"{'':<16} 链路错误: {chain['error']}")
        rows.append((label, text, expect, hook, chain))

    print()
    # 两条路都空才算真 MISS。保留"任一条路有内容就不算漏"这个口径：
    # 两条路的 date_recall 触发条件相同，但桶检索那一侧仍有差别（默认开关、
    # 替换 vs 并存），只看 hook 还是可能把"链路其实召回了"误报成漏召回。
    def both_empty(hook: dict, chain: dict) -> bool:
        if not hook["ok"] or hook["cards"] != 0:
            return False
        return chain["cards"] <= 0
    misses = [r for r in rows if r[2] and both_empty(r[3], r[4])]
    extras = [
        r for r in rows
        if not r[2] and r[3]["ok"] and (r[3]["cards"] > 0 or r[4]["cards"] > 0)
    ]
    print(f"该召回没召回: {len(misses)} 条（两条路都空）")
    for label, text, _, hook, chain in misses:
        print(f"  · {text}   skip_reason={hook.get('skip_reason') or '(无)'} "
              f"sentinel_reason={hook.get('sentinel_reason') or '(空·规则模式下正常)'} "
              f"domains={hook.get('domains')}")
        # 相对日期用例返空时，date_recall_debug 才说得清是没触发还是那天没数据
        if hook.get("date_status") or hook.get("date_skip"):
            print(f"      hook date_recall: status={hook.get('date_status') or '-'} "
                  f"skip={hook.get('date_skip') or '-'} "
                  f"label={hook.get('date_label') or '-'}")
        if not args.skip_messages:
            print(f"      链路 skip_reason={chain.get('skip_reason') or '(无)'} "
                  f"date_recall: status={chain.get('date_status') or '-'} "
                  f"skip={chain.get('date_skip') or '-'} "
                  f"label={chain.get('date_label') or '-'}")
    if misses:
        # skip_reason 是唯一能区分这两种情况的字段：
        #   有值 = 闸拦下了，属于门控问题
        #   (无) = 闸放行了，是检索捞不到，属于检索/覆盖面问题
        # ⚠️ 必须两条路都看。上一轮只看 hook 侧，印出「被闸拦下 0 条」，
        # 而链路侧明明有 low_signal_auto_recall / auto_vague_query，就打印在上面一行。
        gated = [
            r for r in misses
            if r[3].get("skip_reason") or r[4].get("skip_reason")
        ]
        print(f"    其中被闸拦下 {len(gated)} 条（任一条路 skip_reason 有值），"
              f"闸放行但检索空 {len(misses) - len(gated)} 条")
    if extras:
        print(f"对照组多余召回: {len(extras)} 条（不一定是问题，看内容是否离题）")
        for _, text, _, hook, chain in extras:
            print(f"  · {text}   hook={hook['cards']} 条  链路={chain['cards']} 条 "
                  f"{chain.get('ids') or ''}")

    if not args.skip_messages:
        both = [r for r in rows if r[3]["ok"] and r[4]["ok"] and r[4]["cards"] >= 0]
        if both:
            print()
            print("链路侧召回来源分布（哪个子系统出的内容）:")
            for label, text, _, hook, chain in both:
                src = chain.get("ids") or {}
                fields = chain.get("parts") or {}
                summary = ", ".join(f"{k}={v}" for k, v in src.items()) or "无"
                field_summary = ", ".join(f"{k}:{v}字" for k, v in fields.items()) or "无正文"
                print(f"  {label:<16} {text[:22]}")
                print(f"      id: {summary}")
                print(f"      正文: {field_summary}")

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
