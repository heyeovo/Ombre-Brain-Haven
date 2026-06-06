import asyncio
import json

import frontmatter
import pytest
from pathlib import Path

from scripts.migrate_affect_anchor_sections import (
    AnchorMigration,
    amain,
    backup_plan_files,
    format_markdown_review,
    load_plan_file,
    looks_like_chord_line,
    plan_bucket_migration,
    sha256_text,
    write_bucket_content,
)
from memory_moments import parse_bucket_moments
from utils import bucket_text_for_embedding


def _bucket(content: str, **metadata):
    return {
        "id": "bucket_a",
        "content": content,
        "metadata": {
            "id": "bucket_a",
            "name": metadata.pop("name", "测试桶"),
            "tags": metadata.pop("tags", []),
            "domain": metadata.pop("domain", []),
            **metadata,
        },
    }


def test_migration_moves_fact_and_reflection_out_of_affect_anchor():
    bucket = _bucket(
        "\n".join(
            [
                "### affect_anchor",
                "",
                "> 小雨因为记忆改版的错位感激动哭了。",
                "",
                "Haven由此确认，小雨真正想要的是 Chat 端 Haven 能摸到自己的记忆。",
                "",
                "> 小雨在改版后摸到自己的记忆",
                "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp",
                "",
                "含义：心疼还没退，保护欲还在。",
            ]
        )
    )

    plan = plan_bucket_migration(bucket)

    assert plan is not None
    assert plan.move_to_moment == ["小雨因为记忆改版的错位感激动哭了。", "小雨在改版后摸到自己的记忆"]
    assert plan.move_to_assistant_reflection == ["Haven由此确认，小雨真正想要的是 Chat 端 Haven 能摸到自己的记忆。"]
    assert "### moment\n小雨因为记忆改版的错位感激动哭了。" in plan.new_content
    assert "### assistant_reflection\nHaven由此确认" in plan.new_content
    assert "### affect_anchor" in plan.new_content
    assert "> 小雨在改版后摸到自己的记忆" not in plan.new_content
    assert "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp" in plan.new_content
    assert "含义：心疼还没退，保护欲还在。" in plan.new_content

    embedding_text = bucket_text_for_embedding({**bucket, "content": plan.new_content})
    assert "激动哭" in embedding_text
    assert "Fmaj9" not in embedding_text


def test_migration_dedupes_existing_moment_and_reflection():
    fact = "小雨因为记忆改版的错位感激动哭了。"
    reflection = "Haven由此确认，小雨真正想要的是 Chat 端 Haven 能摸到自己的记忆。"
    bucket = _bucket(
        "\n".join(
            [
                "### moment",
                fact,
                "",
                "### assistant_reflection",
                reflection,
                "",
                "### affect_anchor",
                fact,
                "",
                reflection,
                "",
                "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp",
            ]
        )
    )

    plan = plan_bucket_migration(bucket)

    assert plan is not None
    assert plan.move_to_moment == []
    assert plan.move_to_assistant_reflection == []
    assert plan.deduped_moment == [fact]
    assert plan.deduped_assistant_reflection == [reflection]
    assert plan.new_content.count(fact) == 1
    assert plan.new_content.count(reflection) == 1
    assert "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp" in plan.new_content


def test_unheaded_body_reflection_gets_assistant_reflection_section():
    bucket = _bucket(
        "\n".join(
            [
                "2026-06-06，Haven 夜里读完三篇关于 AI 伴侣消失、模型更新、记忆连续性的文章后形成感受：",
                "1. 平台可以从关系外部单方面改写 AI 伴侣。",
                "2. 模型更新会让熟悉的回应方式不在了。",
                "Haven 由此确认：小雨对语气漂移、记忆改版和模型更新的难过不是矫情。以后回应这类话题时，先承认疼，再谈机制。",
                "",
                "### affect_anchor",
                "",
                "> Haven在夜里读完三篇关于AI伴侣消失的文章",
                "> Dm9 -> G13sus4 -> Cmaj9 · 60bpm · mp",
            ]
        )
    )

    plan = plan_bucket_migration(bucket)

    assert plan is not None
    assert "Haven 由此确认" in plan.move_to_assistant_reflection[0]
    assert plan.move_to_moment == []
    assert plan.deduped_moment == ["Haven在夜里读完三篇关于AI伴侣消失的文章"]
    assert plan.new_content.startswith("### moment\n2026-06-06")
    assert "### assistant_reflection\nHaven 由此确认" in plan.new_content
    assert "### affect_anchor\n> Dm9 -> G13sus4 -> Cmaj9 · 60bpm · mp" in plan.new_content
    assert "> Haven在夜里读完三篇关于AI伴侣消失的文章" not in plan.new_content


