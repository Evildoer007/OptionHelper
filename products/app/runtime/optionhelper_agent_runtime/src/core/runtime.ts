import { randomUUID } from "node:crypto"
import { join } from "node:path"

import { AgentRun, type AgentEventPublisher } from "./agent.js"
import { AgentSession } from "./session.js"
import type { AgentActivation, AgentMessage, ContentBlock, ContextPolicy, JsonValue, RuntimePorts } from "./types.js"
import { SqliteSessionStore } from "../persistence/sqlite-store.js"

const DEFAULT_CONTEXT: ContextPolicy = {
  maxContextTokens: 120_000,
  pruneAtTokens: 72_000,
  compactAtTokens: 90_000,
  preservedRecentMessages: 12,
  prunedToolResultChars: 6_000,
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function pick(value: Record<string, unknown>, ...names: string[]): unknown {
  for (const name of names) if (value[name] !== undefined) return value[name]
  return undefined
}

function userContent(params: Record<string, unknown>): readonly ContentBlock[] {
  const raw = params.content
  if (!Array.isArray(raw)) {
    const text = String(params.prompt ?? "")
    if (text.trim() === "") throw new Error("Agent Turn缺少用户内容")
    return [{ type: "text", text }]
  }
  const blocks: ContentBlock[] = []
  for (const item of raw) {
    const value = record(item)
    const type = String(value.type ?? "").replaceAll("_", "-")
    if (type === "text") {
      const text = String(value.text ?? "")
      if (text !== "") blocks.push({ type: "text", text })
      continue
    }
    if (type === "image") {
      blocks.push({
        type: "image",
        attachmentId: identifier(pick(value, "attachmentId", "attachment_id"), "attachmentId"),
        mediaType: String(pick(value, "mediaType", "media_type") ?? ""),
        ...(String(value.name ?? "").trim() === "" ? {} : { name: String(value.name).slice(0, 160) }),
      })
      continue
    }
    if (type === "document-ref") {
      blocks.push({
        type: "document-ref",
        attachmentId: identifier(pick(value, "attachmentId", "attachment_id"), "attachmentId"),
        mediaType: String(pick(value, "mediaType", "media_type") ?? ""),
        name: String(value.name ?? "document").slice(0, 160),
        ...(String(pick(value, "extractionStatus", "extraction_status") ?? "").trim() === ""
          ? {} : { extractionStatus: String(pick(value, "extractionStatus", "extraction_status")) }),
      })
      continue
    }
    throw new Error(`不支持的用户内容块：${type}`)
  }
  if (blocks.length === 0) throw new Error("Agent Turn缺少用户内容")
  return blocks
}

function identifier(value: unknown, name: string, fallback?: string): string {
  const result = String(value ?? fallback ?? "").trim()
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(result)) throw new Error(`${name}格式无效`)
  return result
}

function presetRevision(value: unknown): string {
  const revision = String(value ?? "").trim()
  if (!/^[0-9a-f]{64}$/.test(revision)) throw new Error("presetRevision必须是预设内容的SHA-256标识")
  return revision
}

function workflowRecord(value: unknown, fallbackRootSessionId: string): Record<string, unknown> {
  const spec = record(value)
  return {
    ...spec,
    workflowId: identifier(pick(spec, "workflowId", "workflow_id"), "workflowId"),
    presetId: identifier(pick(spec, "presetId", "preset_id"), "presetId"),
    presetRevision: presetRevision(pick(spec, "presetRevision", "preset_revision")),
    rootSessionId: identifier(pick(spec, "rootSessionId", "root_session_id"), "rootSessionId", fallbackRootSessionId),
  }
}

function positiveInteger(value: unknown, fallback: number, maximum: number): number {
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? Math.min(number, maximum) : fallback
}

function routeId(value: Record<string, unknown>): string {
  const route = record(pick(value, "modelRouteRef", "model_route_ref"))
  return identifier(pick(route, "routeId", "route_id"), "routeId", "route-default")
}

function optionalRouteId(value: Record<string, unknown>): string | undefined {
  const route = record(pick(value, "modelRouteRef", "model_route_ref"))
  const raw = pick(route, "routeId", "route_id")
  return raw === undefined || String(raw).trim() === "" ? undefined : identifier(raw, "routeId")
}

