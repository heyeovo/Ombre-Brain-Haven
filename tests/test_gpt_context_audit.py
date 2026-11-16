import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gpt_chat_requests import GPTChatRequests
from gpt_context_audit import request_hash
from gpt_subscription import GPTSubscriptionService
from gateway_state import GatewayStateStore
from gateway import GatewayService
from starlette.requests import Request


def snapshot():
    digest = {'hash': 'a' * 64, 'bytes': 2}
    return {'version': 1, 'algorithm': 'sha256-json-sorted-v1', 'model': 'test-model', 'body_hash': 'b' * 64,
            'instructions': digest.copy(), 'parts': {key: digest.copy() for key in
                ('persona', 'window', 'background', 'capability', 'recall', 'current_text', 'current_time')},
            'messages': [{**digest, 'role': 'user', 'source_round_id': None, 'prefix_hash': 'c' * 64}],
            'context': {'estimator': 'utf8-bytes-div-3-v1', 'estimate_safety_ratio': 1.05,
                        'input_tokens_estimated': 10, 'application_input_budget': 120000, 'model_context_limit': None,
                        'included_history_rounds': 0, 'omitted_history_rounds': 0, 'context_revision': 0}}


class GPTContextAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.db')
        self.registry = GPTChatRequests(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, profile='p', session='s', request='r', value=None):
        self.registry.control(profile, {'action': 'claim', 'session_id': session, 'request_id': request,
                                       'fingerprint': 'f' * 64, 'owner': 'd' * 32})
        return self.registry.control(profile, {'action': 'prepare', 'session_id': session, 'request_id': request,
                                              'owner': 'd' * 32, 'snapshot': value or snapshot()})

    def test_old_receipt_upgrade_repeat_init_and_restart_preserve_evidence(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE gpt_chat_requests DROP COLUMN audit_json')
        conn.close()
        self.registry = GPTChatRequests(self.path)
        self.assertTrue(self.prepare()['ok'])
        reopened = GPTChatRequests(self.path)
        GPTChatRequests(self.path)
        self.assertEqual(reopened.audit('p', 'r')['snapshot'], snapshot())
        self.assertIsNone(reopened.audit('other', 'r'))

    def test_immutable_prepare_owner_and_unknown_usage(self):
        self.prepare()
        record = self.registry.audit('p', 'r')
        self.assertIsNone(record['usage'])
        self.assertNotIn('sending_at', record)
        self.assertTrue(self.prepare()['ok'])
        changed = snapshot(); changed['parts']['background']['hash'] = 'e' * 64
        self.assertFalse(self.prepare(value=changed)['ok'])
        result = self.registry.control('p', {'action': 'prepare', 'session_id': 's', 'request_id': 'r',
                                           'owner': 'e' * 32, 'snapshot': snapshot()})
        self.assertFalse(result['ok'])
        self.assertEqual(self.registry.audit('p', 'r'), record)

    def test_event_stages_real_zero_and_no_credentials_or_headers(self):
        self.prepare()
        self.registry.audit_event('p', 'r', {'type': 'request', 'body_hash': 'b' * 64})
        with self.assertRaises(ValueError):
            self.registry.audit_event('p', 'r', {'type': 'request', 'body_hash': 'b' * 64})
        self.registry.audit_event('p', 'r', {'type': 'receipt', 'requestId': 'req_real', 'headers': {'authorization': 'secret'}})
        self.registry.audit_event('p', 'r', {'type': 'completed', 'responseId': 'resp_real',
                                          'usage': {'input_tokens': 100, 'cached_tokens': 0}})
        record = self.registry.audit('p', 'r')
        self.assertTrue(record['sending_at']); self.assertTrue(record['acknowledged_at'])
        self.assertEqual(record['usage']['cached_tokens'], 0)
        self.assertIsNone(record['usage']['output_tokens'])
        self.assertNotIn('secret', json.dumps(record))

    def test_arbitrary_body_fields_rejected(self):
        for field in ('instructions', 'parts', 'messages', 'context'):
            value = snapshot()
            if field == 'messages':
                value[field][0]['content'] = 'private text'
            else:
                value[field]['content'] = 'private text'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare(request=field, session=field, value=value)

    def test_cross_language_final_body_hash(self):
        self.assertEqual(request_hash({'model': 'test-model', 'instructions': '背景😀\n',
                                       'input': [{'role': 'user', 'content': '你好'}]}),
                         '4ce2163a5107216540247e8e6257f2d082ea0dd2f6887ced7ff847d13c7d0cee')

    def test_paging_matches_older_turn_evidence_and_isolates_sessions(self):
        for i in range(23):
            self.prepare(session='s', request=f'r{i}')
            self.registry.control('p', {'action': 'finish', 'session_id': 's', 'request_id': f'r{i}', 'owner': 'd' * 32})
        first = self.registry.control('p', {'action': 'audit-list', 'session_id': 's', 'request_ids': ['r0']})
        self.assertEqual(len(first['records']), 20)
        self.assertEqual(first['matched_records'][0]['request_id'], 'r0')
        second = self.registry.control('p', {'action': 'audit-list', 'session_id': 's', 'before_cursor': first['before_cursor']})
        self.assertEqual(len(second['records']), 3)
        self.assertIsNone(second['before_cursor'])
        self.assertEqual(self.registry.control('other', {'action': 'audit-list', 'session_id': 's'})['records'], [])
        self.assertEqual(self.registry.control('p', {'action': 'audit-list', 'session_id': 'other'})['records'], [])

    def test_permanent_delete_erases_only_scoped_audit_and_retains_admission(self):
        state = GatewayStateStore(self.path)
        for profile in ('p', 'other'):
            state.patch_conversation_session_state(profile_id=profile, session_id='s', persona_id='ombre', updates={})
            self.prepare(profile=profile)
        state.permanently_delete_conversation_session(profile_id='p', session_id='s')
        self.assertIsNone(self.registry.audit('p', 'r'))
        self.assertIsNotNone(self.registry.audit('other', 'r'))
        self.assertTrue(self.registry.control('p', {'action': 'status', 'session_id': 's', 'request_id': 'r'})['found'])
        self.assertFalse(self.prepare()['ok'])


class GPTAuditPipe(unittest.IsolatedAsyncioTestCase):
    async def run_pipe(self, record, events=None):
        service = GPTSubscriptionService('.', key_file='dummy', node='node')
        service._running[('p', 'r')] = None
        process = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
        service._spawn = AsyncMock(return_value=process)
        service._stop = AsyncMock()
        service._read = AsyncMock(side_effect=events or [{'type': 'request', 'body_hash': 'a' * 64},
                                              {'type': 'receipt', 'requestId': 'req'},
                                              {'type': 'completed', 'usage': None}])
        events = [event async for event in service.stream('p', 'r', {}, audit={'body_hash': 'a' * 64, 'record': record})]
        return process, events

    async def test_private_ack_only_after_durable_write_and_terminal_record(self):
        stages = []
        process, events = await self.run_pipe(lambda event: stages.append(event['type']))
        self.assertEqual(stages, ['request', 'receipt', 'completed'])
        process.stdin.write.assert_called_once_with(b'{"audit_ack":true}\n')
        self.assertEqual(events[-1]['type'], 'completed')

    async def test_failed_write_never_acknowledges_or_reports_success(self):
        def fail(event):
            raise sqlite3.OperationalError('private details')
        process, events = await self.run_pipe(fail)
        process.stdin.write.assert_not_called()
        self.assertEqual(events[-1]['type'], 'error')
        self.assertNotIn('private details', json.dumps(events))

    async def test_completed_without_request_and_receipt_cannot_fake_audit_success(self):
        process, events = await self.run_pipe(lambda _: None, [{'type': 'completed', 'usage': None}])
        process.stdin.write.assert_not_called()
        self.assertEqual(events[-1]['error']['code'], 'audit_sequence_invalid')


class GPTAuditHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_request_is_checked_before_reserve_and_cannot_be_sent_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = GPTChatRequests(str(Path(directory) / 'state.db'))
            registry.control('trusted', {'action': 'claim', 'request_id': 'r', 'session_id': 's',
                                         'owner': 'd' * 32, 'fingerprint': 'f' * 64})
            payload = {'model': 'test-model', 'instructions': '背景😀\n', 'input': [{'role': 'user', 'content': '你好'}]}
            evidence = snapshot(); evidence['body_hash'] = request_hash(payload)
            registry.control('trusted', {'action': 'prepare', 'request_id': 'r', 'session_id': 's',
                                         'owner': 'd' * 32, 'snapshot': evidence})
            async def stream(profile, request_id, body, *, audit):
                audit['record']({'type': 'request', 'body_hash': audit['body_hash']})
                audit['record']({'type': 'receipt', 'requestId': 'req'})
                audit['record']({'type': 'completed', 'usage': {'cached_tokens': 0}})
                yield {'type': 'completed', 'request_id': request_id, 'usage': {'cached_tokens': 0}}
            service = SimpleNamespace(reserve=Mock(), stream=stream)
            handler = object.__new__(GatewayService)
            handler.gateway_token = 'synthetic'
            handler.persona_engine = SimpleNamespace(profile_id='trusted')
            handler.gpt_chat_requests = registry
            handler.gpt_subscription = service
            def request(body):
                async def receive():
                    return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
                return Request({'type': 'http', 'method': 'POST', 'path': '/api/gpt-subscription/responses',
                                'headers': [(b'authorization', b'Bearer synthetic')], 'query_string': b''}, receive)
            mismatch = await handler.handle_gpt_subscription(request({'request_id': 'r', **payload, 'instructions': 'changed'}))
            self.assertEqual(mismatch.status_code, 409)
            service.reserve.assert_not_called()
            valid = await handler.handle_gpt_subscription(request({'request_id': 'r', **payload}))
            self.assertEqual(valid.status_code, 200)
            self.assertIn('completed', ''.join([part async for part in valid.body_iterator]))
            self.assertEqual(registry.audit('trusted', 'r')['usage']['cached_tokens'], 0)
            duplicate = await handler.handle_gpt_subscription(request({'request_id': 'r', **payload}))
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(service.reserve.call_count, 1)
