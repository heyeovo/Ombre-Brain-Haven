import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import frontmatter

from bucket_manager import BucketManager
from reflection_engine import ReflectionEngine
from scripts.find_misconverted_feels import scan


class FeelIdentityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = BucketManager({"buckets_dir": self.tmp.name})

    async def test_semantic_markers_never_change_normal_identity(self):
        for kind in ("dynamic", "permanent"):
            for marker in ("feel", "沉淀物"):
                with self.subTest(kind=kind, marker=marker):
                    bid = await self.manager.create(
                        content="日记和feel都断了", bucket_type=kind,
                        tags=[marker], domain=[marker],
                    )
                    original = await self.manager.get(bid)
                    self.assertTrue(await self.manager.update(bid, importance=6))
                    updated = await self.manager.get(bid)
                    self.assertEqual(updated["metadata"]["type"], kind)
                    self.assertEqual(updated["path"], original["path"])

    async def test_archive_restore_identity_without_tags_and_repeated_archive(self):
        for restore in ("unarchive", "activate"):
            for kind in ("dynamic", "feel"):
                with self.subTest(restore=restore, kind=kind):
                    bid = await self.manager.create(
                        content="感受", bucket_type=kind,
                        tags=["feel"] if kind == "dynamic" else [],
                    )
                    if kind == "feel":
                        await self.manager.update(bid, pinned=True)
                        await self.manager.update(bid, pinned=False)
                    self.assertTrue(await self.manager.archive(bid))
                    self.assertTrue(await self.manager.archive(bid))
                    self.assertTrue(await self.manager.update(bid, confidence=0.8))
                    archived = await self.manager.get(bid)
                    self.assertEqual(archived["metadata"]["type"], "archived")
                    self.assertEqual(archived["metadata"]["type_before_archive"], kind)
                    self.assertTrue(await getattr(self.manager, restore)(bid))
                    restored = await self.manager.get(bid)
                    self.assertEqual(restored["metadata"]["type"], kind)
                    root = self.manager.feel_dir if kind == "feel" else self.manager.dynamic_dir
                    self.assertTrue(Path(restored["path"]).is_relative_to(root))
                    self.assertNotIn("type_before_archive", restored["metadata"])

    async def test_legacy_tag_only_archive_is_not_guessed_as_feel(self):
        bid = await self.manager.create(content="旧归档普通桶", tags=["feel"])
        await self.manager.archive(bid)
        bucket = await self.manager.get(bid)
        post = frontmatter.load(bucket["path"])
        post.metadata.pop("type_before_archive")
        Path(bucket["path"]).write_text(frontmatter.dumps(post))
        self.assertTrue(await self.manager.unarchive(bid))
        restored = await self.manager.get(bid)
        self.assertEqual(restored["metadata"]["type"], "dynamic")

    async def test_normal_pin_cycle_with_feel_topics_stays_normal(self):
        bid = await self.manager.create(content="普通桶", tags=["feel"], domain=["沉淀物"])
        await self.manager.update(bid, pinned=True)
        pinned = await self.manager.get(bid)
        self.assertEqual(pinned["metadata"]["type"], "permanent")
        self.assertEqual(pinned["metadata"]["type_before_pin"], "dynamic")
        await self.manager.update(bid, pinned=False)
        restored = await self.manager.get(bid)
        self.assertEqual(restored["metadata"]["type"], "dynamic")
        self.assertTrue(Path(restored["path"]).is_relative_to(self.manager.dynamic_dir))

    async def test_enrich_filters_reserved_labels_and_preserves_existing_topics(self):
        bid = await self.manager.create(content="日记和feel都断了", tags=["feel"])
        original = await self.manager.get(bid)
        engine = ReflectionEngine({})
        engine.client = Mock()
        engine._candidate_buckets = AsyncMock(return_value=[])
        engine._api_classify = AsyncMock(return_value={
            "tags": ["feel", " FEEL ", "沉淀物", "journal", "日记", "dynamic",
                     "emotional_echo"],
            "confidence": 0.8,
        })
        edges = Mock()
        edges.add_edges.return_value = []
        result = await engine.enrich_bucket(bid, self.manager, edges)
        self.assertEqual(result["tags"], ["emotional_echo"])
        await self.manager.update(bid, confidence=0.9)
        updated = await self.manager.get(bid)
        self.assertEqual(updated["metadata"]["type"], "dynamic")
        self.assertEqual(updated["path"], original["path"])
        self.assertEqual(updated["metadata"]["tags"], ["feel", "emotional_echo"])

    async def test_audit_is_read_only_and_reports_evidence(self):
        bid = await self.manager.create(content="【事件·生活】\n日记和feel都断了", bucket_type="feel",
                                        extra_metadata={"type_before_pin": "dynamic"})
        bucket = await self.manager.get(bid)
        path = Path(bucket["path"])
        before = path.read_bytes()
        rows = list(scan(Path(self.manager.feel_dir)))
        self.assertEqual(rows[0]["id"], bid)
        self.assertIn("event_heading", rows[0]["reasons"])
        self.assertIn("explicit_non_feel_type_before_pin", rows[0]["reasons"])
        self.assertEqual(path.read_bytes(), before)
        post = frontmatter.load(path)
        post.content = "独立感受"
        post["source"] = "hold"
        post.metadata.pop("type_before_pin")
        path.write_text(frontmatter.dumps(post))
        self.assertEqual(list(scan(Path(self.manager.feel_dir))), [])
