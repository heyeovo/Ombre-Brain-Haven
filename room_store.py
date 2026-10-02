"""Private room index and sealed visits; public projections use an allowlist."""
import copy
import json
import secrets
from datetime import datetime

from darkroom import DarkroomStore, LOCAL_TZ, _now, _now_iso


def parse_time(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=LOCAL_TZ) if dt.tzinfo is None else dt


class RoomStore(DarkroomStore):
    def __init__(self, config):
        super().__init__(config)
        self.rooms_path = self.base_dir / "rooms.json"
        self.visits_path = self.base_dir / "visits.jsonl"
        self.snapshots_path = self.base_dir / "door_snapshots.json"

    def _json(self, path, default):
        if not path.exists():
            return copy.deepcopy(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def _lines(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _rooms(self):
        if self.rooms_path.exists():
            return self._json(self.rooms_path, {})
        groups = {}
        for entry in self._iter_entries_unlocked(visibility=None):
            groups.setdefault(self._entry_room_id(entry), []).append(entry)
        releases = self._lines(self.release_log_path)
        rooms = {}
        for rid, entries in groups.items():
            if all(e.get("visibility") == "retracted" for e in entries):
                continue
            released = [r for r in releases if r.get("room_id") == rid or
                        r.get("entry_id") in {e["id"] for e in entries}]
            locks = [parse_time(e["locked_until"]) for e in entries if e.get("locked_until")]
            locks = [t for t in locks if t > _now()]
            first = entries[0]
            rooms[rid] = {
                "id": rid, "title": (str(first.get("note") or "").splitlines() or ["未命名"])[0][:20] or "未命名",
                "status": "opened" if released else "closed", "note": "",
                "lock_until": max(locks).isoformat() if locks else "",
                "created_at": first.get("created_at", ""), "updated_at": entries[-1].get("created_at", ""),
                "opened_at": released[0].get("created_at", "") if released else "",
            }
        self._write_json_unlocked(self.rooms_path, rooms)
        return rooms

    def _entries(self, rid):
        return [e for e in self._iter_entries_unlocked(visibility=None)
                if self._entry_room_id(e) == rid and e.get("visibility") != "retracted"]

    def _door(self, room):
        visits = [v for v in self._lines(self.visits_path) if v["room_id"] == room["id"]]
        door = {k: room.get(k, "") for k in
                ("id", "title", "status", "lock_until", "created_at", "updated_at", "opened_at")}
        door.update(visit_count=len(visits), total_duration_ms=sum(v["duration_ms"] for v in visits),
                    last_visit=max((v["entered_at"] for v in visits), default=""))
        door["last_visit_duration_ms"] = max(visits, key=lambda v: v["entered_at"])["duration_ms"] if visits else 0
        return door

    def public_rooms(self):
        with self._lock:
            return [self._door(r) for r in self._rooms().values()]

    def public_detail(self, rid):
        with self._lock:
            room = self._rooms().get(rid)
            if not room:
                raise KeyError("房间不存在")
            result = self._door(room)
            if room["status"] == "opened":
                result["entries"] = [{"id": e["id"], "created_at": e.get("created_at", ""),
                                      "content": e.get("content", e.get("note", ""))} for e in self._entries(rid)]
                result["visits"] = [self._public_visit(v) for v in self._lines(self.visits_path) if v["room_id"] == rid]
            return result

    def _public_visit(self, visit):
        result = copy.deepcopy(visit)
        # Room tool replies can contain private continuation notes even after opening.
        # Publish the process, but omit note-bearing tool replies and note arguments.
        for event in result["process"]:
            tool = event.get("tool") if isinstance(event, dict) else None
            if isinstance(tool, dict) and str(tool.get("name", "")).endswith("__room"):
                if isinstance(tool.get("input"), dict):
                    tool["input"].pop("note", None)
                if isinstance(tool.get("input"), dict) and tool["input"].get("action") in ("enter", "leave", "read", "list"):
                    tool["result"] = "房间内部操作（便条已省略）"
                    tool.pop("error", None)
        return result

    def public_visits(self, limit=50, before=""):
        with self._lock:
            rooms = self._rooms()
            visits = sorted(self._lines(self.visits_path), key=lambda v: (v["entered_at"], v["id"]), reverse=True)
            if before:
                visits = [v for v in visits if v["entered_at"] < before]
            return [{**{k: v[k] for k in ("id", "room_id", "entered_at", "left_at", "duration_ms", "session_id", "request_id", "turn_kind")},
                     "room_title": rooms.get(v["room_id"], {}).get("title", "未命名")}
                    for v in visits[:max(1, min(100, int(limit)))]]

    def save_visit(self, data):
        keys = ("id", "room_id", "session_id", "request_id", "turn_kind", "entered_at", "left_at", "duration_ms", "process")
        if not isinstance(data, dict) or any(k not in data for k in keys):
            raise ValueError("来访字段不完整")
        visit = {k: copy.deepcopy(data[k]) for k in keys}
        if any(not isinstance(visit[k], str) or not visit[k] for k in keys[:7]):
            raise ValueError("来访标识与时间须为非空字符串")
        if visit["turn_kind"] not in ("chat", "agent_wake") or not isinstance(visit["process"], list):
            raise ValueError("来访类型或过程无效")
        if not isinstance(visit["duration_ms"], (int, float)) or visit["duration_ms"] < 0:
            raise ValueError("来访时长无效")
        if parse_time(visit["left_at"]) < parse_time(visit["entered_at"]):
            raise ValueError("出门早于进门")
        with self._lock:
            if visit["room_id"] not in self._rooms():
                raise ValueError("房间不存在")
            existing = next((v for v in self._lines(self.visits_path) if v["id"] == visit["id"]), None)
            if existing:
                if existing != visit:
                    raise ValueError("来访 id 冲突")
                return {"ok": True, "id": visit["id"], "replayed": True}
            self._append_jsonl_unlocked(self.visits_path, visit)
            return {"ok": True, "id": visit["id"]}

    def door_snapshot(self, session_id, key):
        if not session_id or not key:
            raise ValueError("session_id 和 key 必填")
        with self._lock:
            snapshots = self._json(self.snapshots_path, {})
            cached = snapshots.get(session_id)
            if cached and cached["key"] == key:
                if cached["content"].startswith("【我的房间】"):
                    cached["content"] = cached["content"].replace("【我的房间】", "【我的房间 · 今天的门牌】", 1)
                    self._write_json_unlocked(self.snapshots_path, snapshots)
                return cached
            lines = ["【我的房间 · 今天的门牌】"]
            for room in self._rooms().values():
                if room["status"] == "closed":
                    door = self._door(room)
                    lines.append(f'[{room["id"]}] {room["title"]} · 锁至 {room["lock_until"] or "无"} · 最后来访 {door["last_visit"] or "无"}\n便条：{room["note"]}')
            snapshot = {"key": key, "content": "\n".join(lines), "created_at": _now_iso()}
            snapshots[session_id] = snapshot
            self._write_json_unlocked(self.snapshots_path, snapshots)
            return snapshot

    def status(self):
        rooms = self.public_rooms()
        return {"count": len(rooms), "closed_count": sum(r["status"] == "closed" for r in rooms),
                "door": f"言之有 {len(rooms)} 间房间", "last_entered_at": max((r["last_visit"] for r in rooms), default="")}

    def act(self, action, room_id="", title="", content="", note="", lock_until="", include_visits=False):
        if action not in ("enter", "write", "read", "list", "leave", "open"):
            raise ValueError("未知房间动作")
        if len(content) > 12000 or (action == "write" and not content.strip()):
            raise ValueError("正文须为 1–12000 字")
        parsed_lock = "" if lock_until == "none" else parse_time(lock_until).isoformat() if lock_until else None
        with self._lock:
            rooms = self._rooms()
            state = self._json(self.state_path, {})
            if action == "enter" and not room_id:
                room_id = self._new_room_id()
                rooms[room_id] = {"id": room_id, "title": title.strip() or "未命名", "status": "closed",
                                  "note": "", "lock_until": "", "created_at": _now_iso(),
                                  "updated_at": _now_iso(), "opened_at": ""}
            if action == "list":
                if parsed_lock is not None:
                    rid = room_id or state.get("current_room_id", "")
                    if rid not in rooms:
                        raise KeyError("房间不存在")
                    rooms[rid].update(lock_until=parsed_lock, updated_at=_now_iso())
                    self._write_json_unlocked(self.rooms_path, rooms)
                return "\n".join(f'[{r["id"]}] {r["title"]} · {r["status"]} · 锁至 {r["lock_until"] or "无"} · 来访 {self._door(r)["visit_count"]} · 最后 {self._door(r)["last_visit"] or "无"} · 便条 {(r["note"].splitlines() or [""])[0]}' for r in rooms.values()) or "暂无房间"
            room_id = room_id or state.get("current_room_id", "")
            if room_id not in rooms:
                raise KeyError("房间不存在")
            room = rooms[room_id]
            if parsed_lock is not None:
                room["lock_until"] = parsed_lock
            if action == "enter":
                state["current_room_id"] = room_id
                self._write_json_unlocked(self.state_path, state)
            if action == "write":
                entries = self._entries(room_id)
                self._append_jsonl_unlocked(self.entries_path, {"id": self._new_entry_id(), "room_id": room_id,
                    "revision": len(entries) + 1, "created_at": _now_iso(), "content": content, "visibility": "active"})
            if action == "leave" and note:
                room["note"] = note
            room["updated_at"] = _now_iso()
            self._write_json_unlocked(self.rooms_path, rooms)
            lock_text = f' · 锁至 {room["lock_until"]}' if room["lock_until"] else (" · 锁已清除" if lock_until == "none" else "")
            if action == "open":
                if room["lock_until"] and parse_time(room["lock_until"]) > _now():
                    return f'房间未打开 [{room_id}] · 锁至 {room["lock_until"]}'
                room.update(status="opened", opened_at=room["opened_at"] or _now_iso())
                self._write_json_unlocked(self.rooms_path, rooms)
            if action in ("read", "open"):
                lines = [f'{"房间已打开" if action == "open" else "房间"} [{room_id}] {room["title"]} · {room["status"]}{lock_text}']
                if action == "read":
                    lines.append(f'便条：{room["note"]}')
                lines.extend(e.get("content", e.get("note", "")) for e in self._entries(room_id))
                if action == "read" and include_visits:
                    visits = sorted((v for v in self._lines(self.visits_path) if v["room_id"] == room_id), key=lambda v: v["entered_at"])[-5:]
                    for visit in visits:
                        lines.append(f'来访 {visit["entered_at"]}')
                        remaining = 4000
                        for event in visit["process"]:
                            if event.get("type") in ("thinking", "text"):
                                text = str(event.get("text", ""))[:remaining]
                                remaining -= len(text)
                                lines.append(text)
                            elif event.get("type") == "tool":
                                tool = event.get("tool", {})
                                lines.append(f'{tool.get("name", "")} {json.dumps(tool.get("input", {}), ensure_ascii=False)[:300]}')
                return "\n\n".join(lines)
            prefix = {"enter": "进门", "write": "写入", "leave": "出门"}[action]
            extra = f' #{len(self._entries(room_id))}' if action == "write" else f' {room["title"]}'
            if action == "enter" and room["note"]:
                extra += f'\n便条：{room["note"]}'
            return f'{prefix} [{room_id}]{extra}{lock_text}'
