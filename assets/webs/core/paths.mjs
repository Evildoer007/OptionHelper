import { promises as fs } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const defaultHome = resolve(homedir(), 'Library', 'Application Support', 'OptionHelper');

export const appDataDir = resolve(process.env.OPTIONHELPER_HOME || defaultHome);
export const activationPath = resolve(appDataDir, 'activation.json');
export const preferencePath = resolve(appDataDir, 'preferences.json');
export const connectionPath = resolve(appDataDir, 'model-connection.json');
export const reportDatabasePath = resolve(appDataDir, 'reports.sqlite');
export const taskDatabasePath = resolve(appDataDir, 'tasks.sqlite');
export const bundledPublicKeyPath = resolve(webRoot, 'distribution', 'license-public.pem');

export const ensureAppDataDir = async () => {
  await fs.mkdir(appDataDir, { recursive: true, mode: 0o700 });
  await fs.chmod(appDataDir, 0o700);
};

export const readPrivateJson = async (filePath, fallback = null) => {
  try {
    return JSON.parse(await fs.readFile(filePath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return fallback;
    throw new Error('本机配置文件无法读取。');
  }
};

export const writePrivateJson = async (filePath, value) => {
  await ensureAppDataDir();
  const temporaryPath = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  await fs.writeFile(temporaryPath, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await fs.chmod(temporaryPath, 0o600);
  await fs.rename(temporaryPath, filePath);
  await fs.chmod(filePath, 0o600);
};