function history(value: unknown): AgentMessage[] {
  if (!Array.isArray(value)) return []
  return value.map((raw, index) => {
    const item = record(raw)
    const role = ["system", "user", "assistant", "tool"].includes(String(item.role)) ? String(item.role) as AgentMessage["role"] : "user"
    const rawContent = item.content
    const content = Array.isArray(rawContent)
      ? rawContent.map((block) => record(block)).flatMap((block): ContentBlock[] => {
        const type = String(block.type ?? "").replaceAll("_", "-")
        if (type === "text" || type === "reasoning") return [{ type, text: String(block.text ?? "") }]
        if (type === "image") return [{
          type: "image", attachmentId: String(pick(block, "attachmentId", "attachment_id") ?? ""),
          mediaType: String(pick(block, "mediaType", "media_type") ?? ""), name: String(block.name ?? ""),
        }]
        if (type === "document-ref") return [{
          type: "document-ref", attachmentId: String(pick(block, "attachmentId", "attachment_id") ?? ""),
          mediaType: String(pick(block, "mediaType", "media_type") ?? ""), name: String(block.name ?? "document"),
        }]
        if (type === "tool-call") return [{
          type: "tool-call", id: String(block.id ?? ""), name: String(block.name ?? ""), arguments: String(block.arguments ?? "{}"),
        }]
        return []
      })
      : [{ type: "text", text: String(rawContent ?? "") } as ContentBlock]
    return { id: String(item.id ?? `legacy-${index}`), role, content }
  })
}

function contextPolicy(value: Record<string, unknown>): ContextPolicy {
  const raw = record(pick(value, "contextPolicy", "context_policy"))
  return {
    maxContextTokens: positiveInteger(pick(raw, "maxContextTokens", "max_context_tokens", "maxTokens", "max_tokens"), DEFAULT_CONTEXT.maxContextTokens, 1_000_000),
    pruneAtTokens: positiveInteger(pick(raw, "pruneAtTokens", "prune_at_tokens"), DEFAULT_CONTEXT.pruneAtTokens, 1_000_000),
    compactAtTokens: positiveInteger(pick(raw, "compactAtTokens", "compact_at_tokens"), DEFAULT_CONTEXT.compactAtTokens, 1_000_000),
    preservedRecentMessages: positiveInteger(pick(raw, "preservedRecentMessages", "preserved_recent_messages"), DEFAULT_CONTEXT.preservedRecentMessages, 100),
    prunedToolResultChars: positiveInteger(pick(raw, "prunedToolResultChars", "pruned_tool_result_chars", "toolResultMaxChars", "tool_result_max_chars"), DEFAULT_CONTEXT.prunedToolResultChars, 100_000),
  }
}

export class OptionHelperAgentRuntime implements AgentEventPublisher {
  private store: SqliteSessionStore | undefined
  private rootSessionId = ""
  private readonly runs = new Map<string, AgentRun>()
  private readonly workflows = new Map<string, Record<string, unknown>>()
  private initialized = false

  constructor(
    private readonly ports: RuntimePorts,
    private readonly emit: (event: JsonValue) => void,
  ) {}

  initialize(params: Record<string, unknown>): Record<string, unknown> {
    const sessionRoot = String(pick(params, "sessionRoot", "session_root") ?? process.env.OPTIONHELPER_AGENT_SESSION_ROOT ?? ".").trim()
    this.rootSessionId = identifier(pick(params, "rootSessionId", "root_session_id"), "rootSessionId", "root-session")
    if (!this.initialized) {
      this.store = new SqliteSessionStore(join(sessionRoot, "agent-sessions.sqlite3"))
      if (!this.store.hasSession(this.rootSessionId)) AgentSession.create(this.store, { id: this.rootSessionId, kind: "root", label: "OptionHelper" })
      for (const workflow of this.store.listWorkflows(this.rootSessionId)) {
        const normalized = workflowRecord(workflow, this.rootSessionId)
        this.workflows.set(String(normalized.workflowId), normalized)
      }
      this.initialized = true
      this.emitRuntime("runtime/ready", this.rootSessionId, { runtimeId: "optionhelper-agent-runtime", protocolVersion: "2.0" })
    }
    return { runtimeId: "optionhelper-agent-runtime", version: "0.2.0", protocolVersion: "2.0", rootSessionId: this.rootSessionId, database: "agent-sessions.sqlite3" }
  }

