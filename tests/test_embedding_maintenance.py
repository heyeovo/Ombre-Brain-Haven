import ast
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from embedding_engine import EmbeddingEngine
from embedding_maintenance import EmbeddingSweeper, refresh_with_retry


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def make_engine(tmp: str, api_key: str = "sk-test-secret") -> EmbeddingEngine:
    return EmbeddingEngine(
        {
            "buckets_dir": tmp,
            "embedding": {"api_key": api_key, "model": "test-model", "base_url": "http://127.0.0.1:9/v1"},
        }
    )


class FakeEmbeddings:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def create(self, model, input):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(data=[SimpleNamespace(embedding=outcome)])


class RefreshWithRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_until_success(self):
        results = [False, False, True]
        slept = []

        async def refresh(bucket_id):
            return results.pop(0)

        async def sleep(seconds):
            slept.append(seconds)

        ok = await refresh_with_retry(refresh, "b1", delays=(1.0, 2.0, 3.0), sleep=sleep)
        self.assertTrue(ok)
        self.assertEqual(slept, [1.0, 2.0])

    async def test_gives_up_after_all_attempts_and_treats_exceptions_as_failures(self):
        calls = []

        async def refresh(bucket_id):
            calls.append(bucket_id)
            if len(calls) == 1:
                raise RuntimeError("Event loop is closed")
            return False

        async def sleep(seconds):
            return None

        with self.assertLogs("ombre_brain.embedding", level="WARNING"):
            ok = await refresh_with_retry(refresh, "b1", delays=(0.1, 0.1), sleep=sleep)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)


class EmbeddingSweeperTest(unittest.IsolatedAsyncioTestCase):
    def sweeper(self, missing, refresh_results=None, enabled=True):
        engine = SimpleNamespace(enabled=enabled, missing_bucket_ids=lambda ids: [i for i in ids if i in missing])
        refreshed = []
        results = dict(refresh_results or {})

        async def list_ids():
            return ["a", "b", "c", "d", ""]

        async def refresh(bucket_id):
            refreshed.append(bucket_id)
            return results.get(bucket_id, True)

        return EmbeddingSweeper(engine, list_ids, refresh), refreshed

    async def test_refreshes_only_missing_within_limit(self):
        sweeper, refreshed = self.sweeper({"b", "c", "d"})
        result = await sweeper.run_once(limit=2)
        self.assertEqual(refreshed, ["b", "c"])
        self.assertEqual(result["checked"], 4)
        self.assertEqual(result["missing"], 3)
        self.assertEqual(result["refreshed"], 2)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(sweeper.last_result["status"], "ok")

    async def test_stops_early_when_api_looks_down(self):
        sweeper, refreshed = self.sweeper({"a", "b", "c", "d"}, {k: False for k in "abcd"})
        with self.assertLogs("ombre_brain.embedding", level="WARNING"):
            result = await sweeper.run_once(limit=None)
        self.assertEqual(refreshed, ["a", "b", "c"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["remaining"], 4)

    async def test_overrides_share_lock_and_busy_is_reported(self):
        sweeper, _ = self.sweeper({"a"})
        used = []

        async def other_list():
            used.append("list")
            return ["a"]

        async def other_refresh(bucket_id):
            used.append(bucket_id)
            return True

        await sweeper.run_once(list_bucket_ids=other_list, refresh=other_refresh)
        self.assertEqual(used, ["list", "a"])
        sweeper._lock.acquire()
        try:
            self.assertEqual((await sweeper.run_once())["status"], "busy")
        finally:
            sweeper._lock.release()

    async def test_disabled_engine_skips(self):
        sweeper, refreshed = self.sweeper({"a"}, enabled=False)
        self.assertEqual((await sweeper.run_once())["status"], "disabled")
        self.assertEqual(refreshed, [])


class EmbeddingEngineHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_recorded_redacted_and_success_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            error = RuntimeError("401 invalid key sk-test-secret")
            error.status_code = 401
            engine.client = SimpleNamespace(embeddings=FakeEmbeddings([error, [0.1, 0.2]]))

            with self.assertLogs("ombre_brain.embedding", level="WARNING"):
                self.assertFalse(await engine.generate_and_store("b1", "text"))
            health = engine.health_snapshot()
            self.assertEqual(health["consecutive_failures"], 1)
            self.assertIn("RuntimeError 401", health["last_error"])
            self.assertNotIn("sk-test-secret", health["last_error"])

            self.assertTrue(await engine.generate_and_store("b1", "text"))
            self.assertEqual(engine.health_snapshot()["consecutive_failures"], 0)
            self.assertIsNotNone(engine.health_snapshot()["last_success_at"])

    async def test_missing_bucket_ids_covers_missing_and_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            conn = sqlite3.connect(engine.db_path)
            conn.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?)",
                ("ok", json.dumps([0.1, 0.2]), "test-model", 2, "now"),
            )
            conn.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?)",
                ("stale", json.dumps([0.1, 0.2]), "old-model", 2, "now"),
            )
            conn.commit()
            conn.close()
            self.assertEqual(engine.missing_bucket_ids(["ok", "stale", "none"]), ["stale", "none"])


class EmbeddingEngineLoopClientTest(unittest.TestCase):
    def test_each_event_loop_gets_its_own_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            base = engine.client

            async def pick():
                return engine._client_for_running_loop()

            first = asyncio.run(pick())
            second = asyncio.run(pick())
            self.assertIs(first, base)
            self.assertIsNot(second, base)

            # Hot reload swaps the client: it rebinds to whichever loop uses it next.
            from openai import AsyncOpenAI

            engine.client = AsyncOpenAI(api_key="sk-new", base_url="http://127.0.0.1:9/v1")
            self.assertIs(asyncio.run(pick()), engine.client)


class ServerEmbeddingWiringTest(unittest.TestCase):
    def test_server_builds_a_single_module_embedding_engine(self):
        tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
        assignments = [
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "embedding_engine" for t in node.targets)
        ]
        # A second instance meant hot reload updated one engine while BucketManager kept the other.
        self.assertEqual(len(assignments), 1)


if __name__ == "__main__":
    unittest.main()
