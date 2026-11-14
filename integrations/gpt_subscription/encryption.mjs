import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { isAbsolute, relative, sep } from 'node:path';
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { ChatGPTError } from './vendor/dist/errors.js';

// The key is supplied by a separate secret mount, never generated beside ciphertext.
export async function fileEncryption(keyFile, storageRoot, scope) {
  const unavailable = () => new ChatGPTError('encryption_key_unavailable', '授权加密密钥不可用；已有授权已保留。', false);
  try {
    if (!keyFile || !isAbsolute(keyFile)) throw unavailable();
    const keyPath = await realpath(keyFile);
    const root = await realpath(storageRoot);
    const rel = relative(root, keyPath);
    if (!rel || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel))) throw unavailable();
    const file = await open(keyFile, constants.O_RDONLY | (process.platform === 'win32' ? 0 : constants.O_NOFOLLOW));
    let key;
    try {
      const info = await file.stat();
      if (!info.isFile() || info.size > 256 || (process.platform !== 'win32' && (info.mode & 0o077))) throw unavailable();
      const encoded = (await file.readFile('utf8')).trim();
      key = Buffer.from(encoded, 'base64');
      if (key.length !== 32 || key.toString('base64') !== encoded) throw unavailable();
    } finally { await file.close(); }
    const aad = Buffer.from(`haven-gpt-subscription:v1:${scope}`);
    return {
      id: 'haven-aes-256-gcm-v1', isAvailable: () => true,
      encrypt(plain) {
        const iv = randomBytes(12);
        const cipher = createCipheriv('aes-256-gcm', key, iv);
        cipher.setAAD(aad);
        const encrypted = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()]);
        return Buffer.concat([iv, cipher.getAuthTag(), encrypted]);
      },
      decrypt(bytes) {
        const data = Buffer.from(bytes);
        if (data.length < 29) throw unavailable();
        const cipher = createDecipheriv('aes-256-gcm', key, data.subarray(0, 12));
        cipher.setAAD(aad);
        cipher.setAuthTag(data.subarray(12, 28));
        return Buffer.concat([cipher.update(data.subarray(28)), cipher.final()]).toString('utf8');
      },
    };
  } catch { throw unavailable(); }
}
