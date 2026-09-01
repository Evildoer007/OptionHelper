export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }

export type SessionKind = "root" | "child"
export type AgentStatus = "idle" | "running" | "waiting_tool" | "completed" | "failed" | "cancelled"
export type InboxTarget = "next-turn" | "next-step"

export interface TextBlock {
  readonly type: "text"
  readonly text: string
}

export interface ReasoningBlock {
  readonly type: "reasoning"
  readonly text: string
}

export interface ToolCallBlock {
  readonly type: "tool-call"
  readonly id: string
  readonly name: string
  readonly arguments: string
}

export interface ImageBlock {
  readonly type: "image"
  readonly attachmentId: string
  readonly mediaType: string
  readonly name?: string
}

export interface DocumentRefBlock {
  readonly type: "document-ref"
  readonly attachmentId: string
  readonly mediaType: string
  readonly name: string
  readonly extractionStatus?: string
}

export type ContentBlock = TextBlock | ReasoningBlock | ToolCallBlock | ImageBlock | DocumentRefBlock

export interface AgentMessage {
  readonly id: string
  readonly role: "system" | "user" | "assistant" | "tool"
  readonly content: readonly ContentBlock[]
  readonly toolCallId?: string
}

export interface TokenUsage {
  readonly input?: number
  readonly output?: number
  readonly reasoning?: number
  readonly cached?: number
  readonly total?: number
}

export type FinishReason =
  | { readonly kind: "stop" }
  | { readonly kind: "tool-calls" }
  | { readonly kind: "max-tokens" }
  | { readonly kind: "cancelled" }
  | { readonly kind: "error"; readonly message?: string }

export type StreamChunk =
  | { readonly type: "block-start"; readonly index: number; readonly blockType: ContentBlock["type"] }
  | { readonly type: "text-delta"; readonly index: number; readonly text: string }
  | { readonly type: "reasoning-delta"; readonly index: number; readonly text: string }
  | { readonly type: "tool-call-delta"; readonly index: number; readonly id: string; readonly name?: string; readonly argumentsDelta: string }
  | { readonly type: "block-end"; readonly index: number; readonly block: ContentBlock }
  | { readonly type: "usage"; readonly usage: TokenUsage }
  | { readonly type: "finish"; readonly reason: FinishReason }

export interface SessionEvent<T extends JsonValue = JsonValue> {
  readonly sessionId: string
  readonly seq: number
  readonly type: string
  readonly turn?: number
  readonly step?: number
  readonly timestamp: string
  readonly data: T
}

export interface SessionHeader {
  readonly id: string
  readonly kind: SessionKind
  readonly parentId?: string
  readonly label: string
  readonly createdAt: string
  readonly importedLegacyHistory: boolean
}

export interface ToolDefinition {
  readonly name: string
  readonly description: string
  readonly parameters: { readonly [key: string]: JsonValue }
  readonly executionMode: "exclusive" | "parallel"
}

export interface ToolExecutionRequest {
  readonly callId: string
  readonly name: string
  readonly arguments: JsonValue
  readonly sessionId: string
  readonly agentRunId: string
  readonly turn: number
  readonly step: number
}

export interface ToolExecutionResult {
  readonly status: "completed" | "failed" | "cancelled" | "unknown"
  readonly result?: JsonValue
  readonly error?: string
  readonly factRefs?: readonly string[]
}

export interface ModelRequest {
  readonly requestId: string
  readonly routeId: string
  readonly messages: readonly AgentMessage[]
  readonly tools: readonly ToolDefinition[]
  readonly sessionId: string
  readonly agentRunId: string
  readonly roleId: string
  readonly turn: number
  readonly step: number
}

export interface RuntimePorts {
  streamModel(request: ModelRequest, signal: AbortSignal): AsyncIterable<StreamChunk>
  executeTool(request: ToolExecutionRequest, signal: AbortSignal): Promise<ToolExecutionResult>
  summarize(messages: readonly AgentMessage[], maxChars: number, signal: AbortSignal): Promise<string>
}

export interface AgentBudget {
  readonly maxSteps: number
  readonly maxTools: number
}

export interface ContextPolicy {
  readonly maxContextTokens: number
  readonly pruneAtTokens: number
  readonly compactAtTokens: number
  readonly preservedRecentMessages: number
  readonly prunedToolResultChars: number
}

export interface AgentActivation {
  readonly runId: string
  readonly workflowId: string
  readonly sessionId: string
  readonly parentSessionId?: string
  readonly parentRunId?: string
  readonly roleId: string
  readonly label: string
  readonly mode: "one-shot" | "continuable"
  readonly routeId: string
  readonly allowedTools: readonly string[]
  readonly parallelTools: boolean
  readonly budget: AgentBudget
  readonly contextPolicy: ContextPolicy
}
