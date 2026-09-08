import type {
  AgentMessage, FinishReason, JsonValue, ModelRequest, RuntimePorts, StreamChunk,
  ToolExecutionRequest, ToolExecutionResult, TokenUsage,
} from "../core/types.js"
import { JsonRpcPeer } from "./json-rpc-peer.js"

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function finishReason(value: unknown): FinishReason {
  const normalized = String(value ?? "stop").toLowerCase().replaceAll("_", "-")
  if (["tool-call", "tool-calls", "tool-use"].includes(normalized)) return { kind: "tool-calls" }
  if (["max-token", "max-tokens", "length"].includes(normalized)) return { kind: "max-tokens" }
  if (["cancelled", "canceled", "interrupted"].includes(normalized)) return { kind: "cancelled" }
  if (["error", "failed"].includes(normalized)) return { kind: "error" }
  return { kind: "stop" }
}

function usage(value: unknown): TokenUsage {
  const raw = object(value)
  const number = (...names: string[]): number | undefined => {
    for (const name of names) if (typeof raw[name] === "number") return raw[name] as number
    return undefined
  }
  const input = number("input", "input_tokens", "prompt_tokens")
  const output = number("output", "output_tokens", "completion_tokens")
  const reasoning = number("reasoning", "reasoning_tokens")
  const cached = number("cached", "cached_tokens")
  const total = number("total", "total_tokens")
  return {
    ...(input === undefined ? {} : { input }),
    ...(output === undefined ? {} : { output }),
    ...(reasoning === undefined ? {} : { reasoning }),
    ...(cached === undefined ? {} : { cached }),
    ...(total === undefined ? {} : { total }),
  }
}

function wireMessage(item: AgentMessage): JsonValue {
  const content = item.content.map((block) => {
    if (block.type === "tool-call") return { type: "tool_call", id: block.id, name: block.name, arguments: block.arguments }
    if (block.type === "image") return {
      type: "image", attachment_id: block.attachmentId, media_type: block.mediaType, name: block.name ?? "",
    }
    if (block.type === "document-ref") return {
      type: "document_ref", attachment_id: block.attachmentId, media_type: block.mediaType,
      name: block.name, extraction_status: block.extractionStatus ?? "",
    }
    return { type: block.type, text: block.text }
  })
  return {
    id: item.id,
    role: item.role,
    content,
    ...(item.toolCallId === undefined ? {} : { tool_call_id: item.toolCallId }),
  }
}

function modelMessages(messages: readonly AgentMessage[]): JsonValue[] {
  // Older stream assembly could persist nameless synthetic calls. Keep the
  // conversation, but exclude those invalid calls and their paired replies.
  const invalidIds = new Set<string>()
  for (const message of messages) for (const block of message.content) {
    if (block.type === "tool-call" && !block.name.trim()) invalidIds.add(block.id)
  }
  return messages.filter(message => !(message.role === "tool" && invalidIds.has(message.toolCallId ?? "")))
    .map(message => ({ ...message, content: message.content.filter(block =>
      block.type !== "tool-call" || !invalidIds.has(block.id)) }))
    .filter(message => message.content.length > 0)
    .map(wireMessage)
}

function latestUserText(messages: readonly AgentMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role !== "user") continue
    return message.content.filter((block) => block.type === "text").map((block) => block.text).join("")
  }
  return ""
}

export class HostRuntimePorts implements RuntimePorts {
  constructor(private readonly peer: JsonRpcPeer) {}