  capabilities(): Record<string, unknown> {
    return {
      state: { detected: true, initialized: this.initialized, verified: this.initialized && this.store !== undefined },
      mainAgent: true, persistentSessions: true, sqlitePersistence: true, nativeToolLoop: true,
      streaming: true, reasoning: true, usage: true, contextCompaction: true,
      oneShotSubagent: true, continuableSubagent: true, coldResume: true,
      workflowPersistence: true, workflowScopedCancel: true, turnModelRoute: true,
      hostOrchestration: true,
      continuableRolesByPreset: {
        "sequential-deliberation": [],
        "product-trader-loop": ["Structurer", "Trader"],
        "independent-council": ["Matcher", "Hedger"],
        "constraint-ranking": [],
      },
    }
  }

  activate(params: Record<string, unknown>, child = false): AgentRun {
    const store = this.requireStore()
    const sessionId = identifier(pick(params, "sessionId", "session_id"), "sessionId", child ? `child-${randomUUID()}` : this.rootSessionId)
    const runId = identifier(pick(params, "runId", "run_id"), "runId", `run-${randomUUID()}`)
    const parentSessionId = child ? identifier(pick(params, "parentSessionId", "parent_session_id"), "parentSessionId", this.rootSessionId) : undefined
    const rawParentRunId = pick(params, "parentRunId", "parent_run_id")
    if (this.runs.has(runId)) return this.runs.get(runId) as AgentRun
    if (child && !store.hasSession(parentSessionId as string)) throw new Error("Parent Session不存在")
    const toolPolicy = record(pick(params, "toolPolicy", "tool_policy"))
    const budget = record(params.budget)
    const activation: AgentActivation = {
      runId,
      workflowId: identifier(pick(params, "workflowId", "workflow_id"), "workflowId", `workflow-${this.rootSessionId}`),
      sessionId,
      ...(parentSessionId === undefined ? {} : { parentSessionId }),
      ...(rawParentRunId === undefined || rawParentRunId === null || String(rawParentRunId).trim() === ""
        ? {} : { parentRunId: identifier(rawParentRunId, "parentRunId") }),
      roleId: identifier(pick(params, "roleId", "role_id"), "roleId", child ? "Agent" : "MainAgent"),
      label: String(params.label ?? (child ? "Agent" : "OptionHelper")).slice(0, 160),
      mode: String(params.mode ?? (child ? "one-shot" : "continuable")) === "one-shot" ? "one-shot" : "continuable",
      routeId: routeId(params),
      allowedTools: Array.isArray(pick(toolPolicy, "allowedTools", "allowed_tools")) ? (pick(toolPolicy, "allowedTools", "allowed_tools") as unknown[]).map(String) : [],
      parallelTools: pick(toolPolicy, "parallelTools", "parallel_tools") === true,
      budget: {
        maxSteps: positiveInteger(pick(budget, "maxSteps", "max_steps"), 8, 8),
        maxTools: positiveInteger(pick(budget, "maxTools", "max_tools"), 12, 12),
      },
      contextPolicy: contextPolicy(params),
    }
    const existing = AgentSession.load(store, sessionId)
    const run = new AgentRun(activation, store, this.ports, this, existing)
    this.runs.set(runId, run)
    run.activate(history(params.history))
    if (child) this.publish(run.session, "subagent/start", {
      runId, childSessionId: sessionId, parentSessionId: parentSessionId as string, roleId: activation.roleId, mode: activation.mode,
    })
    return run
  }

  async turn(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const run = this.getRun(identifier(pick(params, "runId", "run_id"), "runId"))
    const result = await run.turn(userContent(params), optionalRouteId(params))
    if (run.activation.parentSessionId !== undefined) this.publish(run.session, "subagent/end", {
      runId: run.activation.runId, childSessionId: run.activation.sessionId, status: result.status, result: { text: result.result.text },
    })
    return result as unknown as Record<string, unknown>
  }

  async startSubagent(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const run = this.activate(params, true)
    const promise = run.turn(userContent(params))
    if (params.wait === true) return this.turnResult(run, await promise)
    void promise.then((result) => this.publish(run.session, "subagent/end", {
      runId: run.activation.runId, childSessionId: run.activation.sessionId, status: result.status, result: { text: result.result.text },
    })).catch(() => undefined)
    return run.snapshot() as unknown as Record<string, unknown>
  }

  async followup(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const run = this.getRun(identifier(pick(params, "runId", "run_id"), "runId"))
    return this.turnResult(run, await run.followup(userContent(params), optionalRouteId(params)))
  }

