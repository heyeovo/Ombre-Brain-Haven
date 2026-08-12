"""Persistent minute-level schedule calculations for Haven background tasks."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


DAILY_REVIEW_TASK_TYPE = "daily_review"
WEEKLY_JOURNEY_TASK_TYPE = "weekly_journey"
SUPPORTED_TASK_TYPES = {DAILY_REVIEW_TASK_TYPE, WEEKLY_JOURNEY_TASK_TYPE}
FIXED_TIMEZONE = "Asia/Hong_Kong"
FIXED_DAY_START_HOUR = 4


def _int_between(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_policy(task_type: str, policy: dict | None) -> dict:
    task = str(task_type or "").strip()
    if task not in SUPPORTED_TASK_TYPES:
        raise ValueError("unsupported automation task_type")
    raw = policy if isinstance(policy, dict) else {}
    default_hour, default_minute = ((4, 30) if task == DAILY_REVIEW_TASK_TYPE else (5, 0))
    normalized = {
        "hour": _int_between(raw.get("hour"), default_hour, 0, 23),
        "minute": _int_between(raw.get("minute"), default_minute, 0, 59),
        "day_start_hour": FIXED_DAY_START_HOUR,
    }
    if task == WEEKLY_JOURNEY_TASK_TYPE:
        normalized.update({
            "weekday": _int_between(raw.get("weekday"), 0, 0, 6),
            "persona_id": str(raw.get("persona_id") or "").strip(),
            "candidate_only": True,
        })
    return normalized


def next_run_at(
    task_type: str,
    policy: dict,
    *,
    after: datetime | None = None,
    timezone: str = FIXED_TIMEZONE,
) -> datetime:
    if str(timezone or FIXED_TIMEZONE) != FIXED_TIMEZONE:
        raise ValueError("automation timezone is fixed to Asia/Hong_Kong")
    tz = ZoneInfo(FIXED_TIMEZONE)
    local_after = after.astimezone(tz) if after and after.tzinfo else (
        after.replace(tzinfo=tz) if after else datetime.now(tz)
    )
    normalized = normalize_policy(task_type, policy)
    run_time = time(normalized["hour"], normalized["minute"])
    if task_type == DAILY_REVIEW_TASK_TYPE:
        candidate = datetime.combine(local_after.date(), run_time, tzinfo=tz)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        return candidate
    days_ahead = (normalized["weekday"] - local_after.weekday()) % 7
    candidate = datetime.combine(local_after.date() + timedelta(days=days_ahead), run_time, tzinfo=tz)
    if candidate <= local_after:
        candidate += timedelta(days=7)
    return candidate


def schedule_payload(
    task_type: str,
    *,
    enabled: bool,
    policy: dict | None,
    after: datetime | None = None,
) -> dict:
    normalized = normalize_policy(task_type, policy)
    return {
        "enabled": bool(enabled),
        "timezone": FIXED_TIMEZONE,
        "policy": normalized,
        "next_run_at": next_run_at(task_type, normalized, after=after).isoformat(timespec="seconds")
        if enabled else "",
    }
