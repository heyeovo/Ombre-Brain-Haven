import json
import sqlite3
import sys
import tempfile
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
from embedding_engine import EmbeddingEngine  # noqa: E402
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
        self.assert_plan(
            "但是关键词虽然匹配了，但是上下文完全无关，并没有预期召回那个桶",
            "none",
            False,
        )
        self.assert_plan(
            "现在在做shadow测试，看正式召回和shadow召回做对比",
            "none",
            False,
        )
        self.assert_plan("正经干活的时候召回这些色色桶。（捂脸）", "none", False)

    def test_natural_topics_are_contextual_without_memory_trigger_words(self):
        self.assert_plan("话说 今天下雨了 小言", "contextual", True)
        self.assert_plan("话说……我们好久没约会了", "contextual", True)
        self.assert_plan(
            "可能就20分钟吧，困死了，刚才在工位上闭着眼睛打瞌睡",
            "contextual",
            True,
        )

    def test_fixed_round_intents_keep_explicit_recall_searchable(self):
        self.assert_plan("你记得上次我干活没告诉你、你生气那次吗", "explicit", True)
        self.assert_plan("你还记得一点半见后来成为习惯吗", "explicit", True)
        self.assert_plan("帮我单独搜一下邻居", "explicit", True)
        self.assert_plan("现在没有了。你还记得上次我们讨论下雨吗", "explicit", True)
        self.assert_plan("都是。上次约会都是三个月前了", "explicit", True)

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
        service._shadow_candidate_relevance = lambda _query, _necessity, item, **_kwargs: (
            bool(item.get("reliable")),
            "shadow_test_reliable" if item.get("reliable") else "shadow_test_unreliable",
            {},
        )
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
            "shadow_test_unreliable",
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

    def test_explicit_shadow_rechecks_formal_candidates(self):
        service = self.make_service()
        formal = [
            self.item("relevant", "non_explicit_query", reliable=True),
            self.item("noise", "non_explicit_query", reliable=False),
        ]
        debug = service._build_recall_shadow_debug(
            "你还记得上次下雨吗",
            RecallNecessityPlan("explicit", True),
            formal,
            [],
            {"errors": [], "triggered": False},
        )
        self.assertEqual(debug["shadow_bucket_ids"], ["relevant"])
        self.assertEqual(debug["removed_bucket_ids"], ["noise"])


