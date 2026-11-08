"""Phone activity: raw durable events and conservative, pure session inference."""
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def timestamp(value=None, now=None):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00')) if value else (now or datetime.now(TZ))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def parse_event(raw, now=None):
    if not isinstance(raw, dict) or len(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()) > 4096:
        raise ValueError('单条事件须为对象且不超过 4KB')
    app = str(raw.get('app_id') or '').strip()
    if not app or raw.get('event') not in ('opened', 'closed'):
        raise ValueError('app_id 或 event 无效')
    source = raw.get('source', 'shortcut')
    if source not in ('shortcut', 'pwa'):
        raise ValueError('source 无效')
    battery = raw.get('battery_pct')
    if battery is not None:
        battery = float(battery)
        if not math.isfinite(battery) or not 0 <= battery <= 100:
            raise ValueError('电量无效')
        # iOS「获取电池状态」给 0–100；只有小于 1 的小数才按比例理解，1 就是 1%。
        battery = int(battery * 100 + .5) if battery < 1 else int(battery + .5)
    try:
        occurred = timestamp(raw.get('occurred_at'), now)
    except ValueError:
        # 快捷指令日期格式没配成 ISO 时（如「2026年11月8日 05:58」）不丢事件，按接收时间记。
        occurred = timestamp(now=now)
    return dict(event_id=str(raw.get('event_id') or uuid.uuid4()), app_id=app,
                app_name=str(raw.get('app_name') or app), event=raw['event'],
                occurred_at=occurred.isoformat(),
                received_at=timestamp(now=now).isoformat(), battery_pct=battery, source=source)


def derive_sessions(events):
    sessions, active = [], {}
    def finish(item, end=None, estimated=False):
        seconds = (timestamp(end) - timestamp(item['started_at'])).total_seconds() if end else None
        item.update(ended_at=end, duration_seconds=seconds if seconds is not None and 0 <= seconds <= 14400 else None,
                    estimated=estimated, status='ended' if seconds is not None and seconds <= 14400 else 'unknown')
        sessions.append(item)
    ordered = sorted(events, key=lambda e: timestamp(e['occurred_at']))
    for index, e in enumerate(ordered):
        app, at = e['app_id'], e['occurred_at']
        if e['event'] == 'opened':
            for other in list(active):
                if other != app:
                    following = next((x for x in ordered[index + 1:] if x['app_id'] == other), None)
                    if following is None or following['event'] != 'closed':
                        finish(active.pop(other), at, True)
            if app in active:
                # 同一 App 半小时内重复「打开」多半是漏记了中间的离开或页面重复上报，接着算原来那段。
                if timestamp(at) - timestamp(active[app]['started_at']) <= timedelta(minutes=30):
                    continue
                finish(active.pop(app))
            active[app] = dict(app_id=app, app_name=e['app_name'], started_at=at)
        elif app in active:
            finish(active.pop(app), at)
        else:
            sessions.append(dict(app_id=app, app_name=e['app_name'], started_at=None,
                                 ended_at=at, duration_seconds=None, estimated=False, status='closed_only'))
    for item in active.values():
        finish(item)
    return sorted(sessions, key=lambda s: timestamp(s['ended_at'] or s['started_at']), reverse=True)


def session_text(s):
    clock = lambda value: timestamp(value).strftime('%H:%M')
    name = s['app_name'].replace('\n', ' ').replace('\r', ' ')
    if s['status'] == 'closed_only':
        return f'{clock(s["ended_at"])} 离开{name}（没记到打开）'
    if not s['ended_at']:
        return f'{clock(s["started_at"])} 起 {name}（还没记到离开）'
    duration = s['duration_seconds']
    label = f'约 {round(duration / 60)} 分钟' if duration is not None else '时长未知'
    if s['estimated']:
        label += '，离开时间是估算'
    return f'{clock(s["started_at"])}–{clock(s["ended_at"])} {name}（{label}）'


def digest(events, now=None):
    if not events:
        return ''
    now = timestamp(now=now)
    recent = [e for e in events if now - timedelta(hours=6) <= timestamp(e['occurred_at']) <= now]
    if not recent:
        e = max(events, key=lambda e: timestamp(e['occurred_at']))
        return f'[手机 最后记录 {timestamp(e["occurred_at"]).strftime("%m-%d %H:%M")} {"开了" if e["event"] == "opened" else "关了"}{e["app_name"]}]'.replace('\n', ' ').replace('\r', ' ')
    sessions = [s for s in derive_sessions(events)
                if now - timedelta(hours=6) <= timestamp(s['ended_at'] or s['started_at']) <= now]
    parts = [session_text(s) for s in sessions[:3]]
    battery = next((e['battery_pct'] for e in sorted(recent, key=lambda e: timestamp(e['occurred_at']), reverse=True) if e['battery_pct'] is not None), None)
    if battery is not None:
        parts.append(f'电量 {battery}%')
    return '[手机 ' + ' · '.join(parts) + ']'


# 和聊天日、日回顾同一条日界线：凌晨 4 点前算前一天。
DAY_START_HOUR = 4
DETAIL_LIMIT = 8


def day_start(now):
    start = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def minutes_text(seconds):
    minutes = round(seconds / 60)
    return f'{minutes // 60} 小时 {minutes % 60} 分' if minutes >= 60 else f'{minutes} 分'


