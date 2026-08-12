import { createPublicKey, randomUUID, sign, verify } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { activationPath, bundledPublicKeyPath, preferencePath, readPrivateJson, writePrivateJson } from './paths.mjs';

const allowedTiers = new Set(['optchat', 'optdesk', 'admin']);

export const tierProfile = (tier) => {
  if (tier === 'optchat') return { tier, allowedModes: ['chat'] };
  if (tier === 'optdesk' || tier === 'admin') return { tier, allowedModes: ['chat', 'desk'] };
  return null;
};

const signable = (license) => JSON.stringify({
  schemaVersion: 1,
  licenseId: license.licenseId,
  holder: license.holder,
  tier: license.tier,
  issuedAt: license.issuedAt,
  productVersion: license.productVersion,
});

const normalizeLicense = (input) => {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('许可证格式无效。');
  const license = {
    schemaVersion: input.schemaVersion,
    licenseId: typeof input.licenseId === 'string' ? input.licenseId.trim() : '',
    holder: typeof input.holder === 'string' ? input.holder.trim() : '',
    tier: typeof input.tier === 'string' ? input.tier.trim().toLowerCase() : '',
    issuedAt: typeof input.issuedAt === 'string' ? input.issuedAt.trim() : '',
    productVersion: typeof input.productVersion === 'string' ? input.productVersion.trim() : '',
    signature: typeof input.signature === 'string' ? input.signature.trim() : '',
  };
  if (license.schemaVersion !== 1 || !license.licenseId || !license.holder || !allowedTiers.has(license.tier)
    || !license.issuedAt || Number.isNaN(Date.parse(license.issuedAt)) || !license.productVersion || !license.signature) {
    throw new Error('许可证字段不完整或不兼容。');
  }
  return license;
};

const publicKeyPem = async () => {
  if (process.env.OPTIONHELPER_LICENSE_PUBLIC_KEY) return process.env.OPTIONHELPER_LICENSE_PUBLIC_KEY;
  try {
    return await fs.readFile(bundledPublicKeyPath, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') throw new Error('当前安装包未配置许可证公钥。请联系发行方。');
    throw error;
  }
};

export const verifyLicense = async (input) => {
  const license = normalizeLicense(typeof input === 'string' ? JSON.parse(input) : input);
  let publicKey;
  try {
    publicKey = createPublicKey(await publicKeyPem());
  } catch {
    throw new Error('许可证公钥无效。请联系发行方。');
  }
  let valid = false;
  try {
    valid = verify(null, Buffer.from(signable(license)), publicKey, Buffer.from(license.signature, 'base64'));
  } catch {
    valid = false;
  }
  if (!valid) throw new Error('许可证签名校验未通过。请导入发行方提供的原始文件。');
  return license;
};

export const importLicense = async (input) => {
  const license = await verifyLicense(input);
  await writePrivateJson(activationPath, { license, importedAt: new Date().toISOString() });
  const preferences = await readPrivateJson(preferencePath, {});
  await writePrivateJson(preferencePath, { ...preferences, mode: 'chat', updatedAt: new Date().toISOString() });
  return { license, profile: tierProfile(license.tier) };
};

export const currentActivation = async () => {
  const saved = await readPrivateJson(activationPath);
  if (!saved?.license) return null;
  try {
    const license = await verifyLicense(saved.license);
    return { license, profile: tierProfile(license.tier), importedAt: saved.importedAt };
  } catch {
    return null;
  }
};

export const issueSignedLicense = ({ holder, tier, productVersion = '1.0.0', privateKey }) => {
  if (!allowedTiers.has(tier)) throw new Error('许可证层级只能是optchat、optdesk或admin。');
  const normalizedHolder = typeof holder === 'string' ? holder.trim() : '';
  if (!normalizedHolder) throw new Error('请填写签发对象。');
  const license = {
    schemaVersion: 1,
    licenseId: randomUUID(),
    holder: normalizedHolder,
    tier,
    issuedAt: new Date().toISOString(),
    productVersion,
  };
  license.signature = sign(null, Buffer.from(signable(license)), privateKey).toString('base64');
  return license;
};
