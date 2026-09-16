import type { AgentMessage, ContextPolicy, JsonValue } from "../core/types.js"
import { AgentSession } from "../core/session.js"
import { TokenMeter } from "./token-meter.js"

const FINANCIAL_KEYS = new Set([
  "contract", "candidate_version", "candidateversion", "market_snapshot", "marketsnapshot",
  "greeks", "valuation", "backtest", "ledger", "fact_ref", "factrefs",
])

function safeToolProjection(message: AgentMessage, maxChars: number): AgentMessage {
  if (message.role !== "tool") return message
  const raw = message.content.map((block) => {
    if (block.type === "tool-call") return block.arguments
    if (block.type === "image") return `[image:${block.attachmentId}]`
    if (block.type === "document-ref") return `[document:${block.attachmentId}]`
    return block.text
  }).join("\n")
  if (raw.length <= maxChars) return message
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { parsed = undefined }
  const facts: Record<string, JsonValue> = {}
  if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const normalized = key.toLowerCase().replaceAll("-", "_")
      if (FINANCIAL_KEYS.has(normalized) || normalized.includes("fact_ref") || key === "status") {
        facts[key] = JSON.parse(JSON.stringify(value)) as JsonValue
      }
    }
  }
  return {
    ...message,
    content: [{ type: "text", text: JSON.stringify({ ...facts, pruned: true }) }],
  }
}

/** Keep repeated structured inputs once in the model view, never alter stored events. */
export function projectRepeatedResearchInputs(messages: readonly AgentMessage[]): AgentMessage[] {
  const later = new Map<string, { value: string; messageId: string }>()
  const laterEvidence = new Map<string, { value: string; messageId: string; index: number }>()
  const projected = messages.map((message) => structuredClone(message))
  for (let index = projected.length - 1; index >= 0; index -= 1) {
    const message = projected[index]!
    if (message.role !== "user" || message.content.length !== 1 || message.content[0]?.type !== "text") continue
    let envelope: any
    try { envelope = JSON.parse(message.content[0].text) } catch { continue }
    if (envelope?.protocol !== "optionhelper.recommender.step"
      || typeof envelope.role !== "string"
      || envelope.input?.workflow !== "optionhelper.recommender") continue
    const input = envelope.input.input
    if (input === null || typeof input !== "object" || Array.isArray(input)) continue
    let changed = false
    for (const [key, value] of Object.entries(input)) {
      const encoded = JSON.stringify(value)
      if (encoded.length < 512) continue
      const identity = JSON.stringify([envelope.role, key])
      const repeated = later.get(identity)
      if (repeated?.value === encoded) {
        input[key] = {
          repeated_input: true,
          source_message_id: repeated.messageId,
          source_field: `input.input.${key}`,
          instruction: "此字段与后续消息中的完整字段完全相同，请读取该完整字段。历史原文仍保存在会话记录中。",
        }
        changed = true
      } else {
        later.set(identity, { value: encoded, messageId: message.id })
        if (key === "evidence" && Array.isArray(value)) {
          input[key] = value.map((item, evidenceIndex) => {
            if (!item || typeof item !== "object" || typeof item.evidence_id !== "string") return item
            const itemText = JSON.stringify(item)
            if (itemText.length < 512) return item
            const itemKey = JSON.stringify([envelope.role, item.evidence_id])
            const prior = laterEvidence.get(itemKey)
            if (prior?.value === itemText) {
              changed = true
              return { evidence_id: item.evidence_id, repeated_input: true,
                source_message_id: prior.messageId, source_field: `input.input.evidence[${prior.index}]` }
            }
            laterEvidence.set(itemKey, { value: itemText, messageId: message.id, index: evidenceIndex })
            return item
          })
        }
      }
    }
    if (changed) projected[index] = { ...message, content: [{ type: "text", text: JSON.stringify(envelope) }] }
  }
  return projected
}

/** Exact repeated JSON values share one visible source within their user turn.
 * References identify a visible source marker and JSON path. No stored event or
 * financial value changes, and tool calls/results keep their original pairing.
 */
