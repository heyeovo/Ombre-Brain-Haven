"""Bounded, body-free GPT request evidence; never accepts arbitrary prompt snapshots."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_snapshot(value):
    def keys(obj, expected):
        if not isinstance(obj, dict) or set(obj) != set(expected):
            raise ValueError("invalid audit fields")

    def digest(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get("hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", obj["hash"]):
            raise ValueError("invalid audit hash")
        if type(obj.get("bytes")) is not int or not 0 <= obj["bytes"] <= 4 * 1024 * 1024:
            raise ValueError("invalid audit size")

    keys(value, ("version", "algorithm", "model", "body_hash", "instructions", "parts", "messages", "context"))
    if type(value['version']) is not int or value["version"] != 1 or value["algorithm"] != "sha256-json-sorted-v1":
        raise ValueError("invalid audit version")
    if not isinstance(value["model"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value["model"]):
        raise ValueError("invalid audit model")
    digest({"hash": value["body_hash"], "bytes": 0})
    keys(value["instructions"], ("hash", "bytes"))
    digest(value["instructions"])
    keys(value["parts"], ("persona", "window", "background", "capability", "recall", "current_text", "current_time"))
    for part in value["parts"].values():
        keys(part, ("hash", "bytes"))
        digest(part)
    messages = value["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 2000:
        raise ValueError("invalid audit messages")
    for item in messages:
        keys(item, ("hash", "bytes", "role", "source_round_id", "prefix_hash"))
        digest(item)
        digest({"hash": item["prefix_hash"], "bytes": 0})
        if item["role"] not in ("user", "assistant", "developer") or (item["source_round_id"] is not None and
                (type(item["source_round_id"]) is not int or item["source_round_id"] < 0)):
            raise ValueError("invalid audit source")
    context = value["context"]
    keys(context, ("estimator", "estimate_safety_ratio", "input_tokens_estimated", "application_input_budget",
                   "model_context_limit", "included_history_rounds", "omitted_history_rounds", "context_revision"))
    if context["estimator"] != "utf8-bytes-div-3-v1" or context["estimate_safety_ratio"] != 1.05 or context["model_context_limit"] is not None:
        raise ValueError("invalid audit estimator")
    for key in ("input_tokens_estimated", "application_input_budget", "included_history_rounds", "omitted_history_rounds", "context_revision"):
        if type(context[key]) is not int or not 0 <= context[key] <= 10_000_000:
            raise ValueError("invalid audit context")
    return value


def request_hash(payload):
    body = {**payload, "store": False, "stream": True}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cache_evidence(value):
    value = value if isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1 else {}

    def integer(number):
        return number if type(number) is int and 0 <= number <= 9007199254740991 else None

    model = value.get('response_model')
    tier = value.get('service_tier')
    raw = value.get('diagnostics')
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get('type')
    kinds = ('cache_hit', 'cache_miss', 'comparison_response_not_found', 'unavailable')
    reasons = ('model_changed', 'prompt_cache_key_changed', 'service_tier_changed', 'tools_changed',
               'text_format_changed', 'reasoning_effort_changed', 'verbosity_changed', 'context_compacted', 'input_changed')
    return {
        'version': 1,
        'response_model': model if isinstance(model, str) and len(model) <= 200 and re.fullmatch(r'(?:gpt-[A-Za-z0-9._-]+|o[1-9][A-Za-z0-9._-]*)', model) else None,
        'service_tier': tier if tier in ('auto', 'default', 'flex', 'scale', 'priority') else None,
        'cache_write_tokens': integer(value.get('cache_write_tokens')),
        'diagnostics': {
            'type': kind,
            'reason': raw.get('reason') if kind == 'cache_miss' and raw.get('reason') in reasons else None,
            'comparison_reusable_tokens': integer(raw.get('comparison_reusable_tokens')),
            'cache_missed_tokens': integer(raw.get('cache_missed_tokens')),
        } if kind in kinds else None,
    }


def quota_evidence(value):
    """Keep bounded quota candidates only; their account/application scope is unverified."""
    def window(raw):
        if not isinstance(raw, dict):
            return None
        used = raw.get('used_percent')
        if type(used) not in (int, float) or not 0 <= used <= 100 or not math.isfinite(used):
            return None
        minutes, reset = raw.get('window_minutes'), raw.get('resets_at')
        return {'used_percent': used,
                'window_minutes': minutes if type(minutes) is int and 0 < minutes <= 9007199254740991 else None,
                'resets_at': reset if type(reset) is int and 0 < reset <= 253402300799 else None}

    raw = value.get('snapshots') if isinstance(value, dict) else None
    snapshots = {}
    counts = {'response_headers': 0, 'stream_event': 0}
    # Current helper emits at most 16 buckets per source; do not traverse unbounded input.
    for item in raw[:32] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        source, limit_id, observed = item.get('source'), item.get('limit_id'), item.get('observed_at')
        if not isinstance(source, str) or source not in counts or not isinstance(limit_id, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', limit_id):
            continue
        if not isinstance(observed, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', observed):
            continue
        try:
            datetime.fromisoformat(observed.replace('Z', '+00:00'))
        except ValueError:
            continue
        primary, secondary = window(item.get('primary')), window(item.get('secondary'))
        if primary is None and secondary is None:
            continue
        key = (source, limit_id)
        if key not in snapshots:
            if counts[source] >= 16:
                continue
            counts[source] += 1
        snapshots[key] = {'source': source, 'observed_at': observed, 'limit_id': limit_id,
                          'scope': 'unverified', 'primary': primary, 'secondary': secondary}
    return {'status': 'observed' if snapshots else 'unavailable', 'snapshots': list(snapshots.values())}


def apply_event(record, event):
    kind = event.get("type")
    if kind == "request":
        if record.get('sending_at') or event.get("body_hash") != record["snapshot"]["body_hash"]:
            raise ValueError("audit request mismatch")
        record["sending_at"] = now()
        connection_id = event.get('connection_id')
        if isinstance(connection_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', connection_id):
            record['connection_id'] = connection_id
    elif kind == "receipt":
        record["acknowledged_at"] = now()
    elif kind == "tool_continuation":
        record["tool_continuation"] = True
    elif kind in ("completed", "error"):
        record["finished_at"] = now()
        record["outcome"] = "completed" if kind == "completed" else "failed"
        if kind == "error" and event.get("error", {}).get("code") == "cancelled":
            record["outcome"] = "cancelled"
        raw = event.get("usage")
        record["usage"] = {key: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None
                           for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")} if isinstance(raw, dict) else None
        # Old helpers omit this field. Error/cancel events cannot manufacture a completed response.
        if kind == 'completed' and 'cacheEvidence' in event:
            record['cache_evidence'] = cache_evidence(event['cacheEvidence'])
        if kind == 'completed' and 'quotaEvidence' in event:
            record['quota_evidence'] = quota_evidence(event['quotaEvidence'])
    for source, target in (("requestId", "official_request_id"), ("responseId", "response_id")):
        value = event.get(source)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            record[target] = value
    return record


def clear_session_audit(conn, profile_id, session_id):
    # Preserve permanent admission receipts; erase only scoped evidence.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_chat_requests'").fetchone():
        columns = {row[1] for row in conn.execute('PRAGMA table_info(gpt_chat_requests)')}
        if 'audit_json' in columns:
            conn.execute("UPDATE gpt_chat_requests SET audit_json='' WHERE profile_id=? AND session_id=?",
                         (profile_id, session_id))
