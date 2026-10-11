import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway_state import GatewayStateStore


class GatewayStateInitializationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "gateway_state.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_failed_migration_rolls_back_and_releases_retained_connection(self):
        GatewayStateStore(self.db_path)
        connections = []
        original_connect = GatewayStateStore._connect

        def track_connect(store):
            conn = original_connect(store)
            connections.append(conn)
            return conn

        def fail_after_write(conn):
            conn.execute(
                "INSERT INTO cc_permission_config (id, payload) VALUES (?, ?)",
                ("migration-probe", "{}"),
            )
            raise sqlite3.OperationalError("injected migration failure")

        # Keep the exception and connection alive, as an event loop traceback can.
        with mock.patch.object(GatewayStateStore, "_connect", track_connect), mock.patch(
            "gateway_state.initialize_agent_wake_schema", side_effect=fail_after_write
        ):
            try:
                GatewayStateStore(self.db_path)
            except sqlite3.OperationalError as exc:
                retained_error = exc
            else:
                self.fail("migration should fail")

        self.assertIn("injected migration failure", str(retained_error))
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        conn = sqlite3.connect(self.db_path, timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.assertIsNone(conn.execute(
                "SELECT id FROM cc_permission_config WHERE id = 'migration-probe'"
            ).fetchone())
            conn.rollback()
        finally:
            conn.close()
        GatewayStateStore(self.db_path)
        GatewayStateStore(self.db_path)

    def test_failed_first_initialization_can_be_retried(self):
        with mock.patch(
            "gateway_state.initialize_agent_wake_schema",
            side_effect=sqlite3.OperationalError("injected migration failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                GatewayStateStore(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=0)
        try:
            for table in ("cc_personas", "conversation_slice_revisions"):
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?", (table,)
                ).fetchone())
        finally:
            conn.close()
        GatewayStateStore(self.db_path)
        GatewayStateStore(self.db_path)

    def test_concurrent_initializers_wait_before_schema_reads(self):
        GatewayStateStore(self.db_path)
        first_in_schema = threading.Event()
        release_first = threading.Event()
        second_connected = threading.Event()
        second_in_schema = threading.Event()
        db_path = self.db_path

        class FirstStore(GatewayStateStore):
            def _initialize_schema(self, conn):
                super()._initialize_schema(conn)
                first_in_schema.set()
                if not release_first.wait(4):
                    raise RuntimeError("test did not release first initializer")

        class SecondStore(GatewayStateStore):
            def _connect(self):
                conn = super()._connect()
                second_connected.set()
                return conn

            def _initialize_schema(self, conn):
                second_in_schema.set()
                super()._initialize_schema(conn)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(FirstStore, db_path)
            try:
                self.assertTrue(first_in_schema.wait(2))
                second = executor.submit(SecondStore, db_path)
                self.assertTrue(second_connected.wait(2))
                self.assertFalse(second_in_schema.wait(0.1))
                self.assertFalse(second.done())
            finally:
                release_first.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertTrue(second_in_schema.is_set())
        GatewayStateStore(db_path)


if __name__ == "__main__":
    unittest.main()
