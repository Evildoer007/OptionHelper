#!/usr/bin/env node
import { execFile as execFileCallback } from 'node:child_process';
import { generateKeyPairSync } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { issueSignedLicense } from './core/license.mjs';

const execFile = promisify(execFileCallback);
const root = dirname(fileURLToPath(import.meta.url));
const publicKeyPath = resolve(root, 'distribution', 'license-public.pem');
const keychainService = 'OptionHelperLicenseIssuer';
const keychainAccount = 'ed25519-private';

const usage = () => {
  console.log(`用法：
  node assets/web-design/optionhelper-license.mjs init
  node assets/web-design/optionhelper-license.mjs init --replace
  node assets/web-design/optionhelper-license.mjs issue <optchat|optdesk|admin> <签发对象> [--version 1.0.0] [--out /绝对路径/OptionHelper.license]

init仅由发行方在安全的macOS主机执行一次。私钥以Base64形式保存于该主机钥匙串，distribution/license-public.pem随安装包分发。`);
};

const privateKeyFormatError = () => new Error('发行方私钥格式无效。请在确认后使用init --replace重新初始化。');
const isKeychainItemMissing = (error) => /could not be found|item.*not found/i.test(`${error?.stderr || ''}${error?.message || ''}`);

const savePrivateKey = async (pem) => execFile('/usr/bin/security', [
  'add-generic-password', '-U', '-s', keychainService, '-a', keychainAccount, '-w', Buffer.from(pem).toString('base64'),
]);
const readPrivateKey = async () => {
  const { stdout } = await execFile('/usr/bin/security', ['find-generic-password', '-w', '-s', keychainService, '-a', keychainAccount]);
  const stored = stdout.trim();
  if (!stored) throw privateKeyFormatError();
  const pem = Buffer.from(stored, 'base64').toString('utf8');
  if (!pem.startsWith('-----BEGIN PRIVATE KEY-----')) throw privateKeyFormatError();
  return pem;
};

const runInit = async (replace = false) => {
  if (process.platform !== 'darwin') throw new Error('许可证签发工具仅支持macOS钥匙串。');
  try {
    await readPrivateKey();
    if (!replace) throw new Error('发行方私钥已存在。为避免覆盖，请继续使用现有签发环境。');
  } catch (error) {
    if (!replace && !isKeychainItemMissing(error)) throw error;
  }
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  await savePrivateKey(privateKey.export({ type: 'pkcs8', format: 'pem' }));
  await fs.mkdir(dirname(publicKeyPath), { recursive: true });
  await fs.writeFile(publicKeyPath, publicKey.export({ type: 'spki', format: 'pem' }), { mode: 0o644 });
  console.log(`签发环境已初始化。请将${publicKeyPath}随安装包分发；私钥仅保存在当前macOS钥匙串。`);
};

const runIssue = async (args) => {
  const [tier, holder, ...rest] = args;
  if (!tier || !holder) throw new Error('请填写许可证层级和签发对象。');
  let productVersion = '1.0.0';
  let outputPath = null;
  for (let index = 0; index < rest.length; index += 1) {
    if (rest[index] === '--version') productVersion = rest[++index] || '';
    else if (rest[index] === '--out') outputPath = rest[++index] || '';
    else throw new Error(`无法识别参数：${rest[index]}`);
  }
  if (!outputPath && process.stdout.isTTY) throw new Error('请使用--out写入许可证文件，避免在终端中遗留许可证内容。');
  const license = issueSignedLicense({ holder, tier, productVersion, privateKey: await readPrivateKey() });
  const content = `${JSON.stringify(license, null, 2)}\n`;
  if (outputPath) {
    const resolved = resolve(outputPath);
    await fs.writeFile(resolved, content, { mode: 0o600, flag: 'wx' });
    console.log(`许可证已写入${resolved}`);
  } else process.stdout.write(content);
};

const [command, ...args] = process.argv.slice(2);
try {
  if (command === 'init') await runInit(args[0] === '--replace');
  else if (command === 'issue') await runIssue(args);
  else { usage(); process.exitCode = 1; }
} catch (error) {
  console.error(`许可证操作未完成：${error.message || '未知错误'}`);
  process.exitCode = 1;
}
