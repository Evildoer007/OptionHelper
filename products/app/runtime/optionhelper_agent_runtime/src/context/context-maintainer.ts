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
  summarize: (messages: readonly AgentMessage[], maxChars: number, signal: AbortSignal) => Promise<string>,
  signal: AbortSignal,
): Promise<ContextMaintenanceResult> {
  const meter = new TokenMeter()
  let messages = session.deriveMessages()
  let estimatedTokens = meter.estimateMessages(messages)
  let pruned = false
  let compacted = false

  if (estimatedTokens >= policy.pruneAtTokens) {
    const next = messages.map((message) => safeToolProjection(message, policy.prunedToolResultChars))
    if (JSON.stringify(next) !== JSON.stringify(messages)) {
      session.append("context/tool-results-pruned", {
        messageCount: next.length,
      })
      messages = next
      estimatedTokens = meter.estimateMessages(messages)
      pruned = true
    }
  }

  if (estimatedTokens >= policy.compactAtTokens) {
    const keep = Math.max(2, policy.preservedRecentMessages)
    const source = messages.slice(0, Math.max(0, messages.length - keep))
    const recent = messages.slice(-keep)
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

  if (estimatedTokens > policy.maxContextTokens) throw new Error("context exceeds configured token limit")
  return { messages, estimatedTokens, pruned, compacted }
}
