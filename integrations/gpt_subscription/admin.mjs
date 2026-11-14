// Server-side stdin-only import. Raw credentials never pass through Python or HTTP.
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { openConnection, prepareImport, importConnection } from './connection.mjs';
import { asError, ChatGPTError } from './vendor/dist/errors.js';

try {
  const args = process.argv.slice(2);
  const value = (name) => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
  const buckets = value('--buckets-dir');
  const profile = value('--profile');
  if (!buckets || !profile) throw new ChatGPTError('invalid_config', '需要 --buckets-dir 与可信的 --profile。');
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 4 * 1024 * 1024) throw new ChatGPTError('invalid_import', '导入数据过大。');
    chunks.push(chunk);
  }
  const command = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!['prepare-import', 'import'].includes(command.op)) throw new ChatGPTError('invalid_import', '不支持的管理操作。');
  const scope = createHash('sha256').update(profile).digest('hex');
  const { store } = await openConnection(resolve(buckets, 'gpt-subscription'), scope, process.env.OMBRE_GPT_KEY_FILE);
  const result = command.op === 'prepare-import' ? await prepareImport(store) : await importConnection(store, command.transfer);
  console.log(JSON.stringify({ ok: true, ...result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: asError(error).toJSON() }));
  process.exitCode = 1;
}
