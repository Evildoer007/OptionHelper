import { randomUUID } from "node:crypto"

import { maintainContext } from "../context/context-maintainer.js"
import { TokenMeter } from "../context/token-meter.js"
import { BlockAssembler } from "../llm/assembler.js"
import { DEFAULT_RETRY_POLICY, retryDelay, shouldRetry, waitForRetry } from "../llm/retry-policy.js"
import { AgentSession } from "./session.js"
import { executeToolCalls } from "./tool-scheduler.js"
import type {
  AgentActivation, AgentMessage, AgentStatus, ContentBlock, JsonValue, RuntimePorts,
  StreamChunk, ToolCallBlock, ToolDefinition, TokenUsage,
} from "./types.js"
import { SqliteSessionStore } from "../persistence/sqlite-store.js"
import { Inbox } from "./inbox.js"

export interface AgentEventPublisher {
  publish(session: AgentSession, type: string, data: JsonValue, position?: { turn?: number; step?: number }): void
}

interface RunSnapshot {
  readonly runId: string
  readonly workflowId: string
  readonly sessionId: string
  readonly parentSessionId?: string
  readonly parentRunId?: string
  readonly roleId: string
  readonly label: string
  readonly mode: string
  readonly status: AgentStatus
  readonly turn: number
  readonly step: number
  readonly result: { readonly text: string; readonly blocks: readonly ContentBlock[]; readonly usage: TokenUsage; readonly turn: number }
  readonly error?: string
  readonly usage: TokenUsage
  readonly routeId: string
  readonly activation: AgentActivation
}

function textOf(blocks: readonly ContentBlock[]): string {
  return blocks.filter((block) => block.type === "text").map((block) => block.text).join("").trim()
}

function message(role: AgentMessage["role"], content: AgentMessage["content"], id: string = randomUUID()): AgentMessage {
  return { id, role, content }
}

function normalizeAllowedTools(value: readonly string[]): ToolDefinition[] {
  return value.map((name) => ({
    name,
    description: `OptionHelper受控业务工具：${name}`,
    parameters: { type: "object", additionalProperties: true },
    executionMode: "exclusive",
  }))
}

function abortError(reason: unknown): Error {
  const error = new Error(String(reason ?? "cancelled"))
  error.name = "AbortError"
  return error
}

type PartialStreamError = Error & { partialBlocks?: readonly ContentBlock[] }

/** Durable agent execution with Inbox, native tool continuation and bounded turns. */
export class AgentRun {
  readonly session: AgentSession
  readonly inbox: Inbox
  private statusValue: AgentStatus = "idle"
  private turnValue = 0
  private stepValue = 0
  private resultValue = ""
  private resultBlocks: readonly ContentBlock[] = []
  private errorValue: string | undefined
  private usageValue: TokenUsage = {}
  private active: Promise<RunSnapshot> | undefined
  private abortController: AbortController | undefined
  private toolCount = 0
  private routeIdValue: string

  constructor(
    readonly activation: AgentActivation,
    private readonly store: SqliteSessionStore,
    private readonly ports: RuntimePorts,
    private readonly publisher: AgentEventPublisher,
    session?: AgentSession,
  ) {
    this.routeIdValue = activation.routeId
    this.session = session ?? AgentSession.create(store, {
      id: activation.sessionId,
      kind: activation.parentSessionId === undefined ? "root" : "child",
      ...(activation.parentSessionId === undefined ? {} : { parentId: activation.parentSessionId }),
      label: activation.label,
    })
    this.inbox = new Inbox(this.session)
    const stored = store.loadRun(activation.runId)
    if (stored !== undefined) {
      this.statusValue = stored.status === "running" || stored.status === "waiting_tool" ? "idle" : String(stored.status) as AgentStatus
      this.turnValue = Number(stored.turn ?? 0)
      this.stepValue = Number(stored.step ?? 0)
      const storedResult = stored.result !== null && typeof stored.result === "object" && !Array.isArray(stored.result)
        ? stored.result as { text?: unknown; blocks?: unknown }
        : undefined
      this.resultValue = String(storedResult?.text ?? stored.result ?? "")
      const storedBlocks = storedResult?.blocks
      this.resultBlocks = Array.isArray(storedBlocks) ? storedBlocks as ContentBlock[] : []
      this.errorValue = stored.error === undefined ? undefined : String(stored.error)
      this.routeIdValue = typeof stored.routeId === "string" ? stored.routeId : activation.routeId
      if (stored.status === "running" || stored.status === "waiting_tool") {
        this.session.append("session/interrupted", { reason: "runtime_restarted", turn: this.turnValue, step: this.stepValue })
      }
    }
  }