  startWorkflow(params: Record<string, unknown>): Record<string, unknown> {
    const rawSpec = record(params.spec ?? params)
    const spec = workflowRecord(
      { ...rawSpec, workflowId: pick(rawSpec, "workflowId", "workflow_id") ?? `workflow-${randomUUID()}` },
      this.rootSessionId,
    )
    const workflowId = String(spec.workflowId)
    const frozen = structuredClone({ ...spec, status: "queued", autoExecute: false, orchestrationOwner: "optionhelper_host" })
    this.workflows.set(workflowId, frozen)
    this.requireStore().saveWorkflow(workflowId, this.rootSessionId, frozen)
    this.emitRuntime("workflow/start", this.rootSessionId, { workflowId, status: "queued" })
    return frozen
  }

  workflowStatus(params: Record<string, unknown>): Record<string, unknown> {
    const workflowId = identifier(pick(params, "workflowId", "workflow_id"), "workflowId")
    const workflow = this.workflows.get(workflowId) ?? this.requireStore().loadWorkflow(workflowId)
    if (workflow === undefined) throw new Error("Workflow不存在")
    return workflow
  }

  cancelWorkflow(params: Record<string, unknown>): Record<string, unknown> {
    const workflowId = identifier(pick(params, "workflowId", "workflow_id"), "workflowId")
    for (const run of this.runs.values()) {
      if (run.activation.workflowId === workflowId) run.cancel(String(params.reason ?? "workflow_cancelled"))
    }
    const workflow = this.workflows.get(workflowId) ?? this.requireStore().loadWorkflow(workflowId) ?? { workflowId }
    const cancelled = { ...workflow, status: "cancelled" }
    this.workflows.set(workflowId, cancelled)
    this.requireStore().saveWorkflow(workflowId, String(workflow.rootSessionId ?? this.rootSessionId), cancelled)
    this.emitRuntime("workflow/end", this.rootSessionId, { workflowId, status: "cancelled" })
    return cancelled
  }

  sessionResume(params: Record<string, unknown>): Record<string, unknown> {
    const sessionId = identifier(pick(params, "sessionId", "session_id"), "sessionId")
    const loaded = this.requireStore().loadSession(sessionId)
    if (loaded === undefined) throw new Error("Session不存在")
    const restoredRuns = this.restoreRuns(sessionId)
    const unresolved = this.requireStore().unresolvedCheckpoints(sessionId)
    const recovery = unresolved.map((item) => ({
      ...item,
      recovery: item.operationKind === "tool" && ["started", "outcome_unknown"].includes(String(item.state))
        ? "TOOL_OUTCOME_UNKNOWN" : "TOOL_NOT_STARTED",
    }))
    this.emitRuntime("session/recovered", sessionId, { unresolved: recovery.length })
    return {
      sessionId, nextSeq: loaded.events.length, recovery, events: loaded.events,
      restoredRuns: restoredRuns.map((run) => run.snapshot()),
      workflows: this.requireStore().listWorkflows(sessionId),
    }
  }

  sessionHistory(params: Record<string, unknown>): Record<string, unknown> {
    const sessionId = identifier(pick(params, "sessionId", "session_id"), "sessionId")
    const loaded = this.requireStore().loadSession(sessionId)
    if (loaded === undefined) throw new Error("Session不存在")
    const after = Math.max(0, Number(params.afterSeq ?? params.after_seq ?? 0))
    const limit = positiveInteger(params.limit, 500, 5_000)
    return { sessionId, events: loaded.events.slice(after, after + limit), nextSeq: loaded.events.length }
  }

  sessionTree(params: Record<string, unknown>): Record<string, unknown> {
    const root = String(pick(params, "sessionId", "session_id") ?? this.rootSessionId)
    const sessions = this.requireStore().listSessions()
    const selected = sessions.filter((session) => session.id === root || this.descendsFrom(session.id, root, sessions))
    return { rootSessionId: root, sessions: selected.map((session) => ({
      sessionId: session.id, parentSessionId: session.parentId ?? null, kind: session.kind, label: session.label,
    })) }
  }

  getRun(runId: string): AgentRun {
    let run = this.runs.get(runId)
    if (run === undefined) {
      const stored = this.requireStore().loadRun(runId)
      if (stored !== undefined) run = this.restoreRun(stored)
    }
    if (run === undefined) throw new Error("AgentRun不存在或尚未在本进程恢复")
    return run
  }

  publish(session: AgentSession, type: string, data: JsonValue, position: { turn?: number; step?: number } = {}): void {
    const event = session.append(type, data, position)
    this.emit({
      eventId: `${session.id}:${event.seq}`,
      seq: event.seq,
      sessionId: session.id,
      type,
      timestamp: event.timestamp,
      ...(position.turn === undefined ? {} : { turn: position.turn }),
      ...(position.step === undefined ? {} : { step: position.step }),
      data,
    })
  }

