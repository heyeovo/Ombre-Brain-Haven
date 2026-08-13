import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUCKET_MANAGER_SOURCE = (ROOT / "bucket_manager.py").read_text(encoding="utf-8")
SERVER_SOURCE = (ROOT / "server.py").read_text(encoding="utf-8")
DECAY_ENGINE_SOURCE = (ROOT / "decay_engine.py").read_text(encoding="utf-8")


class MemoryMaintenanceContractsTest(unittest.TestCase):
    def test_pin_transition_preserves_and_restores_previous_state(self):
        self.assertIn('post["importance_before_pin"] = int(post.get("importance", 5))', BUCKET_MANAGER_SOURCE)
        self.assertIn('post["type_before_pin"] = str(post.get("type") or "dynamic")', BUCKET_MANAGER_SOURCE)
        self.assertIn('kwargs["importance"] = int(post.get("importance_before_pin", 5))', BUCKET_MANAGER_SOURCE)
        self.assertIn('post["type"] = str(post.get("type_before_pin") or "dynamic")', BUCKET_MANAGER_SOURCE)
        self.assertIn('elif unpinning and not is_protected:', BUCKET_MANAGER_SOURCE)

    def test_journal_uses_isolated_detail_and_update_methods(self):
        self.assertIn("async def get_journal", BUCKET_MANAGER_SOURCE)
        self.assertIn("async def update_journal", BUCKET_MANAGER_SOURCE)
        self.assertIn('@mcp.custom_route("/api/journal/{journal_id}", methods=["GET", "PATCH", "DELETE"])', SERVER_SOURCE)
        self.assertIn("await bucket_mgr.update_journal(journal_id, **updates)", SERVER_SOURCE)

    def test_hold_journal_persists_title_and_event_time(self):
        self.assertIn("journal_title = title.strip()", SERVER_SOURCE)
        self.assertIn("journal_event_time = str(event_time or event_date or \"\").strip()", SERVER_SOURCE)
        self.assertIn("name=journal_title or None", SERVER_SOURCE)
        self.assertIn("event_time=journal_event_time", SERVER_SOURCE)

    def test_moment_diagnostics_include_cross_bucket_edges(self):
        self.assertIn('"cross_bucket_edges": cross_bucket_edges[:limit]', SERVER_SOURCE)

    def test_decay_cycle_never_changes_bucket_lifecycle(self):
        cycle_source = DECAY_ENGINE_SOURCE.split("async def run_decay_cycle", 1)[1].split(
            "async def ensure_started", 1
        )[0]
        self.assertNotIn("bucket_mgr.update", cycle_source)
        self.assertNotIn("bucket_mgr.archive", cycle_source)
        self.assertIn('"archived": archived', cycle_source)
        self.assertIn('"auto_resolved": auto_resolved', cycle_source)
        self.assertIn('"related_bucket_name": str(other_meta.get("name") or other_id)', SERVER_SOURCE)


if __name__ == "__main__":
    unittest.main()
