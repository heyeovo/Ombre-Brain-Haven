from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from raw_events import RawEventStore


CLAUDE_SOURCE = "claude_official_export"
KELIVO_SOURCE = "kelivo_export"


def _clean(value: Any) -> str:
    return re.sub(r"\r\n?", "\n", str(value or "")).strip()


def _without_thinking(text: str, thinking_parts: list[str]) -> str:
    visible = _clean(text)
    for thinking in sorted((_clean(item) for item in thinking_parts), key=len, reverse=True):
        if thinking:
            visible = visible.replace(thinking, "")
    visible = re.sub(r"\n{3,}", "\n\n", visible)
    return visible.strip()


def iter_archive_corrections(path: Path) -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith("claude/conversations/") and name.endswith(".json"):
                conversation = json.loads(archive.read(name))
                for message in conversation.get("chat_messages") or []:
                    if not isinstance(message, dict):
                        continue
                    thinking_parts = [
                        _clean(value) for value in (message.get("thinking") or []) if _clean(value)
                    ]
                    yield {
                        "source": CLAUDE_SOURCE,
                        "source_event_id": str(message.get("uuid") or ""),
                        "text": _without_thinking(str(message.get("text") or ""), thinking_parts),
                        "thinking": "\n\n".join(thinking_parts),
                    }
            elif name.startswith("kelivo/messages/") and name.endswith(".json"):
                message = json.loads(archive.read(name))
                if not isinstance(message, dict):
                    continue
                yield {
                    "source": KELIVO_SOURCE,
                    "source_event_id": str(message.get("id") or ""),
                    "text": _clean(message.get("content")),
                    "thinking": _clean(message.get("reasoningText")),
                }


def repair_store(store: RawEventStore, *, dry_run: bool = True) -> dict[str, Any]:
    conn = store._connect()
    try:
        archive_paths = [
            Path(str(row["archive_path"]))
            for row in conn.execute(
                "SELECT archive_path FROM raw_imports WHERE status = 'archived' AND archive_path != ''"
            ).fetchall()
        ]
    finally:
        conn.close()
    counts: Counter[str] = Counter()
    processed: set[tuple[str, str]] = set()
    for archive_path in archive_paths:
        if not archive_path.is_file():
            counts["missing_archive"] += 1
            continue
        for correction in iter_archive_corrections(archive_path):
            key = (correction["source"], correction["source_event_id"])
            if key in processed:
                continue
            processed.add(key)
            result = store.repair_archive_message(**correction, dry_run=dry_run)
            counts[str(result.get("status") or "unknown")] += 1
    return {
        "ok": True,
        "dry_run": dry_run,
        "archive_count": len(archive_paths),
        "message_count": len(processed),
        "counts": dict(counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair historical archive body/thinking separation")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--buckets-dir", default="buckets")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    args = parser.parse_args()
    store = RawEventStore({"state_dir": args.state_dir, "buckets_dir": args.buckets_dir})
    print(json.dumps(repair_store(store, dry_run=not args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