export function projectRepeatedResearchValues(messages: readonly AgentMessage[]): AgentMessage[] {
  const projected = messages.map((message) => structuredClone(message))
  let researchTurn = false
  let seen = new Map<string, { message: string; path: (string | number)[] }>()
  const referencedSources = new Set<string>()
  const referenceTurns = new Set<string>()
  const sourceLabels = new Map(projected.map((message, index) => [message.id, `r${index}`]))
  let userMessageId = ""
  for (const message of projected) {
    if (message.role === "user") {
      userMessageId = message.id
      seen = new Map()
      researchTurn = false
      try {
        const block = message.content[0]
        const input = block?.type === "text" ? JSON.parse(block.text) : null
        researchTurn = input?.protocol === "optionhelper.recommender.step"
          && input?.input?.workflow === "optionhelper.recommender"
      } catch { /* Ordinary user text is left intact. */ }
    }
    if (!researchTurn || (message.role !== "user" && message.role !== "tool") || message.content.length !== 1) continue
    const content = message.content.map((block, blockIndex) => {
      if (block.type !== "text") return block
      let input: unknown
      try { input = JSON.parse(block.text) } catch { return block }
      if (input === null || typeof input !== "object" || Array.isArray(input) || "context_source_message_id" in input || "context_reference_protocol" in input) return block
      function visit(value: any, path: (string | number)[]): any {
        const encoded = JSON.stringify(value)
        const eligible = value !== null && (typeof value === "object" || typeof value === "string")
          && encoded.length >= 160
        if (eligible) {
          const previous = seen.get(encoded)
          if (previous !== undefined) {
            const reference = { repeated_research_value: previous }
            if (JSON.stringify(reference).length + 32 < encoded.length) {
              referenceTurns.add(userMessageId)
              referencedSources.add(previous.message)
              return reference
            }
          } else {
            seen.set(encoded, { message: sourceLabels.get(message.id)!, path })
          }
        }
        if (Array.isArray(value)) return value.map((item, index) => visit(item, [...path, index]))
        if (value !== null && typeof value === "object") {
          return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, visit(item, [...path, key])]))
        }
        return value
      }
      const output = visit(input, [])
      if (JSON.stringify(output) === JSON.stringify(input)) return block
      return { ...block, text: JSON.stringify(output) }
    })
    Object.assign(message, { content })
  }
  for (const message of projected) {
    const label = sourceLabels.get(message.id)!
    if (!referencedSources.has(label) && !referenceTurns.has(message.id)) continue
    const block = message.content[0]
    if (block?.type !== "text") continue
    const value = JSON.parse(block.text)
    if (referencedSources.has(label)) value.context_source_message_id = label
    if (referenceTurns.has(message.id)) value.context_reference_protocol = "repeated_research_value表示完全相同的JSON值。以正文context_source_message_id匹配message，再按path中的字段/数组下标逐层读取，必要时继续解析引用。候选身份与证据归属沿用各自记录，不能据此合并候选。此元数据不写入业务输出。完整原值仍保存在会话中。"
    Object.assign(message, { content: [{ ...block, text: JSON.stringify(value) }] })
  }
  return projected
}

/** Financial research envelopes must never enter the generic lossy tool filter. */
function researchToolMessageIds(messages: readonly AgentMessage[]): Set<string> {
  const ids = new Set<string>()
  let researchTurn = false
  for (const message of messages) {
    if (message.role === "user") {
      researchTurn = false
      try {
        const block = message.content[0]
        const input = block?.type === "text" ? JSON.parse(block.text) : null
        researchTurn = input?.protocol === "optionhelper.recommender.step"
          && input?.input?.workflow === "optionhelper.recommender"
      } catch { /* Non-research traffic keeps its existing pruning policy. */ }
    }
    if (researchTurn && message.role === "tool") ids.add(message.id)
  }
  return ids
}


/** Completed historical read pages remain recoverable from the exact RunRef.
 * Keep authoritative evaluations and the latest complete read exchange in full.
 */