  async *streamModel(request: ModelRequest, signal: AbortSignal): AsyncIterable<StreamChunk> {
    let nextIndex = 0
    type OpenBlock = {
      readonly index: number
      readonly blockType: "text" | "reasoning" | "tool-call"
      text: string
      id?: string
      name?: string
      arguments: string
    }
    let activeTextBlock: OpenBlock | undefined
    const toolBlocks = new Map<string, OpenBlock>()
    const toolIndexes = new Map<number, OpenBlock>()
    const startTextBlock = (blockType: "text" | "reasoning"): OpenBlock => {
      if (activeTextBlock?.blockType === blockType) return activeTextBlock
      activeTextBlock = { index: nextIndex++, blockType, text: "", arguments: "" }
      return activeTextBlock
    }
    const closeTextBlock = function* (): Generator<StreamChunk> {
      if (activeTextBlock === undefined) return
      const block = activeTextBlock
      activeTextBlock = undefined
      yield {
        type: "block-end",
        index: block.index,
        block: block.blockType === "text"
          ? { type: "text", text: block.text }
          : { type: "reasoning", text: block.text },
      }
    }
    const closeToolBlocks = function* (): Generator<StreamChunk> {
      for (const block of toolBlocks.values()) {
        yield {
          type: "block-end",
          index: block.index,
          block: {
            type: "tool-call",
            id: block.id ?? `call-${block.index}`,
            name: block.name ?? "",
            arguments: block.arguments,
          },
        }
      }
      toolBlocks.clear()
      toolIndexes.clear()
    }
    const stream = this.peer.stream("host.model.stream", {
      requestId: request.requestId,
      routeId: request.routeId,
      modelRouteRef: { routeId: request.routeId },
      sessionId: request.sessionId,
      agentRunId: request.agentRunId,
      roleId: request.roleId,
      prompt: latestUserText(request.messages),
      turn: request.turn,
      step: request.step,
      messages: modelMessages(request.messages),
      tools: request.tools as unknown as JsonValue,
    }, signal)
    for await (const raw of stream) {
      const item = object(raw)
      const type = String(item.type ?? item.event ?? "text-delta").replaceAll("_", "-")
      if (type === "text-delta" || type === "reasoning-delta") {
        const blockType = type === "text-delta" ? "text" : "reasoning"
        if (activeTextBlock !== undefined && activeTextBlock.blockType !== blockType) {
          yield* closeTextBlock()
        }
        const block = startTextBlock(blockType)
        if (block.text.length === 0) yield { type: "block-start", index: block.index, blockType }
        const text = String(item.text ?? item.delta ?? (type === "text-delta" ? item.content : "") ?? "")
        block.text += text
        yield { type, index: block.index, text }
      }
      else if (type === "tool-call-delta") {
        yield* closeTextBlock()
        const suppliedId = String(item.id ?? item.tool_call_id ?? "")
        const providerIndex = typeof item.index === "number" ? item.index : undefined
        let block = (suppliedId ? toolBlocks.get(suppliedId) : undefined)
          ?? (providerIndex === undefined ? undefined : toolIndexes.get(providerIndex))
        if (block === undefined) {
          if (!suppliedId && providerIndex === undefined) throw new Error("Tool delta is missing both id and index")
          const id = suppliedId || `call-${nextIndex}`
          block = { index: nextIndex++, blockType: "tool-call", text: "", id, arguments: "" }
          toolBlocks.set(id, block)
          yield { type: "block-start", index: block.index, blockType: "tool-call" }
        }
        if (providerIndex !== undefined) toolIndexes.set(providerIndex, block)
        if (suppliedId && suppliedId !== block.id) {
          toolBlocks.delete(block.id!)
          block.id = suppliedId
          toolBlocks.set(suppliedId, block)
        }
        const id = block.id!
        if (item.name !== undefined || item.tool_name !== undefined) block.name = String(item.name ?? item.tool_name)
        const argumentsDelta = String(item.argumentsDelta ?? item.arguments_delta ?? item.arguments ?? item.delta ?? "")
        block.arguments += argumentsDelta
        yield {
          type,
          index: block.index,
          id,
          ...(item.name === undefined && item.tool_name === undefined ? {} : { name: String(item.name ?? item.tool_name) }),
          argumentsDelta,
        }
      } else if (type === "usage") yield { type, usage: usage(item.usage ?? item) }
      else if (type === "finish") {
        yield* closeTextBlock()
        yield* closeToolBlocks()
        if (item.usage !== undefined) yield { type: "usage", usage: usage(item.usage) }
        yield { type, reason: finishReason(item.reason ?? item.finish_reason) }
      }
    }
    yield* closeTextBlock()
    yield* closeToolBlocks()
  }

  async executeTool(request: ToolExecutionRequest, signal: AbortSignal): Promise<ToolExecutionResult> {
    const raw = object(await this.peer.request("host.tool.call", {
      toolCallId: request.callId,
      toolName: request.name,
      arguments: request.arguments,
      sessionId: request.sessionId,
      agentRunId: request.agentRunId,
      turn: request.turn,
      step: request.step,
    }, signal))
    const status = String(raw.status ?? "completed")
    // Business states such as a returned clarification are successful tool
    // responses; they do not mean the requested research is already complete.
    const normalized = ["complete", "succeeded", "success", "available", "pending_question", "needs_input", "pending_approval", "candidate_ready"].includes(status)
      ? "completed" : ["outcome_unknown", "interrupted"].includes(status) ? "unknown" : status
    const factRefs = Array.isArray(raw.fact_refs) ? raw.fact_refs.map(String)
      : Array.isArray(raw.factRefs) ? raw.factRefs.map(String)
        : raw.fact_ref === undefined ? [] : [String(raw.fact_ref)]
    return {
      status: ["completed", "failed", "cancelled", "unknown"].includes(normalized)
        ? normalized as ToolExecutionResult["status"] : "failed",
      result: raw as unknown as JsonValue,
      ...(raw.error === undefined ? {} : { error: String(raw.error) }),
      factRefs,
    }
  }

  async summarize(messages: readonly AgentMessage[], maxChars: number, _signal: AbortSignal): Promise<string> {
    const lines: string[] = ["以下是此前对话的上下文摘要；精确金融数值必须通过FactRef重新读取："]
    for (const item of messages) {
      const raw = item.content.map((block) => {
        if (block.type === "tool-call") return `[工具请求:${block.name}]`
        if (block.type === "image") return `[图片附件:${block.attachmentId}]`
        if (block.type === "document-ref") return `[文档附件:${block.attachmentId}:${block.name}]`
        return block.text
      }).join(" ").trim()
      const factRefs = [...raw.matchAll(/fact[_:.-][A-Za-z0-9_.:-]+/g)].map((match) => match[0])
      const content = item.role === "tool"
        ? `[已验证工具结果${factRefs.length > 0 ? `；FactRef:${factRefs.join(",")}` : ""}]`
        : raw.replace(/(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?%?/g, "[数值见事实引用]")
      if (content) lines.push(`${item.role}: ${content}`)
      if (lines.join("\n").length >= maxChars) break
    }
    return lines.join("\n").slice(0, maxChars)
  }
}
