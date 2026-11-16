// Synthetic issuer and network only. Loaded explicitly by tests, never by runtime.
import { readFile, writeFile, appendFile } from 'node:fs/promises';

export function installMock(config) {
  globalThis.fetch = async (url, options = {}) => {
    const address = String(url);
    if (address.endsWith('/.well-known/openid-configuration')) return Response.json({
      issuer: 'https://auth.openai.com', authorization_endpoint: 'https://auth.openai.com/oauth/authorize',
      token_endpoint: 'https://auth.openai.com/oauth/token', revocation_endpoint: 'https://auth.openai.com/oauth/revoke',
      jwks_uri: 'https://auth.openai.com/jwks',
    });
    if (address.endsWith('/jwks')) {
      if (config.keysUnavailable) return new Response('', { status: 503 });
      return Response.json({ keys: [config.jwk] });
    }
    if (address.endsWith('/oauth/token')) {
      const current = config.rotationFile ? (await readFile(config.rotationFile, 'utf8')).trim() : 'refresh-old';
      if (new URLSearchParams(options.body).get('refresh_token') !== current) return Response.json({ error: 'invalid_grant' }, { status: 400 });
      if (config.callsFile) await appendFile(config.callsFile, 'refresh\n');
      if (config.rotationFile) await writeFile(config.rotationFile, 'refresh-new');
      await new Promise((r) => setTimeout(r, 30));
      return Response.json({ access_token: 'access-new', refresh_token: 'refresh-new', token_type: 'bearer',
        expires_in: 3600, id_token: config.idToken });
    }
    if (address.endsWith('/v1/models')) return Response.json({ models: [
      { slug: 'hidden', display_name: 'Hidden', visibility: 'hidden' },
      { slug: 'test-model', display_name: 'Test Model', visibility: 'list' },
    ] });
    if (address.endsWith('/v1/responses')) {
      if (config.auditCallsFile) await appendFile(config.auditCallsFile, 'response\n');
      const payload = JSON.parse(options.body);
      if (payload.store !== false || payload.stream !== true || 'tools' in payload) throw new Error('unsafe request');
      if (config.waitForCancel) return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('data: {"type":"response.output_text.delta","delta":"partial"}\n\n'));
          options.signal.addEventListener('abort', () => controller.error(new DOMException('aborted', 'AbortError')), { once: true });
        },
      }), { headers: { 'content-type': 'text/event-stream' } });
      const frames = [{ type: 'response.output_text.delta', delta: 'synthetic reply' }, ...(config.quotaEvents ?? [])];
      if (config.failStream) frames.push({ type: 'response.failed', response: { error: { code: 'subscription_sharing_usage_limit_exceeded', message: 'private secret never expose' } } });
      else frames.push({ type: 'response.completed', response: { id: 'resp_test', status: 'completed', ...(config.noUsage ? {} : { usage: {
        input_tokens: 20, output_tokens: 8, total_tokens: 28, input_tokens_details: { cached_tokens: 3 },
        output_tokens_details: { reasoning_tokens: 2 }, secret: 'never expose',
      } }) } });
      return new Response(frames.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'content-type': 'text/event-stream', 'x-request-id': 'req_test', ...config.quotaHeaders } });
    }
    if (address.endsWith('/oauth/revoke')) return new Response('', { status: 200 });
    throw new Error('Unexpected synthetic endpoint');
  };
}

if (process.env.HAVEN_TEST_FIXTURE) installMock(JSON.parse(await readFile(process.env.HAVEN_TEST_FIXTURE, 'utf8')));
