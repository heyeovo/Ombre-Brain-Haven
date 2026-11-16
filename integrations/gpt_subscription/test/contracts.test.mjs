import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, writeFile, rm, mkdir, stat, copyFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { randomBytes, randomUUID, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { generateKeyPair, exportJWK, SignJWT } from 'jose';
import { openConnection, prepareImport, importConnection, publicSession } from '../connection.mjs';
import { freezeForTransfer } from '../login-transfer.mjs';
import { installMock } from './mock-openai.mjs';

const scope = (value) => createHash('sha256').update(value).digest('hex');
const { privateKey, publicKey } = await generateKeyPair('RS256');
const jwk = { ...await exportJWK(publicKey), kid: 'haven-test', alg: 'RS256', use: 'sig' };
async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'haven-gpt-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const keyFile = join(directory, 'key');
  await writeFile(keyFile, randomBytes(32).toString('base64'), { mode: 0o600 });
  const root = join(directory, 'buckets', 'gpt-subscription');
  const profile = { version: 1, id: randomUUID(), label: 'Connection 1', clientId: 'client_test', subject: 'test-subject', status: 'connected',
    scopes: ['openid', 'offline_access', 'chatgpt.tokens.use.direct'], savedAt: new Date().toISOString(),
    identity: { email: 'private@example.invalid' }, credentials: { accessToken: 'access-old', refreshToken: 'refresh-old', expiresAt: Date.now() + 3600_000 } };
  const idToken = await new SignJWT({ iss: 'https://auth.openai.com', aud: profile.clientId, sub: profile.subject, email: profile.identity.email,
    iat: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600 }).setProtectedHeader({ alg: 'RS256', kid: 'haven-test' }).sign(privateKey);
  profile.profileIdToken = idToken;
  const config = { jwk, idToken, callsFile: join(directory, 'calls'), rotationFile: join(directory, 'rotation') };
  await writeFile(config.callsFile, ''); await writeFile(config.rotationFile, 'refresh-old');
  const mockPath = join(directory, 'mock.json');
  await writeFile(mockPath, JSON.stringify(config));
  installMock(config);
  const connection = await openConnection(root, scope('profile-a'), keyFile);
  const target = await prepareImport(connection.store);
  const transfer = { version: 1, transfer_id: randomUUID(), host_id: target.host_id, generation: target.generation, profile };
  return { directory, root, keyFile, connection, config, mockPath, profile, transfer };
}
async function seed(f, expired = false) {
  const p = structuredClone(f.profile);
  if (expired) p.credentials.expiresAt = Date.now() - 1000;
  await f.connection.store.withLock(() => f.connection.store.write({ version: 2, profiles: [p], activeProfileId: p.id, pendingRegistrations: [] }));
}
function helper(f, command, extra = {}) {
  return new Promise((accept, reject) => {
    const child = spawn(process.execPath, ['--import', pathToFileURL(resolve('test/mock-openai.mjs')).href, resolve('helper.mjs')], { env: {
      ...process.env, HAVEN_GPT_ROOT: f.root, HAVEN_GPT_SCOPE: scope('profile-a'), OMBRE_GPT_KEY_FILE: f.keyFile, HAVEN_TEST_FIXTURE: f.mockPath, ...extra,
    }, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    child.stdout.on('data', (data) => { out += data; }); child.stderr.on('data', (data) => { err += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      try { assert.equal(code, 0, err); assert.equal(err, ''); accept(out.trim().split('\n').map(JSON.parse)); } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(command) + '\n');
  });
}

test('encrypted persistence survives process restart; profiles, keys and Linux permissions stay isolated', async (t) => {
  const f = await fixture(t); await seed(f);
  const path = join(f.root, scope('profile-a'), 'chatgpt-auth.json');
  const encrypted = await readFile(path, 'utf8');
  for (const secret of ['access-old', 'refresh-old', 'private@example.invalid', f.profile.profileIdToken]) assert.ok(!encrypted.includes(secret));
  const [first] = await helper(f, { op: 'status' });
  const [second] = await helper(f, { op: 'status' });
  assert.equal(first.data.status, 'connected'); assert.deepEqual(first, second);
  assert.equal(first.data.account, 'p***'); assert.equal(first.data.limits, null);
  const other = await openConnection(f.root, scope('profile-b'), f.keyFile);
  assert.equal((await other.client.getSession()).status, 'disconnected');
  await copyFile(path, join(f.root, scope('profile-b'), 'chatgpt-auth.json'));
  const status = await other.client.getSession(); assert.equal(status.error.code, 'storage_decryption_failed');
  await writeFile(f.keyFile, randomBytes(32).toString('base64'), { mode: 0o600 });
  const [failed] = await helper(f, { op: 'status' }); assert.equal(failed.data.error.code, 'storage_decryption_failed');
  assert.equal(await readFile(path, 'utf8'), encrypted);
  if (process.platform !== 'win32') {
    assert.equal((await stat(path)).mode & 0o077, 0);
    assert.equal((await stat(f.root)).mode & 0o077, 0);
  }
});

test('import is atomic, host-bound, idempotent even after rotation; bad identity never overwrites usable credentials', async (t) => {
  const f = await fixture(t);
  const first = await importConnection(f.connection.store, f.transfer); assert.equal(first.replayed, false);
  const path = join(f.root, scope('profile-a'), 'chatgpt-auth.json');
  await f.connection.store.withLock(async () => {
    const state = await f.connection.store.read(); state.profiles[0].credentials.refreshToken = 'successor'; await f.connection.store.write(state);
  });
  const replay = await importConnection(f.connection.store, f.transfer); assert.equal(replay.replayed, true);
  const before = await readFile(path, 'utf8');
  const bad = structuredClone(f.transfer); bad.transfer_id = randomUUID(); bad.generation = 1; bad.profile.profileIdToken += 'tampered';
  await assert.rejects(importConnection(f.connection.store, bad), { code: 'invalid_id_token' });
  assert.equal(await readFile(path, 'utf8'), before);
  await assert.rejects(importConnection(f.connection.store, { ...f.transfer, transfer_id: randomUUID() }), { code: 'import_conflict' });
  await assert.rejects(importConnection(f.connection.store, { ...f.transfer, host_id: `urn:uuid:${randomUUID()}` }), { code: 'host_mismatch' });
  const state = await f.connection.store.withLock(() => f.connection.store.read());
  assert.equal(state.profiles[0].credentials.refreshToken, 'successor');
  assert.equal((await prepareImport(f.connection.store)).host_id, f.transfer.host_id);
  const other = await openConnection(f.root, scope('profile-b'), f.keyFile);
  const target = await prepareImport(other.store);
  await assert.rejects(importConnection(other.store, { ...f.transfer, transfer_id: randomUUID(), host_id: target.host_id }), { code: 'registration_owned' });
  assert.equal((await other.client.getSession()).status, 'disconnected');
});

test('two independent helper processes serialize one rotating refresh and reuse the persisted successor after restart', async (t) => {
  const f = await fixture(t); await seed(f, true);
  const results = await Promise.all([helper(f, { op: 'models' }), helper(f, { op: 'models' })]);
  for (const result of results) assert.equal(result[0].data.models[0].slug, 'test-model');
  assert.equal((await readFile(f.config.callsFile, 'utf8')).trim(), 'refresh');
  const state = await f.connection.store.withLock(() => f.connection.store.read());
  assert.equal(state.profiles[0].credentials.refreshToken, 'refresh-new');
  assert.ok(!state.profiles[0].pendingRefresh);
  assert.equal((await helper(f, { op: 'models' }))[0].type, 'result');
  assert.equal((await readFile(f.config.callsFile, 'utf8')).trim(), 'refresh');
});

test('a rotated grant checkpoint recovers in a new process after JWKS outage without reusing the consumed token', async (t) => {
  const f = await fixture(t); await seed(f, true);
  await writeFile(f.mockPath, JSON.stringify({ ...f.config, keysUnavailable: true }));
  const [unavailable] = await helper(f, { op: 'models' }); assert.equal(unavailable.error.code, 'identity_verification_unavailable');
  const checkpoint = await f.connection.store.withLock(() => f.connection.store.read());
  assert.equal(checkpoint.profiles[0].pendingRefresh.credentials.refreshToken, 'refresh-new');
  await writeFile(f.mockPath, JSON.stringify(f.config));
  const [recovered] = await helper(f, { op: 'models' }); assert.equal(recovered.type, 'result');
  assert.equal((await readFile(f.config.callsFile, 'utf8')).trim(), 'refresh');
});

test('terminal usage is actual and allowlisted; missing usage stays null; mid-stream quota failure cannot complete', async (t) => {
  const f = await fixture(t); await seed(f);
  const request = { model: 'test-model', input: [{ role: 'user', content: 'synthetic input' }] };
  const events = await helper(f, { op: 'stream', request });
  assert.equal(events[0].text, 'synthetic reply');
  assert.deepEqual(events[1].usage, { input_tokens: 20, output_tokens: 8, total_tokens: 28, cached_tokens: 3, reasoning_tokens: 2 });
  assert.equal(events[1].responseId, 'resp_test');
  await writeFile(f.mockPath, JSON.stringify({ ...f.config, noUsage: true }));
  assert.equal((await helper(f, { op: 'stream', request }))[1].usage, null);
  await writeFile(f.mockPath, JSON.stringify({ ...f.config, failStream: true }));
  const failure = await helper(f, { op: 'stream', request });
  assert.equal(failure[0].type, 'delta'); assert.equal(failure[1].type, 'error');
  assert.equal(failure[1].error.code, 'subscription_sharing_usage_limit_exceeded');
  assert.ok(!JSON.stringify(failure).includes('private secret'));
});

test('real helper waits for private durable acknowledgement and can cancel before HTTP', async (t) => {
  const f = await fixture(t); await seed(f);
  const auditCallsFile = join(f.directory, 'audit-calls');
  await writeFile(f.mockPath, JSON.stringify({ ...f.config, auditCallsFile }));
  const request = { model: 'test-model', input: [{ role: 'user', content: 'synthetic input' }] };
  const body = { input: [{ content: 'synthetic input', role: 'user' }], model: 'test-model', store: false, stream: true };
  const audit_hash = createHash('sha256').update(JSON.stringify(body)).digest('hex');
  for (const cancel of [false, true]) {
    await writeFile(auditCallsFile, '');
    const events = await new Promise((accept, reject) => {
      const child = spawn(process.execPath, ['--import', pathToFileURL(resolve('test/mock-openai.mjs')).href, resolve('helper.mjs')], { env: {
        ...process.env, HAVEN_GPT_ROOT: f.root, HAVEN_GPT_SCOPE: scope('profile-a'), OMBRE_GPT_KEY_FILE: f.keyFile, HAVEN_TEST_FIXTURE: f.mockPath,
      }, stdio: ['pipe', 'pipe', 'pipe'] });
      const received = [];
      const timeout = setTimeout(() => { child.kill(); reject(new Error('audit helper timed out')); }, 10000);
      const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
      lines.on('line', line => {
        const event = JSON.parse(line); received.push(event);
        if (event.type === 'request') void (async () => {
          await new Promise(resolve => setTimeout(resolve, 30));
          assert.equal(await readFile(auditCallsFile, 'utf8'), '');
          child.stdin.end(cancel ? '{"cancel":true}\n' : '{"audit_ack":true}\n');
        })().catch(error => { child.kill(); reject(error); });
      });
      child.on('error', reject);
      child.on('close', () => { clearTimeout(timeout); accept(received); });
      child.stdin.write(JSON.stringify({ op: 'stream', request, audit_hash }) + '\n');
    });
    assert.equal(events[0].type, 'request');
    if (cancel) {
      assert.equal(events.at(-1).error.code, 'cancelled');
      assert.equal(await readFile(auditCallsFile, 'utf8'), '');
    } else {
      assert.deepEqual(events.map(event => event.type), ['request', 'receipt', 'delta', 'completed']);
      assert.equal(await readFile(auditCallsFile, 'utf8'), 'response\n');
    }
  }
});

test('local handoff durably freezes refresh credentials before transfer and retains encrypted recovery metadata', async (t) => {
  const f = await fixture(t); await seed(f);
  const transfer = await freezeForTransfer(f.connection.store, { host_id: f.transfer.host_id, generation: 0 });
  const saved = await f.connection.store.withLock(() => f.connection.store.read());
  assert.equal(saved.profiles[0].status, 'disconnected'); assert.ok(!saved.profiles[0].credentials);
  assert.equal(saved.havenTransfer.profile.credentials.refreshToken, 'refresh-old');
  assert.equal(saved.havenTransfer.transfer_id, transfer.transfer_id);
  assert.equal(publicSession(await f.connection.client.getSession()).sharing, false);
  assert.ok(!(await readFile(join(f.root, scope('profile-a'), 'chatgpt-auth.json'), 'utf8')).includes('refresh-old'));
});

test('cancelling a real helper process yields one cancelled terminal and preserves its credential file', async (t) => {
  const f = await fixture(t); await seed(f);
  await writeFile(f.mockPath, JSON.stringify({ ...f.config, waitForCancel: true }));
  const before = await readFile(join(f.root, scope('profile-a'), 'chatgpt-auth.json'), 'utf8');
  const events = await new Promise((accept, reject) => {
    const child = spawn(process.execPath, ['--import', pathToFileURL(resolve('test/mock-openai.mjs')).href, resolve('helper.mjs')], { env: {
      ...process.env, HAVEN_GPT_ROOT: f.root, HAVEN_GPT_SCOPE: scope('profile-a'), OMBRE_GPT_KEY_FILE: f.keyFile, HAVEN_TEST_FIXTURE: f.mockPath,
    }, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', cancelled = false;
    const timeout = setTimeout(() => { child.kill(); reject(new Error('cancellation timed out')); }, 5000);
    child.stdout.on('data', (data) => {
      output += data;
      if (!cancelled && output.includes('"type":"delta"')) { cancelled = true; child.stdin.write('{"cancel":true}\n'); }
    });
    child.on('close', () => { clearTimeout(timeout); accept(output.trim().split('\n').map(JSON.parse)); });
    child.stdin.write(JSON.stringify({ op: 'stream', request: { model: 'test-model', input: 'test' } }) + '\n');
  });
  assert.deepEqual(events.map((e) => e.type), ['delta', 'error']);
  assert.equal(events[1].error.code, 'cancelled');
  assert.equal(await readFile(join(f.root, scope('profile-a'), 'chatgpt-auth.json'), 'utf8'), before);
});

test('raw imports are unavailable through the runtime helper even if its environment requests admin access', async (t) => {
  const f = await fixture(t);
  const [result] = await helper(f, { op: 'import', transfer: f.transfer }, { HAVEN_GPT_ADMIN: '1' });
  assert.equal(result.error.code, 'invalid_request');
  assert.equal((await f.connection.client.getSession()).status, 'disconnected');
});

test('key material cannot be placed anywhere in the credential data volume', async (t) => {
  const f = await fixture(t);
  const inside = join(f.directory, 'buckets', 'misplaced-key');
  await writeFile(inside, randomBytes(32).toString('base64'), { mode: 0o600 });
  await assert.rejects(openConnection(f.root, scope('profile-a'), inside), { code: 'encryption_key_unavailable' });
});

test('stdin-only server admin imports and replays a safe receipt across separate processes', async (t) => {
  const f = await fixture(t);
  const invoke = (command) => new Promise((accept, reject) => {
    const child = spawn(process.execPath, ['--import', pathToFileURL(resolve('test/mock-openai.mjs')).href,
      resolve('admin.mjs'), '--buckets-dir', dirname(f.root), '--profile', 'profile-a'], {
      env: { ...process.env, OMBRE_GPT_KEY_FILE: f.keyFile, HAVEN_TEST_FIXTURE: f.mockPath }, stdio: ['pipe', 'pipe', 'pipe'],
    });
    let out = '', err = '';
    child.stdout.on('data', (b) => { out += b; }); child.stderr.on('data', (b) => { err += b; });
    child.on('error', reject);
    child.on('close', (code) => {
      try {
        assert.equal(err, ''); assert.equal(code, 0, out);
        assert.ok(!out.includes('refresh-old')); assert.ok(!out.includes('access-old')); assert.ok(!out.includes(f.profile.profileIdToken));
        accept(JSON.parse(out));
      } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(command));
  });
  const target = await invoke({ op: 'prepare-import' });
  assert.equal(target.host_id, f.transfer.host_id);
  const imported = await invoke({ op: 'import', transfer: f.transfer }); assert.equal(imported.replayed, false);
  const replayed = await invoke({ op: 'import', transfer: f.transfer }); assert.equal(replayed.replayed, true);
  assert.equal(replayed.transfer_id, imported.transfer_id);
});