export function projectArchivedResearchPages(messages: readonly AgentMessage[]): AgentMessage[] {
  const projected = messages.map((message) => structuredClone(message))
  const tools = researchToolMessageIds(projected)
  const calls = new Map<string, any>()
  const callGroups = new Map<string, string>()
  const eligible: { message: AgentMessage; wrapper: any; page: any; arguments: any; group: string }[] = []
  function hasProtectedFacts(value: any): boolean {
    if (Array.isArray(value)) return value.some(hasProtectedFacts)
    if (value === null || typeof value !== "object") return false
    return Object.entries(value).some(([key, child]) =>
      (["verified_metrics", "candidate_snapshot", "fact_ref", "warnings", "warning", "limitations", "error", "missing_information"].includes(key)
        && child !== null && child !== "" && JSON.stringify(child) !== "[]") || hasProtectedFacts(child))
  }
  for (const message of projected) {
    if (message.role === "assistant") for (const block of message.content) {
      if (block.type === "tool-call" && block.name === "read_research_evidence") {
        try { calls.set(block.id, JSON.parse(block.arguments)); callGroups.set(block.id, message.id) } catch { /* Invalid calls are never archived. */ }
      }
    }
    if (!tools.has(message.id) || message.content.length !== 1) continue
    const block = message.content[0]
    if (block?.type !== "text") continue
    let wrapper: any
    try { wrapper = JSON.parse(block.text) } catch { continue }
    const page = wrapper?.result?.result
    const args = calls.get(message.toolCallId ?? "")
    const ref = page?.module_run_ref
    if (wrapper.status !== "completed" || wrapper.result?.status !== "completed"
      || wrapper.result?.tool_name !== "read_research_evidence" || page?.status !== "completed"
      || page?.source !== "verified_frozen_result" || page?.calculation_started !== false
      || !args?.candidate_id || ref?.module !== args.module
      || !["run_id", "task_id", "tenant_id", "expected_result_file_hash", "expected_artifact_manifest_hash"].every(key => typeof ref?.[key] === "string" && ref[key])
      || !Array.isArray(page.result_path) || !Number.isSafeInteger(page.offset)
      || hasProtectedFacts(page.value)) continue
    eligible.push({ message, wrapper, page, group: callGroups.get(message.toolCallId ?? "")!, arguments: { ...args, result_path: page.result_path,
      offset: page.offset, limit: args.limit ?? 64, expected_run_ref: ref } })
  }
  const latestGroup = eligible.at(-1)?.group
  for (const item of eligible.filter(item => item.group !== latestGroup)) {
    const archived = { archived_research_page: true, tool: "read_research_evidence", arguments: item.arguments,
      note: "此前成功读取页的完整值仍保存在此冻结RunRef中；需要细项时用以上参数取回，不进行计算。来源或候选已变更时必须报错，不可替换为其他运行。" }
    const recoverableSize = JSON.stringify({ value: item.page.value,
      deferred_fields: item.page.deferred_fields, value_indices: item.page.value_indices }).length
    if (JSON.stringify(archived).length + 64 >= recoverableSize) continue
    item.page.value = archived
    // These are navigation coordinates for the archived page, returned intact
    // by the pinned reread; no error, missing-data note or financial fact is removed.
    delete item.page.deferred_fields
    delete item.page.value_indices
    Object.assign(item.message, { content: [{ type: "text", text: JSON.stringify(item.wrapper) }] })
  }
  return projected
}
/** Only completed exchanges may become a Host-verified continuation checkpoint. */
function completeResearchExchange(messages: readonly AgentMessage[]): boolean {
  const start = messages.findLastIndex(message => message.role === "user")
  if (start < 0 || messages.at(-1)?.role !== "tool") return false
  try {
    const block = messages[start]!.content[0]
    const input = block?.type === "text" ? JSON.parse(block.text) : null
    if (input?.protocol !== "optionhelper.recommender.step") return false
  } catch { return false }
  const pending = new Set<string>()
  let calls = 0
  for (const message of messages.slice(start + 1)) {
    for (const block of message.content) if (message.role === "assistant" && block.type === "tool-call") {
      if (pending.has(block.id)) return false
      pending.add(block.id); calls++
    }
    if (message.role === "tool" && (!message.toolCallId || !pending.delete(message.toolCallId))) return false
  }
  return calls > 0 && pending.size === 0
}

export interface ContextMaintenanceResult {
  readonly messages: AgentMessage[]
  readonly estimatedTokens: number
  readonly pruned: boolean
  readonly compacted: boolean
}

