// Interactive admin utility. Never emits credentials or authorization URLs.
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, open, rename, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createChatGPT } from './vendor/dist/index.js';
import { ConnectionStore } from './vendor/dist/storage.js';
import { ChatGPTError, asError } from './vendor/dist/errors.js';
import { APP } from './connection.mjs';
import { fileEncryption } from './encryption.mjs';

function child(command, args, input, timeout = 120_000, acceptFailureOutput = false) {
  return new Promise((accept, reject) => {
    const process = spawn(command, args, { stdio: ['pipe', 'pipe', 'ignore'], shell: false, windowsHide: true });
    const output = [];
    let bytes = 0;
    const timer = setTimeout(() => { process.kill(); reject(new ChatGPTError('transfer_unconfirmed', '未确认交接成功；本机授权保持冻结，请用 --retry 重试。')); }, timeout);
    process.on('error', () => { clearTimeout(timer); reject(new ChatGPTError('helper_unavailable', '无法启动登录或 SSH 辅助程序。')); });
    process.stdout.on('data', (data) => {
      bytes += data.length;
      if (bytes <= 4 * 1024 * 1024) output.push(data);
      else process.kill();
    });
    process.on('close', (code) => {
      clearTimeout(timer);
      if ((code !== 0 && !acceptFailureOutput) || bytes > 4 * 1024 * 1024) reject(new ChatGPTError('transfer_unconfirmed', '操作未确认成功；如已交接，本机仍保持冻结。'));
      else accept(Buffer.concat(output).toString('utf8').trim());
    });
    process.stdin.on('error', () => {});
    process.stdin.end(input);
  });
}

