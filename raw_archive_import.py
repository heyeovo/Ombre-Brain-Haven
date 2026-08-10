from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import mmap
import os
import re
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from raw_events import RAW_EVENT_ARCHIVE_SCOPE, RawEventStore


CLAUDE_SOURCE = "claude_official_export"
KELIVO_SOURCE = "kelivo_export"
ARCHIVE_FORMAT = "ombre-selected-chat-v2"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _balanced_value_end(buffer: mmap.mmap, start: int) -> int:
    stack: list[int] = []
    in_string = False
    escaped = False
    index = start
    while index < len(buffer):
        value = buffer[index]
        if in_string:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                in_string = False
        else:
            if value == 34:
                in_string = True
            elif value in (91, 123):
                stack.append(value)
            elif value in (93, 125):
                if stack:
                    stack.pop()
                    if not stack:
                        return index + 1
                else:
                    return index
            elif value == 44 and not stack:
                return index
        index += 1
    raise ValueError("unterminated JSON value")


def _find_array_start(buffer: mmap.mmap, key: str | None) -> int:
    if key is None:
        match = re.match(rb"(?:\xef\xbb\xbf)?\s*\[", buffer[:64])
    else:
        match = re.search(rb'"' + re.escape(key.encode("utf-8")) + rb'"\s*:\s*\[', buffer)
    if not match:
        raise ValueError(f"JSON array not found: {key or '<root>'}")
    return match.end()


def iter_json_array_items(path: Path, key: str | None = None) -> Iterator[bytes]:
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as buffer:
        index = _find_array_start(buffer, key)
        while index < len(buffer):
            while index < len(buffer) and buffer[index] in b" \t\r\n,":
                index += 1
            if index >= len(buffer) or buffer[index] == 93:
                return
            end = _balanced_value_end(buffer, index)
            yield bytes(buffer[index:end]).strip()
            index = end


def iter_json_object_items(path: Path, key: str) -> Iterator[tuple[str, bytes]]:
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as buffer:
        match = re.search(rb'"' + re.escape(key.encode("utf-8")) + rb'"\s*:\s*\{', buffer)
        if not match:
            return
        index = match.end()
        while index < len(buffer):
            while index < len(buffer) and buffer[index] in b" \t\r\n,":
                index += 1
            if index >= len(buffer) or buffer[index] == 125:
                return
            if buffer[index] != 34:
                raise ValueError(f"invalid JSON object key in {key}")
            key_end = index + 1
            escaped = False
            while key_end < len(buffer):
                value = buffer[key_end]
                if escaped:
                    escaped = False
                elif value == 92:
                    escaped = True
                elif value == 34:
                    break
                key_end += 1
            item_key = json.loads(bytes(buffer[index : key_end + 1]))
            index = key_end + 1
            while index < len(buffer) and buffer[index] in b" \t\r\n":
                index += 1
            if index >= len(buffer) or buffer[index] != 58:
                raise ValueError(f"invalid JSON object separator in {key}")
            index += 1
            while index < len(buffer) and buffer[index] in b" \t\r\n":
                index += 1
            end = _balanced_value_end(buffer, index)
            yield str(item_key), bytes(buffer[index:end]).strip()
            index = end


def _extract_string_field(raw: bytes, field: str) -> str:
    match = re.search(
        rb'"' + re.escape(field.encode("utf-8")) + rb'"\s*:\s*"((?:\\.|[^"\\])*)"',
        raw[: min(len(raw), 65536)],
    )
    if not match:
        return ""
    try:
        return json.loads(b'"' + match.group(1) + b'"')
    except Exception:
        return match.group(1).decode("utf-8", errors="replace")


def _normalize_visible_text(value: Any) -> str:
    return re.sub(r"\r\n?", "\n", str(value or "")).strip()


def _claude_visible_text(message: dict[str, Any]) -> str:
    direct = _normalize_visible_text(message.get("text"))
    if direct:
        return direct
    parts = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or str(block.get("type") or "").lower() != "text":
            continue
        if block.get("hidden") or block.get("hidden_in_chat"):
            continue
        text = _normalize_visible_text(block.get("text"))
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _parse_claude_time(value: Any) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        return "", True
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds"), False


def _parse_kelivo_time(value: Any) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if not raw:
        return "", True
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return "", True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds"), False


