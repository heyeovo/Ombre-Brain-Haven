import tempfile
import unittest
from pathlib import Path

try:
    from bucket_manager import BucketManager
    from decay_engine import DecayEngine
    MISSING_RUNTIME_DEPENDENCY = ""
except ModuleNotFoundError as exc:  # Lightweight local validation runtime.
    BucketManager = None
    DecayEngine = None
    MISSING_RUNTIME_DEPENDENCY = str(exc)


@unittest.skipIf(BucketManager is None, f"Haven runtime dependency missing: {MISSING_RUNTIME_DEPENDENCY}")
class PinTransitionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = {"buckets_dir": str(self.root / "buckets")}
        self.manager = BucketManager(self.config)
        self.decay = DecayEngine(self.config, self.manager)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_unpin_restores_dynamic_type_and_normal_weight(self):
        bucket_id = await self.manager.create(
            content="一条普通记忆",
            name="普通记忆",
            importance=6,
            domain=["生活"],
        )
        self.assertTrue(await self.manager.update(bucket_id, pinned=True))
        pinned = await self.manager.get(bucket_id)
        self.assertTrue(pinned["metadata"]["pinned"])
        self.assertEqual(pinned["metadata"]["type"], "permanent")
        self.assertEqual(self.decay.calculate_score(pinned["metadata"]), 999.0)

        self.assertTrue(await self.manager.update(bucket_id, pinned=False))
        restored = await self.manager.get(bucket_id)
        self.assertFalse(restored["metadata"]["pinned"])
        self.assertEqual(restored["metadata"]["type"], "dynamic")
        self.assertEqual(restored["metadata"]["importance"], 6)
        self.assertNotEqual(self.decay.calculate_score(restored["metadata"]), 999.0)

    async def test_legacy_pinned_bucket_without_backup_falls_back_to_normal_weight(self):
        bucket_id = await self.manager.create(
            content="旧钉选记忆",
            name="旧钉选记忆",
            pinned=True,
            domain=["生活"],
        )
        self.assertTrue(await self.manager.update(bucket_id, pinned=False))
        restored = await self.manager.get(bucket_id)
        self.assertFalse(restored["metadata"]["pinned"])
        self.assertEqual(restored["metadata"]["type"], "dynamic")
        self.assertEqual(restored["metadata"]["importance"], 5)
        self.assertNotEqual(self.decay.calculate_score(restored["metadata"]), 999.0)

    async def test_feel_pin_is_selection_only_and_never_changes_type_or_importance(self):
        bucket_id = await self.manager.create(
            content="一条独立感受",
            name="独立感受",
            importance=5,
            bucket_type="feel",
            domain=[],
        )

        self.assertTrue(await self.manager.update(bucket_id, pinned=True))
        pinned = await self.manager.get(bucket_id)
        self.assertTrue(pinned["metadata"]["pinned"])
        self.assertEqual(pinned["metadata"]["type"], "feel")
        self.assertEqual(pinned["metadata"]["importance"], 5)
        self.assertEqual(Path(pinned["path"]).parent.name, "沉淀物")
        self.assertEqual(Path(pinned["path"]).parent.parent.name, "feel")

        self.assertTrue(await self.manager.update(bucket_id, pinned=False))
        restored = await self.manager.get(bucket_id)
        self.assertFalse(restored["metadata"]["pinned"])
        self.assertEqual(restored["metadata"]["type"], "feel")
        self.assertEqual(restored["metadata"]["importance"], 5)
        self.assertEqual(Path(restored["path"]).parent.name, "沉淀物")
        self.assertEqual(Path(restored["path"]).parent.parent.name, "feel")

    async def test_feel_created_pinned_keeps_normal_feel_metadata(self):
        bucket_id = await self.manager.create(
            content="默认常驻的感受",
            name="默认常驻感受",
            importance=5,
            bucket_type="feel",
            pinned=True,
            domain=[],
        )
        bucket = await self.manager.get(bucket_id)
        self.assertTrue(bucket["metadata"]["pinned"])
        self.assertEqual(bucket["metadata"]["type"], "feel")
        self.assertEqual(bucket["metadata"]["importance"], 5)
        self.assertEqual(Path(bucket["path"]).parent.name, "沉淀物")
        self.assertEqual(Path(bucket["path"]).parent.parent.name, "feel")

    async def test_legacy_feel_marker_restores_feel_on_unpin(self):
        bucket_id = await self.manager.create(
            content="旧版误改类型的感受",
            name="旧版感受",
            importance=10,
            bucket_type="permanent",
            pinned=True,
            tags=["feel"],
            domain=[],
        )

        self.assertTrue(await self.manager.update(bucket_id, pinned=False))
        restored = await self.manager.get(bucket_id)
        self.assertFalse(restored["metadata"]["pinned"])
        self.assertEqual(restored["metadata"]["type"], "feel")
        self.assertEqual(restored["metadata"]["importance"], 5)
        self.assertEqual(Path(restored["path"]).parent.name, "沉淀物")
        self.assertEqual(Path(restored["path"]).parent.parent.name, "feel")


if __name__ == "__main__":
    unittest.main()
