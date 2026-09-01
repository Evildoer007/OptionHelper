import type { AgentMessage, ContentBlock, SessionEvent, TokenUsage } from "../core/types.js"

function blockText(block: ContentBlock): string {
  if (block.type === "tool-call") return `${block.name}\n${block.arguments}`
  if (block.type === "image") return `[image:${block.attachmentId}]`
  if (block.type === "document-ref") return `[document:${block.attachmentId}:${block.name}]`
  return block.text
}

/** Stable conservative meter used before a provider-specific tokenizer is available. */
export class TokenMeter {
  estimateMessages(messages: readonly AgentMessage[]): number {
    let characters = 0
    for (const message of messages) {
      characters += message.role.length + 12
      for (const block of message.content) characters += blockText(block).length + 8
    }
    return Math.ceil(characters / 3.2)
  }

  estimateEvents(events: readonly SessionEvent[]): number {
    return Math.ceil(JSON.stringify(events).length / 3.2)
  }

  merge(...items: readonly (TokenUsage | undefined)[]): TokenUsage {
    const result = { input: 0, output: 0, reasoning: 0, cached: 0, total: 0 }
    for (const item of items) {
      if (item === undefined) continue
      result.input += item.input ?? 0
      result.output += item.output ?? 0
      result.reasoning += item.reasoning ?? 0
      result.cached += item.cached ?? 0
      result.total += item.total ?? (item.input ?? 0) + (item.output ?? 0) + (item.reasoning ?? 0)
    }
    return result
  }
}
