import { chmodSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { ensureAppDataDir, reportDatabasePath, taskDatabasePath } from './paths.mjs';

let completed = false;

const tableExists = (database, name) => Boolean(database.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?").get(name));

const removeLegacyRows = (database, tables) => {
  tables.filter((name) => tableExists(database, name)).forEach((name) => database.exec(`DELETE FROM ${name}`));
};

export const migrateLegacyTaskScope = async () => {
  if (completed) return;
  await ensureAppDataDir();
  const taskDatabase = new DatabaseSync(taskDatabasePath);
  let requiresReset = false;
  try {
    try { chmodSync(taskDatabasePath, 0o600); } catch { /* First launch may defer permission update. */ }
    if (tableExists(taskDatabase, 'tasks')) {
      const columns = taskDatabase.prepare('PRAGMA table_info(tasks)').all().map((column) => column.name);
      requiresReset = !columns.includes('scope');
      if (requiresReset) {
        taskDatabase.exec('BEGIN');
        try {
          removeLegacyRows(taskDatabase, ['task_messages', 'task_modules', 'tasks']);
          taskDatabase.exec("ALTER TABLE tasks ADD COLUMN scope TEXT NOT NULL DEFAULT 'chat' CHECK(scope IN ('chat', 'desk'))");
          taskDatabase.exec('COMMIT');
        } catch (error) {
          taskDatabase.exec('ROLLBACK');
          throw error;
        }
      }
    }
  } finally {
    taskDatabase.close();
  }

  if (requiresReset) {
    const reportDatabase = new DatabaseSync(reportDatabasePath);
    try {
      try { chmodSync(reportDatabasePath, 0o600); } catch { /* First launch may defer permission update. */ }
      removeLegacyRows(reportDatabase, ['reports']);
    } finally {
      reportDatabase.close();
    }
  }
  completed = true;
};
