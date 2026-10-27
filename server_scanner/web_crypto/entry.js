import { gcm } from '@noble/ciphers/aes';
import { pbkdf2Async } from '@noble/hashes/pbkdf2';
import { sha256 } from '@noble/hashes/sha256';

// 密钥仅保留在页面内存，不写入浏览器存储。
globalThis.reportCrypto = {
  derive: async (password, salt) => {
    const input = new TextEncoder().encode(password);
    if (globalThis.crypto?.subtle) {
      const material = await crypto.subtle.importKey('raw', input, 'PBKDF2', false, ['deriveBits']);
      return new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: 210000 }, material, 256));
    }
    return pbkdf2Async(sha256, input, salt, { c: 210000, dkLen: 32 });
  },
  decrypt: (key, nonce, ciphertext) => gcm(key, nonce).decrypt(ciphertext),
};
