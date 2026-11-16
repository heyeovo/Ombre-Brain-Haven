import assert from 'node:assert/strict';
import test from 'node:test';
import { streamResponse } from '../vendor/dist/responses.js';

const completed = { type: 'response.completed', response: { status: 'completed' } };
const quota = (primary, extra = {}) => ({ type: 'codex.rate_limits', rate_limits: { primary }, ...extra });
const window = (used_percent, window_minutes = 300, reset_at = 1900000000) => ({ used_percent, window_minutes, reset_at });

async function capture(t, headers = {}, events = [], terminal = completed) {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls++;
    assert.equal(String(url), 'https://api.openai.com/v1/responses');
    assert.equal(init.method, 'POST');
    assert.equal(new Headers(init.headers).get('authorization'), 'Bearer synthetic-token');
    return new Response([...events, terminal].map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''), {
      headers: { 'content-type': 'text/event-stream', ...headers },
    });
  });
  let metadata;
  const run = streamResponse('synthetic-token', { model: 'test-model', input: 'synthetic input',
    onCompleted: (value) => { metadata = value; } }, new AbortController().signal);
  if (terminal.type !== 'response.completed') {
    await assert.rejects(run);
    assert.equal(metadata, undefined);
    return;
  }
  assert.deepEqual(await run, { text: '' });
  assert.equal(calls, 1, 'passive observation must not make a second request');
  return metadata.quotaEvidence;
}

test('header windows retain actual periods and timestamps; zero usage is observed', async (t) => {
  const evidence = await capture(t, {
    'x-codex-primary-used-percent': '0',
    'x-codex-primary-window-minutes': '60',
    'x-codex-primary-reset-at': '1900000000',
    'x-codex-secondary-used-percent': '37.5',
    'x-codex-secondary-window-minutes': '10080',
    'x-codex-secondary-reset-at': '1900600000',
    'set-cookie': 'private-cookie', 'x-unrelated': 'private-header',
  });
  assert.equal(evidence.status, 'observed');
  assert.equal(evidence.snapshots.length, 1);
  const snapshot = evidence.snapshots[0];
  assert.equal(snapshot.source, 'response_headers');
  assert.equal(snapshot.scope, 'unverified');
  assert.equal(snapshot.limit_id, 'codex');
  assert.ok(Number.isFinite(Date.parse(snapshot.observed_at)));
  assert.deepEqual(snapshot.primary, { used_percent: 0, window_minutes: 60, resets_at: 1900000000 });
  assert.deepEqual(snapshot.secondary, { used_percent: 37.5, window_minutes: 10080, resets_at: 1900600000 });
  assert.doesNotMatch(JSON.stringify(evidence), /private|cookie|synthetic input|synthetic-token/);
});

test('missing quota and API RPM/TPM headers stay unavailable', async (t) => {
  assert.deepEqual(await capture(t, {
    'x-ratelimit-remaining-tokens': '10000', 'x-ratelimit-limit-requests': '500',
    'x-ratelimit-primary-used-percent': '20', 'retry-after': '30',
  }), { status: 'unavailable', snapshots: [] });
});

for (const invalid of ['', 'NaN', 'Infinity', '-1', '100.1', 'true', '0x10', '1e2']) {
  test(`invalid header percent ${JSON.stringify(invalid)} is not a quota`, async (t) => {
    assert.equal((await capture(t, { 'x-codex-primary-used-percent': invalid })).status, 'unavailable');
  });
}

test('partial windows preserve unknown duration/reset without assuming 5h or a week', async (t) => {
  const evidence = await capture(t, {
    'x-codex-secondary-used-percent': '25', 'x-codex-secondary-window-minutes': '0',
    'x-codex-secondary-reset-at': '999999999999999999',
  });
  assert.equal(evidence.snapshots[0].primary, null);
  assert.deepEqual(evidence.snapshots[0].secondary, { used_percent: 25, window_minutes: null, resets_at: null });
});

test('header and stream observations remain separate and keep distinct bucket IDs', async (t) => {
  const evidence = await capture(t, { 'x-codex-primary-used-percent': '10', 'x-codex-other-primary-used-percent': '3' }, [
    quota(window(20)), quota(window(30), { metered_limit_name: 'codex' }),
    quota(window(75, 1440), { metered_limit_name: 'codex_model_x', secret: 'private-event' }),
  ]);
  assert.equal(evidence.snapshots.length, 4);
  const stream = evidence.snapshots.filter((s) => s.source === 'stream_event');
  assert.deepEqual(stream.map((s) => [s.limit_id, s.primary.used_percent]), [['codex', 30], ['codex_model_x', 75]]);
  assert.doesNotMatch(JSON.stringify(evidence), /private-event|secret/);
});

test('malformed and unrelated stream events cannot create quota observations', async (t) => {
  assert.deepEqual(await capture(t, {}, [
    { type: 'unrelated', rate_limits: { primary: window(10) } },
    quota(window('10')), quota(window(null)), quota(window(-1)), quota(window(101)),
    quota(window(10), { metered_limit_name: 'invalid id private text' }),
    { type: 'codex.rate_limits', rate_limits: null },
  ]), { status: 'unavailable', snapshots: [] });
});

test('a partial stream observation does not borrow fields from earlier observations', async (t) => {
  const evidence = await capture(t, {}, [quota(window(10)), quota({ used_percent: 20 })]);
  assert.deepEqual(evidence.snapshots[0].primary, { used_percent: 20, window_minutes: null, resets_at: null });
});

test('quota metadata is bounded even if many metered IDs arrive', async (t) => {
  const headers = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`x-codex-b${i}-primary-used-percent`, '10']));
  const events = Array.from({ length: 30 }, (_, i) => quota(window(i), { metered_limit_name: `bucket_${i}` }));
  assert.equal((await capture(t, headers, events)).snapshots.length, 32);
});

test('quota evidence does not turn a failed response into successful completion', async (t) => {
  await capture(t, { 'x-codex-primary-used-percent': '100' }, [quota(window(100))],
    { type: 'response.failed', response: { error: { code: 'subscription_sharing_usage_limit_exceeded' } } });
});
