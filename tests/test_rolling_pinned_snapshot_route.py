import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gateway import GatewayService
from gateway_state import ConversationPersonaConflictError


class RollingPinnedSnapshotRouteTest(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service = object.__new__(GatewayService)
        service._authorize = Mock(return_value=None)
        service.persona_engine = SimpleNamespace(profile_id="trusted-profile")
        service.state_store = Mock()
        service.state_store.initialize_conversation_pinned_snapshot.return_value = {
            "context_revision": 7, "rolling_context": {"pinned_snapshot": []},
        }
        return service

    def request(self, **overrides):
        return SimpleNamespace(method="PATCH", headers={}, json=AsyncMock(return_value={
            "session_id": "session-1", "persona_id": "ombre",
            "rolling_pinned_snapshot": [], "expected_context_revision": 7,
            "profile_id": "untrusted-profile", **overrides,
        }))

    async def test_initialization_uses_authenticated_profile_and_returns_saved_snapshot(self):
        service = self.service()
        response = await service.handle_conversation_session(self.request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["session"]["rolling_context"]["pinned_snapshot"], [])
        service.state_store.initialize_conversation_pinned_snapshot.assert_called_once_with(
            profile_id="trusted-profile", session_id="session-1", persona_id="ombre",
            expected_context_revision=7, snapshot=[],
        )

    async def test_revision_and_persona_conflicts_return_409(self):
        for error in (ValueError("context_revision_conflict"), ConversationPersonaConflictError("other", "ombre")):
            service = self.service()
            service.state_store.initialize_conversation_pinned_snapshot.side_effect = error
            response = await service.handle_conversation_session(self.request())
            self.assertEqual(response.status_code, 409)

    async def test_missing_invalid_or_negative_revision_never_writes(self):
        for revision in (None, True, -1, "7"):
            service = self.service()
            response = await service.handle_conversation_session(self.request(expected_context_revision=revision))
            self.assertEqual(response.status_code, 400)
            service.state_store.initialize_conversation_pinned_snapshot.assert_not_called()
