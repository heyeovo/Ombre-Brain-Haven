import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from prompt_store import (  # noqa: E402
    EmptyPromptError,
    PromptConflictError,
    PromptStore,
    PromptStoreError,
    UnknownPromptError,
)


class PromptStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "prompt_overrides.sqlite"
        self.defaults = {"analyze": "系统打标默认", "merge": "系统合并默认"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_store(self):
        return PromptStore({}, self.defaults, db_path=str(self.db_path), profile_id="p1")

    def test_default_save_restart_and_reset(self):
        store = self.make_store()
        self.assertEqual(store.describe("analyze")["source"], "system_default")

        saved = store.save("analyze", "用户打标偏好", expected_revision=0)
        self.assertTrue(saved["customized"])
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(store.get_effective("analyze"), "用户打标偏好")

        restarted = self.make_store()
        self.assertEqual(restarted.get_effective("analyze"), "用户打标偏好")
        reset = restarted.reset("analyze", expected_revision=1)
        self.assertFalse(reset["customized"])
        self.assertEqual(reset["content"], "系统打标默认")

    def test_rejects_unknown_empty_and_stale_revision(self):
        store = self.make_store()
        with self.assertRaises(UnknownPromptError):
            store.save("missing", "正文")
        with self.assertRaises(EmptyPromptError):
            store.save("analyze", "   ")
        with self.assertRaises(PromptStoreError):
            store.save("analyze", {"not": "text"})
        store.save("analyze", "第一版", expected_revision=0)
        with self.assertRaises(PromptConflictError):
            store.save("analyze", "过期覆盖", expected_revision=0)
        self.assertEqual(store.get_effective("analyze"), "第一版")

    def test_old_table_is_upgraded_idempotently(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE prompt_overrides (profile_id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(profile_id, name))"
        )
        conn.execute("INSERT INTO prompt_overrides(profile_id, name, content) VALUES('p1', 'merge', '旧覆盖')")
        conn.commit()
        conn.close()

        first = self.make_store()
        second = self.make_store()
        self.assertEqual(first.get_effective("merge"), "旧覆盖")
        self.assertEqual(second.describe("merge")["revision"], 1)

    def test_profiles_are_isolated(self):
        first = self.make_store()
        second = PromptStore({}, self.defaults, db_path=str(self.db_path), profile_id="p2")
        first.save("merge", "p1 覆盖")
        self.assertEqual(second.get_effective("merge"), "系统合并默认")


if __name__ == "__main__":
    unittest.main()
