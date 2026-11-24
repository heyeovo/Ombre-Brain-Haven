import assert from 'node:assert/strict';
import test from 'node:test';
import { listModels } from '../vendor/dist/models.js';
import { streamResponse } from '../vendor/dist/responses.js';

test('capacity and effort choices come only from valid upstream metadata', async t => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ models: [
    { slug: 'fixture', display_name: 'Fixture', visibility: 'list', context_window: 400000,
      max_output_tokens: 32000, supported_reasoning_levels: [{ effort: 'high' }], default_reasoning_level: 'high' },
    { slug: 'unknown', display_name: 'Unknown', visibility: 'list', context_window: '400000', max_output_tokens: -1 },
  ] }));
  assert.deepEqual(await listModels('synthetic-token', new AbortController().signal), [
    { slug: 'fixture', displayName: 'Fixture', contextWindow: 400000, maxOutputTokens: 32000,
      reasoningEfforts: ['high'], defaultReasoningEffort: 'high' },
    { slug: 'unknown', displayName: 'Unknown' },
  ]);
});

test('reasoning effort reaches the actual Responses request', async t => {
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body).reasoning, { effort: 'high' });
    return new Response('data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } });
  });
  await streamResponse('synthetic-token', { model: 'fixture', input: 'hello', reasoning: { effort: 'high' } },
    new AbortController().signal);
});
