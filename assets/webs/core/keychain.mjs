import { execFile as execFileCallback } from 'node:child_process';
import { promisify } from 'node:util';

const execFile = promisify(execFileCallback);
const service = 'OptionHelperModelConnection';

const validateProvider = (provider) => {
  if (!['deepseek', 'openai', 'anthropic', 'gemini', 'compatible'].includes(provider)) throw new Error('模型服务无效。');
};

const keychainError = (error) => {
  const message = `${error?.stderr || ''}${error?.message || ''}`;
  if (/could not be found|item.*not found/i.test(message)) return new Error('尚未保存该模型服务的API Key。');
  return new Error('无法访问macOS钥匙串。请确认当前用户已解锁钥匙串。');
};

export const saveApiKey = async (provider, apiKey) => {
  validateProvider(provider);
  if (process.platform !== 'darwin') throw new Error('当前版本仅支持在macOS钥匙串中保存API Key。');
  if (typeof apiKey !== 'string' || apiKey.trim().length < 8) throw new Error('API Key格式无效。');
  try {
    await execFile('/usr/bin/security', ['add-generic-password', '-U', '-s', service, '-a', provider, '-w', apiKey.trim()]);
  } catch (error) {
    throw keychainError(error);
  }
};

export const readApiKey = async (provider) => {
  validateProvider(provider);
  if (process.platform !== 'darwin') throw new Error('当前版本仅支持在macOS钥匙串中读取API Key。');
  try {
    const { stdout } = await execFile('/usr/bin/security', ['find-generic-password', '-w', '-s', service, '-a', provider]);
    const value = stdout.trim();
    if (!value) throw new Error('empty');
    return value;
  } catch (error) {
    throw keychainError(error);
  }
};

export const deleteApiKey = async (provider) => {
  validateProvider(provider);
  if (process.platform !== 'darwin') return;
  try {
    await execFile('/usr/bin/security', ['delete-generic-password', '-s', service, '-a', provider]);
  } catch (error) {
    if (!/could not be found|item.*not found/i.test(`${error?.stderr || ''}${error?.message || ''}`)) throw keychainError(error);
  }
};