  shutdown(): Record<string, unknown> {
    for (const run of this.runs.values()) if (["running", "waiting_tool"].includes(run.snapshot().status)) run.cancel("runtime_shutdown")
    this.store?.close()
    this.store = undefined
    this.initialized = false
    return { status: "closed" }
  }

  private turnResult(run: AgentRun, result: ReturnType<AgentRun["snapshot"]>): Record<string, unknown> {
    this.publish(run.session, "subagent/end", {
      runId: run.activation.runId, childSessionId: run.activation.sessionId, status: result.status, result: { text: result.result.text },
    })
    return result as unknown as Record<string, unknown>
  }

  private emitRuntime(type: string, sessionId: string, data: JsonValue): void {
    this.emit({ eventId: `${sessionId}:${type}:${Date.now()}`, sessionId, type, timestamp: new Date().toISOString(), data })
  }

  private requireStore(): SqliteSessionStore {
    if (!this.initialized || this.store === undefined) throw new Error("Runtime尚未初始化")
    return this.store
  }

  private restoreRuns(rootSessionId: string): AgentRun[] {
    const sessions = this.requireStore().listSessions()
    const allowed = new Set(
      sessions
        .filter((session) => session.id === rootSessionId || this.descendsFrom(session.id, rootSessionId, sessions))
        .map((session) => session.id),
    )
    const restored: AgentRun[] = []
    for (const snapshot of this.requireStore().listRuns()) {
      if (!allowed.has(String(snapshot.sessionId ?? ""))) continue
      const runId = String(snapshot.runId ?? "")
      const existing = this.runs.get(runId)
      restored.push(existing ?? this.restoreRun(snapshot))
    }
    return restored
  }

  private restoreRun(snapshot: Record<string, unknown>): AgentRun {
    const raw = record(snapshot.activation)
    const runId = identifier(pick(raw, "runId", "run_id") ?? snapshot.runId, "runId")
    const existing = this.runs.get(runId)
    if (existing !== undefined) return existing
    const sessionId = identifier(pick(raw, "sessionId", "session_id") ?? snapshot.sessionId, "sessionId")
    const parentSession = pick(raw, "parentSessionId", "parent_session_id")
    const parentRun = pick(raw, "parentRunId", "parent_run_id")
    const budget = record(raw.budget)
    const policy = record(pick(raw, "contextPolicy", "context_policy"))
    const activation: AgentActivation = {
      runId,
      workflowId: identifier(pick(raw, "workflowId", "workflow_id") ?? snapshot.workflowId, "workflowId"),
      sessionId,
      ...(parentSession === undefined ? {} : { parentSessionId: identifier(parentSession, "parentSessionId") }),
      ...(parentRun === undefined ? {} : { parentRunId: identifier(parentRun, "parentRunId") }),
      roleId: identifier(pick(raw, "roleId", "role_id") ?? snapshot.roleId, "roleId"),
      label: String(raw.label ?? snapshot.label ?? "Agent").slice(0, 160),
      mode: String(raw.mode ?? snapshot.mode) === "one-shot" ? "one-shot" : "continuable",
      routeId: identifier(raw.routeId ?? snapshot.routeId, "routeId"),
      allowedTools: Array.isArray(raw.allowedTools) ? raw.allowedTools.map(String) : [],
      parallelTools: raw.parallelTools === true,
      budget: {
        maxSteps: positiveInteger(budget.maxSteps, 8, 8),
        maxTools: positiveInteger(budget.maxTools, 12, 12),
      },
      contextPolicy: contextPolicy({ contextPolicy: policy }),
    }
    const session = AgentSession.load(this.requireStore(), sessionId)
    if (session === undefined) throw new Error("AgentRun对应Session不存在")
    const run = new AgentRun(activation, this.requireStore(), this.ports, this, session)
    this.runs.set(runId, run)
    return run
  }

  private descendsFrom(sessionId: string, root: string, sessions: readonly { id: string; parentId?: string }[]): boolean {
    let current = sessions.find((session) => session.id === sessionId)?.parentId
    const seen = new Set<string>()
    while (current !== undefined && !seen.has(current)) {
      if (current === root) return true
      seen.add(current)
      current = sessions.find((session) => session.id === current)?.parentId
    }
    return false
  }
}
