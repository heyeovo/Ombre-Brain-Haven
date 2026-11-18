import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gpt_chat_requests import GPTChatRequests
from gpt_context_audit import cache_evidence, quota_evidence, request_hash
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


def quota_snapshot(limit_id='codex', source='response_headers'):
    return {'source': source, 'observed_at': '2026-11-17T10:00:00.000Z', 'limit_id': limit_id,
            'scope': 'unverified', 'primary': {'used_percent': 0, 'window_minutes': 300,
                                              'resets_at': 1794913200}, 'secondary': None}


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

    def test_tool_continuation_is_bound_to_owner_profile_and_connection(self):
        self.prepare()
        request = {'action': 'prepare-tool-step', 'session_id': 's', 'request_id': 'r',
                   'owner': 'd' * 32, 'step': 1, 'body_hash': 'e' * 64}
        self.assertFalse(self.registry.control('p', request)['ok'])
        self.registry.audit_event('p', 'r', {'type': 'request', 'body_hash': 'b' * 64, 'connection_id': 'fixture-connection'})
        self.registry.audit_event('p', 'r', {'type': 'receipt'})
        self.registry.audit_event('p', 'r', {'type': 'tool_continuation', 'items': ['fixture-only']})
        self.registry.audit_event('p', 'r', {'type': 'completed'})
        self.assertFalse(self.registry.control('other', request)['ok'])
        self.assertFalse(self.registry.control('p', {**request, 'owner': 'a' * 32})['ok'])
        self.assertTrue(self.registry.control('p', request)['ok'])
        self.assertFalse(self.registry.control('p', request)['ok'])
        self.assertIsNone(self.registry.audit('other', 'r', 1))
        evidence = self.registry.audit('p', 'r', 1)
        self.assertEqual(evidence['connection_id'], 'fixture-connection')
        self.assertEqual(evidence['snapshot']['body_hash'], 'e' * 64)
        self.assertNotIn('fixture-only', json.dumps(self.registry.audit('p', 'r')))

    def test_immutable_prepare_owner_and_unknown_usage(self):
        self.prepare()
        record = self.registry.audit('p', 'r')
        self.assertIsNone(record['usage'])
        self.assertNotIn('sending_at', record)
        self.assertNotIn('cache_evidence', record)
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

    def test_cache_evidence_is_allowlisted_durable_and_profile_scoped(self):
        self.prepare()
        supplied = {'version': 1, 'response_model': 'gpt-5.6-sol-actual', 'service_tier': 'priority',
                    'cache_write_tokens': 0, 'diagnostics': {'type': 'cache_miss', 'reason': 'model_changed',
                    'comparison_reusable_tokens': 2048, 'cache_missed_tokens': 1024, 'secret': 'private-extra'},
                    'body': 'private-body', 'headers': {'authorization': 'private-token'}}
        self.registry.audit_event('p', 'r', {'type': 'completed', 'cacheEvidence': supplied})
        reopened = GPTChatRequests(self.path)
        record = reopened.audit('p', 'r')
        self.assertEqual(record['cache_evidence'], {'version': 1, 'response_model': 'gpt-5.6-sol-actual',
            'service_tier': 'priority', 'cache_write_tokens': 0, 'diagnostics': {'type': 'cache_miss',
            'reason': 'model_changed', 'comparison_reusable_tokens': 2048, 'cache_missed_tokens': 1024}})
        self.assertNotIn('private', json.dumps(record))
        self.assertIsNone(reopened.audit('other', 'r'))
        self.assertEqual(reopened.control('other', {'action': 'audit-list', 'session_id': 's'})['records'], [])

    def test_missing_or_invalid_cache_metadata_stays_unknown(self):
        empty = {'version': 1, 'response_model': None, 'service_tier': None, 'cache_write_tokens': None, 'diagnostics': None}
        for value in (None, [], {}, {'version': 2, 'response_model': 'gpt-5.6-sol'}, {'version': True}):
            with self.subTest(value=value):
                self.assertEqual(cache_evidence(value), empty)
        for number in (-1, 0.5, '0', True, None, 9007199254740992):
            with self.subTest(number=number):
                self.assertEqual(cache_evidence({'version': 1, 'response_model': 'Bearer private-token',
                    'service_tier': 'private-tier', 'cache_write_tokens': number,
                    'diagnostics': {'type': 'private-type'}}), empty)
        self.prepare()
        self.registry.audit_event('p', 'r', {'type': 'completed', 'usage': None})
        self.assertNotIn('cache_evidence', self.registry.audit('p', 'r'))
        self.registry.audit_event('p', 'r', {'type': 'completed', 'cacheEvidence': None})
        self.assertEqual(self.registry.audit('p', 'r')['cache_evidence'], empty)

    def test_cache_diagnostic_enums_and_counters_are_bounded(self):
        for kind in ('cache_hit', 'comparison_response_not_found', 'unavailable', 'cache_miss'):
            with self.subTest(kind=kind):
                evidence = cache_evidence({'version': 1, 'diagnostics': {'type': kind,
                    'reason': 'private-reason', 'comparison_reusable_tokens': True, 'cache_missed_tokens': '1024'}})
                self.assertEqual(evidence['diagnostics'], {'type': kind, 'reason': None,
                    'comparison_reusable_tokens': None, 'cache_missed_tokens': None})

    def test_errors_and_cancellation_do_not_fabricate_completed_cache_evidence(self):
        for request, code in (('failed', 'stream_interrupted'), ('cancelled', 'cancelled')):
            with self.subTest(code=code):
                self.prepare(session=request, request=request)
                self.registry.audit_event('p', request, {'type': 'error', 'error': {'code': code},
                    'cacheEvidence': {'version': 1, 'cache_write_tokens': 100}})
                self.assertNotIn('cache_evidence', self.registry.audit('p', request))

    def test_arbitrary_body_fields_rejected(self):
        for field in ('instructions', 'parts', 'messages', 'context'):
            value = snapshot()
            if field == 'messages':
                value[field][0]['content'] = 'private text'
            else:
                value[field]['content'] = 'private text'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare(request=field, session=field, value=value)

    def test_quota_candidates_are_allowlisted_durable_and_profile_scoped(self):
        self.prepare()
        candidates = [quota_snapshot(), quota_snapshot('other_bucket', 'stream_event')]
        supplied = {'status': 'observed', 'snapshots': [
            {**item, 'scope': 'account', 'headers': {'authorization': 'private-token'},
             'body': 'private-body', 'primary': {**item['primary'], 'secret': 'private-extra'}}
            for item in candidates]}
        self.registry.audit_event('p', 'r', {'type': 'completed', 'quotaEvidence': supplied})
        reopened = GPTChatRequests(self.path)
        record = reopened.audit('p', 'r')
        self.assertEqual(record['quota_evidence'], {'status': 'observed', 'snapshots': candidates})
        self.assertNotIn('private', json.dumps(record))
        self.assertIsNone(reopened.audit('other', 'r'))
        self.assertEqual(reopened.control('other', {'action': 'audit-list', 'session_id': 's'})['records'], [])

    def test_missing_unavailable_and_failed_quota_do_not_borrow_previous_values(self):
        self.prepare()
        self.registry.audit_event('p', 'r', {'type': 'completed', 'quotaEvidence': {
            'status': 'observed', 'snapshots': [quota_snapshot()]}})
        for request, event in (
                ('old-helper', {'type': 'completed'}),
                ('unavailable', {'type': 'completed', 'quotaEvidence': {'status': 'unavailable', 'snapshots': []}}),
                ('failed', {'type': 'error', 'error': {'code': 'stream_interrupted'},
                            'quotaEvidence': {'snapshots': [quota_snapshot()]}}),
                ('cancelled', {'type': 'error', 'error': {'code': 'cancelled'},
                               'quotaEvidence': {'snapshots': [quota_snapshot()]}})):
            with self.subTest(request=request):
                self.prepare(request=request, session=request)
                self.registry.audit_event('p', request, event)
                record = self.registry.audit('p', request)
                if request == 'unavailable':
                    self.assertEqual(record['quota_evidence'], {'status': 'unavailable', 'snapshots': []})
                else:
                    self.assertNotIn('quota_evidence', record)

    def test_quota_invalid_values_stay_unknown_and_real_zero_survives(self):
        empty = {'status': 'unavailable', 'snapshots': []}
        for value in (None, [], {}, {'status': 'observed'}, {'snapshots': [None, [], 'private']}):
            with self.subTest(value=value):
                self.assertEqual(quota_evidence(value), empty)
        for used in (-1, 101, float('nan'), float('inf'), True, '0', None):
            item = quota_snapshot()
            item['primary']['used_percent'] = used
            with self.subTest(used=used):
                self.assertEqual(quota_evidence({'snapshots': [item]}), empty)
        for key, value in (('source', 'x-ratelimit-remaining-tokens'), ('source', []),
                           ('limit_id', 'Bearer private-token'), ('observed_at', '2026-02-30T00:00:00.000Z')):
            self.assertEqual(quota_evidence({'snapshots': [{**quota_snapshot(), key: value}]}), empty)
        item = quota_snapshot()
        item['primary'].update(window_minutes=True, resets_at=253402300800)
        actual = quota_evidence({'snapshots': [item]})['snapshots'][0]['primary']
        self.assertEqual(actual, {'used_percent': 0, 'window_minutes': None, 'resets_at': None})

    def test_quota_buckets_are_bounded_separate_and_not_merged(self):
        first = quota_snapshot()
        latest = quota_snapshot()
        latest.update(primary=None, secondary={'used_percent': 60.5, 'window_minutes': None, 'resets_at': None})
        other_source = quota_snapshot(source='stream_event')
        result = quota_evidence({'snapshots': [first, latest, other_source]})['snapshots']
        self.assertEqual(result, [latest, other_source])
        many = [quota_snapshot(f'bucket{i}') for i in range(100)]
        self.assertEqual(len(quota_evidence({'snapshots': many})['snapshots']), 16)
        both = many[:16] + [quota_snapshot(f'bucket{i}', 'stream_event') for i in range(16)]
        self.assertEqual(len(quota_evidence({'snapshots': both})['snapshots']), 32)

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

    async def test_completed_stream_releases_reservation_before_the_client_closes(self):
        service = GPTSubscriptionService('.', key_file='dummy', node='node')
        service.reserve('p', 'r')
        service._spawn = AsyncMock(return_value=object())
        service._stop = AsyncMock()
        service._read = AsyncMock(return_value={'type': 'completed', 'usage': None})
        events = service.stream('p', 'r', {})
        self.assertEqual((await anext(events))['type'], 'completed')
        service.reserve('p', 'r')
        await events.aclose()
        self.assertIn(('p', 'r'), service._running)

    async def test_cache_evidence_crosses_pipe_and_survives_registry_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = GPTChatRequests(str(Path(directory) / 'state.db'))
            registry.control('p', {'action': 'claim', 'session_id': 's', 'request_id': 'r',
                                  'fingerprint': 'f' * 64, 'owner': 'd' * 32})
            registry.control('p', {'action': 'prepare', 'session_id': 's', 'request_id': 'r',
                                  'owner': 'd' * 32, 'snapshot': snapshot()})
            evidence = {'version': 1, 'response_model': 'gpt-5.6-sol', 'service_tier': 'default',
                        'cache_write_tokens': 0, 'diagnostics': None}
            quota = {'status': 'observed', 'snapshots': [quota_snapshot()]}
            _, events = await self.run_pipe(lambda event: registry.audit_event('p', 'r', event), [
                {'type': 'request', 'body_hash': 'b' * 64}, {'type': 'receipt', 'requestId': 'req'},
                {'type': 'completed', 'usage': {'cached_tokens': 0}, 'cacheEvidence': evidence, 'quotaEvidence': quota}])
            self.assertEqual(events[-1]['cacheEvidence'], evidence)
            self.assertEqual(events[-1]['quotaEvidence'], quota)
            record = GPTChatRequests(registry.db_path).audit('p', 'r')
            self.assertEqual(record['cache_evidence'], evidence)
            self.assertEqual(record['quota_evidence'], quota)
            self.assertEqual(record['usage']['cached_tokens'], 0)

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