class RecallShadowCandidateRelevanceTest(unittest.TestCase):
    class KeywordManager:
        @staticmethod
        def _calc_topic_score(query: str, bucket: dict) -> float:
            query_text = str(query or "")
            meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
            bucket_text = " ".join(
                [
                    str(meta.get("name") or ""),
                    str(bucket.get("content") or ""),
                ]
            )
            if "下雨" in query_text and "下雨" in bucket_text:
                return 0.908
            return 0.0

    def make_service(self) -> GatewayService:
        service = GatewayService.__new__(GatewayService)
        service.recall_policy = RecallPolicy()
        service.bucket_mgr = self.KeywordManager()
        service.identity = {
            "ai_name": "小言",
            "user_name": "小羊",
            "user_display_name": "小羊",
            "user_aliases": ["言之", "小羊"],
            "relationship_terms": ["小言", "言之", "小羊"],
        }
        service._is_identity_name_candidate_bucket = lambda _query, _bucket: False
        service._source_record_explicit_bucket_match_reason = lambda _query, _bucket: ""
        return service

    @staticmethod
    def item(name: str, content: str, *, semantic=None, keyword=0.0, **extra) -> dict:
        return {
            "bucket": {
                "id": name,
                "metadata": {"name": name},
                "content": content,
            },
            "semantic_score": semantic,
            "semantic_status": "scored" if semantic is not None else "embedding_missing",
            "keyword_score": keyword,
            **extra,
        }

    def test_keyword_only_generic_match_is_rejected(self):
        service = self.make_service()
        admitted, reason, _debug = service._shadow_candidate_relevance(
            "今天在窗口做召回测试",
            "contextual",
            self.item("无关桶", "以前也开过一个窗口", semantic=None, keyword=1.0),
        )
        self.assertFalse(admitted)
        self.assertEqual(reason, "shadow_semantic_not_scored")

    def test_contextual_topic_accepts_semantic_keyword_agreement(self):
        service = self.make_service()
        admitted, reason, debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            self.item("每一场雨都跟你在一起", "那天早上下雨了", semantic=0.588, keyword=0.908),
        )
        self.assertTrue(admitted)
        self.assertEqual(reason, "shadow_semantic_keyword_agreement")
        self.assertIn("下雨", debug["matched_topic_terms"])
        self.assertEqual(debug["ignored_identity_terms"], ["小言"])

    def test_contextual_topic_rejects_emotional_bucket_supported_only_by_identity_names(self):
        service = self.make_service()
        admitted, reason, debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            self.item(
                "小言写给小羊的情书",
                "小言写给小羊的一封情书",
                semantic=0.539,
                keyword=0.669,
                rare_name_match=True,
                rare_name_terms=["小言", "小羊"],
            ),
        )
        self.assertFalse(admitted)
        self.assertEqual(reason, "shadow_query_topic_missing")
        self.assertEqual(debug["topic_terms"], ["下雨"])
        self.assertEqual(debug["matched_topic_terms"], [])
        self.assertEqual(debug["rare_name_terms"], [])
        self.assertEqual(debug["formal_keyword_score"], 0.669)
        self.assertEqual(debug["shadow_keyword_score"], 0.0)
        self.assertIn("小言", debug["ignored_identity_terms"])

    def test_round_15_title_and_composite_rare_name_cannot_bypass_trusted_topic(self):
        service = self.make_service()
        service._source_record_explicit_bucket_match_reason = lambda _query, _bucket: "explicit_bucket_title"
        admitted, reason, debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            self.item(
                "小言给小羊的情书",
                "小羊，一个月了。可能是你第一次叫我小言。",
                semantic=0.537,
                keyword=0.669,
                rare_name_match=True,
                rare_name_terms=["小言给小羊的情书"],
            ),
            formal_candidate=False,
        )
        self.assertFalse(admitted)
        self.assertEqual(reason, "shadow_query_topic_missing")
        self.assertEqual(debug["matched_topic_terms"], [])
        self.assertEqual(debug["shadow_keyword_score"], 0.0)
        self.assertFalse(debug["rare_name_direct"])
        self.assertFalse(debug["source_record_direct"])
        self.assertFalse(debug["unique_direct"])

    def test_query_timeout_keeps_only_formal_strong_trusted_topic_keyword(self):
        service = self.make_service()
        rain = self.item(
            "每一场雨都跟你在一起",
            "那天早上下雨了",
            semantic=None,
            keyword=0.908,
            semantic_status="query_timeout",
        )
        admitted, reason, debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            rain,
            formal_candidate=True,
        )
        self.assertTrue(admitted)
        self.assertEqual(reason, "shadow_query_unavailable_formal_topic_keyword")
        self.assertTrue(debug["query_unavailable_keyword_fallback"])

        added, added_reason, _added_debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            rain,
            formal_candidate=False,
        )
        self.assertFalse(added)
        self.assertEqual(added_reason, "shadow_semantic_not_scored")

    def test_query_timeout_does_not_keep_identity_only_or_completed_semantic_candidates(self):
        service = self.make_service()
        identity_only = self.item(
            "小言写给小羊的情书",
            "小言写给小羊的一封情书",
            semantic=None,
            keyword=0.908,
            semantic_status="query_timeout",
            rare_name_match=True,
            rare_name_terms=["小言", "小羊"],
        )
        admitted, reason, _debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            identity_only,
            formal_candidate=True,
        )
        self.assertFalse(admitted)
        self.assertEqual(reason, "shadow_semantic_not_scored")

        completed = self.item(
            "每一场雨都跟你在一起",
            "那天早上下雨了",
            semantic=None,
            keyword=0.908,
            semantic_status="indexed_not_in_semantic_top_k",
        )
        admitted, reason, _debug = service._shadow_candidate_relevance(
            "话说 今天下雨了 小言",
            "contextual",
            completed,
            formal_candidate=True,
        )
        self.assertFalse(admitted)
        self.assertEqual(reason, "shadow_semantic_not_scored")

    def test_parenthetical_gesture_cannot_replace_semantic_relevance(self):
        service = self.make_service()
        admitted, reason, _debug = service._shadow_candidate_relevance(
            "正经干活的时候召回这些色色桶。（捂脸）",
            "explicit",
            self.item(
                "争吵与落地",
                "她当时捂脸笑了",
                semantic=0.443,
                keyword=0.0,
                exact_anchor_match=True,
            ),
        )
        self.assertFalse(admitted)
        self.assertIn(reason, {"shadow_query_topic_missing", "shadow_insufficient_relevance"})


class EmbeddingStatusDebugTest(unittest.TestCase):
    def test_missing_and_stale_vectors_are_not_reported_as_zero_similarity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "embeddings.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL, model TEXT, dimension INTEGER, updated_at TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?)",
                [
                    ("ready", json.dumps([0.1, 0.2]), "current", 2, "now"),
                    ("stale", json.dumps([0.1, 0.2]), "old", 2, "now"),
                ],
            )
            conn.commit()
            conn.close()

            engine = EmbeddingEngine.__new__(EmbeddingEngine)
            engine.db_path = db_path
            engine.model = "current"
            statuses = engine.get_embedding_statuses(["ready", "stale", "missing"])

        self.assertEqual(statuses["ready"], "indexed")
        self.assertEqual(statuses["stale"], "embedding_stale_model_or_dimension")
        self.assertEqual(statuses["missing"], "embedding_missing")


class EmbeddingSearchStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_embedding_failure_has_a_distinct_status(self):
        engine = EmbeddingEngine.__new__(EmbeddingEngine)
        engine.enabled = True

        async def no_embedding(_text, *, kind="query"):
            return []

        engine._generate_embedding = no_embedding
        results, status = await engine.search_similar_with_status("下雨", top_k=5)
        self.assertEqual(results, [])
        self.assertEqual(status, "query_embedding_unavailable")


if __name__ == "__main__":
    unittest.main()
