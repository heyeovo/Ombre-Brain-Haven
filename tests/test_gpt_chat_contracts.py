import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from gateway_state import GatewayStateStore, SessionStateConflictError, ConversationConflictError
from gpt_chat_requests import GPTChatRequests
from gateway import GatewayService
from starlette.requests import Request


class GPTChatContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.db")
        self.store = GatewayStateStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, profile="default", **updates):
        return self.store.patch_conversation_session_state(
            profile_id=profile, session_id="s", persona_id="ombre", updates=updates)

    def test_old_schema_upgrade_repeat_init_and_reopen(self):
        # Simulate the pre-GPT schema, preserving a real old session.
        self.save(selfhost_overrides={"model": "old"})
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE conversation_sessions DROP COLUMN gpt_subscription_overrides_json")
        conn.close()
        self.store = GatewayStateStore(self.path)
        GatewayStateStore(self.path)
        state = self.store.get_conversation_session_state(profile_id="default", session_id="s")
        self.assertEqual(state["gpt_subscription_overrides"], {})
        self.assertEqual(state["selfhost_overrides"], {"model": "old"})
        self.save(local_engine_preference="gpt_subscription", gpt_subscription_overrides={"model": "gpt-test"})
        state = GatewayStateStore(self.path).get_conversation_session_state(profile_id="default", session_id="s")
        self.assertEqual(state["local_engine_preference"], "gpt_subscription")
        self.assertEqual(state["gpt_subscription_overrides"], {"model": "gpt-test"})

    def test_preferences_preserve_other_engines_and_profile_isolation(self):
        self.save(selfhost_overrides={"model": "old"}, gpt_subscription_overrides={"model": "gpt-a"})
        self.save("another", gpt_subscription_overrides={"model": "gpt-b"})
        state = self.save(mode="chat")
        self.assertEqual(state["gpt_subscription_overrides"]["model"], "gpt-a")
        self.assertEqual(state["selfhost_overrides"]["model"], "old")
        self.assertEqual(self.save("another", mode="chat")["gpt_subscription_overrides"]["model"], "gpt-b")
        self.assertEqual(self.save(gpt_subscription_overrides={})["gpt_subscription_overrides"], {})

    def test_preference_cas_and_validation(self):
        self.save(gpt_subscription_overrides={"model": "gpt-a"})
        with self.assertRaises(SessionStateConflictError):
            self.store.patch_conversation_session_state(profile_id="default", session_id="s", persona_id="ombre",
                updates={"gpt_subscription_overrides": {"model": "gpt-b"}}, expected_state_version=0)
        for value in ({"token": "no"}, {"model": 123}, [], {"model": "x" * 201}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save(gpt_subscription_overrides=value)

    @staticmethod
    def claim(request_id="r", session_id="s", owner="a" * 32, fingerprint="b" * 64):
        return dict(action="claim", request_id=request_id, session_id=session_id, owner=owner, fingerprint=fingerprint)

    def test_receipt_reopen_never_repeats_same_request_and_checks_identity(self):
        requests = GPTChatRequests(self.path)
        self.assertTrue(requests.control("p", self.claim())["ok"])
        requests = GPTChatRequests(self.path)
        self.assertEqual(requests.control("p", self.claim())["code"], "request_already_admitted")
        self.assertEqual(requests.control("p", self.claim(fingerprint="c" * 64))["code"], "request_id_reused")
        self.assertEqual(requests.control("p", self.claim(session_id="other"))["code"], "request_id_reused")
        self.assertTrue(requests.control("other-profile", self.claim())["ok"])

    def test_atomic_cross_connection_claims_only_one_session_request(self):
        GPTChatRequests(self.path)
        def claim(i):
            return GPTChatRequests(self.path).control("p", self.claim(request_id=f"r{i}"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(next(r["code"] for r in results if not r["ok"]), "conversation_busy")

    def test_expired_receipt_unknown_not_rerunnable_and_finish_owner_check(self):
        requests = GPTChatRequests(self.path)
        with patch("gpt_chat_requests.time.time", return_value=0):
            requests.control("p", self.claim())
        self.assertEqual(requests.control("p", dict(action="status", request_id="r", session_id="s"))["status"], "interrupted")
        self.assertEqual(requests.control("p", self.claim())["code"], "request_already_admitted")
        self.assertEqual(requests.control("p", dict(action="finish", request_id="r", session_id="s", owner="c" * 32))["code"], "request_owner_conflict")
        self.assertTrue(requests.control("p", self.claim(request_id="new"))["ok"])

    def test_finished_receipt_unlocks_session_but_not_request(self):
        requests = GPTChatRequests(self.path)
        requests.control("p", self.claim())
        self.assertTrue(requests.control("p", dict(action="finish", request_id="r", session_id="s", owner="a" * 32))["ok"])
        self.assertEqual(requests.control("p", self.claim())["code"], "request_already_admitted")
        self.assertTrue(requests.control("p", self.claim(request_id="new"))["ok"])

    def test_gpt_turn_replay_history_and_daily_projection_use_existing_tables(self):
        arguments = dict(profile_id="p", session_id="s", persona_id="ombre", request_id="r",
                         expected_last_round_id=0, user_text="问题", assistant_text="部分回复",
                         source="gpt_subscription", model="gpt-test",
                         raw_json=json.dumps({"engine": "gpt_subscription", "outcome": "cancelled", "usage": None}))
        first = self.store.commit_conversation_turn(**arguments)
        replay = self.store.commit_conversation_turn(**arguments)
        self.assertEqual(first["turn"]["id"], replay["turn"]["id"])
        turn = self.store.get_conversation_turn_by_request_id(profile_id="p", request_id="r")
        self.assertEqual(turn["source"], "gpt_subscription")
        self.assertEqual(turn["assistant_text"], "部分回复")
        self.assertIsNone(json.loads(turn["raw_json"])["usage"])
        self.assertEqual(self.store.get_conversation_turn_by_request_id(profile_id="other", request_id="r"), {})
        days = self.store.list_conversation_context_days(profile_id="p", session_id="s", persona_id="ombre")
        self.assertEqual(len(days), 1)
        self.assertGreater(days[0]["token_estimate"]["conversation"], 0)
        with self.assertRaises(ConversationConflictError):
            self.store.commit_conversation_turn(**{**arguments, "request_id": "another"})


    def test_image_only_gpt_turn_reopens_and_replays_existing_attachment_binding(self):
        image = self.store.create_conversation_attachment(
            profile_id="p", session_id="s", filename="image.png", data=b"\x89PNG\r\n\x1a\n")
        arguments = dict(profile_id="p", session_id="s", persona_id="ombre", request_id="image-request",
                         expected_last_round_id=0, user_text="", assistant_text="",
                         source="gpt_subscription", attachment_ids=[image["id"]])
        first = self.store.commit_conversation_turn(**arguments)
        self.store = GatewayStateStore(self.path)
        replay = self.store.commit_conversation_turn(**arguments)
        self.assertEqual(first["turn"]["id"], replay["turn"]["id"])
        stored = self.store.get_conversation_turn_by_request_id(profile_id="p", request_id="image-request")
        self.assertEqual([item["id"] for item in stored["attachments"]], [image["id"]])
        self.assertEqual(self.store.get_conversation_turn_by_request_id(profile_id="other", request_id="image-request"), {})


class GPTChatHTTPContracts(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(body, token="test-token"):
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        return Request({"type": "http", "method": "POST", "path": "/api/gpt-subscription/chat-requests",
                        "headers": [(b"authorization", f"Bearer {token}".encode())], "query_string": b""}, receive)

    async def test_auth_then_profile_scoped_receipt_control(self):
        with tempfile.TemporaryDirectory() as temp:
            service = object.__new__(GatewayService)
            service.gateway_token = "test-token"
            service.persona_engine = SimpleNamespace(profile_id="trusted-profile")
            service.gpt_chat_requests = GPTChatRequests(str(Path(temp) / "state.db"))
            body = GPTChatContracts.claim()
            body["profile_id"] = "untrusted-override"
            unauthorized = await service.handle_gpt_subscription(self.request(body, token="wrong"))
            self.assertEqual(unauthorized.status_code, 401)
            response = await service.handle_gpt_subscription(self.request(body))
            self.assertEqual(response.status_code, 200)
            duplicate = await service.handle_gpt_subscription(self.request(body))
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(json.loads(duplicate.body)["code"], "request_already_admitted")
            self.assertTrue(service.gpt_chat_requests.control("untrusted-override", GPTChatContracts.claim())["ok"])
            invalid = await service.handle_gpt_subscription(self.request({"action": "import"}))
            self.assertEqual(invalid.status_code, 400)

if __name__ == "__main__":
    unittest.main()