async function localEncryption(directory, scope) {
  if (process.platform !== 'win32') return fileEncryption(process.env.OMBRE_GPT_LOCAL_KEY_FILE, dirname(directory), scope);
  const ps = (expression, input) => child('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    'Add-Type -AssemblyName System.Security; $value=[Console]::In.ReadToEnd(); ' + expression], input);
  return {
    id: 'windows-dpapi-current-user-v1', isAvailable: () => true,
    encrypt: async (plain) => Buffer.from(await ps('[Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect([Convert]::FromBase64String($value),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))', Buffer.from(plain).toString('base64')), 'base64'),
    decrypt: async (bytes) => Buffer.from(await ps('[Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($value),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))', Buffer.from(bytes).toString('base64')), 'base64').toString('utf8'),
  };
}

export async function freezeForTransfer(store, target) {
  return store.withLock(async () => {
    const state = await store.read();
    const profile = state?.profiles.find((p) => p.id === state.activeProfileId);
    if (!profile?.credentials?.refreshToken || profile.status !== 'connected' || !profile.scopes.includes('chatgpt.tokens.use.direct')) throw new ChatGPTError('sign_in_required', '请先完成订阅授权。');
    const transfer = { version: 1, transfer_id: randomUUID(), host_id: target.host_id, generation: target.generation, profile: structuredClone(profile) };
    // Durable checkpoint BEFORE SSH starts. The transferable record is encrypted,
    // and local active credentials are removed, so this utility cannot refresh it.
    state.havenTransfer = transfer;
    state.profiles = state.profiles.map((p) => {
      if (p.id !== profile.id) return p;
      const { credentials, pendingRefresh, ...metadata } = p;
      return { ...metadata, status: 'disconnected', scopes: [] };
    });
    await store.write(state);
    return transfer;
  });
}

async function main() {
  const args = process.argv.slice(2);
  const value = (flag) => args[args.indexOf(flag) + 1];
  if (!args.includes('--host') || !args.includes('--remote-command')) {
    console.error('用法: node login-transfer.mjs --host <SSH别名> --remote-command <Haven导入命令> [--retry] [--reconsent]');
    process.exitCode = 2; return;
  }
  const host = value('--host');
  const remoteCommand = value('--remote-command');
  if (!/^[A-Za-z0-9_.@-]+$/.test(host) || host.startsWith('-') || !remoteCommand || /[\r\n\0]/.test(remoteCommand)) throw new ChatGPTError('invalid_config', 'SSH 参数无效。');
  const scope = createHash('sha256').update(`${host}\0${remoteCommand}`).digest('hex');
  const directory = resolve(dirname(fileURLToPath(import.meta.url)), '.local-auth', scope);
  await mkdir(dirname(directory), { recursive: true, mode: 0o700 });
  const encryption = await localEncryption(directory, scope);
  const store = new ConnectionStore(directory, encryption);
  const remote = async (command) => {
    let result;
    try { result = JSON.parse(await child('ssh', ['-T', '-o', 'BatchMode=yes', host, remoteCommand], JSON.stringify(command), 120_000, true)); }
    catch { throw new ChatGPTError('transfer_unconfirmed', '未确认远端操作成功；已冻结的本机记录已保留，可用 --retry 重试。'); }
    if (result.ok !== true) {
      // Static recovery messages only: never echo remote output or arbitrary text.
      const recoveries = {
        import_expired: '交接授权已过期。请不带 --retry 重新运行命令并登录。',
        import_conflict: '服务器授权版本已变化。请不带 --retry 重新准备交接并登录。',
        host_mismatch: '服务器身份已变化。先核对目标，再不带 --retry 重新登录。',
        registration_owned: '该登记已属于另一个 Haven 身份，当前身份需要独立的新授权登记。',
        invalid_id_token: '授权身份未通过校验。请不带 --retry 重新登录。',
        identity_verification_unavailable: '身份验证服务暂不可用；本机记录已保留，请稍后用 --retry 重试。',
        encryption_key_unavailable: '服务器密钥文件不可用；先修复密钥配置，再用 --retry 重试。',
      };
      const code = result.error?.code;
      throw new ChatGPTError(Object.hasOwn(recoveries, code) ? code : 'transfer_unconfirmed', recoveries[code] ?? '远端未接受授权；本机记录已保留。请核对服务器配置，或重新登录。');
    }
    return result;
  };
  // One interprocess utility lock covers sign-in + checkpoint + transfer.
  const lockfile = (await import('proper-lockfile')).default;
  const release = await lockfile.lock(dirname(directory), { lockfilePath: `${directory}.transfer-lock`, realpath: false, stale: 15 * 60_000, update: 20_000, retries: 0 });
  try {
    let transfer;
    if (args.includes('--retry')) {
      transfer = await store.withLock(async () => (await store.read())?.havenTransfer);
      if (!transfer) throw new ChatGPTError('no_pending_transfer', '没有待重试的授权交接。');
    } else {
      const target = await remote({ op: 'prepare-import' });
      if (!/^urn:uuid:[a-f0-9-]{36}$/.test(target.host_id) || !Number.isSafeInteger(target.generation)) throw new ChatGPTError('invalid_target', '目标身份无效。');
      // Only the local helper adopts the target's host ID. The VM never imports ours.
      await store.withLock(async () => {
        const temporary = join(directory, `host-${randomUUID()}.tmp`);
        try {
          const file = await open(temporary, 'wx', 0o600);
          try { await file.writeFile(JSON.stringify({ version: 1, id: target.host_id })); await file.sync(); } finally { await file.close(); }
          await rename(temporary, join(directory, 'chatgpt-host.json'));
        } finally { await rm(temporary, { force: true }); }
      });
      const client = createChatGPT({ ...APP, storageDir: directory, credentialEncryption: encryption });
      console.error('即将在本机浏览器登录 Ombre Brain Haven。交接开始后，本机只保留加密的恢复记录。');
      await client.signIn({ reconsent: args.includes('--reconsent') });
      transfer = await freezeForTransfer(store, target);
    }
    const receipt = await remote({ op: 'import', transfer });
    if (!receipt.imported || receipt.transfer_id !== transfer.transfer_id) throw new ChatGPTError('transfer_unconfirmed', '交接回执不匹配；请用 --retry 重试。');
    await store.withLock(async () => {
      const state = await store.read();
      if (state?.havenTransfer?.transfer_id === transfer.transfer_id) {
        delete state.havenTransfer;
        await store.write(state);
      }
    });
    console.log('授权已交接给 Haven；只有 Haven 负责后续刷新。');
  } finally { await release(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(asError(error).message); process.exitCode = 1; });
}
