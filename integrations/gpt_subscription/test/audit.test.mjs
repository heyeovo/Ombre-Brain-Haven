import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { streamResponse } from '../vendor/dist/responses.js';
const completed = { type: 'response.completed', response: { status: 'completed' } };
const frame = event => `data: ${JSON.stringify(event)}\n\n`;
const request = (options = {}) => streamResponse('synthetic-oauth-token', {
  model: 'test-model', input: 'private clipboard text', ...options,
}, new AbortController().signal);
const sse = wire => new Response(wire, { headers: { 'content-type': 'text/event-stream', 'x-request-id': 'req_test' } });

test('audit persists before the actual SDK request and hashes its exact body', async (t) => {
  const canonical = value => Array.isArray(value) ? `[${value.map(canonical).join(',')}]` : value && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}` : JSON.stringify(value);
  const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
  const body = { model: 'test-model', instructions: '背景😀\n', input: [{ role: 'user', content: '你好' }], store: false, stream: true };
  assert.equal(hash(body), '4ce2163a5107216540247e8e6257f2d082ea0dd2f6887ced7ff847d13c7d0cee');
  const stages = [];
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    stages.push('fetch');
    assert.equal(hash(JSON.parse(init.body)), hash(body));
    assert.deepEqual(stages, ['persist', 'fetch']);
    return sse(frame(completed));
  });
  await request({ instructions: body.instructions, input: body.input, expectedRequestHash: hash(body),
    onRequest: async value => { assert.equal(value.body_hash, hash(body)); stages.push('persist'); },
    onReceipt: value => { assert.equal(value.requestId, 'req_test'); stages.push('receipt'); } });
  assert.deepEqual(stages, ['persist', 'fetch', 'receipt']);
});

test('audit mismatch or rejected durable acknowledgement prevents HTTP', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new Error('must not send'); });
  await assert.rejects(request({ expectedRequestHash: 'a'.repeat(64), onRequest: async () => {} }), { code: 'audit_mismatch' });
  assert.equal(fetch.mock.callCount(), 0);
  const hash = createHash('sha256').update(JSON.stringify({ input: [{ content: 'private clipboard text', role: 'user' }], model: 'test-model', store: false, stream: true })).digest('hex');
  await assert.rejects(request({ expectedRequestHash: hash, onRequest: async () => { throw new Error('private DB error'); } }));
  assert.equal(fetch.mock.callCount(), 0);
});

async function responseEvidence(t, response) {
  let calls = 0, metadata;
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    calls++;
    assert.deepEqual(JSON.parse(init.body), { model: 'test-model', input: [{ role: 'user', content: 'private clipboard text' }], store: false, stream: true });
    return sse(frame({ type: 'response.completed', response: { status: 'completed', ...response } }));
  });
  await request({ onCompleted: value => { metadata = value; } });
  assert.equal(calls, 1, 'evidence extraction must not add model calls');
  return metadata.cacheEvidence;
}

test('completed response metadata is allowlisted without changing requests or echoing extras', async (t) => {
  const evidence = await responseEvidence(t, { model: 'gpt-5.6-sol-actual', service_tier: 'priority',
    usage: { input_tokens_details: { cache_write_tokens: 0 } },
    prompt_cache_diagnostics: { type: 'cache_miss', reason: 'service_tier_changed', comparison_reusable_tokens: 2048, cache_missed_tokens: 1024,
      secret: 'private-extra' }, instructions: 'private instructions', output: ['private output'], headers: { authorization: 'secret' } });
  assert.deepEqual(evidence, { version: 1, response_model: 'gpt-5.6-sol-actual', service_tier: 'priority', cache_write_tokens: 0,
    diagnostics: { type: 'cache_miss', reason: 'service_tier_changed', comparison_reusable_tokens: 2048, cache_missed_tokens: 1024 } });
  assert.doesNotMatch(JSON.stringify(evidence), /private|authorization|secret/);
});

test('missing response metadata remains unknown instead of inferred from request or usage', async (t) => {
  assert.deepEqual(await responseEvidence(t, {}), { version: 1, response_model: null, service_tier: null, cache_write_tokens: null, diagnostics: null });
});

for (const invalid of [-1, 0.5, '0', true, null, 9007199254740992]) {
  test(`invalid cache write count ${JSON.stringify(invalid)} stays unknown`, async (t) => {
    const evidence = await responseEvidence(t, { model: 'Bearer private-token', service_tier: 'private-tier',
      usage: { input_tokens_details: { cache_write_tokens: invalid } }, prompt_cache_diagnostics: { type: 'private-type', reason: 'private-reason' } });
    assert.deepEqual(evidence, { version: 1, response_model: null, service_tier: null, cache_write_tokens: null, diagnostics: null });
  });
}

test('unknown diagnostic reasons and malformed diagnostic counters are not forwarded', async (t) => {
  const evidence = await responseEvidence(t, { usage: { input_tokens_details: { cache_write_tokens: 4096 } },
    prompt_cache_diagnostics: { type: 'cache_miss', reason: 'private-reason', cache_missed_tokens: true, comparison_reusable_tokens: '100' } });
  assert.equal(evidence.cache_write_tokens, 4096);
  assert.deepEqual(evidence.diagnostics, { type: 'cache_miss', reason: null, cache_missed_tokens: null, comparison_reusable_tokens: null });
});

for (const type of ['cache_hit', 'comparison_response_not_found', 'unavailable']) {
  test(`preserves diagnostic status ${type} without fabricating a reason`, async (t) => {
    const evidence = await responseEvidence(t, { prompt_cache_diagnostics: { type, reason: 'model_changed' } });
    assert.equal(evidence.diagnostics.type, type);
    assert.equal(evidence.diagnostics.reason, null);
  });
}

test('failed stream cannot emit completed cache evidence', async (t) => {
  let completions = 0;
  t.mock.method(globalThis, 'fetch', async () => sse(frame({ type: 'response.failed', response: {
    error: { code: 'subscription_sharing_usage_limit_exceeded', message: 'private message' },
    model: 'gpt-5.6-sol', usage: { input_tokens_details: { cache_write_tokens: 100 } },
  } })));
  await assert.rejects(request({ onCompleted: () => { completions++; } }));
  assert.equal(completions, 0);
});