/** Prunes large tool payloads first, then compacts only settled conversation history. */
export async function maintainContext(
  session: AgentSession,
  policy: ContextPolicy,
  summarize: (messages: readonly AgentMessage[], maxChars: number, signal: AbortSignal, purpose?: "research-checkpoint") => Promise<string>,
  signal: AbortSignal,
): Promise<ContextMaintenanceResult> {
  const meter = new TokenMeter()
  let messages = projectRepeatedResearchInputs(session.deriveMessages())
  let estimatedTokens = meter.estimateMessages(messages)
  let pruned = false
  let compacted = false
  if (estimatedTokens >= policy.pruneAtTokens && completeResearchExchange(messages)) {
    const publicMessages = messages.map(message => ({ ...message,
      content: message.content.filter(block => block.type !== "reasoning") }))
    const summary = await summarize(publicMessages, policy.maxContextTokens * 3.2, signal, "research-checkpoint")
    const payload = JSON.parse(summary)
    if (payload?.protocol !== "optionhelper.recommender.step" || payload?.input?.research_checkpoint?.source !== "host_verified_research_state")
      throw new Error("research checkpoint was not verified by Host")
    const checkpoint: AgentMessage = { id: `research-checkpoint-${session.events.length}`, role: "user", content: [{ type: "text", text: summary }] }
    const tokens = meter.estimateMessages([checkpoint])
    if (tokens > policy.maxContextTokens) throw new Error("complete research checkpoint exceeds configured token limit")
    const sourceSeqs = session.surfaceEvents().map(event => event.seq)
    session.append("compaction/summary", { summary, sourceSeqs, researchCheckpoint: true })
    return { messages: [checkpoint], estimatedTokens: tokens, pruned: false, compacted: true }
  }

  if (estimatedTokens >= policy.pruneAtTokens) {
    const protectedTools = researchToolMessageIds(messages)
    const next = messages.map((message) => protectedTools.has(message.id) ? message : safeToolProjection(message, policy.prunedToolResultChars))
    if (JSON.stringify(next) !== JSON.stringify(messages)) {
      session.append("context/tool-results-pruned", {
        messageCount: next.length,
      })
      messages = next
      estimatedTokens = meter.estimateMessages(messages)
      pruned = true
    }
  }

  if (estimatedTokens >= policy.pruneAtTokens) {
    messages = projectRepeatedResearchValues(projectArchivedResearchPages(messages))
    estimatedTokens = meter.estimateMessages(messages)
  }

  if (estimatedTokens >= policy.compactAtTokens) {
    const keep = Math.max(2, policy.preservedRecentMessages)
    let boundary = Math.max(0, messages.length - keep)
    // A few large stage inputs can exceed the limit before the message-count
    // threshold is reached. Preserve the entire current user/tool turn and
    // summarize only older settled turns; never split a tool-call/result pair.
    const latestUser = messages.findLastIndex((message) => message.role === "user")
    // Keep whole user turns. A count-based boundary can otherwise retain tool
    // results after summarizing away the assistant calls that created them.
    if (latestUser >= 0 && boundary > latestUser) boundary = latestUser
    if (boundary > 0) {
      boundary = Math.max(0, messages.slice(0, boundary + 1)
        .findLastIndex((message) => message.role === "user"))
    }
    if (latestUser > boundary
      && meter.estimateMessages(messages.slice(boundary)) > policy.compactAtTokens - 2_500) {
      boundary = latestUser
    }
    const source = messages.slice(0, boundary)
    const recent = messages.slice(boundary)
    if (source.length > 0) {
      session.append("compaction/start", { estimatedTokens })
      try {
        const summary = await summarize(source, 8_000, signal)
        if (!summary.trim()) throw new Error("context summary is empty")
        const sourceSeqs = session.surfaceEvents()
          .filter((event) => {
            if (event.type === "compaction/summary") return true
            const data = event.data as Record<string, unknown>
            return typeof data.role === "string" && Array.isArray(data.content)
          })
          .slice(0, source.length)
          .map((event) => event.seq)
        session.append("compaction/summary", { summary, sourceSeqs })
        messages = [{ id: `summary-${session.events.length}`, role: "system", content: [{ type: "text", text: summary }] }, ...recent]
        estimatedTokens = meter.estimateMessages(messages)
        compacted = true
        session.append("compaction/end", { status: "completed", estimatedTokens })
      } catch (error) {
        session.append("compaction/end", { status: "failed", error: String(error) })
        throw error
      }
    }
  }

  if (estimatedTokens > policy.maxContextTokens) {
    const fullPages = messages
    messages = projectRepeatedResearchValues(messages)
    estimatedTokens = meter.estimateMessages(messages)
    if (estimatedTokens > policy.maxContextTokens) {
      messages = projectRepeatedResearchValues(projectArchivedResearchPages(fullPages))
      estimatedTokens = meter.estimateMessages(messages)
    }
  }

  if (estimatedTokens > policy.maxContextTokens) throw new Error("context exceeds configured token limit")
  return { messages, estimatedTokens, pruned, compacted }
}
