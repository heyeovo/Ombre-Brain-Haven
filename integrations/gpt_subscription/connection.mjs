import { createHash } from 'node:crypto';
import { mkdir, lstat, chmod } from 'node:fs/promises';
import { isAbsolute, join, dirname } from 'node:path';
import { createChatGPT } from './vendor/dist/index.js';
import { ChatGPTError } from './vendor/dist/errors.js';
import { ConnectionStore, validateState } from './vendor/dist/storage.js';
import { verifyIdentity } from './vendor/dist/oauth.js';
import { listModels } from './vendor/dist/models.js';
import { fileEncryption } from './encryption.mjs';

export const APP = { appName: 'Ombre Brain Haven', appId: 'ombre-brain-haven', redirectPort: 0, sendHostId: true };
export const USAGE_URL = 'https://chatgpt.com/settings/usage';
const empty = () => ({ version: 2, profiles: [], pendingRegistrations: [] });
const fail = (code, message) => { throw new ChatGPTError(code, message); };
const ownershipStores = new WeakMap();

export async function openConnection(root, scope, keyFile) {
  if (!isAbsolute(root) || !/^[a-f0-9]{64}$/.test(scope)) fail('invalid_config', '授权存储配置无效。');
  await mkdir(root, { recursive: true, mode: 0o700 });
  const info = await lstat(root);
  if (!info.isDirectory() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid())) fail('storage_unsafe', '授权目录必须属于运行服务的账号。');
  if (process.platform !== 'win32') await chmod(root, 0o700);
  // The key must be outside the entire buckets volume, not just the profile subdirectory.
  const encryption = await fileEncryption(keyFile, dirname(root), scope);
  const directory = join(root, scope);
  const store = new ConnectionStore(directory, encryption);
  ownershipStores.set(store, { scope, store: new ConnectionStore(join(root, '.registrations'),
    await fileEncryption(keyFile, dirname(root), 'registration-owners')) });
  const client = createChatGPT({ ...APP, storageDir: directory, credentialEncryption: encryption });
  return { store, client };
}

export function publicSession(session) {
  const email = session.identity?.email;
  return {
    configured: true, login_method: 'local_ssh_import',
    status: session.status, sharing: session.sharing,
    ...(session.profileId ? { connection_id: session.profileId } : {}),
    ...(session.profileLabel ? { label: session.profileLabel } : {}),
    ...(typeof email === 'string' ? { account: `${email.slice(0, 1)}***` } : {}),
    ...(session.error ? { error: session.error } : {}),
    usage_url: USAGE_URL, limits: null,
  };
}

export async function prepareImport(store) {
  return store.withLock(async () => {
    const state = (await store.read()) ?? empty();
    return { host_id: await store.getHostId(), generation: state.haven?.generation ?? 0 };
  });
}

// Trusted stdin-only admin entry. Never register this operation as a public route.
export async function importConnection(store, transfer) {
  if (!transfer || transfer.version !== 1 || !/^[a-f0-9-]{36}$/.test(transfer.transfer_id ?? '') ||
      !Number.isSafeInteger(transfer.generation) || transfer.generation < 0) fail('invalid_import', '授权交接格式无效。');
  const candidate = transfer.profile;
  validateState({ version: 2, profiles: [candidate], activeProfileId: candidate?.id, pendingRegistrations: [] });
  if (candidate.status !== 'connected' || !candidate.subject || !candidate.profileIdToken ||
      !candidate.credentials?.refreshToken || candidate.pendingRefresh ||
      !candidate.scopes.includes('chatgpt.tokens.use.direct') || !candidate.scopes.includes('offline_access')) fail('invalid_import', '请完成一次新的订阅授权后重试导入。');
  const digest = createHash('sha256').update(JSON.stringify(transfer)).digest('hex');
  const ownership = ownershipStores.get(store);
  if (!ownership) fail('invalid_config', '导入必须使用 Haven 授权存储。');
  return ownership.store.withLock(async () => {
    const owners = (await ownership.store.read()) ?? empty();
    const registration = createHash('sha256').update(candidate.clientId).digest('hex');
    const existingOwner = owners.haven?.owners?.[registration];
    if (existingOwner && existingOwner !== ownership.scope) fail('registration_owned', '此授权登记已属于另一个 Haven 身份，请为当前身份重新登录并创建独立登记。');
    return store.withLock(async () => {
    const state = (await store.read()) ?? empty();
    const hostId = await store.getHostId();
    if (transfer.host_id !== hostId) fail('host_mismatch', '目标服务器身份已变化，导入已停止。');
    const receipt = state.haven?.imports?.find((entry) => entry.id === transfer.transfer_id);
    if (receipt) {
      if (receipt.digest !== digest) fail('import_conflict', '同一交接编号不能用于不同授权。');
      return { imported: true, replayed: true, transfer_id: transfer.transfer_id };
    }
    if ((state.haven?.generation ?? 0) !== transfer.generation) fail('import_conflict', '服务器授权已发生变化，请重新准备导入。');
    if (candidate.credentials.expiresAt <= Date.now() + 60_000) fail('import_expired', '交接已过期；请在本机重新登录。');
    const prior = state.profiles.find((entry) => entry.id === candidate.id || entry.clientId === candidate.clientId);
    if (prior && (prior.id !== candidate.id || prior.clientId !== candidate.clientId || prior.subject !== candidate.subject)) fail('account_mismatch', '授权与原有账号记录不一致。');
    // Validate the signed client/subject and access grant, without consuming a refresh token.
    const verified = await verifyIdentity(candidate.profileIdToken, candidate.clientId);
    if (verified.subject !== candidate.subject) fail('account_mismatch', '导入账号身份校验失败。');
    await listModels(candidate.credentials.accessToken, AbortSignal.timeout(30_000));
    const profile = { ...candidate, ...verified };
    if (state.profiles.some((entry) => entry.id !== profile.id && entry.label === profile.label)) profile.label = `Connection ${profile.id}`;
    state.profiles = [...state.profiles.filter((entry) => entry.id !== profile.id), profile];
    state.pendingRegistrations = state.pendingRegistrations.filter((entry) => entry.id !== profile.id);
    state.activeProfileId = profile.id;
    state.haven = { generation: transfer.generation + 1, imports: [...(state.haven?.imports ?? []), { id: transfer.transfer_id, digest }] };
    // Claim first. A later write failure conservatively retains this ownership,
    // so another profile can never race a second copy of the rotating grant.
    owners.haven = { owners: { ...(owners.haven?.owners ?? {}), [registration]: ownership.scope } };
    await ownership.store.write(owners);
    await store.write(state);
    return { imported: true, replayed: false, transfer_id: transfer.transfer_id };
    });
  });
}
