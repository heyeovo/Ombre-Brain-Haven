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


if __name__ == "__main__":
    unittest.main()
