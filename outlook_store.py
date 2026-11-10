"""Single personal Outlook account; durable credentials never leave the server."""
import asyncio
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo('Asia/Shanghai')
AUTH = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/'
GRAPH = 'https://graph.microsoft.com/v1.0'
SCOPES = 'Calendars.ReadWrite Tasks.ReadWrite User.Read offline_access'
MISSING = '未配置 OUTLOOK_CLIENT_ID，请先配置微软个人账户应用的客户端 ID'
REAUTH = '授权失效，需要小羊重新授权'


class OutlookError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except (ValueError, TypeError):
        raise OutlookError('时间格式无效，请用 YYYY-MM-DD HH:MM、ISO 时间或全天日期 YYYY-MM-DD') from None


def graph_time(value):
    return {'dateTime': parse_time(value).replace(tzinfo=None).isoformat(), 'timeZone': 'China Standard Time'}


def display_time(value):
    if not value:
        return ''
    raw = value['dateTime']
    dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        zone = value.get('timeZone', 'China Standard Time')
        dt = dt.replace(tzinfo=TZ if zone == 'China Standard Time' else ZoneInfo(zone))
    return dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M')


def segment(value):
    return quote(str(value), safe='')


class OutlookStore:
    def __init__(self, config=None, *, transport=None, sleep=asyncio.sleep, clock=time.time):
        config = config or {}
        state = config.get('state_dir') or os.path.join(os.path.dirname(os.path.abspath(config.get('buckets_dir', 'buckets'))), 'state')
        os.makedirs(state, exist_ok=True)
        self.db_path = os.path.join(state, 'outlook.sqlite')
        self.transport, self.sleep, self.clock = transport, sleep, clock
        self.auth_lock, self.refresh_lock, self.resource_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        self.pending = None
        self.auth_task = None
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS outlook_token (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), refresh_token TEXT NOT NULL,
                access_token TEXT NOT NULL, expires_at REAL NOT NULL, account TEXT NOT NULL,
                scope TEXT NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS outlook_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS outlook_ids (
                short TEXT PRIMARY KEY, kind TEXT NOT NULL, full_id TEXT NOT NULL,
                parent_id TEXT NOT NULL, UNIQUE(kind, full_id))''')
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def configured(self):
        client_id = os.environ.get('OUTLOOK_CLIENT_ID', '').strip()
        if not client_id:
            raise OutlookError(MISSING)
        return client_id

    def meta(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO outlook_meta VALUES (?,?)', (key, value))
            row = db.execute('SELECT value FROM outlook_meta WHERE key=?', (key,)).fetchone()
            return row['value'] if row else ''

    def token(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM outlook_token WHERE singleton=1').fetchone()
            return dict(row) if row else None

    def save_token(self, data, previous=None, account=None):
        previous = previous or {}
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO outlook_token VALUES (1,?,?,?,?,?,?)', (
                data.get('refresh_token', previous.get('refresh_token', '')),
                data['access_token'], self.clock() + float(data['expires_in']),
                account if account is not None else previous.get('account', ''),
                data.get('scope', previous.get('scope', '')), self.clock()))
        self.meta('last_error', '')

    def clear_credentials(self):
        with self.db() as db:
            db.execute('DELETE FROM outlook_token')
            db.execute('DELETE FROM outlook_ids')
            db.execute("DELETE FROM outlook_meta WHERE key IN ('calendar_id','todo_list_id')")

    async def disconnect(self):
        self.configured()
        async with self.auth_lock:
            if self.auth_task and not self.auth_task.done():
                self.auth_task.cancel()
                try:
                    await self.auth_task
                except asyncio.CancelledError:
                    pass
            self.pending = None
            async with self.refresh_lock:
                self.clear_credentials()
                self.meta('last_error', '')
        return {'connected': False, 'message': '已断开 Outlook，已删除本地授权'}

    async def request(self, method, url, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
            return await client.request(method, url, **kwargs)

    @staticmethod
    def failure(response, step):
        try:
            body = response.json()
            error = body.get('error', {})
            message = error.get('message', '') if isinstance(error, dict) else body.get('error_description', error)
        except (ValueError, AttributeError):
            message = ''
        return OutlookError(f'{step}失败：{message or response.reason_phrase}', response.status_code)

    async def start_auth(self):
        client_id = self.configured()
        async with self.auth_lock:
            if self.pending and self.auth_task and not self.auth_task.done() and self.pending['expires_at'] > self.clock():
                return self.public_pending()
            if self.auth_task and not self.auth_task.done():
                self.auth_task.cancel()
                try:
                    await self.auth_task
                except asyncio.CancelledError:
                    pass
            try:
                response = await self.request('POST', AUTH + 'devicecode', data={'client_id': client_id, 'scope': SCOPES})
                if response.is_error:
                    raise self.failure(response, '获取设备码')
                data = response.json()
                self.pending = {**data, 'expires_at': self.clock() + data['expires_in']}
                self.meta('last_error', '')
                self.auth_task = asyncio.create_task(self.poll_auth(dict(self.pending), client_id))
                return self.public_pending()
            except httpx.HTTPError:
                raise OutlookError('获取设备码失败：网络连接异常，请稍后重试') from None

    def public_pending(self):
        return {key: self.pending.get(key) for key in ('user_code', 'verification_uri', 'expires_in', 'message', 'expires_at')}

    async def poll_auth(self, pending, client_id):
        interval = max(1, float(pending.get('interval', 5)))
        try:
            while self.clock() < pending['expires_at']:
                await self.sleep(min(interval, max(0, pending['expires_at'] - self.clock())))
                if self.clock() >= pending['expires_at']:
                    break
                response = await self.request('POST', AUTH + 'token', data={
                    'client_id': client_id, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                    'device_code': pending['device_code']})
                data = response.json()
                if response.is_success:
                    # Fetch identity before persisting, so switching accounts never reuses old IDs.
                    me = await self.request('GET', GRAPH + '/me', headers={'Authorization': 'Bearer ' + data['access_token'],
                        'Prefer': 'outlook.timezone="China Standard Time"'})
                    if me.is_error:
                        raise self.failure(me, '读取 Outlook 账户')
                    account = me.json().get('mail') or me.json().get('userPrincipalName', '')
                    async with self.refresh_lock:
                        self.clear_credentials()
                        self.save_token(data, account=account)
                    return
                error = data.get('error')
                if error == 'authorization_pending':
                    continue
                if error == 'slow_down':
                    interval += 5
                    continue
                labels = {'expired_token': '设备码已过期，请重新开始授权',
                          'authorization_declined': '小羊已拒绝授权，请重新开始授权',
                          'access_denied': '小羊已拒绝授权，请重新开始授权'}
                raise OutlookError(labels.get(error, '设备授权失败，请重新开始授权'))
            self.meta('last_error', '设备码已过期，请重新开始授权')
        except asyncio.CancelledError:
            raise
        except (OutlookError, httpx.HTTPError, ValueError, KeyError) as exc:
            self.meta('last_error', str(exc) if isinstance(exc, OutlookError) else '设备授权失败：网络或响应异常，请重试')
        finally:
            self.pending = None

    async def access_token(self):
        client_id = self.configured()
        async with self.refresh_lock:
            token = self.token()
            if not token:
                raise OutlookError(self.meta('last_error') or 'Outlook 尚未授权，请小羊先完成设备码授权')
            if token['expires_at'] > self.clock() + 300:
                return token['access_token']
            response = await self.request('POST', AUTH + 'token', data={
                'client_id': client_id, 'grant_type': 'refresh_token',
                'refresh_token': token['refresh_token'], 'scope': SCOPES})
            data = response.json()
            if response.is_error:
                if data.get('error') == 'invalid_grant':
                    self.clear_credentials()
                    self.meta('last_error', REAUTH)
                    raise OutlookError(REAUTH)
                raise self.failure(response, '刷新 Outlook 授权')
            self.save_token(data, token)
            return data['access_token']

    async def status(self):
        self.configured()
        error = self.meta('last_error')
        if self.token():
            try:
                me = await self.graph('GET', '/me', step='读取 Outlook 账户')
                with self.db() as db:
                    db.execute('UPDATE outlook_token SET account=? WHERE singleton=1', (me.get('mail') or me.get('userPrincipalName', ''),))
            except (OutlookError, httpx.HTTPError) as exc:
                error = str(exc) if isinstance(exc, OutlookError) else '读取 Outlook 账户失败：网络连接异常'
        token = self.token()
        return {'connected': bool(token), 'account': token['account'] if token else '',
                'pending': {'expires_at': self.pending['expires_at']} if self.pending else None,
                'last_error': error, 'calendar_id': bool(self.meta('calendar_id')), 'todo_list_id': bool(self.meta('todo_list_id'))}

    async def graph(self, method, path, *, step='Outlook 操作', **kwargs):
        url = path if path.startswith('https://') else GRAPH + path
        if not url.startswith(GRAPH + '/') or urlparse(url).netloc != 'graph.microsoft.com':
            raise OutlookError('Graph 分页地址无效')
        token = await self.access_token()
        headers = {'Authorization': 'Bearer ' + token, 'Prefer': 'outlook.timezone="China Standard Time"'}
        for attempt in range(2):
            response = await self.request(method, url, headers=headers, **kwargs)
            if response.status_code in (429, 503) and attempt == 0:
                retry = response.headers.get('Retry-After', '1')
                try:
                    delay = float(retry)
                except ValueError:
                    delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                await self.sleep(max(0, delay))
                continue
            if response.is_error:
                raise self.failure(response, step)
            return response.json() if response.content else {}

    async def values(self, path, **kwargs):
        result = []
        while path:
            data = await self.graph('GET', path, **kwargs)
            result.extend(data.get('value', []))
            path = data.get('@odata.nextLink')
            kwargs = {}  # nextLink already contains query parameters
        return result

    @staticmethod
    def collection(kind):
        return '/me/calendars' if kind == 'calendar' else '/me/todo/lists'

    async def owned(self, kind):
        async with self.resource_lock:
            key = 'calendar_id' if kind == 'calendar' else 'todo_list_id'
            root = self.collection(kind)
            field = 'name' if kind == 'calendar' else 'displayName'
            cached = self.meta(key)
            if cached:
                try:
                    item = await self.graph('GET', root + '/' + segment(cached), step='检查言之日历/列表')
                    if item.get(field) == '言之':
                        return cached
                    self.meta(key, '')
                except OutlookError as exc:
                    if exc.status != 404:
                        raise
                    self.meta(key, '')
            items = await self.values(root)
            item = next((x for x in items if x.get(field) == '言之'), None)
            if item is None:
                item = await self.graph('POST', root, json={field: '言之'}, step='创建言之日历/列表')
            self.meta(key, item['id'])
            return item['id']

    def short_id(self, kind, full_id, parent):
        with self.db() as db:
            existing = db.execute('SELECT short FROM outlook_ids WHERE kind=? AND full_id=?', (kind, full_id)).fetchone()
            if existing:
                db.execute('UPDATE outlook_ids SET parent_id=? WHERE short=?', (parent, existing['short']))
                return existing['short']
            digest = hashlib.sha1((kind + ':' + full_id).encode()).hexdigest()
            for length in range(8, 41):
                short = digest[:length]
                if not db.execute('SELECT 1 FROM outlook_ids WHERE short=?', (short,)).fetchone():
                    db.execute('INSERT INTO outlook_ids VALUES (?,?,?,?)', (short, kind, full_id, parent))
                    return short
        raise OutlookError('短码冲突，请使用完整 ID')

    def resolve(self, kind, value):
        with self.db() as db:
            row = db.execute('SELECT * FROM outlook_ids WHERE kind=? AND (short=? OR full_id=?)', (kind, value, value)).fetchone()
            return dict(row) if row else {'full_id': value, 'parent_id': ''}

    async def target(self, kind, value):
        if not value:
            raise OutlookError('请提供 id')
        resolved = self.resolve(kind, value)
        full = segment(resolved['full_id'])
        # Always ask Graph for the current parent; stored mappings may be stale after moves.
        if kind == 'calendar':
            parent = await self.graph('GET', f'/me/events/{full}/calendar', step='读取事件所属日历')
            return f'/me/events/{full}', parent
        for parent in await self.values('/me/todo/lists'):
            path = f'/me/todo/lists/{segment(parent["id"])}/tasks/{full}'
            try:
                await self.graph('GET', path, step='查找提醒事项')
                return path, parent
            except OutlookError as exc:
                if exc.status != 404:
                    raise
        raise OutlookError('找不到提醒事项，请重新 list 获取 id')

    async def guard(self, kind, parent, confirmed):
        own_id = await self.owned(kind)
        if parent['id'] != own_id and not confirmed:
            name = parent.get('name') or parent.get('displayName', '')
            noun = '日历' if kind == 'calendar' else '列表'
            raise OutlookError(f'这是小羊自己的{noun}（{name}），先问她，她同意后带 confirmed=true 再来')

    def result_id(self, kind, result, original, parent):
        full_id = result.get('id') or self.resolve(kind, original)['full_id']
        return self.short_id(kind, full_id, parent)

    async def calendar(self, action, *, id=None, title=None, start=None, end=None, all_day=None,
                       location=None, notes=None, remind_minutes=None, confirmed=False):
        self.configured()
        if action not in ('list', 'create', 'update', 'delete'):
            raise OutlookError('calendar action 只支持 list/create/update/delete')
        if action == 'list':
            begin = parse_time(start) if start else datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            finish = parse_time(end) if end else begin + timedelta(days=7)
            if finish <= begin:
                raise OutlookError('结束时间必须晚于开始时间')
            rows, failed = [], []
            for parent in await self.values('/me/calendars'):
                try:
                    events = await self.values(f'/me/calendars/{segment(parent["id"])}/calendarView',
                        params={'startDateTime': begin.isoformat(), 'endDateTime': finish.isoformat()})
                except OutlookError:
                    # One unreadable calendar (subscribed / holiday) must not hide the rest.
                    failed.append(parent.get('name', ''))
                    continue
                for event in events:
                    code = self.short_id('calendar', event['id'], parent['id'])
                    at = display_time(event['start'])
                    span = at[:10] + ' 全天' if event.get('isAllDay') else at + '–' + display_time(event['end'])
                    text = f'{span} {event.get("subject", "")} ［{parent["name"]}］ {event.get("location", {}).get("displayName", "")} id:{code}'
                    rows.append((at, text))
            rows.sort(key=lambda row: row[0])
            text = self.lines([row[1] for row in rows], '没有日程')
            return text + (f'\n（读取失败的日历：{"、".join(failed)}）' if failed else '')
        if action == 'create' and (not title or not start):
            raise OutlookError('创建日程需要 title 和 start')
        body = {}
        if title is not None:
            body['subject'] = title
        if all_day is not None:
            body['isAllDay'] = all_day
        if start is not None or end is not None or all_day is not None:
            current = {}
            if action == 'update':
                path, parent = await self.target('calendar', id)
                await self.guard('calendar', parent, confirmed)
                current = await self.graph('GET', path, step='读取原日程')
            is_all_day = all_day if all_day is not None else current.get('isAllDay', False)
            begin = parse_time(start) if start else parse_time(display_time(current['start']))
            if end:
                finish = parse_time(end)
            elif action == 'update':
                duration = parse_time(display_time(current['end'])) - parse_time(display_time(current['start']))
                finish = begin + duration
            else:
                finish = begin + (timedelta(days=1) if is_all_day else timedelta(hours=1))
            if is_all_day and (begin.time() != datetime.min.time() or finish.time() != datetime.min.time()):
                raise OutlookError('全天事件起止时间必须为北京时间零点，结束日期不包含在内')
            if finish <= begin:
                raise OutlookError('结束时间必须晚于开始时间')
            body.update(start=graph_time(begin), end=graph_time(finish))
        if location is not None:
            body['location'] = {'displayName': location}
        if notes is not None:
            body['body'] = {'contentType': 'text', 'content': notes}
        if remind_minutes is not None or action == 'create':
            minutes = 15 if remind_minutes is None else remind_minutes
            if minutes < -1:
                raise OutlookError('remind_minutes 须为 -1 或非负整数')
            body['isReminderOn'] = minutes != -1
            if minutes != -1:
                body['reminderMinutesBeforeStart'] = minutes
        if action == 'create':
            parent_id = await self.owned('calendar')
            path = f'/me/calendars/{segment(parent_id)}/events'
        else:
            path, parent = await self.target('calendar', id)
            await self.guard('calendar', parent, confirmed)
            parent_id = parent['id']
        result = await self.graph({'create': 'POST', 'update': 'PATCH', 'delete': 'DELETE'}[action], path, step='日程操作', **({'json': body} if action != 'delete' else {}))
        code = self.result_id('calendar', result, id, parent_id) if action != 'delete' else id
        return f'日程已{ {"create": "创建", "update": "更新", "delete": "删除"}[action]}，id:{code}'

    @staticmethod
    def lines(rows, empty):
        return '\n'.join(rows[:50] + ([f'还有 {len(rows)-50} 条'] if len(rows) > 50 else [])) or empty

    async def reminders(self, action, *, id=None, list=None, include_done=False, title=None,
                        remind_at=None, due=None, notes=None, confirmed=False):
        self.configured()
        if action not in ('list', 'create', 'update', 'complete', 'delete'):
            raise OutlookError('reminders action 只支持 list/create/update/complete/delete')
        if action == 'list':
            rows = []
            for parent in await self.values('/me/todo/lists'):
                if list and parent['displayName'] != list:
                    continue
                query = {} if include_done else {'params': {'$filter': "status ne 'completed'"}}
                for task in await self.values(f'/me/todo/lists/{segment(parent["id"])}/tasks', **query):
                    if not include_done and task.get('status') == 'completed':
                        continue
                    code = self.short_id('reminders', task['id'], parent['id'])
                    dates = []
                    if task.get('dueDateTime'):
                        dates.append('到期 ' + display_time(task['dueDateTime'])[:10])
                    if task.get('isReminderOn') and task.get('reminderDateTime'):
                        dates.append('提醒 ' + display_time(task['reminderDateTime']))
                    rows.append(f'［{parent["displayName"]}］ {task.get("title", "")} {" / ".join(dates)} {"已完成 " if task.get("status") == "completed" else ""}id:{code}')
            return self.lines(rows, '没有提醒事项')
        if action == 'create' and not title:
            raise OutlookError('创建提醒事项需要 title')
        body = {}
        if title is not None:
            body['title'] = title
        if remind_at is not None:
            body.update(isReminderOn=bool(remind_at), reminderDateTime=graph_time(remind_at) if remind_at else None)
        if due is not None:
            body['dueDateTime'] = graph_time(due) if due else None
        if notes is not None:
            body['body'] = {'contentType': 'text', 'content': notes}
        if action == 'complete':
            body['status'] = 'completed'
        if action == 'create':
            parent_id = await self.owned('reminders')
            path = f'/me/todo/lists/{segment(parent_id)}/tasks'
        else:
            path, parent = await self.target('reminders', id)
            await self.guard('reminders', parent, confirmed)
            parent_id = parent['id']
        result = await self.graph('POST' if action == 'create' else 'DELETE' if action == 'delete' else 'PATCH', path,
                                  step='提醒事项操作', **({'json': body} if action != 'delete' else {}))
        code = self.result_id('reminders', result, id, parent_id) if action != 'delete' else id
        return f'提醒事项已{ {"create": "创建", "update": "更新", "complete": "完成", "delete": "删除"}[action]}，id:{code}'

    async def tool(self, kind, action, **kwargs):
        try:
            return await getattr(self, kind)(action, **kwargs)
        except OutlookError as exc:
            return str(exc)
        except httpx.HTTPError:
            return 'Outlook 操作失败：网络连接异常，请稍后重试'
        except (ValueError, KeyError, TypeError, OverflowError):
            return 'Outlook 操作失败：时间参数或服务响应无效'