  activate(history: readonly AgentMessage[] = []): RunSnapshot {
    if (history.length > 0 && this.session.events.length === 0) this.session.importHistory(history)
    this.publisher.publish(this.session, "agent/activated", {
      runId: this.activation.runId,
      sessionId: this.activation.sessionId,
      parentSessionId: this.activation.parentSessionId ?? null,
      parentRunId: this.activation.parentRunId ?? null,
      roleId: this.activation.roleId,
      label: this.activation.label,
      mode: this.activation.mode,
    })
    this.persist()
    return this.snapshot()
  }

  turn(prompt: string | readonly ContentBlock[], routeId?: string): Promise<RunSnapshot> {
    if (routeId !== undefined && routeId !== this.routeIdValue) {
      if (this.active !== undefined) throw new Error("Agent正在运行，不能切换当前Turn模型路由")
      this.routeIdValue = routeId
      this.publisher.publish(this.session, "model/route-bound", {
        runId: this.activation.runId, routeId, nextTurn: this.turnValue + 1,
      })
      this.persist()
    }
    if (this.active !== undefined) {
      if (this.activation.mode !== "continuable") throw new Error("one-shot Agent正在运行")
      this.inbox.append("next-turn", message("user", typeof prompt === "string" ? [{ type: "text", text: prompt }] : prompt))
      return this.active
    }
    const queued = message("user", typeof prompt === "string" ? [{ type: "text", text: prompt }] : prompt)
    this.inbox.append("next-turn", queued)
    this.active = this.drainTurns().finally(() => { this.active = undefined })
    return this.active
  }

  followup(prompt: string | readonly ContentBlock[], routeId?: string): Promise<RunSnapshot> {
    if (this.activation.mode !== "continuable") throw new Error("one-shot Child Agent不接受后续消息")
    return this.turn(prompt, routeId)
  }

  cancel(reason = "cancelled"): RunSnapshot {
    this.abortController?.abort(reason)
    this.statusValue = "cancelled"
    this.errorValue = reason
    this.publisher.publish(this.session, "agent/status", { runId: this.activation.runId, roleId: this.activation.roleId, status: "cancelled", reason })
    this.persist()
    return this.snapshot()
  }

  snapshot(): RunSnapshot {
    return {
      runId: this.activation.runId,
      workflowId: this.activation.workflowId,
      sessionId: this.activation.sessionId,
      ...(this.activation.parentSessionId === undefined ? {} : { parentSessionId: this.activation.parentSessionId }),
      ...(this.activation.parentRunId === undefined ? {} : { parentRunId: this.activation.parentRunId }),
      roleId: this.activation.roleId,
      label: this.activation.label,
      mode: this.activation.mode,
      status: this.statusValue,
      turn: this.turnValue,
      step: this.stepValue,
      result: { text: this.resultValue, blocks: this.resultBlocks, usage: this.usageValue, turn: this.turnValue },
      ...(this.errorValue === undefined ? {} : { error: this.errorValue }),
      usage: this.usageValue,
      routeId: this.routeIdValue,
      activation: { ...this.activation, routeId: this.routeIdValue },
    }
  }

  private async drainTurns(): Promise<RunSnapshot> {
    while (this.inbox.nextTurn.length > 0 && this.statusValue !== "cancelled") {
      const claimed = this.inbox.claim("next-turn", this.turnValue + 1)
      if (claimed.length === 0) break
      for (const item of claimed) this.session.append("conversation/message", item as unknown as JsonValue)
      await this.executeTurn()
      if (this.activation.mode === "one-shot") break
    }
    return this.snapshot()
  }

