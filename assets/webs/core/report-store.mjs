import { randomUUID } from 'node:crypto';
import { chmodSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { ensureAppDataDir, reportDatabasePath } from './paths.mjs';
import { migrateLegacyTaskScope } from './task-scope-migration.mjs';

let database;

const getDatabase = async () => {
  if (database) return database;
  await migrateLegacyTaskScope();
  await ensureAppDataDir();
  database = new DatabaseSync(reportDatabasePath);
  try { chmodSync(reportDatabasePath, 0o600); } catch { /* first launch may defer permission update */ }
  database.exec(`
    CREATE TABLE IF NOT EXISTS reports (
      id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      format TEXT NOT NULL CHECK(format IN ('simple', 'full')),
      title TEXT NOT NULL,
      sections_json TEXT NOT NULL,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      ai_status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS reports_created_at ON reports(created_at DESC);
  `);
  return database;
};

const hydrate = (row) => row && ({
  id: row.id,
  taskId: row.task_id,
  format: row.format,
  title: row.title,
  sections: JSON.parse(row.sections_json),
  provider: row.provider,
  model: row.model,
  aiStatus: row.ai_status,
  createdAt: row.created_at,
  updatedAt: row.updated_at,
});

export const createReport = async ({ taskId, format, title, sections, provider, model }) => {
  const db = await getDatabase();
  const now = new Date().toISOString();
  const report = {
    id: randomUUID(),
    taskId: taskId || randomUUID(),
    format,
    title,
    sections,
    provider,
    model,
    aiStatus: 'AI生成，未经人工审核',
    createdAt: now,
    updatedAt: now,
  };
  db.prepare(`INSERT INTO reports (id, task_id, format, title, sections_json, provider, model, ai_status, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(report.id, report.taskId, report.format, report.title, JSON.stringify(report.sections), report.provider,
      report.model, report.aiStatus, report.createdAt, report.updatedAt);
  return report;
};

export const listReports = async (profile, taskId = null) => {
  const db = await getDatabase();
  const rows = taskId
    ? db.prepare('SELECT * FROM reports WHERE task_id = ? ORDER BY created_at DESC').all(taskId)
    : db.prepare('SELECT * FROM reports ORDER BY created_at DESC').all();
  return rows.map(hydrate).filter((report) => profile.allowedModes.includes('desk') || report.format === 'simple');
};

export const readReport = async (id, profile) => {
  const db = await getDatabase();
  const report = hydrate(db.prepare('SELECT * FROM reports WHERE id = ?').get(id));
  if (!report) return null;
  if (report.format === 'full' && !profile.allowedModes.includes('desk')) {
    const error = new Error('当前许可证无权读取完整版Report。');
    error.statusCode = 403;
    throw error;
  }
  return report;
};

export const deleteReport = async (id, profile) => {
  const report = await readReport(id, profile);
  if (!report) return false;
  const db = await getDatabase();
  db.prepare('DELETE FROM reports WHERE id = ?').run(id);
  return true;
};
