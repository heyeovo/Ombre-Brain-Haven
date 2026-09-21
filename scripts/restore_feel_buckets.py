import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from utils import load_config


async def build_plan(manager: BucketManager, target_states: dict[str, bool]) -> list[dict]:
    plan = []
    for bucket_id, target_pinned in target_states.items():
        bucket = await manager.get(bucket_id)
        if not bucket:
            plan.append({"id": bucket_id, "status": "missing", "target_pinned": target_pinned})
            continue
        metadata = bucket.get("metadata", {})
        plan.append(
            {
                "id": bucket_id,
                "status": "ready",
                "name": metadata.get("name", bucket_id),
                "current_type": metadata.get("type", "dynamic"),
                "current_pinned": bool(metadata.get("pinned", False)),
                "current_importance": metadata.get("importance", 5),
                "current_path": bucket.get("path", ""),
                "target_type": "feel",
                "target_pinned": target_pinned,
                "target_directory": "feel/沉淀物",
            }
        )
    return plan


async def apply_plan(manager: BucketManager, plan: list[dict]) -> list[dict]:
    results = []
    for item in plan:
        record = dict(item)
        if item.get("status") != "ready":
            results.append(record)
            continue
        restored = await manager.restore_as_feel(item["id"], pinned=bool(item["target_pinned"]))
        if not restored:
            record["status"] = "restore_failed"
        else:
            record["status"] = "restored"
            record["restored_path"] = restored.get("path", "")
        results.append(record)
    return results


def summarize(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore explicitly confirmed bucket IDs as canonical standalone feel buckets."
    )
    parser.add_argument("--id", action="append", default=[], help="Bucket ID to restore as an unpinned feel.")
    parser.add_argument("--pinned-id", action="append", default=[], help="Bucket ID to restore as a pinned feel.")
    parser.add_argument("--apply", action="store_true", help="Apply the repair. Default is dry-run.")
    args = parser.parse_args()

    target_states = {str(item).strip(): False for item in args.id if str(item).strip()}
    target_states.update({str(item).strip(): True for item in args.pinned_id if str(item).strip()})
    if not target_states:
        parser.error("at least one --id or --pinned-id is required")

    manager = BucketManager(load_config())
    plan = await build_plan(manager, target_states)
    if args.apply:
        results = await apply_plan(manager, plan)
    else:
        results = [{**item, "status": "dry_run" if item.get("status") == "ready" else item["status"]} for item in plan]

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "summary": summarize(results),
                "items": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