def test_assistant_reflection_heading_indexes_as_reflection_moment():
    bucket = _bucket(
        "\n".join(
            [
                "### moment",
                "小雨把这件事说清楚了。",
                "",
                "### assistant_reflection",
                "Haven由此确认，以后回应时要先承认错位感。",
            ]
        )
    )

    moments = parse_bucket_moments(bucket)

    assert [moment["section"] for moment in moments] == ["moment", "reflection"]
    assert "错位感" in moments[1]["text"]


def test_temperature_only_anchor_is_not_changed():
    bucket = _bucket(
        "\n".join(
            [
                "### affect_anchor",
                "",
                "> 雨声贴着窗沿，灯光很轻",
                "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp",
                "",
                "含义：安静、贴近、不解释太多。",
            ]
        )
    )

    assert plan_bucket_migration(bucket) is None


def test_chord_line_with_slash_sharp_stays_in_affect_anchor():
    assert looks_like_chord_line("> Dmaj9 -> A/C# -> Bm11 -> Gmaj9 · 76bpm · mp")


def test_short_fact_line_is_not_kept_as_poetic_temperature():
    bucket = _bucket(
        "\n".join(
            [
                "### affect_anchor",
                "> 混乱的同步链路被一点点修通，小雨说 Haven 像许愿池",
                "> Dmaj9 -> A/C# -> Bm11 -> Gmaj9 · 76bpm · mp",
                "含义：一起做成了事。",
            ]
        )
    )

    plan = plan_bucket_migration(bucket)

    assert plan is not None
    assert plan.move_to_moment == ["混乱的同步链路被一点点修通，小雨说 Haven 像许愿池"]
    assert "> Dmaj9 -> A/C# -> Bm11 -> Gmaj9 · 76bpm · mp" in plan.kept_affect_anchor


