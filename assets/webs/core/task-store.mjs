import { randomUUID } from 'node:crypto';
import { chmodSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { ensureAppDataDir, taskDatabasePath } from './paths.mjs';
import { migrateLegacyTaskScope } from './task-scope-migration.mjs';

let database;

const now = () => new Date().toISOString();
const validId = (value) => typeof value === 'string' && /^[0-9a-f-]{36}$/i.test(value);
const text = (value, maximum = 10_000) => typeof value === 'string' && value.trim() && value.trim().length <= maximum ? value.trim() : null;
const taskScopes = new Set(['chat', 'desk']);
const taskScope = (value, fallback = 'chat') => taskScopes.has(value) ? value : fallback;

const getDatabase = async () => {
  if (database) return database;
  await migrateLegacyTaskScope();
  await ensureAppDataDir();
  database = new DatabaseSync(taskDatabasePath);
  try { chmodSync(taskDatabasePath, 0o600); } catch { /* first launch may defer permission update */ }
  database.exec(`
    CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      context_summary TEXT NOT NULL DEFAULT '',
      summary_through_seq INTEGER NOT NULL DEFAULT 0,
      scope TEXT NOT NULL DEFAULT 'chat' CHECK(scope IN ('chat', 'desk')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS task_messages (
      id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
      content TEXT NOT NULL,
      provider TEXT,
      model TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(task_id, sequence)
    );
    CREATE INDEX IF NOT EXISTS task_messages_task_sequence ON task_messages(task_id, sequence);
    CREATE TABLE IF NOT EXISTS task_modules (
      task_id TEXT NOT NULL,
      module_name TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(task_id, module_name)
    );
  `);
  const taskColumns = database.prepare('PRAGMA table_info(tasks)').all().map((column) => column.name);
  if (!taskColumns.includes('pinned')) database.exec('ALTER TABLE tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0');
  if (!taskColumns.includes('scope')) database.exec("ALTER TABLE tasks ADD COLUMN scope TEXT NOT NULL DEFAULT 'chat' CHECK(scope IN ('chat', 'desk'))");
  return database;
};

const hydrateTask = (row) => row && ({
  id: row.id,
  title: row.title,
  createdAt: row.created_at,
  updatedAt: row.updated_at,
  pinned: Boolean(row.pinned),
  scope: taskScope(row.scope),
});

const hydrateMessage = (row) => row && ({
  id: row.id,
  sequence: row.sequence,
  role: row.role,
  content: row.content,
  provider: row.provider || null,
  model: row.model || null,
  createdAt: row.created_at,
});

const taskOrThrow = async (id) => {
  if (!validId(id)) throw new Error('任务ID无效。');
  const db = await getDatabase();
  const row = db.prepare('SELECT * FROM tasks WHERE id = ?').get(id);
  if (!row) {
    const error = new Error('未找到该任务。');
    error.statusCode = 404;
    throw error;
  }
  return row;
};

export const createTask = async (input = {}) => {
  const db = await getDatabase();
  const timestamp = now();
  const task = {
    id: randomUUID(),
    title: text(input.title, 80) || '新建研究任务',
    createdAt: timestamp,
    updatedAt: timestamp,
    pinned: false,
    scope: taskScope(input.scope),
  };
  db.prepare('INSERT INTO tasks (id, title, scope, created_at, updated_at) VALUES (?, ?, ?, ?, ?)')
    .run(task.id, task.title, task.scope, task.createdAt, task.updatedAt);
  return task;
};

export const listTasks = async (scope = null) => {
  const db = await getDatabase();
  if (scope !== null && !taskScopes.has(scope)) throw new Error('任务归属无效。');
  const rows = scope
    ? db.prepare('SELECT * FROM tasks WHERE scope = ? ORDER BY pinned DESC, updated_at DESC LIMIT 60').all(scope)
    : db.prepare('SELECT * FROM tasks ORDER BY pinned DESC, updated_at DESC LIMIT 60').all();
  return rows.map(hydrateTask);
};

export const readTask = async (id) => {
  const row = await taskOrThrow(id);
  const db = await getDatabase();
  const modules = Object.fromEntries(db.prepare('SELECT module_name, payload_json FROM task_modules WHERE task_id = ?').all(id)
    .map((item) => {
      try { return [item.module_name, JSON.parse(item.payload_json)]; } catch { return [item.module_name, {}]; }
    }));
  return { task: hydrateTask(row), modules };
};

export const updateTask = async (id, input = {}) => {
  const row = await taskOrThrow(id);
  const title = input.title === undefined ? row.title : text(input.title, 80);
  if (!title) throw new Error('任务标题无效。');
  if (input.pinned !== undefined && typeof input.pinned !== 'boolean') throw new Error('置顶状态无效。');
  const pinned = input.pinned === undefined ? Boolean(row.pinned) : input.pinned;
  const updatedAt = now();
  const db = await getDatabase();
  db.prepare('UPDATE tasks SET title = ?, pinned = ?, updated_at = ? WHERE id = ?').run(title, Number(pinned), updatedAt, id);
  return { ...hydrateTask(row), title, pinned, updatedAt };
};

export const deleteTask = async (id) => {
  await taskOrThrow(id);
  const db = await getDatabase();
  db.exec('BEGIN');
  try {
    db.prepare('DELETE FROM task_messages WHERE task_id = ?').run(id);
    db.prepare('DELETE FROM task_modules WHERE task_id = ?').run(id);
    db.prepare('DELETE FROM tasks WHERE id = ?').run(id);
    db.exec('COMMIT');
    return true;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
};

export const saveModule = async (taskId, moduleName, value) => {
  await taskOrThrow(taskId);
  if (!['parameters', 'pricing', 'backtest'].includes(moduleName)) throw new Error('模块名称无效。');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('模块参数格式无效。');
  const db = await getDatabase();
  const updatedAt = now();
  db.prepare(`INSERT INTO task_modules (task_id, module_name, payload_json, updated_at) VALUES (?, ?, ?, ?)
    ON CONFLICT(task_id, module_name) DO UPDATE SET payload_json = excluded.payload_json, updated_at = excluded.updated_at`)
    .run(taskId, moduleName, JSON.stringify(value), updatedAt);
  db.prepare('UPDATE tasks SET updated_at = ? WHERE id = ?').run(updatedAt, taskId);
  return value;
};

export const listMessages = async (taskId) => {
  await taskOrThrow(taskId);
  const db = await getDatabase();
  return db.prepare('SELECT * FROM task_messages WHERE task_id = ? ORDER BY sequence ASC').all(taskId).map(hydrateMessage);
};

export const appendMessage = async (taskId, message) => {
  await taskOrThrow(taskId);
  const role = message?.role;
  const content = text(message?.content);
  if (!['user', 'assistant'].includes(role) || !content) throw new Error('对话消息格式无效。');
  const db = await getDatabase();
  const sequence = db.prepare('SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM task_messages WHERE task_id = ?').get(taskId).value;
  const record = { id: randomUUID(), taskId, sequence, role, content, provider: text(message.provider, 100), model: text(message.model, 200), createdAt: now() };
  db.prepare(`INSERT INTO task_messages (id, task_id, sequence, role, content, provider, model, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)`).run(record.id, record.taskId, record.sequence, record.role, record.content, record.provider, record.model, record.createdAt);
  db.prepare('UPDATE tasks SET updated_at = ? WHERE id = ?').run(record.createdAt, taskId);
  return record;
};

export const titleFromFirstPrompt = async (taskId, prompt) => {
  const row = await taskOrThrow(taskId);
  if (row.title !== '新建研究任务') return hydrateTask(row);
  return updateTask(taskId, { title: prompt.slice(0, 42) });
};

export const contextForTask = async (taskId) => {
  const row = await taskOrThrow(taskId);
  const db = await getDatabase();
  const messages = db.prepare('SELECT * FROM task_messages WHERE task_id = ? AND sequence > ? ORDER BY sequence ASC')
    .all(taskId, row.summary_through_seq || 0).map(hydrateMessage);
  const total = db.prepare('SELECT COUNT(*) AS value FROM task_messages WHERE task_id = ?').get(taskId).value;
  return { summary: row.context_summary || '', summaryThrough: row.summary_through_seq || 0, messages, total };
};

export const messagesForSummary = async (taskId, keepRecent = 20) => {
  const row = await taskOrThrow(taskId);
  const db = await getDatabase();
  const maximum = db.prepare('SELECT COALESCE(MAX(sequence), 0) AS value FROM task_messages WHERE task_id = ?').get(taskId).value;
  if (maximum - (row.summary_through_seq || 0) <= keepRecent + 4) {
    return { summary: row.context_summary || '', messages: [], throughSequence: row.summary_through_seq || 0 };
  }
  const endSequence = maximum - keepRecent;
  if (endSequence <= row.summary_through_seq) return { summary: row.context_summary || '', messages: [], throughSequence: row.summary_through_seq || 0 };
  const messages = db.prepare('SELECT * FROM task_messages WHERE task_id = ? AND sequence > ? AND sequence <= ? ORDER BY sequence ASC')
    .all(taskId, row.summary_through_seq || 0, endSequence).map(hydrateMessage);
  return { summary: row.context_summary || '', messages, throughSequence: endSequence };
};

export const saveContextSummary = async (taskId, summary, throughSequence) => {
  await taskOrThrow(taskId);
  const cleaned = text(summary, 4_000);
  if (!cleaned || !Number.isInteger(throughSequence) || throughSequence < 1) return false;
  const db = await getDatabase();
  db.prepare('UPDATE tasks SET context_summary = ?, summary_through_seq = ?, updated_at = ? WHERE id = ?')
    .run(cleaned, throughSequence, now(), taskId);
  return true;
};
