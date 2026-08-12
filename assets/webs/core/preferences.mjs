import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { connectionPath, preferencePath, readPrivateJson, writePrivateJson } from './paths.mjs';

const providers = new Set(['deepseek', 'openai', 'anthropic', 'gemini', 'compatible']);
const connectionIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const taskIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
let workspacePreferenceWrites = Promise.resolve();

const readWorkspacePreferences = async () => {
  const saved = await readPrivateJson(preferencePath, {});
  return {
    mode: saved?.mode === 'desk' ? 'desk' : 'chat',
    lastChatTaskId: taskIdPattern.test(saved?.lastChatTaskId || '') ? saved.lastChatTaskId : null,
    lastDeskTaskId: taskIdPattern.test(saved?.lastDeskTaskId || '') ? saved.lastDeskTaskId : null,
  };
};

const saveWorkspacePreferences = (changes) => {
  const write = workspacePreferenceWrites.then(async () => {
    const current = await readWorkspacePreferences();
    const next = { ...current, ...changes, updatedAt: new Date().toISOString() };
    await writePrivateJson(preferencePath, next);
    return next;
  });
  workspacePreferenceWrites = write.catch(() => undefined);
  return write;
};

export const getModePreference = async () => (await readWorkspacePreferences()).mode;

export const getWorkspaceTaskPreference = async () => {
  const { lastChatTaskId, lastDeskTaskId } = await readWorkspacePreferences();
  return { lastChatTaskId, lastDeskTaskId };
};

export const setModePreference = async (mode, profile) => {
  if (!profile?.allowedModes?.includes(mode)) throw new Error('当前许可证不允许使用该模式。');
  await saveWorkspacePreferences({ mode });
  return mode;
};

export const setWorkspaceTaskPreference = async (mode, taskId, profile) => {
  if (!['chat', 'desk'].includes(mode)) throw new Error('任务模式无效。');
  if (mode === 'desk' && !profile?.allowedModes?.includes('desk')) throw new Error('当前许可证不允许使用Desk。');
  if (!taskIdPattern.test(taskId || '')) throw new Error('任务ID无效。');
  const key = mode === 'desk' ? 'lastDeskTaskId' : 'lastChatTaskId';
  return saveWorkspacePreferences({ [key]: taskId });
};

const normalizeConnection = (connection, fallbackId = randomUUID()) => {
  if (!connection || !providers.has(connection.provider) || typeof connection.model !== 'string' || !connection.model.trim()) return null;
  const id = connectionIdPattern.test(connection.id || '') ? connection.id : fallbackId;
  return {
    id,
    provider: connection.provider,
    model: connection.model.trim(),
    baseUrl: typeof connection.baseUrl === 'string' ? connection.baseUrl.trim() : '',
    thinking: connection.thinking === 'enabled' ? 'enabled' : 'disabled',
    updatedAt: connection.updatedAt || new Date().toISOString(),
  };
};

const loadConnectionStore = async () => {
  const saved = await readPrivateJson(connectionPath);
  if (!saved) return { activeConnectionId: null, connections: [] };
  if (Array.isArray(saved.connections)) {
    const connections = saved.connections.map((connection) => normalizeConnection(connection)).filter(Boolean);
    const activeConnectionId = connections.some((connection) => connection.id === saved.activeConnectionId)
      ? saved.activeConnectionId
      : connections[0]?.id || null;
    return { activeConnectionId, connections };
  }
  const legacy = normalizeConnection(saved);
  if (!legacy) return { activeConnectionId: null, connections: [] };
  const migrated = { activeConnectionId: legacy.id, connections: [legacy] };
  await writePrivateJson(connectionPath, migrated);
  return migrated;
};

const persistConnectionStore = async (store) => {
  const activeConnectionId = store.connections.some((connection) => connection.id === store.activeConnectionId)
    ? store.activeConnectionId
    : store.connections[0]?.id || null;
  const normalized = { activeConnectionId, connections: store.connections };
  await writePrivateJson(connectionPath, normalized);
  return normalized;
};

export const getConnections = async () => (await loadConnectionStore()).connections;

export const getConnection = async (connectionId = null) => {
  const store = await loadConnectionStore();
  if (connectionId !== null) return store.connections.find((connection) => connection.id === connectionId) || null;
  return store.connections.find((connection) => connection.id === store.activeConnectionId) || null;
};

export const saveConnection = async (connection) => {
  const normalized = normalizeConnection(connection);
  if (!normalized) throw new Error('请选择模型服务并填写模型名称。');
  const store = await loadConnectionStore();
  const existingIndex = store.connections.findIndex((item) => item.id === normalized.id);
  if (existingIndex >= 0) store.connections.splice(existingIndex, 1, normalized);
  else store.connections.push(normalized);
  store.activeConnectionId = normalized.id;
  await persistConnectionStore(store);
  return normalized;
};

export const setActiveConnection = async (connectionId) => {
  if (!connectionIdPattern.test(connectionId || '')) throw new Error('模型连接无效。');
  const store = await loadConnectionStore();
  const connection = store.connections.find((item) => item.id === connectionId);
  if (!connection) throw new Error('未找到指定模型连接。');
  store.activeConnectionId = connection.id;
  await persistConnectionStore(store);
  return connection;
};

export const clearConnection = async (connectionId = null) => {
  if (connectionId === null) {
    try { await fs.unlink(connectionPath); } catch (error) { if (error.code !== 'ENOENT') throw new Error('模型连接元信息无法清除。'); }
    return null;
  }
  const store = await loadConnectionStore();
  const removed = store.connections.find((connection) => connection.id === connectionId) || null;
  if (!removed) return null;
  store.connections = store.connections.filter((connection) => connection.id !== connectionId);
  if (!store.connections.length) {
    await clearConnection();
    return removed;
  }
  if (store.activeConnectionId === connectionId) store.activeConnectionId = store.connections[0].id;
  await persistConnectionStore(store);
  return removed;
};
