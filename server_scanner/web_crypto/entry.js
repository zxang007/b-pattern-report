import { gcm } from '@noble/ciphers/aes';
import { pbkdf2Async } from '@noble/hashes/pbkdf2';
import { sha256 } from '@noble/hashes/sha256';

// 密钥仅保留在页面内存，不写入浏览器存储。
window.reportCrypto = {
  derive: (password, salt) => pbkdf2Async(sha256, new TextEncoder().encode(password), salt, { c: 210000, dkLen: 32 }),
  decrypt: (key, nonce, ciphertext) => gcm(key, nonce).decrypt(ciphertext),
};