  private async executeTurn(): Promise<void> {
    this.turnValue += 1
    this.stepValue = 0
    this.toolCount = 0
    this.resultValue = ""
    this.resultBlocks = []
    this.errorValue = undefined
    this.abortController = new AbortController()
    const signal = this.abortController.signal
    this.setStatus("running")
    this.publisher.publish(this.session, "turn/start", { runId: this.activation.runId, turn: this.turnValue }, { turn: this.turnValue })
    try {
      for (let step = 1; (this.activation.budget.maxSteps === 0 || step <= this.activation.budget.maxSteps); step += 1) {
        if (signal.aborted) throw abortError(signal.reason)
        this.stepValue = step
        this.publisher.publish(this.session, "step/start", { runId: this.activation.runId, turn: this.turnValue, step }, { turn: this.turnValue, step })
        const operationId = `model:${this.activation.runId}:${this.turnValue}:${step}`
        const checkpointPayload = { turn: this.turnValue, step, routeId: this.routeIdValue }
        this.store.checkpoint(randomUUID(), this.session.id, "model", operationId, "not_started", checkpointPayload)
        let assembler: BlockAssembler
        try {
          const maintained = await maintainContext(
            this.session,
            this.activation.contextPolicy,
            this.ports.summarize,
            signal,
          )
          const requestId = randomUUID()
          this.store.checkpoint(randomUUID(), this.session.id, "model", operationId, "started", {
            ...checkpointPayload, requestId,
          })
          assembler = await this.streamWithRetry({
            requestId,
            routeId: this.routeIdValue,
            messages: maintained.messages,
            tools: normalizeAllowedTools(this.activation.allowedTools),
            sessionId: this.session.id,
            agentRunId: this.activation.runId,
            roleId: this.activation.roleId,
            turn: this.turnValue,
            step,
          }, signal)
          this.store.checkpoint(randomUUID(), this.session.id, "model", operationId, "completed", {
            ...checkpointPayload, finishReason: assembler.finish.kind, usage: assembler.usage,
          })
        } catch (error) {
          this.store.checkpoint(
            randomUUID(), this.session.id, "model", operationId,
            signal.aborted || (error instanceof Error && error.name === "AbortError") ? "cancelled" : "failed",
            { ...checkpointPayload, error: String(error) },
          )
          throw error
        }
        this.usageValue = new TokenMeter().merge(this.usageValue, assembler.usage)
        const blocks = assembler.blocks()
        const assistant = message("assistant", blocks)
        this.session.append("assistant/message", assistant as unknown as JsonValue, { turn: this.turnValue, step })
        this.publisher.publish(this.session, "assistant/message", {
          runId: this.activation.runId,
          text: textOf(blocks),
          blocks,
          finishReason: assembler.finish.kind,
        } as unknown as JsonValue, { turn: this.turnValue, step })
        const calls = blocks.filter((block): block is ToolCallBlock => block.type === "tool-call")
        if (calls.length === 0) {
          if (assembler.finish.kind === "error") throw new Error(assembler.finish.message ?? "model request failed")
          this.resultValue = textOf(blocks)
          this.resultBlocks = blocks
          this.publisher.publish(this.session, "step/end", { runId: this.activation.runId, reason: "completed" }, { turn: this.turnValue, step })
          this.publisher.publish(this.session, "turn/end", { runId: this.activation.runId, reason: "completed" }, { turn: this.turnValue, step })
          this.setStatus("completed")
          return
        }
        if (this.activation.budget.maxTools > 0 && this.toolCount + calls.length > this.activation.budget.maxTools) throw new Error("Agent工具调用次数超过预算")
        this.toolCount += calls.length
        this.setStatus("waiting_tool")
        const results = await executeToolCalls(
          calls,
          (call) => ({
            callId: call.id,
            name: call.name,
            arguments: JSON.parse(call.arguments || "{}") as JsonValue,
            sessionId: this.session.id,
            agentRunId: this.activation.runId,
            turn: this.turnValue,
            step,
          }),
          (request, currentSignal) => this.executeTool(request, currentSignal),
          signal,
          this.activation.parallelTools,
        )
        for (const item of results) {
          const toolMessage = message("tool", [{ type: "text", text: JSON.stringify(item.result) }], `tool-result-${item.call.id}`)
          const withCall = { ...toolMessage, toolCallId: item.call.id }
          this.session.append("tool/result", withCall as unknown as JsonValue, { turn: this.turnValue, step })
        }
        this.setStatus("running")
        this.publisher.publish(this.session, "step/end", { runId: this.activation.runId, reason: "tool-results-available" }, { turn: this.turnValue, step })
      }
      throw new Error("Agent达到最大Step仍未形成最终答复")
    } catch (error) {
      const cancelled = signal.aborted || (error instanceof Error && error.name === "AbortError")
      const partialBlocks = error instanceof Error ? (error as PartialStreamError).partialBlocks ?? [] : []
      if (partialBlocks.length > 0) {
        const interrupted = message("assistant", partialBlocks)
        this.session.append("assistant/message", interrupted as unknown as JsonValue, {
          turn: this.turnValue,
          step: this.stepValue,
        })
        this.resultBlocks = partialBlocks
        this.resultValue = textOf(partialBlocks)
        this.publisher.publish(this.session, "assistant/message", {
          runId: this.activation.runId,
          text: this.resultValue,
          blocks: partialBlocks,
          finishReason: cancelled ? "cancelled" : "error",
          interrupted: true,
        } as unknown as JsonValue, { turn: this.turnValue, step: this.stepValue })
      }
      this.errorValue = String(error)
      this.publisher.publish(this.session, "turn/end", {
        runId: this.activation.runId,
        reason: cancelled ? "cancelled" : "failed",
        error: this.errorValue,
      }, { turn: this.turnValue, step: this.stepValue })
      if (cancelled) this.session.append("session/interrupted", { reason: String(signal.reason ?? "cancelled") })
      this.setStatus(cancelled ? "cancelled" : "failed")
      if (!cancelled) throw error
    } finally {
      this.abortController = undefined
    }
  }

