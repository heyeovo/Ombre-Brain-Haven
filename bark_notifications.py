"""Profile-scoped Bark configuration, durable outbox, and delivery worker."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx


OUTBOX_TERMINAL_STATUSES = {"sent", "failed_final"}
OUTBOX_RETRY_LIMIT = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _profile_id(value: str) -> str:
    return str(value or "default").strip() or "default"


def _mask_secret(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


def initialize_bark_notification_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bark_profile_configs (
            profile_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            server_url TEXT NOT NULL DEFAULT 'https://api.day.app',
            device_key TEXT NOT NULL DEFAULT '',
            encryption_enabled INTEGER NOT NULL DEFAULT 0,
            encryption_key TEXT NOT NULL DEFAULT '',
            dashboard_base_url TEXT NOT NULL DEFAULT '',
            hide_body INTEGER NOT NULL DEFAULT 0,
            segment_interval_ms INTEGER NOT NULL DEFAULT 1000,
            max_segments INTEGER NOT NULL DEFAULT 8,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            batch_key TEXT NOT NULL,
            notification_kind TEXT NOT NULL DEFAULT 'agent_wake',
            profile_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            lane_id TEXT NOT NULL DEFAULT '',
            turn_id INTEGER NOT NULL DEFAULT 0,
            segment_index INTEGER NOT NULL DEFAULT 0,
            splitter_version INTEGER NOT NULL DEFAULT 0,
            segment_kind TEXT NOT NULL DEFAULT 'text',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            level TEXT NOT NULL DEFAULT 'active',
            group_name TEXT NOT NULL DEFAULT '',
            deep_link TEXT NOT NULL DEFAULT '',
            interval_ms INTEGER NOT NULL DEFAULT 1000,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
        ON notification_outbox (status, next_attempt_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_scope
        ON notification_outbox (profile_id, session_id, lane_id, created_at DESC, id DESC)
        """
    )


class BarkNotificationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._connect()
        try:
            initialize_bark_notification_schema(conn)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _default_config(profile_id: str) -> dict[str, Any]:
        return {
            "profile_id": _profile_id(profile_id),
            "enabled": False,
            "server_url": "https://api.day.app",
            "device_key": "",
            "encryption_enabled": False,
            "encryption_key": "",
            "dashboard_base_url": "",
            "hide_body": False,
            "segment_interval_ms": 1000,
            "max_segments": 8,
            "created_at": "",
            "updated_at": "",
        }

    @classmethod
    def _config_payload(cls, row: sqlite3.Row | dict[str, Any] | None, profile_id: str) -> dict[str, Any]:
        value = cls._default_config(profile_id)
        if row is not None:
            value.update(dict(row))
        value["enabled"] = bool(value.get("enabled"))
        value["encryption_enabled"] = bool(value.get("encryption_enabled"))
        value["hide_body"] = bool(value.get("hide_body"))
        value["segment_interval_ms"] = int(value.get("segment_interval_ms") or 1000)
        value["max_segments"] = int(value.get("max_segments") or 8)
        return value

    def get_config(self, *, profile_id: str) -> dict[str, Any]:
        profile = _profile_id(profile_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bark_profile_configs WHERE profile_id = ?", (profile,)
            ).fetchone()
            return self._config_payload(row, profile)
        finally:
            conn.close()

    def get_public_config(self, *, profile_id: str) -> dict[str, Any]:
        config = self.get_config(profile_id=profile_id)
        device_key = str(config.pop("device_key", ""))
        encryption_key = str(config.pop("encryption_key", ""))
        return {
            **config,
            "has_device_key": bool(device_key),
            "device_key_masked": _mask_secret(device_key),
            "has_encryption_key": bool(encryption_key),
            "encryption_key_masked": _mask_secret(encryption_key),
            "ready": bool(
                config.get("enabled")
                and config.get("server_url")
                and device_key
                and config.get("dashboard_base_url")
                and (not config.get("encryption_enabled") or encryption_key)
            ),
        }

    def save_config(self, *, profile_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        profile = _profile_id(profile_id)
        current = self.get_config(profile_id=profile)
        allowed = {
            "enabled", "server_url", "device_key", "encryption_enabled", "encryption_key",
            "dashboard_base_url", "hide_body", "segment_interval_ms", "max_segments",
        }
        unknown = set(changes) - allowed - {"clear_device_key", "clear_encryption_key"}
        if unknown:
            raise ValueError(f"unsupported Bark config fields: {', '.join(sorted(unknown))}")

        next_value = dict(current)
        for key in allowed:
            if key not in changes:
                continue
            value = changes[key]
            if key in {"device_key", "encryption_key"}:
                if str(value or "").strip():
                    next_value[key] = str(value).strip()
            elif key in {"enabled", "encryption_enabled", "hide_body"}:
                next_value[key] = bool(value)
            elif key in {"segment_interval_ms", "max_segments"}:
                try:
                    next_value[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be an integer") from exc
            else:
                next_value[key] = str(value or "").strip()
        if changes.get("clear_device_key"):
            next_value["device_key"] = ""
        if changes.get("clear_encryption_key"):
            next_value["encryption_key"] = ""

        server_url = str(next_value.get("server_url") or "").rstrip("/")
        if server_url.lower().endswith("/push"):
            server_url = server_url[:-5].rstrip("/")
        dashboard_url = str(next_value.get("dashboard_base_url") or "").rstrip("/")
        if server_url and not server_url.lower().startswith(("http://", "https://")):
            raise ValueError("server_url must start with http:// or https://")
        if dashboard_url and not dashboard_url.lower().startswith(("http://", "https://")):
            raise ValueError("dashboard_base_url must start with http:// or https://")
        interval = int(next_value.get("segment_interval_ms") or 1000)
        maximum = int(next_value.get("max_segments") or 8)
        if not 250 <= interval <= 10_000:
            raise ValueError("segment_interval_ms must be between 250 and 10000")
        if not 1 <= maximum <= 20:
            raise ValueError("max_segments must be between 1 and 20")
        encryption_key = str(next_value.get("encryption_key") or "")
        if next_value.get("encryption_enabled") and len(encryption_key.encode("utf-8")) != 16:
            raise ValueError("Bark encryption key must be exactly 16 UTF-8 bytes")
        if next_value.get("enabled"):
            if not server_url or not str(next_value.get("device_key") or "") or not dashboard_url:
                raise ValueError("enabled Bark requires server_url, device_key and dashboard_base_url")

        now = _iso()
        created_at = str(current.get("created_at") or now)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO bark_profile_configs
                (profile_id, enabled, server_url, device_key, encryption_enabled, encryption_key,
                 dashboard_base_url, hide_body, segment_interval_ms, max_segments, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    server_url = excluded.server_url,
                    device_key = excluded.device_key,
                    encryption_enabled = excluded.encryption_enabled,
                    encryption_key = excluded.encryption_key,
                    dashboard_base_url = excluded.dashboard_base_url,
                    hide_body = excluded.hide_body,
                    segment_interval_ms = excluded.segment_interval_ms,
                    max_segments = excluded.max_segments,
                    updated_at = excluded.updated_at
                """,
                (
                    profile, int(bool(next_value.get("enabled"))), server_url,
                    str(next_value.get("device_key") or ""),
                    int(bool(next_value.get("encryption_enabled"))), encryption_key,
                    dashboard_url, int(bool(next_value.get("hide_body"))), interval, maximum,
                    created_at, now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_public_config(profile_id=profile)

    @staticmethod
    def _parse_segments(raw_json: str) -> tuple[int, list[dict[str, str]]]:
        try:
            raw = json.loads(raw_json or "{}")
        except (TypeError, ValueError):
            return 0, []
        display = raw.get("display_segments") if isinstance(raw, dict) else None
        if not isinstance(display, dict) or not isinstance(display.get("segments"), list):
            return 0, []
        try:
            version = int(display.get("version") or 0)
        except (TypeError, ValueError):
            return 0, []
        segments: list[dict[str, str]] = []
        for item in display["segments"]:
            if not isinstance(item, dict) or not isinstance(item.get("markdown"), str):
                continue
            markdown = str(item["markdown"])
            if not markdown.strip():
                continue
            segments.append({
                "kind": "atomic" if item.get("kind") == "atomic" else "text",
                "markdown": markdown,
            })
        return version, segments

    @classmethod
    def enqueue_agent_wake_for_turn(
        cls,
        conn: sqlite3.Connection,
        *,
        profile_id: str,
        session_id: str,
        lane_id: str,
        turn_id: int,
        assistant_text: str,
        raw_json: str,
        created_at: str,
    ) -> int:
        if not str(assistant_text or "").strip():
            return 0
        schedule = conn.execute(
            """SELECT bark_notification_enabled FROM agent_wake_schedules
               WHERE profile_id = ? AND session_id = ? AND lane_id = ?""",
            (_profile_id(profile_id), str(session_id), str(lane_id)),
        ).fetchone()
        if schedule is None or not bool(schedule["bark_notification_enabled"]):
            return 0
        config_row = conn.execute(
            "SELECT * FROM bark_profile_configs WHERE profile_id = ?", (_profile_id(profile_id),)
        ).fetchone()
        config = cls._config_payload(config_row, profile_id)
        if not (
            config.get("enabled") and config.get("server_url") and config.get("device_key")
            and config.get("dashboard_base_url")
        ):
            return 0
        version, segments = cls._parse_segments(raw_json)
        if version <= 0 or not segments:
            return 0

        maximum = max(1, min(20, int(config.get("max_segments") or 8)))
        if len(segments) > maximum:
            visible = segments[:max(0, maximum - 1)]
            visible.append({
                "kind": "atomic",
                "markdown": f"Claude 还有 {len(segments) - len(visible)} 段消息，点击打开会话查看。",
            })
            segments = visible
        base_url = str(config.get("dashboard_base_url") or "").rstrip("/")
        deep_link = f"{base_url}/cc?session_id={quote(str(session_id), safe='')}"
        batch_key = f"bark:{_profile_id(profile_id)}:{int(turn_id)}:{version}"
        now = str(created_at or _iso())
        inserted = 0
        for index, segment in enumerate(segments):
            kind = segment["kind"]
            if config.get("hide_body"):
                body = "Claude 发来了一条新消息，点击打开会话查看。"
            elif kind == "atomic":
                body = (
                    segment["markdown"].strip()
                    if segment["markdown"].startswith("Claude 还有 ")
                    else "Claude 还发来了一段代码或结构化内容，点击打开会话查看。"
                )
            else:
                body = segment["markdown"].strip()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notification_outbox
                (idempotency_key, batch_key, notification_kind, profile_id, session_id, lane_id,
                 turn_id, segment_index, splitter_version, segment_kind, title, body, level,
                 group_name, deep_link, interval_ms, status, attempt_count, next_attempt_at,
                 lease_owner, lease_until, sent_at, last_error, created_at, updated_at)
                VALUES (?, ?, 'agent_wake', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', 0, ?, '', '', '', '', ?, ?)
                """,
                (
                    f"{batch_key}:{index}", batch_key, _profile_id(profile_id), str(session_id),
                    str(lane_id), int(turn_id), index, version, kind, "Claude 主动消息", body,
                    "active" if index == 0 else "passive", f"cc:{session_id}", deep_link,
                    int(config.get("segment_interval_ms") or 1000), now, now, now,
                ),
            )
            inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    def enqueue_test(self, *, profile_id: str) -> dict[str, Any]:
        profile = _profile_id(profile_id)
        config = self.get_config(profile_id=profile)
        if not config.get("server_url") or not config.get("device_key"):
            raise ValueError("save a Bark server URL and device key first")
        if config.get("encryption_enabled") and not config.get("encryption_key"):
            raise ValueError("save the Bark encryption key first")
        now = _iso()
        batch_key = f"test:{uuid.uuid4().hex}"
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO notification_outbox
                (idempotency_key, batch_key, notification_kind, profile_id, title, body, level,
                 group_name, interval_ms, status, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, 'test', ?, 'Bark 测试推送', 'Dashboard 与 Haven 的 Bark 通知链路已接通。',
                        'active', 'cc:test', ?, 'pending', ?, ?, ?)
                """,
                (batch_key, batch_key, profile, int(config.get("segment_interval_ms") or 1000), now, now, now),
            )
            conn.commit()
            return {"queued": True, "outbox_id": int(cursor.lastrowid or 0), "batch_key": batch_key}
        finally:
            conn.close()

    def recent_status(self, *, profile_id: str, session_id: str = "", lane_id: str = "") -> dict[str, Any] | None:
        profile = _profile_id(profile_id)
        clauses = ["profile_id = ?"]
        params: list[Any] = [profile]
        if str(session_id).strip():
            clauses.append("session_id = ?")
            params.append(str(session_id).strip())
        if str(lane_id).strip():
            clauses.append("lane_id = ?")
            params.append(str(lane_id).strip())
        conn = self._connect()
        try:
            latest = conn.execute(
                f"""SELECT * FROM notification_outbox WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC, id DESC LIMIT 1""",
                params,
            ).fetchone()
            if latest is None:
                return None
            rows = conn.execute(
                "SELECT status, sent_at, last_error, updated_at FROM notification_outbox WHERE batch_key = ? ORDER BY segment_index, id",
                (latest["batch_key"],),
            ).fetchall()
            statuses = [str(row["status"]) for row in rows]
            if rows and all(status == "sent" for status in statuses):
                status = "sent"
            elif any(status == "processing" for status in statuses):
                status = "sending"
            elif any(status in {"pending", "retry"} for status in statuses):
                status = "retrying" if any(status == "retry" for status in statuses) else "pending"
            else:
                status = "failed"
            error = next((str(row["last_error"] or "") for row in reversed(rows) if row["last_error"]), "")
            sent_at = next((str(row["sent_at"] or "") for row in reversed(rows) if row["sent_at"]), "")
            return {
                "batch_key": str(latest["batch_key"]),
                "kind": str(latest["notification_kind"]),
                "status": status,
                "sent_count": sum(1 for item in statuses if item == "sent"),
                "total_count": len(rows),
                "sent_at": sent_at,
                "last_error": error,
                "updated_at": max((str(row["updated_at"] or "") for row in rows), default=""),
            }
        finally:
            conn.close()

    def claim_next(self, *, owner: str, lease_seconds: int = 60, now: datetime | None = None) -> dict[str, Any] | None:
        safe_owner = str(owner or "").strip()
        if not safe_owner:
            raise ValueError("owner is required")
        current = (now or _utc_now()).astimezone(timezone.utc)
        now_iso = _iso(current)
        lease_until = _iso(current + timedelta(seconds=max(10, int(lease_seconds))))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE notification_outbox
                   SET status = 'retry', lease_owner = '', lease_until = '', next_attempt_at = ?, updated_at = ?
                   WHERE status = 'processing' AND lease_until != '' AND lease_until <= ?""",
                (now_iso, now_iso, now_iso),
            )
            row = conn.execute(
                """
                SELECT outbox.* FROM notification_outbox AS outbox
                JOIN bark_profile_configs AS config ON config.profile_id = outbox.profile_id
                WHERE outbox.status IN ('pending', 'retry')
                  AND outbox.next_attempt_at <= ?
                  AND config.server_url != '' AND config.device_key != ''
                  AND (outbox.notification_kind = 'test' OR config.enabled = 1)
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_outbox AS earlier
                      WHERE earlier.batch_key = outbox.batch_key
                        AND earlier.id < outbox.id
                        AND earlier.status NOT IN ('sent', 'failed_final')
                  )
                ORDER BY outbox.next_attempt_at, outbox.id
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """UPDATE notification_outbox
                   SET status = 'processing', lease_owner = ?, lease_until = ?, updated_at = ?
                   WHERE id = ? AND status IN ('pending', 'retry')""",
                (safe_owner, lease_until, now_iso, int(row["id"])),
            )
            claimed = conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (int(row["id"]),)).fetchone()
            conn.commit()
            return dict(claimed) if claimed is not None else None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish_delivery(self, *, outbox_id: int, owner: str, sent: bool, error: str = "", now: datetime | None = None) -> dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        now_iso = _iso(current)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (int(outbox_id),)).fetchone()
            if row is None:
                raise KeyError("notification outbox item not found")
            if str(row["status"]) in OUTBOX_TERMINAL_STATUSES:
                conn.commit()
                return dict(row)
            if str(row["status"]) != "processing" or str(row["lease_owner"] or "") != str(owner or ""):
                raise ValueError("notification outbox lease was lost")
            if sent:
                conn.execute(
                    """UPDATE notification_outbox
                       SET status = 'sent', sent_at = ?, last_error = '', lease_owner = '',
                           lease_until = '', updated_at = ? WHERE id = ?""",
                    (now_iso, now_iso, int(outbox_id)),
                )
                next_at = _iso(current + timedelta(milliseconds=max(0, int(row["interval_ms"] or 0))))
                conn.execute(
                    """UPDATE notification_outbox SET next_attempt_at = ?, updated_at = ?
                       WHERE batch_key = ? AND id > ? AND status = 'pending'""",
                    (next_at, now_iso, str(row["batch_key"]), int(outbox_id)),
                )
            else:
                attempts = int(row["attempt_count"] or 0) + 1
                final = attempts >= OUTBOX_RETRY_LIMIT
                delay = min(3600, 5 * (2 ** max(0, attempts - 1)))
                conn.execute(
                    """UPDATE notification_outbox
                       SET status = ?, attempt_count = ?, next_attempt_at = ?, last_error = ?,
                           lease_owner = '', lease_until = '', updated_at = ? WHERE id = ?""",
                    (
                        "failed_final" if final else "retry", attempts,
                        "" if final else _iso(current + timedelta(seconds=delay)),
                        str(error or "Bark delivery failed")[:500], now_iso, int(outbox_id),
                    ),
                )
            updated = conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (int(outbox_id),)).fetchone()
            conn.commit()
            return dict(updated)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class BarkNotificationWorker:
    def __init__(
        self,
        store: BarkNotificationStore,
        *,
        owner: str,
        timeout_seconds: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.store = store
        self.owner = str(owner or "").strip()
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.http_client = http_client
        if not self.owner:
            raise ValueError("owner is required")

    @staticmethod
    def _encrypted_payload(payload: dict[str, Any], encryption_key: str) -> tuple[str, str]:
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:
            raise RuntimeError("Bark encryption dependency is unavailable") from exc
        key = str(encryption_key or "").encode("utf-8")
        if len(key) != 16:
            raise ValueError("Bark encryption key must be exactly 16 UTF-8 bytes")
        iv_text = secrets.token_hex(8)
        padder = padding.PKCS7(128).padder()
        plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        padded = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv_text.encode("ascii"))).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode("ascii"), iv_text

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        item = self.store.claim_next(owner=self.owner, now=now)
        if not item:
            return {"status": "idle"}
        outbox_id = int(item["id"])
        try:
            config = self.store.get_config(profile_id=str(item["profile_id"]))
            inner = {
                "title": str(item["title"]),
                "body": str(item["body"]),
                "level": str(item["level"]),
                "group": str(item["group_name"]),
            }
            if str(item["deep_link"] or ""):
                inner["url"] = str(item["deep_link"])
            payload: dict[str, Any] = {"device_key": str(config.get("device_key") or ""), **inner}
            if config.get("encryption_enabled"):
                ciphertext, iv_text = self._encrypted_payload(inner, str(config.get("encryption_key") or ""))
                payload = {
                    "device_key": str(config.get("device_key") or ""),
                    "ciphertext": ciphertext,
                    "iv": iv_text,
                }
            endpoint = f"{str(config.get('server_url') or '').rstrip('/')}/push"
            if self.http_client is not None:
                response = await self.http_client.post(endpoint, json=payload)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(endpoint, json=payload)
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"Bark returned HTTP {response.status_code}")
            try:
                response_data = response.json()
            except ValueError:
                response_data = None
            if isinstance(response_data, dict) and response_data.get("code") not in {None, 200}:
                raise RuntimeError(f"Bark returned code {response_data.get('code')}")
            finished = self.store.finish_delivery(
                outbox_id=outbox_id, owner=self.owner, sent=True, now=now
            )
            return {"status": "sent", "outbox_id": outbox_id, "batch_key": finished["batch_key"]}
        except Exception as exc:
            finished = self.store.finish_delivery(
                outbox_id=outbox_id,
                owner=self.owner,
                sent=False,
                error=str(exc),
                now=now,
            )
            return {
                "status": str(finished["status"]),
                "outbox_id": outbox_id,
                "error": str(exc),
            }
