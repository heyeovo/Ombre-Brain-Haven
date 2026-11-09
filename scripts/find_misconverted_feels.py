"""Read-only heuristic audit. Candidates require human review; no repair is inferred."""
import argparse
import json
from pathlib import Path

import frontmatter


def scan(feel_dir: Path):
    if not feel_dir.is_dir():
        raise ValueError(f"Not a feel directory: {feel_dir}")
    for path in sorted(feel_dir.rglob("*.md")):
        if path.is_symlink() or not path.resolve().is_relative_to(feel_dir.resolve()):
            continue
        try:
            post = frontmatter.load(path)
        except Exception as exc:
            yield {"path": str(path), "error": str(exc)}
            continue
        reasons = []
        before = post.get("type_before_pin")
        if before and before != "feel":
            reasons.append("explicit_non_feel_type_before_pin")
        first_line = post.content.strip().splitlines()[:1]
        if first_line and first_line[0].startswith("【事件·"):
            reasons.append("event_heading")
        # Standalone manual feels may also lack source: this is weak evidence only.
        if not any(post.get(key) for key in (
            "source", "source_bucket", "source_bucket_ids", "period",
        )):
            reasons.append("no_source_metadata_weak")
        if reasons:
            yield {
                "id": post.get("id"), "name": post.get("name"), "path": str(path),
                "type": post.get("type"), "type_before_pin": before, "reasons": reasons,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feel-dir", required=True, type=Path, help="Explicit path to buckets/feel")
    args = parser.parse_args()
    if not args.feel_dir.is_dir():
        parser.error("--feel-dir must be an existing directory")
    for item in scan(args.feel_dir):
        print(json.dumps(item, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