  private async streamWithRetry(request: Parameters<RuntimePorts["streamModel"]>[0], signal: AbortSignal): Promise<BlockAssembler> {
    let attempt = 0
    while (true) {
      const assembler = new BlockAssembler()
      try {
        for await (const chunk of this.ports.streamModel(request, signal)) {
          assembler.push(chunk)
          this.publishChunk(chunk)
        }
        return assembler
      } catch (error) {
        const code = error instanceof Error && error.name === "AbortError" ? "ABORT" : "TRANSPORT"
        if (!shouldRetry(DEFAULT_RETRY_POLICY, code, attempt + 1, assembler.blocks().length > 0) || signal.aborted) {
          if (error instanceof Error) (error as PartialStreamError).partialBlocks = assembler.interruptedBlocks()
          throw error
        }
        await waitForRetry(retryDelay(DEFAULT_RETRY_POLICY, attempt + 1), signal)
        attempt += 1
      }
    }
  }

  private publishChunk(chunk: StreamChunk): void {
    const position = { turn: this.turnValue, step: this.stepValue }
    if (chunk.type === "block-start") this.publisher.publish(this.session, "assistant/block-start", {
      index: chunk.index, blockType: chunk.blockType,
    }, position)
    else if (chunk.type === "block-end") this.publisher.publish(this.session, "assistant/block-end", {
      index: chunk.index, block: chunk.block,
    } as unknown as JsonValue, position)
    else if (chunk.type === "text-delta") this.publisher.publish(this.session, "assistant.text_delta", { index: chunk.index, text: chunk.text }, position)
    else if (chunk.type === "reasoning-delta") this.publisher.publish(this.session, "assistant.reasoning_delta", {
      index: chunk.index, text: chunk.text, available: chunk.text.length > 0,
    }, position)
    else if (chunk.type === "usage") this.publisher.publish(this.session, "usage/updated", { usage: { ...chunk.usage } } as JsonValue, position)
  }

  private async executeTool(request: Parameters<RuntimePorts["executeTool"]>[0], signal: AbortSignal) {
    this.store.checkpoint(randomUUID(), this.session.id, "tool", request.callId, "not_started", { toolName: request.name })
    this.publisher.publish(this.session, "tool/requested", { toolCallId: request.callId, toolName: request.name, arguments: request.arguments }, { turn: request.turn, step: request.step })
    this.store.checkpoint(randomUUID(), this.session.id, "tool", request.callId, "started", { toolName: request.name })
    this.publisher.publish(this.session, "tool/started", { toolCallId: request.callId, toolName: request.name }, { turn: request.turn, step: request.step })
    try {
      const result = await this.ports.executeTool(request, signal)
      const status = result.status === "unknown" ? "outcome_unknown" : result.status
      this.store.checkpoint(randomUUID(), this.session.id, "tool", request.callId, status, { toolName: request.name, factRefs: result.factRefs ?? [] })
      this.publisher.publish(this.session, result.status === "completed" ? "tool/completed" : `tool/${result.status}`, {
        toolCallId: request.callId,
        toolName: request.name,
        status: result.status,
        factRefs: [...(result.factRefs ?? [])],
        error: result.error ?? null,
      }, { turn: request.turn, step: request.step })
      return result
    } catch (error) {
      const status = signal.aborted ? "cancelled" : "outcome_unknown"
      this.store.checkpoint(randomUUID(), this.session.id, "tool", request.callId, status, { toolName: request.name, error: String(error) })
      this.publisher.publish(this.session, signal.aborted ? "tool/cancelled" : "tool/failed", {
        toolCallId: request.callId, toolName: request.name, status, error: String(error),
      }, { turn: request.turn, step: request.step })
      if (!signal.aborted) return { status: "unknown" as const, error: String(error) }
      throw error
    }
  }

  private setStatus(status: AgentStatus): void {
    this.statusValue = status
    this.publisher.publish(this.session, "agent/status", { runId: this.activation.runId, roleId: this.activation.roleId, status })
    this.persist()
  }

  private persist(): void {
    this.store.saveRun(this.activation.runId, this.session.id, this.snapshot())
  }
}