def _content_hash(role: str, text: str) -> str:
    payload = json.dumps(
        {"role": role, "text": re.sub(r"\s+", " ", text).strip()},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iter_selected_claude(
    path: Path,
    selected_ids: set[str],
) -> Iterator[tuple[bytes, dict[str, Any], list[dict[str, Any]]]]:
    found: set[str] = set()
    for raw in iter_json_array_items(path):
        conversation_id = _extract_string_field(raw, "uuid")
        if conversation_id not in selected_ids:
            continue
        conversation = json.loads(raw)
        found.add(conversation_id)
        events = []
        for message in conversation.get("chat_messages") or []:
            if not isinstance(message, dict):
                continue
            sender = str(message.get("sender") or "").strip().lower()
            role = "user" if sender == "human" else sender
            created_at, missing_time = _parse_claude_time(message.get("created_at"))
            block_types = Counter(
                str(block.get("type") or "<missing>")
                for block in (message.get("content") or [])
                if isinstance(block, dict)
            )
            events.append(
                {
                    "source": CLAUDE_SOURCE,
                    "source_event_id": str(message.get("uuid") or ""),
                    "role": role,
                    "text": _claude_visible_text(message),
                    "created_at": created_at,
                    "conversation_id": conversation_id,
                    "session_id": conversation_id,
                    "client": "claude_official",
                    "usage_scope": RAW_EVENT_ARCHIVE_SCOPE,
                    "metadata": {
                        "archive_format": ARCHIVE_FORMAT,
                        "conversation_title": str(conversation.get("name") or ""),
                        "original_sender": sender,
                        "original_created_at": str(message.get("created_at") or ""),
                        "timestamp_missing": missing_time,
                        "parent_message_uuid": str(message.get("parent_message_uuid") or ""),
                        "_preview_content_block_types": dict(block_types),
                        "_preview_attachment_count": len(message.get("attachments") or []),
                        "_preview_file_count": len(message.get("files") or []),
                    },
                }
            )
        yield raw, conversation, events
    missing = selected_ids - found
    if missing:
        raise ValueError(f"Claude selected conversations not found: {sorted(missing)}")


def _selected_kelivo_conversations(
    path: Path,
    selected_ids: set[str],
) -> tuple[list[tuple[bytes, dict[str, Any]]], set[str]]:
    selected = []
    message_ids: set[str] = set()
    found: set[str] = set()
    for raw in iter_json_array_items(path, "conversations"):
        conversation_id = _extract_string_field(raw, "id")
        if conversation_id not in selected_ids:
            continue
        conversation = json.loads(raw)
        selected.append((raw, conversation))
        found.add(conversation_id)
        message_ids.update(str(value) for value in conversation.get("messageIds") or [])
    missing = selected_ids - found
    if missing:
        raise ValueError(f"Kelivo selected conversations not found: {sorted(missing)}")
    return selected, message_ids


def iter_selected_kelivo(
    path: Path,
    selected_ids: set[str],
) -> tuple[list[tuple[bytes, dict[str, Any]]], list[tuple[bytes, dict[str, Any]]], list[dict[str, Any]]]:
    conversations, message_ids = _selected_kelivo_conversations(path, selected_ids)
    titles = {str(item.get("id")): str(item.get("title") or "") for _, item in conversations}
    selected_messages: list[tuple[bytes, dict[str, Any]]] = []
    events = []
    for raw in iter_json_array_items(path, "messages"):
        message_id = _extract_string_field(raw, "id")
        if message_id not in message_ids:
            continue
        message = json.loads(raw)
        selected_messages.append((raw, message))
        role = str(message.get("role") or "").strip().lower()
        conversation_id = str(message.get("conversationId") or "")
        created_at, missing_time = _parse_kelivo_time(message.get("timestamp"))
        events.append(
            {
                "source": KELIVO_SOURCE,
                "source_event_id": message_id,
                "role": role,
                "text": _normalize_visible_text(message.get("content")),
                "created_at": created_at,
                "conversation_id": conversation_id,
                "session_id": conversation_id,
                "client": "kelivo",
                "usage_scope": RAW_EVENT_ARCHIVE_SCOPE,
                "metadata": {
                    "archive_format": ARCHIVE_FORMAT,
                    "conversation_title": titles.get(conversation_id, ""),
                    "original_created_at": str(message.get("timestamp") or ""),
                    "source_timezone": "Asia/Hong_Kong",
                    "timestamp_missing": missing_time,
                    "has_reasoning": bool(message.get("reasoningText")),
                    "_preview_reasoning_present": bool(message.get("reasoningText")),
                },
            }
        )
    unresolved = message_ids - {str(item.get("id") or "") for _, item in selected_messages}
    if unresolved:
        raise ValueError(f"Kelivo selected messages not found: {len(unresolved)}")
    return conversations, selected_messages, events


def selected_kelivo_auxiliary(
    path: Path,
    selected_message_ids: set[str],
) -> dict[str, list[tuple[str, bytes]]]:
    result: dict[str, list[tuple[str, bytes]]] = {"toolEvents": [], "geminiThoughtSigs": []}
    for mapping_name in result:
        for item_key, raw in iter_json_object_items(path, mapping_name):
            if item_key in selected_message_ids:
                result[mapping_name].append((item_key, raw))
    return result


def _source_preview(events: Iterable[dict[str, Any]], conversation_count: int) -> dict[str, Any]:
    roles: Counter[str] = Counter()
    preserved_nonindexed: Counter[str] = Counter()
    excluded_content: Counter[str] = Counter()
    invalid_roles = 0
    missing_time = 0
    empty_text = 0
    timestamps = []
    exact_hashes: Counter[str] = Counter()
    content_hashes: Counter[str] = Counter()
    content_records: dict[str, list[dict[str, Any]]] = {}
    empty_preserved_nonindexed = 0
    empty_excluded_nontext = 0
    truly_empty = 0
    event_count = 0
    for event in events:
        event_count += 1
        role = str(event.get("role") or "")
        roles[role] += 1
        if role not in {"user", "assistant"}:
            invalid_roles += 1
        text = str(event.get("text") or "")
        if not text:
            empty_text += 1
        created_at = str(event.get("created_at") or "")
        if not created_at:
            missing_time += 1
        else:
            timestamps.append(created_at)
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        block_types = metadata.get("_preview_content_block_types") or {}
        for key, count in block_types.items():
            if key == "thinking":
                preserved_nonindexed["thinking"] += int(count or 0)
            elif key != "text":
                excluded_content[str(key)] += int(count or 0)
        if metadata.get("_preview_reasoning_present"):
            preserved_nonindexed["reasoningText"] += 1
        if metadata.get("_preview_attachment_count"):
            excluded_content["attachments"] += int(metadata["_preview_attachment_count"])
        if metadata.get("_preview_file_count"):
            excluded_content["files"] += int(metadata["_preview_file_count"])
        has_preserved_nonindexed = bool(
            metadata.get("_preview_reasoning_present") or int(block_types.get("thinking") or 0) > 0
        )
        has_excluded_nontext = bool(
            metadata.get("_preview_attachment_count")
            or metadata.get("_preview_file_count")
            or any(
                key not in {"text", "thinking"} and int(count or 0) > 0
                for key, count in block_types.items()
            )
        )
        if not text:
            if has_preserved_nonindexed:
                empty_preserved_nonindexed += 1
            elif has_excluded_nontext:
                empty_excluded_nontext += 1
            else:
                truly_empty += 1
        exact_hashes[
            RawEventStore.canonical_event_hash(role=role, text=text, created_at=created_at)
        ] += 1
        content_fingerprint = _content_hash(role, text)
        content_hashes[content_fingerprint] += 1
        content_records.setdefault(content_fingerprint, []).append(
            {
                "source_event_id": str(event.get("source_event_id") or ""),
                "conversation_id": str(event.get("conversation_id") or ""),
                "created_at": created_at,
                "text_length": len(text),
            }
        )
    return {
        "conversation_count": conversation_count,
        "message_count": event_count,
        "date_range": {
            "earliest": min(timestamps) if timestamps else "",
            "latest": max(timestamps) if timestamps else "",
        },
        "roles": dict(roles),
        "invalid_role_count": invalid_roles,
        "missing_time_count": missing_time,
        "empty_text_count": empty_text,
        "preserved_nonindexed": dict(preserved_nonindexed),
        "excluded_content": dict(excluded_content),
        "unhandled_content": {
            "invalid_role_messages": invalid_roles,
            "empty_visible_text_total": empty_text,
            "preserved_but_not_indexed_only": empty_preserved_nonindexed,
            "excluded_nontext_only_messages": empty_excluded_nontext,
            "truly_empty_messages": truly_empty,
        },
        "within_source_exact_duplicate_count": sum(count - 1 for count in exact_hashes.values() if count > 1),
        "_exact_hashes": set(exact_hashes),
        "_content_hashes": set(content_hashes),
        "_content_records": content_records,
    }


def build_preview(
    *,
    claude_path: Path,
    kelivo_path: Path,
    claude_ids: set[str],
    kelivo_ids: set[str],
) -> dict[str, Any]:
    claude_events: list[dict[str, Any]] = []
    for _, _, events in iter_selected_claude(claude_path, claude_ids):
        claude_events.extend(events)
    kelivo_conversations, kelivo_messages, kelivo_events = iter_selected_kelivo(kelivo_path, kelivo_ids)
    claude = _source_preview(claude_events, len(claude_ids))
    kelivo = _source_preview(kelivo_events, len(kelivo_conversations))
    kelivo_message_ids = {str(message.get("id") or "") for _, message in kelivo_messages}
    kelivo_auxiliary = selected_kelivo_auxiliary(kelivo_path, kelivo_message_ids)
    kelivo["excluded_content"]["toolEvents"] = len(kelivo_auxiliary["toolEvents"])
    kelivo["excluded_content"]["geminiThoughtSigs"] = len(kelivo_auxiliary["geminiThoughtSigs"])
    exact_overlap = claude["_exact_hashes"] & kelivo["_exact_hashes"]
    content_overlap = claude["_content_hashes"] & kelivo["_content_hashes"]
    duplicate_counts = Counter()
    duplicate_examples = []
    for fingerprint in content_overlap:
        for left in claude["_content_records"].get(fingerprint, []):
            for right in kelivo["_content_records"].get(fingerprint, []):
                minimum_length = min(int(left["text_length"]), int(right["text_length"]))
                if minimum_length < 12:
                    duplicate_counts["short_text_ignored"] += 1
                    continue
                try:
                    left_time = datetime.fromisoformat(str(left["created_at"]).replace("Z", "+00:00"))
                    right_time = datetime.fromisoformat(str(right["created_at"]).replace("Z", "+00:00"))
                except ValueError:
                    duplicate_counts["missing_or_invalid_time"] += 1
                    continue
                distance = abs((left_time - right_time).total_seconds())
                local_zone = ZoneInfo("Asia/Hong_Kong")
                if distance <= 300:
                    classification = "strong_within_5_minutes"
                elif left_time.astimezone(local_zone).date() == right_time.astimezone(local_zone).date():
                    classification = "medium_same_hong_kong_date"
                else:
                    duplicate_counts["same_text_other_date_ignored"] += 1
                    continue
                duplicate_counts[classification] += 1
                if len(duplicate_examples) < 20:
                    duplicate_examples.append(
                        {
                            "classification": classification,
                            "text_length": minimum_length,
                            "claude": left,
                            "kelivo": right,
                        }
                    )
    for item in (claude, kelivo):
        item.pop("_exact_hashes", None)
        item.pop("_content_hashes", None)
        item.pop("_content_records", None)
    selection_payload = json.dumps(
        {"claude": sorted(claude_ids), "kelivo": sorted(kelivo_ids)},
        sort_keys=True,
    ).encode("utf-8")
    return {
        "preview_version": 2,
        "archive_format": ARCHIVE_FORMAT,
        "files": {
            "claude": {"size": claude_path.stat().st_size, "sha256": _sha256_file(claude_path)},
            "kelivo": {"size": kelivo_path.stat().st_size, "sha256": _sha256_file(kelivo_path)},
        },
        "selection_hash": hashlib.sha256(selection_payload).hexdigest(),
        "claude": claude,
        "kelivo": kelivo,
        "cross_source_suspected_duplicates": {
            "exact_role_text_time": len(exact_overlap),
            **dict(duplicate_counts),
            "candidate_examples": duplicate_examples,
        },
        "existing_haven_duplicate_check": "pending_until_deployed_endpoint_is_available",
        "import_authorized": False,
    }


def write_selected_archive(
    output_path: Path,
    *,
    claude_path: Path,
    kelivo_path: Path,
    claude_ids: set[str],
    kelivo_ids: set[str],
) -> dict[str, Any]:
    preview = build_preview(
        claude_path=claude_path,
        kelivo_path=kelivo_path,
        claude_ids=claude_ids,
        kelivo_ids=kelivo_ids,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(preview, ensure_ascii=False, indent=2))
        for _, conversation, _ in iter_selected_claude(claude_path, claude_ids):
            sanitized_messages = []
            for message in conversation.get("chat_messages") or []:
                if not isinstance(message, dict):
                    continue
                thinking = [
                    str(block.get("thinking") or "")
                    for block in (message.get("content") or [])
                    if isinstance(block, dict)
                    and str(block.get("type") or "").lower() == "thinking"
                    and str(block.get("thinking") or "")
                ]
                sanitized_messages.append(
                    {
                        "uuid": str(message.get("uuid") or ""),
                        "sender": str(message.get("sender") or ""),
                        "created_at": str(message.get("created_at") or ""),
                        "updated_at": str(message.get("updated_at") or ""),
                        "parent_message_uuid": str(message.get("parent_message_uuid") or ""),
                        "text": _claude_visible_text(message),
                        "thinking": thinking,
                    }
                )
            sanitized_conversation = {
                "uuid": str(conversation.get("uuid") or ""),
                "name": str(conversation.get("name") or ""),
                "created_at": str(conversation.get("created_at") or ""),
                "updated_at": str(conversation.get("updated_at") or ""),
                "chat_messages": sanitized_messages,
            }
            archive.writestr(
                f"claude/conversations/{conversation['uuid']}.json",
                json.dumps(sanitized_conversation, ensure_ascii=False, indent=2),
            )
        conversations, messages, _ = iter_selected_kelivo(kelivo_path, kelivo_ids)
        for _, conversation in conversations:
            sanitized_conversation = {
                "id": str(conversation.get("id") or ""),
                "title": str(conversation.get("title") or ""),
                "createdAt": str(conversation.get("createdAt") or ""),
                "updatedAt": str(conversation.get("updatedAt") or ""),
                "messageIds": [str(value) for value in conversation.get("messageIds") or []],
            }
            archive.writestr(
                f"kelivo/conversations/{conversation['id']}.json",
                json.dumps(sanitized_conversation, ensure_ascii=False, indent=2),
            )
        for _, message in messages:
            sanitized_message = {
                "id": str(message.get("id") or ""),
                "conversationId": str(message.get("conversationId") or ""),
                "role": str(message.get("role") or ""),
                "timestamp": str(message.get("timestamp") or ""),
                "content": _normalize_visible_text(message.get("content")),
                "reasoningText": str(message.get("reasoningText") or ""),
            }
            archive.writestr(
                f"kelivo/messages/{message['id']}.json",
                json.dumps(sanitized_message, ensure_ascii=False, indent=2),
            )
    return {
        "path": str(output_path),
        "size": output_path.stat().st_size,
        "sha256": _sha256_file(output_path),
        "preview": preview,
    }


def selected_events(
    *,
    claude_path: Path,
    kelivo_path: Path,
    claude_ids: set[str],
    kelivo_ids: set[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _, _, conversation_events in iter_selected_claude(claude_path, claude_ids):
        events.extend(conversation_events)
    _, _, kelivo_events = iter_selected_kelivo(kelivo_path, kelivo_ids)
    events.extend(kelivo_events)
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event["metadata"] = {
            key: value for key, value in metadata.items() if not str(key).startswith("_preview_")
        }
    return events


def _haven_opener(haven_url: str, password: str) -> tuple[urllib.request.OpenerDirector, str]:
    base_url = str(haven_url or "").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("invalid Haven URL")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        base_url + "/auth/login",
        data=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Haven login failed: HTTP {response.status}")
    return opener, base_url


def _post_ingest(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    body: dict[str, Any],
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + "/api/ingest-raw",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Haven ingest failed: HTTP {exc.code}: {detail}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


def check_existing_haven_matches(
    events: list[dict[str, Any]],
    *,
    haven_url: str,
    password: str,
) -> dict[str, Any]:
    opener, base_url = _haven_opener(haven_url, password)
    hashes = list(
        dict.fromkeys(
            RawEventStore.canonical_event_hash(
                role=str(event.get("role") or ""),
                text=str(event.get("text") or ""),
                created_at=str(event.get("created_at") or ""),
            )
            for event in events
            if event.get("text")
        )
    )
    matches: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(hashes), 5000):
        result = _post_ingest(
            opener,
            base_url,
            {"action": "check-canonical", "canonical_hashes": hashes[start : start + 5000]},
        )
        matches.update(result.get("matches") or {})
    return {
        "checked_hash_count": len(hashes),
        "matched_hash_count": len(matches),
        "matched_event_count": sum(len(items) for items in matches.values()),
        "matches": matches,
    }


def commit_selected_import(
    *,
    archive_path: Path,
    preview: dict[str, Any],
    events: list[dict[str, Any]],
    haven_url: str,
    password: str,
    batch_size: int = 200,
) -> dict[str, Any]:
    opener, base_url = _haven_opener(haven_url, password)
    archive_hash = _sha256_file(archive_path)
    import_id = f"history-{preview['selection_hash'][:16]}-{archive_hash[:16]}"
    combined_source_hash = hashlib.sha256(
        json.dumps(preview["files"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    chunk_size = 1024 * 1024
    total_chunks = max(1, (archive_path.stat().st_size + chunk_size - 1) // chunk_size)
    with archive_path.open("rb") as stream:
        for index in range(total_chunks):
            chunk = stream.read(chunk_size)
            _post_ingest(
                opener,
                base_url,
                {
                    "action": "archive-chunk",
                    "import_id": import_id,
                    "index": index,
                    "total_chunks": total_chunks,
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                    "source": "historical_exports",
                    "source_file_sha256": combined_source_hash,
                    "selection_hash": preview["selection_hash"],
                    "archive_sha256": archive_hash,
                    "metadata": {"files": preview["files"], "archive_format": ARCHIVE_FORMAT},
                },
                timeout=120,
            )
    archive_result = _post_ingest(
        opener,
        base_url,
        {"action": "archive-commit", "import_id": import_id},
        timeout=120,
    )
    inserted = duplicate = conflict = rejected = 0
    safe_batch_size = max(1, min(int(batch_size or 200), 500))
    for start in range(0, len(events), safe_batch_size):
        result = _post_ingest(
            opener,
            base_url,
            {
                "events": events[start : start + safe_batch_size],
                "usage_scope": RAW_EVENT_ARCHIVE_SCOPE,
                "import_id": import_id,
            },
            timeout=120,
        )
        inserted += int(result.get("inserted") or 0)
        duplicate += int(result.get("duplicate") or 0)
        conflict += int(result.get("conflict") or 0)
        rejected += int(result.get("rejected") or 0)
    return {
        "ok": True,
        "import_id": import_id,
        "archive": archive_result,
        "inserted": inserted,
        "duplicate": duplicate,
        "conflict": conflict,
        "rejected": rejected,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview selected Claude/Kelivo raw archive imports")
    parser.add_argument("--claude", required=True, type=Path)
    parser.add_argument("--kelivo", required=True, type=Path)
    parser.add_argument("--claude-id", action="append", default=[])
    parser.add_argument("--kelivo-id", action="append", default=[])
    parser.add_argument("--archive-output", type=Path)
    parser.add_argument("--haven-url")
    parser.add_argument("--password-env", default="OMBRE_SESSION")
    parser.add_argument("--check-existing", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--confirm-selection-hash")
    parser.add_argument("--allow-existing-matches", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    return parser


def main() -> int:
    args = _parser().parse_args()
    kwargs = {
        "claude_path": args.claude,
        "kelivo_path": args.kelivo,
        "claude_ids": set(args.claude_id),
        "kelivo_ids": set(args.kelivo_id),
    }
    result = build_preview(**kwargs)
    events = None
    if args.check_existing or args.commit:
        password = str(os.environ.get(args.password_env) or "")
        if not args.haven_url or not password:
            raise SystemExit("--haven-url and the configured password environment variable are required")
        events = selected_events(**kwargs)
    if args.check_existing or args.commit:
        result["existing_haven_duplicate_check"] = check_existing_haven_matches(
            events or [],
            haven_url=args.haven_url,
            password=password,
        )
    if args.commit:
        if args.confirm_selection_hash != result["selection_hash"]:
            raise SystemExit("commit blocked: --confirm-selection-hash must exactly match the preview")
        if not args.archive_output:
            raise SystemExit("commit blocked: --archive-output is required")
        if (
            result["existing_haven_duplicate_check"].get("matched_event_count")
            and not args.allow_existing_matches
        ):
            raise SystemExit(
                "commit blocked: existing Haven matches require explicit --allow-existing-matches"
            )
        archive_result = write_selected_archive(args.archive_output, **kwargs)
        result["commit"] = commit_selected_import(
            archive_path=args.archive_output,
            preview=result,
            events=events or [],
            haven_url=args.haven_url,
            password=password,
            batch_size=args.batch_size,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
