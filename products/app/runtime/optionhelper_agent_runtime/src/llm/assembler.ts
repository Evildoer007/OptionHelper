import type { ContentBlock, FinishReason, StreamChunk, TokenUsage } from "../core/types.js"

interface PartialBlock {
  blockType: ContentBlock["type"]
  text: string
  toolCallId?: string
  toolCallName?: string
  toolCallArguments: string
  block?: ContentBlock
}

function assertNever(value: never): never {
  throw new Error(`unsupported stream chunk: ${JSON.stringify(value)}`)
}

/** Incrementally assembles provider chunks while preserving stream order. */
export class BlockAssembler {
  private readonly partials = new Map<number, PartialBlock>()
  private readonly order: number[] = []
  private currentUsage: TokenUsage | undefined
  private currentFinish: FinishReason | undefined

  push(chunk: StreamChunk): void {
    switch (chunk.type) {
      case "block-start":
        if (!this.partials.has(chunk.index)) {
          this.order.push(chunk.index)
          this.partials.set(chunk.index, {
            blockType: chunk.blockType,
            text: "",
            toolCallArguments: "",
          })
        }
        return
      case "text-delta":
      case "reasoning-delta": {
        const partial = this.ensure(chunk.index, chunk.type === "text-delta" ? "text" : "reasoning")
        if (partial.block === undefined) partial.text += chunk.text
        return
      }
      case "tool-call-delta": {
        const partial = this.ensure(chunk.index, "tool-call")
        if (partial.block !== undefined) return
        partial.toolCallId = chunk.id
        if (chunk.name !== undefined) partial.toolCallName = chunk.name
        partial.toolCallArguments += chunk.argumentsDelta
        return
      }
      case "block-end": {
        const partial = this.ensure(chunk.index, chunk.block.type)
        partial.block ??= chunk.block
        return
      }
      case "usage":
        this.currentUsage = Object.freeze({ ...chunk.usage })
        return
      case "finish":
        this.currentFinish = Object.freeze({ ...chunk.reason })
        return
      default:
        return assertNever(chunk)
    }
  }

  private ensure(index: number, blockType: ContentBlock["type"]): PartialBlock {
    const current = this.partials.get(index)
    if (current !== undefined) return current
    const created: PartialBlock = { blockType, text: "", toolCallArguments: "" }
    this.partials.set(index, created)
    this.order.push(index)
    return created
  }

  private assemble(partial: PartialBlock, index: number): ContentBlock {
    if (partial.block !== undefined) return partial.block
    switch (partial.blockType) {
      case "text":
        return { type: "text", text: partial.text }
      case "reasoning":
        return { type: "reasoning", text: partial.text }
      case "tool-call":
        return {
          type: "tool-call",
          id: partial.toolCallId ?? `call-${index}`,
          name: partial.toolCallName ?? "",
          arguments: partial.toolCallArguments,
        }
      case "image":
      case "document-ref":
        throw new Error("模型输出流不能创建附件引用")
      default:
        return assertNever(partial.blockType)
    }
  }

  private mustGet(index: number): PartialBlock {
    const partial = this.partials.get(index)
    if (partial === undefined) throw new Error(`missing stream block ${index}`)
    return partial
  }

  blocks(): ContentBlock[] {
    const all = this.order.map((index) => this.assemble(this.mustGet(index), index))
    return this.finish.kind === "max-tokens" ? all.filter((block) => block.type !== "tool-call") : all
  }

  interruptedBlocks(): ContentBlock[] {
    return this.order
      .map((index) => {
        const partial = this.mustGet(index)
        const type = partial.block?.type ?? partial.blockType
        return type === "text" || type === "reasoning" ? this.assemble(partial, index) : undefined
      })
      .filter((block): block is Extract<ContentBlock, { type: "text" | "reasoning" }> =>
        block !== undefined && (block.type === "text" || block.type === "reasoning") && block.text.trim().length > 0)
  }

  get usage(): TokenUsage | undefined {
    return this.currentUsage
  }

  get finish(): FinishReason {
    return this.currentFinish ?? { kind: "stop" }
  }
}
