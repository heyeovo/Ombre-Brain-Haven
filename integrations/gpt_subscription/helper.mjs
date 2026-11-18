import { createInterface } from 'node:readline';
import { openConnection, publicSession } from './connection.mjs';
import { asError, ChatGPTError } from './vendor/dist/errors.js';

const emit = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const abort = new AbortController();
process.on('SIGTERM', () => abort.abort());
process.on('SIGINT', () => abort.abort());
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
let started = false;
let acknowledge;
lines.on('line', (line) => {
  if (started) {
    if (line === '{"cancel":true}') abort.abort();
    if (line === '{"audit_ack":true}') acknowledge?.();
    return;
  }
  started = true;
  void run(line).finally(() => { lines.close(); process.stdin.destroy(); });
});

async function run(line) {
  try {
    if (Buffer.byteLength(line) > 4 * 1024 * 1024) throw new ChatGPTError('invalid_request', '请求过大。');
    const command = JSON.parse(line);
    const { store, client } = await openConnection(process.env.HAVEN_GPT_ROOT ?? '', process.env.HAVEN_GPT_SCOPE ?? '', process.env.OMBRE_GPT_KEY_FILE);
    if (command.op === 'status') emit({ type: 'result', data: publicSession(await client.getSession()) });
    else if (command.op === 'models') emit({ type: 'result', data: { models: await client.listModels({ signal: abort.signal }) } });
    else if (command.op === 'profiles') {
      const profiles = await client.listProfiles();
      emit({ type: 'result', data: { connections: profiles.map((p) => ({ connection_id: p.id, label: p.label, status: p.status, sharing: p.sharing })) } });
    } else if (command.op === 'select') emit({ type: 'result', data: publicSession(await client.selectProfile(command.connection_id)) });
    else if (command.op === 'disconnect') { await client.disconnect(); emit({ type: 'result', data: publicSession(await client.getSession()) }); }
    else if (command.op === 'stream') {
      let metadata = { usage: null };
      await client.streamResponse({ ...command.request, signal: abort.signal,
        expectedProfileId: command.expected_connection_id,
        ...(command.request.tools ? { onToolContinuation: (items) => emit({ type: 'tool_continuation', items }) } : {}),
        ...(command.audit_hash ? { expectedRequestHash: command.audit_hash,
          onRequest: ({ body_hash, connection_id }) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => { acknowledge = undefined; reject(new ChatGPTError('audit_timeout', 'Request audit was not acknowledged.')); }, 15000);
            const cancel = () => { clearTimeout(timer); acknowledge = undefined; reject(new ChatGPTError('cancelled', 'The response was cancelled.')); };
            abort.signal.addEventListener('abort', cancel, { once: true });
            acknowledge = () => { clearTimeout(timer); abort.signal.removeEventListener('abort', cancel); acknowledge = undefined; resolve(); };
            if (abort.signal.aborted) { cancel(); return; }
            emit({ type: 'request', body_hash, connection_id });
          }),
          onReceipt: (value) => emit({ type: 'receipt', ...value }),
        } : {}),
        onDelta: (text) => emit({ type: 'delta', text }), onCompleted: (value) => { metadata = value; } });
      emit({ type: 'completed', ...metadata });
    } else throw new ChatGPTError('invalid_request', '不支持的授权操作。');
  } catch (error) {
    // Never print arbitrary exceptions: OAuth/SDK/network errors may contain secrets.
    emit({ type: 'error', error: asError(error).toJSON() });
  }
}
