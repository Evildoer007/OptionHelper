import { mkdirSync } from "node:fs"
import { dirname } from "node:path"
import { DatabaseSync } from "node:sqlite"

import type { SessionEvent, SessionHeader } from "../core/types.js"

const SCHEMA_VERSION = 2

function parseObject<T>(value: unknown, label: string): T {
  if (typeof value !== "string") throw new Error(`${label} is not stored as JSON text`)
  const parsed: unknown = JSON.parse(value)
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must decode to an object`)
  }
  return parsed as T
}

/** SQLite authority for Runtime sessions, events, run state and checkpoints. */
export class SqliteSessionStore {
  private readonly database: DatabaseSync

  constructor(readonly path: string) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
    this.database = new DatabaseSync(path)
    this.database.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;")
    this.migrate()
  }

  private migrate(): void {
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS runtime_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        header_json TEXT NOT NULL,
        next_seq INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS session_events (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        PRIMARY KEY (session_id, seq),
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL DEFAULT '',
        role_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (session_id, operation_id),
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS workflows (
        workflow_id TEXT PRIMARY KEY,
        root_session_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (root_session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
    `)
    this.ensureColumn("agent_runs", "workflow_id", "TEXT NOT NULL DEFAULT ''")
    this.ensureColumn("agent_runs", "role_id", "TEXT NOT NULL DEFAULT ''")
    const row = this.database.prepare("SELECT value FROM runtime_meta WHERE key='schema_version'").get() as { value?: unknown } | undefined
    if (row === undefined) {
      this.database.prepare("INSERT INTO runtime_meta(key,value) VALUES('schema_version',?)").run(String(SCHEMA_VERSION))
    } else if (Number(row.value) === 1) {
      this.database.prepare("UPDATE runtime_meta SET value=? WHERE key='schema_version'").run(String(SCHEMA_VERSION))
    } else if (Number(row.value) !== SCHEMA_VERSION) {
      throw new Error(`unsupported Runtime schema version ${String(row.value)}`)
    }
  }

  private ensureColumn(table: string, column: string, declaration: string): void {
    const columns = this.database.prepare(`PRAGMA table_info(${table})`).all() as { name?: unknown }[]
    if (!columns.some((item) => item.name === column)) this.database.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${declaration}`)
  }

  createSession(header: SessionHeader): void {
    const now = new Date().toISOString()
    this.database.prepare(
      "INSERT INTO sessions(session_id,header_json,next_seq,updated_at) VALUES(?,?,0,?)",
    ).run(header.id, JSON.stringify(header), now)
  }

  hasSession(sessionId: string): boolean {
    return this.database.prepare("SELECT 1 AS present FROM sessions WHERE session_id=?").get(sessionId) !== undefined
  }

  loadSession(sessionId: string): { header: SessionHeader; events: SessionEvent[] } | undefined {
    const row = this.database.prepare("SELECT header_json,next_seq FROM sessions WHERE session_id=?").get(sessionId) as
      | { header_json: unknown; next_seq: unknown }
      | undefined
    if (row === undefined) return undefined
    const events = this.database.prepare(
      "SELECT event_json FROM session_events WHERE session_id=? ORDER BY seq",
    ).all(sessionId) as { event_json: unknown }[]
    const parsed = events.map((item, index) => {
      const event = parseObject<SessionEvent>(item.event_json, `session event ${index}`)
      if (event.sessionId !== sessionId || event.seq !== index) throw new Error(`non-contiguous Runtime session ${sessionId}`)
      return event
    })
    if (Number(row.next_seq) !== parsed.length) throw new Error(`Runtime session ${sessionId} next_seq mismatch`)
    return { header: parseObject<SessionHeader>(row.header_json, "session header"), events: parsed }
  }

  appendEvent(event: SessionEvent): void {
    this.database.exec("BEGIN IMMEDIATE")
    try {
      const row = this.database.prepare("SELECT next_seq FROM sessions WHERE session_id=?").get(event.sessionId) as
        | { next_seq: unknown }
        | undefined
      if (row === undefined) throw new Error(`Runtime session ${event.sessionId} does not exist`)
      if (Number(row.next_seq) !== event.seq) throw new Error(`Runtime session ${event.sessionId} expected seq ${String(row.next_seq)}`)
      this.database.prepare(
        "INSERT INTO session_events(session_id,seq,event_json) VALUES(?,?,?)",
      ).run(event.sessionId, event.seq, JSON.stringify(event))
      this.database.prepare(
        "UPDATE sessions SET next_seq=next_seq+1,updated_at=? WHERE session_id=?",
      ).run(event.timestamp, event.sessionId)
      this.database.exec("COMMIT")
    } catch (error) {
      this.database.exec("ROLLBACK")
      throw error
    }
  }

  saveRun(runId: string, sessionId: string, snapshot: object): void {
    const now = new Date().toISOString()
    const value = snapshot as Record<string, unknown>
    const workflowId = String(value.workflowId ?? "")
    const roleId = String(value.roleId ?? "")
    this.database.prepare(`
      INSERT INTO agent_runs(run_id,workflow_id,role_id,session_id,snapshot_json,updated_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(run_id) DO UPDATE SET
        workflow_id=excluded.workflow_id,role_id=excluded.role_id,
        session_id=excluded.session_id,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
    `).run(runId, workflowId, roleId, sessionId, JSON.stringify(snapshot), now)
  }

  loadRun(runId: string): Record<string, unknown> | undefined {
    const row = this.database.prepare("SELECT snapshot_json FROM agent_runs WHERE run_id=?").get(runId) as
      | { snapshot_json: unknown }
      | undefined
    return row === undefined ? undefined : parseObject<Record<string, unknown>>(row.snapshot_json, "agent run")
  }

  listRuns(): Record<string, unknown>[] {
    const rows = this.database.prepare("SELECT snapshot_json FROM agent_runs ORDER BY updated_at").all() as { snapshot_json: unknown }[]
    return rows.map((row) => parseObject<Record<string, unknown>>(row.snapshot_json, "agent run"))
  }

  saveWorkflow(workflowId: string, rootSessionId: string, snapshot: object): void {
    this.database.prepare(`
      INSERT INTO workflows(workflow_id,root_session_id,snapshot_json,updated_at) VALUES(?,?,?,?)
      ON CONFLICT(workflow_id) DO UPDATE SET
        root_session_id=excluded.root_session_id,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
    `).run(workflowId, rootSessionId, JSON.stringify(snapshot), new Date().toISOString())
  }

  loadWorkflow(workflowId: string): Record<string, unknown> | undefined {
    const row = this.database.prepare("SELECT snapshot_json FROM workflows WHERE workflow_id=?").get(workflowId) as
      | { snapshot_json: unknown }
      | undefined
    return row === undefined ? undefined : parseObject<Record<string, unknown>>(row.snapshot_json, "workflow")
  }

  listWorkflows(rootSessionId?: string): Record<string, unknown>[] {
    const rows = rootSessionId === undefined
      ? this.database.prepare("SELECT snapshot_json FROM workflows ORDER BY updated_at").all()
      : this.database.prepare("SELECT snapshot_json FROM workflows WHERE root_session_id=? ORDER BY updated_at").all(rootSessionId)
    return (rows as { snapshot_json: unknown }[]).map(
      (row) => parseObject<Record<string, unknown>>(row.snapshot_json, "workflow"),
    )
  }

  checkpoint(
    checkpointId: string,
    sessionId: string,
    operationKind: string,
    operationId: string,
    state: string,
    payload: object,
  ): void {
    this.database.prepare(`
      INSERT INTO checkpoints(checkpoint_id,session_id,operation_kind,operation_id,state,payload_json,updated_at)
      VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(session_id,operation_id) DO UPDATE SET
        state=excluded.state,payload_json=excluded.payload_json,updated_at=excluded.updated_at
    `).run(checkpointId, sessionId, operationKind, operationId, state, JSON.stringify(payload), new Date().toISOString())
  }

  unresolvedCheckpoints(sessionId: string): Record<string, unknown>[] {
    const rows = this.database.prepare(`
      SELECT checkpoint_id,operation_kind,operation_id,state,payload_json,updated_at
      FROM checkpoints WHERE session_id=? AND state NOT IN ('completed','failed','cancelled') ORDER BY updated_at
    `).all(sessionId) as Record<string, unknown>[]
    return rows.map((row) => ({
      checkpointId: row.checkpoint_id,
      operationKind: row.operation_kind,
      operationId: row.operation_id,
      state: row.state,
      payload: parseObject<Record<string, unknown>>(row.payload_json, "checkpoint payload"),
      updatedAt: row.updated_at,
    }))
  }

  listSessions(): SessionHeader[] {
    const rows = this.database.prepare("SELECT header_json FROM sessions ORDER BY updated_at DESC").all() as { header_json: unknown }[]
    return rows.map((row) => parseObject<SessionHeader>(row.header_json, "session header"))
  }

  close(): void {
    this.database.close()
  }
}
