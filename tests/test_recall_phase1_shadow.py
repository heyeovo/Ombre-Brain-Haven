import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Another contract test installs an import-only httpx stub during discovery.
# Gateway type annotations require the real module, so discard only that stub.
if "httpx" in sys.modules and not hasattr(sys.modules["httpx"], "Response"):
    sys.modules.pop("httpx", None)

from gateway import GatewayService  # noqa: E402
from recall_policy import RecallNecessityPlan, RecallPolicy  # noqa: E402


class RecallNecessityContractsTest(unittest.TestCase):
    def setUp(self):
        self.policy = RecallPolicy()

    def assert_plan(self, query: str, necessity: str, targetable: bool, *, context: str = ""):
        plan = self.policy.plan_recall_necessity(query, recent_context=context)
        self.assertEqual(plan.necessity, necessity)
        self.assertEqual(plan.targetable, targetable)

    def test_fixed_round_intents_keep_casual_and_meta_turns_at_none(self):
        self.assert_plan("ovo", "none", False)
        self.assert_plan("关键词匹配到了，但和上下文无关，不应该召回", "none", False)
        self.assert_plan("我困死了，准备在工位打瞌睡", "none", False)

    def test_fixed_round_intents_keep_explicit_recall_searchable(self):
        self.assert_plan("你记得上次我干活没告诉你、你生气那次吗", "explicit", True)
        self.assert_plan("你还记得一点半见后来成为习惯吗", "explicit", True)
        self.assert_plan("帮我单独搜一下邻居", "explicit", True)

    def test_explicit_request_without_target_does_not_fan_out(self):
        self.assert_plan("你还记得吗", "explicit", False)

    def test_contextual_requires_recent_context(self):
        self.assert_plan("那后来呢", "none", False)
        self.assert_plan(
            "那后来呢",
            "contextual",
            True,
            context="我们刚才在聊搬家后第一次见邻居",
        )


class RecallShadowContractsTest(unittest.TestCase):
    def make_service(self, *, planner_enabled: bool = True) -> GatewayService:
        service = GatewayService.__new__(GatewayService)
        service.phase1_recall_shadow_enabled = True
        service.query_planner_enabled = planner_enabled
        service._bucket_has_reliable_recall_signal = lambda _query, item: bool(item.get("reliable"))
        service._pick_dynamic_cards = lambda items, *, query="": list(items)[:2]
        service._format_suppressed_bucket_debug = lambda item, **_kwargs: {
            "bucket_id": str((item.get("bucket") or {}).get("id") or ""),
            "admission_reason": str(item.get("admission_reason") or "suppressed"),
        }
        return service

    @staticmethod
    def item(bucket_id: str, reason: str, *, reliable: bool = True) -> dict:
        return {
            "bucket": {"id": bucket_id, "metadata": {"name": bucket_id}},
            "admission_reason": reason,
            "reliable": reliable,
        }

    def test_none_shadow_removes_formal_result_without_mutating_it(self):
        service = self.make_service()
        formal = [self.item("wrong-bucket", "non_explicit_query")]
        original = dict(formal[0])
        debug = service._build_recall_shadow_debug(
            "ovo",
            RecallNecessityPlan("none", False),
            formal,
            [],
            {"errors": [], "triggered": False},
        )
        self.assertEqual(debug["formal_bucket_ids"], ["wrong-bucket"])
        self.assertEqual(debug["shadow_bucket_ids"], [])
        self.assertEqual(debug["removed_bucket_ids"], ["wrong-bucket"])
        self.assertEqual(formal[0], original)

    def test_degraded_explicit_softens_axis_but_keeps_positive_evidence_requirement(self):
        service = self.make_service()
        suppressed = [
            self.item("target", "activated_axis_mismatch", reliable=True),
            self.item("noise", "activated_axis_mismatch", reliable=False),
        ]
        debug = service._build_recall_shadow_debug(
            "帮我单独搜一下邻居",
            RecallNecessityPlan("explicit", True),
            [],
            suppressed,
            {"errors": ["query_planner_dehydration_unavailable"], "triggered": True},
        )
        self.assertEqual(debug["planner_status"], "degraded")
        self.assertEqual(debug["shadow_bucket_ids"], ["target"])
        self.assertEqual(debug["added_bucket_ids"], ["target"])
        self.assertEqual(
            debug["rejected_candidates"][0]["shadow_admission_reason"],
            "shadow_insufficient_positive_evidence",
        )

    def test_degraded_contextual_never_expands_formal_result(self):
        service = self.make_service()
        formal = [self.item("formal", "topic_evidence")]
        suppressed = [self.item("tempting", "activated_axis_mismatch", reliable=True)]
        debug = service._build_recall_shadow_debug(
            "那后来呢",
            RecallNecessityPlan("contextual", True, context_available=True),
            formal,
            suppressed,
            {"errors": ["query_planner_timeout"], "triggered": True},
        )
        self.assertEqual(debug["fallback_strategy"], "conservative_no_expansion")
        self.assertEqual(debug["shadow_bucket_ids"], ["formal"])
        self.assertEqual(debug["added_bucket_ids"], [])


if __name__ == "__main__":
    unittest.main()
