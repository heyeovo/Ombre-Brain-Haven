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