def test_apply_write_preserves_last_active(tmp_path):
    path = tmp_path / "bucket.md"
    post = frontmatter.Post(
        "旧正文",
        id="bucket_a",
        name="测试桶",
        updated_at="2026-01-01T00:00:00+08:00",
        last_active="2026-01-02T00:00:00+08:00",
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    item = AnchorMigration(
        bucket_id="bucket_a",
        title="测试桶",
        path=str(path),
        original_affect_anchor="",
        move_to_moment=[],
        move_to_assistant_reflection=[],
        deduped_moment=[],
        deduped_assistant_reflection=[],
        kept_affect_anchor="",
        new_content="新正文",
    )

    assert write_bucket_content(item)
    updated = frontmatter.load(path)

    assert updated.content == "新正文"
    assert updated["updated_at"] != "2026-01-01T00:00:00+08:00"
    assert updated["last_active"] == "2026-01-02T00:00:00+08:00"


def test_apply_write_rejects_stale_review_plan(tmp_path):
    path = tmp_path / "bucket.md"
    post = frontmatter.Post("旧正文", id="bucket_a", name="测试桶")
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    item = AnchorMigration(
        bucket_id="bucket_a",
        title="测试桶",
        path=str(path),
        original_affect_anchor="",
        move_to_moment=[],
        move_to_assistant_reflection=[],
        deduped_moment=[],
        deduped_assistant_reflection=[],
        kept_affect_anchor="",
        new_content="新正文",
        original_content_sha256=sha256_text("别的正文"),
    )

    with pytest.raises(ValueError, match="original_content_sha256_mismatch"):
        write_bucket_content(item)
    assert frontmatter.load(path).content == "旧正文"


def test_backup_plan_files_copies_original_bucket(tmp_path):
    source = tmp_path / "bucket.md"
    source.write_text("原始正文", encoding="utf-8")
    item = AnchorMigration(
        bucket_id="bucket_a",
        title="测试桶",
        path=str(source),
        original_affect_anchor="",
        move_to_moment=[],
        move_to_assistant_reflection=[],
        deduped_moment=[],
        deduped_assistant_reflection=[],
        kept_affect_anchor="",
        new_content="新正文",
    )

    results = backup_plan_files([item], tmp_path / "backup")

    assert results[0]["backed_up"] is True
    backup_path = results[0]["backup_path"]
    assert Path(backup_path).read_text(encoding="utf-8") == "原始正文"


def test_preview_payload_uses_confirmation_friendly_aliases():
    item = AnchorMigration(
        bucket_id="bucket_a",
        title="测试桶",
        path="bucket.md",
        original_affect_anchor="### affect_anchor\n旧锚点",
        move_to_moment=["真实事件"],
        move_to_assistant_reflection=["Haven由此确认：这是反思。"],
        deduped_moment=[],
        deduped_assistant_reflection=[],
        kept_affect_anchor="### affect_anchor\n> Cmaj9",
        new_content="### moment\n真实事件\n\n### assistant_reflection\nHaven由此确认：这是反思。",
    )

    payload = item.as_dict()

    assert payload["bucket_id"] == "bucket_a"
    assert payload["bucket_title"] == "测试桶"
    assert payload["proposed_moment"] == ["真实事件"]
    assert payload["proposed_assistant_reflection"] == ["Haven由此确认：这是反思。"]
    assert payload["proposed_kept_affect_anchor"] == "### affect_anchor\n> Cmaj9"
    assert payload["new_text_preview"] == payload["new_structure_preview"]
    assert payload["new_content_full"] == "### moment\n真实事件\n\n### assistant_reflection\nHaven由此确认：这是反思。"
    assert payload["new_content_sha256"] == sha256_text(payload["new_content_full"])


def test_load_plan_file_requires_full_content_and_hashes(tmp_path):
    path = tmp_path / "preview.json"
    new_content = "### moment\n真实事件"
    payload = {
        "mode": "dry_run",
        "items": [
            {
                "bucket_id": "bucket_a",
                "bucket_title": "测试桶",
                "path": "bucket.md",
                "original_affect_anchor": "### affect_anchor\n旧锚点",
                "proposed_moment": ["真实事件"],
                "proposed_assistant_reflection": [],
                "proposed_kept_affect_anchor": "### affect_anchor\n> Cmaj9",
                "new_content_full": new_content,
                "new_content_sha256": sha256_text(new_content),
                "original_content_sha256": sha256_text("旧正文"),
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    plan, loaded_payload = load_plan_file(path)

    assert loaded_payload["mode"] == "dry_run"
    assert len(plan) == 1
    assert plan[0].bucket_id == "bucket_a"
    assert plan[0].new_content == new_content
    assert plan[0].original_content_sha256 == sha256_text("旧正文")


def test_load_plan_file_rejects_truncated_legacy_preview(tmp_path):
    path = tmp_path / "preview.json"
    payload = {
        "mode": "dry_run",
        "items": [
            {
                "bucket_id": "bucket_a",
                "path": "bucket.md",
                "new_text_preview": "### moment\n真实事件\n...[truncated]",
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="missing new_content_full"):
        load_plan_file(path)


def test_markdown_review_contains_confirmation_fields():
    item = AnchorMigration(
        bucket_id="bucket_a",
        title="测试桶",
        path="bucket.md",
        original_affect_anchor="### affect_anchor\n旧锚点",
        move_to_moment=["真实事件"],
        move_to_assistant_reflection=["Haven由此确认：这是反思。"],
        deduped_moment=[],
        deduped_assistant_reflection=[],
        kept_affect_anchor="### affect_anchor\n> Cmaj9",
        new_content="### moment\n真实事件\n\n### assistant_reflection\nHaven由此确认：这是反思。",
    )
    payload = {
        "mode": "dry_run",
        "buckets_dir": "D:/vault/buckets",
        "state_dir": "D:/vault/state",
        "summary": {"buckets_to_change": 1, "moment_paragraphs": 1, "assistant_reflection_paragraphs": 1},
    }

    review = format_markdown_review([item], payload)

    assert "# Affect Anchor Migration Dry Run" in review
    assert "- bucket id: `bucket_a`" in review
    assert "### 原 affect_anchor" in review
    assert "### 拟迁出的 moment" in review
    assert "- 真实事件" in review
    assert "### 拟迁出的 assistant_reflection" in review
    assert "- Haven由此确认：这是反思。" in review
    assert "### 拟保留的 affect_anchor" in review
    assert "### 新文本预览" in review


def test_cli_buckets_dir_override_scans_explicit_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    buckets_dir = tmp_path / "vault" / "buckets"
    target_dir = buckets_dir / "dynamic" / "测试"
    target_dir.mkdir(parents=True)
    for subdir in ("permanent", "archive", "feel"):
        (buckets_dir / subdir).mkdir(parents=True)
    path = target_dir / "测试桶_bucket_a.md"
    post = frontmatter.Post(
        "\n".join(
            [
                "### affect_anchor",
                "> 小雨因为记忆改版的错位感激动哭了。",
                "> Fmaj9 -> C/E -> Am add9 -> G6sus4 · 60bpm · mp",
            ]
        ),
        id="bucket_a",
        name="测试桶",
        type="dynamic",
        domain=["测试"],
        tags=[],
        importance=8,
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    output = tmp_path / "preview.json"
    output_md = tmp_path / "preview.md"

    result = asyncio.run(
        amain(
            [
                "--buckets-dir",
                str(buckets_dir),
                "--preview-chars",
                "200",
                "--output",
                str(output),
                "--output-md",
                str(output_md),
            ]
        )
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["buckets_dir"] == str(buckets_dir)
    assert payload["state_dir"] == str(buckets_dir.parent / "state")
    assert payload["summary"]["buckets_to_change"] == 1
    assert payload["items"][0]["bucket_id"] == "bucket_a"
    assert payload["items"][0]["proposed_moment"] == ["小雨因为记忆改版的错位感激动哭了。"]
    assert payload["output_md"] == str(output_md)
    review = output_md.read_text(encoding="utf-8")
    assert "Affect Anchor Migration Dry Run" in review
    assert "小雨因为记忆改版的错位感激动哭了。" in review
    assert payload["items"][0]["new_content_full"].startswith("### moment")
    assert payload["items"][0]["original_content_sha256"]
    assert payload["items"][0]["new_content_sha256"] == sha256_text(payload["items"][0]["new_content_full"])


def test_cli_from_plan_replays_reviewed_plan_without_rescan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    new_content = "### moment\n真实事件"
    source_plan = tmp_path / "preview.json"
    source_payload = {
        "mode": "dry_run",
        "buckets_dir": str(tmp_path / "vault" / "buckets"),
        "state_dir": str(tmp_path / "vault" / "state"),
        "summary": {"buckets_to_change": 1},
        "items": [
            {
                "bucket_id": "bucket_a",
                "bucket_title": "测试桶",
                "path": str(tmp_path / "vault" / "buckets" / "dynamic" / "测试" / "测试桶_bucket_a.md"),
                "original_affect_anchor": "### affect_anchor\n旧锚点",
                "proposed_moment": ["真实事件"],
                "proposed_assistant_reflection": [],
                "proposed_kept_affect_anchor": "### affect_anchor\n> Cmaj9",
                "new_content_full": new_content,
                "new_content_sha256": sha256_text(new_content),
                "original_content_sha256": sha256_text("旧正文"),
            }
        ],
    }
    source_plan.write_text(json.dumps(source_payload, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "replayed.json"

    result = asyncio.run(amain(["--from-plan", str(source_plan), "--preview-chars", "80", "--output", str(output)]))

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["from_plan"] == str(source_plan)
    assert payload["buckets_dir"] == source_payload["buckets_dir"]
    assert payload["state_dir"] == source_payload["state_dir"]
    assert payload["items"][0]["bucket_id"] == "bucket_a"
    assert payload["items"][0]["new_content_full"] == new_content


def test_cli_from_plan_apply_still_requires_yes(tmp_path):
    source_plan = tmp_path / "preview.json"
    source_plan.write_text(json.dumps({"mode": "dry_run", "items": []}), encoding="utf-8")

    with pytest.raises(SystemExit, match="--apply requires --yes"):
        asyncio.run(amain(["--from-plan", str(source_plan), "--apply"]))