def now_line(events, now):
    """events 为倒序原始事件。优先看还开着的那段，避免「切回来」和「离开」同一秒到达时说反。"""
    clock = lambda at: at.strftime('%H:%M') if at >= day_start(now) else at.strftime('%m-%d %H:%M')
    open_now = [s for s in derive_sessions(events) if not s['ended_at'] and s['started_at']
                and now - timestamp(s['started_at']) <= timedelta(hours=4)]
    if open_now:
        s = max(open_now, key=lambda s: timestamp(s['started_at']))
        at = timestamp(s['started_at'])
        return f'现在：在{s["app_name"]}（{clock(at)} 打开，已经 {minutes_text((now - at).total_seconds())}）'
    event = events[0]
    at = timestamp(event['occurred_at'])
    name = event['app_name'].replace('\n', ' ')
    if event['event'] == 'opened':
        return f'现在：{clock(at)} 打开{name}后没有记录'
    return f'现在：{clock(at)} 离开{name}，之后没有记录'


def _start_key(s):
    return timestamp(s['started_at'] or s['ended_at'])


def merge_fragments(sessions):
    """把同一 App 前后间隔不到 3 分钟的碎段并成一段（按开始时间，输出倒序）。"""
    merged = []
    for s in sorted(sessions, key=_start_key):
        prev = merged[-1] if merged else None
        if (prev and prev['app_id'] == s['app_id'] and prev['ended_at']
                and _start_key(s) - timestamp(prev['ended_at']) <= timedelta(minutes=3)):
            known = [d for d in (prev['duration_seconds'], s['duration_seconds']) if d is not None]
            started = prev['started_at'] or s['started_at']
            prev.update(ended_at=s['ended_at'], started_at=started,
                         duration_seconds=sum(known) if known else None,
                         estimated=prev['estimated'] or s['estimated'],
                         status='ended' if started and s['ended_at'] and known else s['status'])
        else:
            merged.append(dict(s))
    return merged[::-1]


def today_totals(events, today):
    totals = {}
    for s in derive_sessions([e for e in events if timestamp(e['occurred_at']) >= today]):
        if s['duration_seconds']:
            totals[s['app_name']] = totals.get(s['app_name'], 0) + s['duration_seconds']
    items = [f'{name} {minutes_text(sec)}' for name, sec in sorted(totals.items(), key=lambda kv: -kv[1]) if sec >= 30]
    return '今天（4 点起）：' + (' · '.join(items) if items else '还没有超过一分钟的记录')


class DeviceEventStore:
    def __init__(self, config=None):
        config = config or {}
        state = config.get('state_dir') or os.path.join(os.path.dirname(os.path.abspath(config.get('buckets_dir', 'buckets'))), 'state')
        os.makedirs(state, exist_ok=True)
        self.db_path = os.path.join(state, 'device_events.sqlite')
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, app_id TEXT NOT NULL,
                app_name TEXT NOT NULL, event TEXT NOT NULL, occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL, battery_pct INTEGER, source TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS device_events_time ON device_events(occurred_at)')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.execute('PRAGMA busy_timeout=5000')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(self, payload, now=None):
        items = payload.get('events') if isinstance(payload, dict) and 'events' in payload else [payload]
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError('批量须为 1–50 条')
        events = [parse_event(e, now) for e in items]
        accepted = 0
        with self._connect() as db:
            for event in events:
                cols = ','.join(event)
                accepted += db.execute(f'INSERT OR IGNORE INTO device_events ({cols}) VALUES ({",".join("?" for _ in event)})', list(event.values())).rowcount
            db.execute('DELETE FROM device_events WHERE occurred_at < ?', ((timestamp(now=now) - timedelta(days=14)).isoformat(),))
        return dict(accepted=accepted, duplicates=len(events)-accepted)

    def events(self, hours=None, limit=None, now=None):
        with self._connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM device_events ORDER BY occurred_at DESC, id DESC')]
        if hours is not None:
            cutoff = timestamp(now=now) - timedelta(hours=hours)
            rows = [e for e in rows if timestamp(e['occurred_at']) >= cutoff]
        return rows[:limit] if limit else rows

    def activity(self, hours=6, now=None):
        now = timestamp(now=now)
        today = day_start(now)
        span = max(hours, (now - today).total_seconds() / 3600)
        events = self.events(span, now=now)
        if not events:
            last = self.events(limit=1)
            return now_line(last, now) if last else '还没有任何手机记录。'
        cutoff = now - timedelta(hours=hours)
        recent = merge_fragments([s for s in derive_sessions(events)
                                  if timestamp(s['ended_at'] or s['started_at']) >= cutoff])
        shown = [s for s in recent if not (s['ended_at'] and s['duration_seconds'] is not None and s['duration_seconds'] < 30)]
        lines = [now_line(events, now), f'最近 {hours} 小时：']
        lines += [f'- {session_text(s)}' for s in shown[:DETAIL_LIMIT]] or ['- 没有超过一分钟的记录']
        if len(shown) > DETAIL_LIMIT:
            lines.append(f'（更早还有 {len(shown) - DETAIL_LIMIT} 段）')
        lines.append(today_totals(events, today))
        battery = next((e['battery_pct'] for e in events if e['battery_pct'] is not None), None)
        if battery is not None:
            lines.append(f'电量：{battery}%')
        lines.append('关闭事件可能漏记，时长是估算。')
        return '\n'.join(lines)
